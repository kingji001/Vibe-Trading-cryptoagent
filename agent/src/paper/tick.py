"""Mark-to-market tick: conditional stop/TP evaluation + equity snapshot.

``VIBE_PAPER_TICK_INTERVAL`` selects the cadence: ``1D`` (default —
once-per-UTC-day behavior) or ``1H`` (intraday — each tick evaluates every
confirmed 1H bar after a persisted per-symbol watermark, chronologically,
applying the same per-bar rules). See ``run_tick``.

Every tick also runs the deterministic event check (``events.py``) for the
watched symbols (open positions ∪ ``VIBE_COMMITTEE_SYMBOLS``), surfacing
``event_triggers`` so the scheduled tick job can fire an ad-hoc committee run
on a material price/funding move. ``tick_state.json`` is created only when it's
needed — 1H mode (bar watermark) OR at least one event threshold enabled; in
1D mode with events disabled (both thresholds 0) no ``tick_state.json`` is ever
written (byte-identical to pre-event behavior).

In 1D mode, for every open position it fetches the latest confirmed daily OHLC
bar (reusing the journal's
loader-routing path — okx -> ccxt fallback, same as
``committee_journal_tool._loader_fetch_bars`` — adapted to keep high/low,
which that helper drops since its 24h/72h/7d alpha math only needs
open/close), evaluates stop/TP triggers via
``PaperBroker.evaluate_conditionals``, then marks the account to market and
appends one ``equity.jsonl`` row for the day.

Never invents a price: a bar-fetch failure for a symbol is recorded in
``errors`` and that position is left completely untouched this tick (no
conditional evaluation, no mutation) — it is retried on the next tick.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from src.paper.broker import (
    PaperBroker,
    PriceFn,
    _fmt_ts,
    _parse_iso,
    bar_status_vs_entry,
)
from src.paper.store import PaperStore, paper_root

# In 1D mode ``bars_fn(symbol, now)`` returns ONE bar dict; in 1H mode it
# returns a LIST of confirmed 1H bar dicts (evaluated after the watermark).
BarsFn = Callable[[str, datetime], Any]

# Retriable noops older than this are not re-driven by the tick (the spec's
# "retried on next tick" promise is bounded so stale decisions don't resurrect).
_RETRY_MAX_AGE = timedelta(days=7)

# Tick interval -> bar period, used for the entry-partial-bar skip. 1D is the
# default (byte-identical to pre-intraday behavior); 1H shrinks the unprotected
# entry window from <=1 day to <=1 hour.
_INTERVAL_PERIOD = {"1D": timedelta(days=1), "1H": timedelta(hours=1)}


def _tick_interval() -> str:
    """Resolve ``VIBE_PAPER_TICK_INTERVAL``: ``1D`` (default) or ``1H``.

    Anything other than a case-insensitive ``1H`` falls back to ``1D`` so a
    typo can never silently enable intraday mode.
    """
    val = (os.environ.get("VIBE_PAPER_TICK_INTERVAL") or "1D").strip().upper()
    return "1H" if val == "1H" else "1D"


# --------------------------------------------------------------------------- #
# Default bars_fn — reuses the journal's loader-routing path, adapted for OHLC #
# --------------------------------------------------------------------------- #
def _frame_to_ohlc_bars(df: Any) -> list[dict[str, Any]]:
    """Normalize a loader OHLCV DataFrame to ``[{ts, open, high, low, close}, ...]``.

    Adapted from ``committee_journal_tool._frame_to_bars`` (which keeps only
    ts/open/close — sufficient for its return-window math, not for stop/TP
    evaluation) to also carry high/low, required by the conditional-order
    fill rules.
    """
    import pandas as pd

    frame = df.reset_index()
    frame.columns = [str(c).lower() for c in frame.columns]
    ts_col = next(
        (
            c
            for c in ("date", "datetime", "time", "timestamp", "trade_date", "index")
            if c in frame.columns
        ),
        frame.columns[0],
    )
    bars: list[dict[str, Any]] = []
    for _, row in frame.iterrows():
        ts = pd.Timestamp(row[ts_col])
        ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
        bars.append(
            {
                "ts": ts.to_pydatetime(),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
            }
        )
    return bars


def default_bars_fn(symbol: str, now: datetime) -> dict[str, Any]:
    """Fetch the latest confirmed daily bar for ``symbol`` via okx -> ccxt.

    Same fallback routing as ``committee_journal_tool._loader_fetch_bars``
    (``get_loader_cls_with_fallback`` over ``("okx", "ccxt")``), but requests
    ``interval="1D"`` and keeps the full OHLC. A 5-day lookback window
    absorbs weekend/holiday gaps in the underlying source; OKX's loader
    already filters to *confirmed* (closed) candles only, so the last bar
    returned is always the latest fully-closed UTC day.

    Raises ``RuntimeError`` on failure for both sources — callers must never
    invent a price; the caller (``run_tick``) records the error and leaves
    the position untouched this tick.
    """
    from backtest.loaders.registry import get_loader_cls_with_fallback

    start = now - timedelta(days=5)
    last_exc: Exception | None = None
    for source in ("okx", "ccxt"):
        try:
            loader = get_loader_cls_with_fallback(source)()
            frames = loader.fetch(
                [symbol],
                start.strftime("%Y-%m-%d"),
                now.strftime("%Y-%m-%d"),
                None,
                interval="1D",
            )
            df = frames.get(symbol)
            if df is None or getattr(df, "empty", True):
                continue
            bars = _frame_to_ohlc_bars(df)
            if not bars:
                continue
            return bars[-1]
        except Exception as exc:  # try the next source
            last_exc = exc
    raise RuntimeError(f"no daily bar for {symbol} via okx/ccxt: {last_exc}")


def default_bars_fn_1h(symbol: str, now: datetime) -> list[dict[str, Any]]:
    """Fetch recent confirmed 1H bars for ``symbol`` via okx -> ccxt.

    Same fallback routing as ``default_bars_fn`` but ``interval="1H"`` and it
    returns the whole confirmed list (``run_tick`` filters to the bars after the
    per-symbol watermark and evaluates them chronologically). A 3-day lookback
    covers a stopped/late job across a couple of days; a longer outage simply
    skips the intervening bars (no deep backfill, per design §2.1). OKX's loader
    filters to *confirmed* (closed) candles, so the still-forming bar is never
    returned.

    Raises ``RuntimeError`` on failure for both sources — callers must never
    invent a price; ``run_tick`` records the error and leaves the position (and
    its watermark) untouched this tick.
    """
    from backtest.loaders.registry import get_loader_cls_with_fallback

    start = now - timedelta(days=3)
    last_exc: Exception | None = None
    for source in ("okx", "ccxt"):
        try:
            loader = get_loader_cls_with_fallback(source)()
            frames = loader.fetch(
                [symbol],
                start.strftime("%Y-%m-%d"),
                now.strftime("%Y-%m-%d"),
                None,
                interval="1H",
            )
            df = frames.get(symbol)
            if df is None or getattr(df, "empty", True):
                continue
            bars = _frame_to_ohlc_bars(df)
            if not bars:
                continue
            return bars
        except Exception as exc:  # try the next source
            last_exc = exc
    raise RuntimeError(f"no 1H bars for {symbol} via okx/ccxt: {last_exc}")


# --------------------------------------------------------------------------- #
# Tick                                                                         #
# --------------------------------------------------------------------------- #
def _fmt_date(now: datetime) -> str:
    dt = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")


def _iso(now: datetime) -> str:
    dt = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    return (
        dt.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _retriable_decision_ids(store: PaperStore) -> list[str]:
    """Decision ids in the ledger whose ONLY rows are retriable (price-
    unavailable) noops — i.e. they were never actually executed and the spec
    promises a retry. Reuses the translator's retriable-noop semantics."""
    from src.paper.translator import _is_retriable_noop

    only_retriable: dict[str, bool] = {}
    for row in store.iter_ledger():
        did = row.get("decision_id")
        if not did:
            continue
        r = _is_retriable_noop(row)
        only_retriable[did] = r if did not in only_retriable else (only_retriable[did] and r)
    return [did for did, ok in only_retriable.items() if ok]


def _drive_retries(broker: PaperBroker, now: datetime) -> list[dict]:
    """Re-run execute_decision for retriable decisions decided within the last
    7 days whose price is (now) available (review I3). Kill switch is honored by
    execute_decision itself; results are returned for the tick payload."""
    from src.committee import journal
    from src.paper.broker import _parse_iso
    from src.paper.translator import execute_decision

    retriable = _retriable_decision_ids(broker.store)
    if not retriable:
        return []
    entries = {e["id"]: e for e in journal.load_entries()}
    results: list[dict] = []
    for did in retriable:
        entry = entries.get(did)
        if entry is None:
            continue
        decided = _parse_iso(entry.get("decided_at"))
        if decided is None or (now - decided) > _RETRY_MAX_AGE:
            continue
        results.append(execute_decision(entry, broker))
    return results


def _evaluate_1d(
    store: PaperStore,
    broker: PaperBroker,
    fetch_bar: BarsFn,
    now: datetime,
    conditional_fills: list[dict],
    errors: list[dict],
    notes: list[str],
    marks: dict[str, float],
) -> None:
    """1D tick: one confirmed daily bar per position. Byte-identical to the
    pre-intraday behavior — no ``tick_state.json`` is created or read."""
    for pos in store.load_positions():
        symbol = pos["symbol"]
        try:
            bar = fetch_bar(symbol, now)
        except Exception as exc:  # never invent a price; position stays untouched
            errors.append({"symbol": symbol, "error": str(exc)})
            continue
        marks[symbol] = float(bar["close"])
        # Review C2: the entry-day bar is not evaluated for conditionals — note
        # it so the skip is visible, then still mark the position to market.
        if bar_status_vs_entry(pos.get("opened_at"), bar.get("ts")) == "entry_day":
            notes.append(
                f"entry-day bar skipped for {symbol} — conservative: "
                "no same-day conditional fills"
            )
        conditional_fills.extend(broker.evaluate_conditionals(symbol, bar))


def _bars_to_evaluate(bars: list[dict], watermark: str | None) -> list[dict]:
    """Chronologically-sorted subset of ``bars`` to evaluate this 1H tick.

    First-ever tick (no watermark): only the NEWEST confirmed bar (no deep
    backfill). Otherwise: every bar strictly AFTER the watermark.
    """
    ordered = sorted(bars, key=lambda b: _parse_iso(b.get("ts")) or datetime.min.replace(tzinfo=timezone.utc))
    if watermark is None:
        return ordered[-1:]
    wm = _parse_iso(watermark)
    if wm is None:
        return ordered[-1:]
    return [b for b in ordered if (_parse_iso(b.get("ts")) or wm) > wm]


def _evaluate_1h(
    store: PaperStore,
    broker: PaperBroker,
    fetch_bar: BarsFn,
    now: datetime,
    conditional_fills: list[dict],
    errors: list[dict],
    notes: list[str],
    marks: dict[str, float],
    state: dict,
) -> None:
    """1H intraday tick: evaluate every confirmed bar after each symbol's
    watermark, chronologically, then advance the watermark in ``state``.

    The per-bar rules (stop-beats-TP, gap-at-open, entry-partial-bar skip) are
    unchanged — only applied per 1H bar in order. ``bars_fn`` returns the list
    of confirmed 1H bars; a fetch failure records an error and leaves the
    position AND its watermark untouched (retried next tick — never invent a
    price). The watermark advances whenever bars are evaluated, even if no
    conditional fired. ``state`` is the shared tick_state dict owned by
    ``run_tick`` (which persists it once, after the event check), so the 1H
    watermark and the event bookkeeping land in a single atomic write.
    """
    period = _INTERVAL_PERIOD["1H"]
    last_bar_ts: dict[str, Any] = state["last_bar_ts"]
    last_price: dict[str, Any] = state["last_price"]

    for pos in store.load_positions():
        symbol = pos["symbol"]
        try:
            bars = fetch_bar(symbol, now)
        except Exception as exc:  # never invent a price; watermark stays put
            errors.append({"symbol": symbol, "error": str(exc)})
            continue
        bars = list(bars or [])
        if not bars:
            continue  # no confirmed bars: nothing to mark or advance

        to_eval = _bars_to_evaluate(bars, last_bar_ts.get(symbol))
        # Mark-to-market at the newest confirmed bar's close (latest price),
        # regardless of how many bars are newer than the watermark.
        newest = max(bars, key=lambda b: _parse_iso(b.get("ts")) or datetime.min.replace(tzinfo=timezone.utc))
        marks[symbol] = float(newest["close"])
        last_price[symbol] = marks[symbol]

        noted_entry_skip = False
        for bar in to_eval:
            if (
                not noted_entry_skip
                and bar_status_vs_entry(pos.get("opened_at"), bar.get("ts"), period)
                == "entry_day"
            ):
                notes.append(
                    f"entry bar skipped for {symbol} — conservative: no "
                    "conditional fills on the entry-hour (partial) bar"
                )
                noted_entry_skip = True
            conditional_fills.extend(
                broker.evaluate_conditionals(symbol, bar, bar_period=period)
            )

        if to_eval:  # advance watermark to the last evaluated bar
            last_bar_ts[symbol] = _fmt_ts(to_eval[-1].get("ts"))


# --------------------------------------------------------------------------- #
# Event trigger (Task 3) — default fetchers + watched-symbol resolution        #
# --------------------------------------------------------------------------- #
def default_event_price_fn(symbol: str) -> float:
    """Live OKX last price for the event price-move check.

    Reuses the broker's ``default_price_fn`` (which reuses the snapshot
    module's fetch path — no new HTTP) and returns the bare float. Raises when
    no live price is available; ``run_tick`` records that as a tick error and
    the symbol simply gets no trigger this tick (never invent a price).
    """
    from src.paper.broker import default_price_fn

    return float(default_price_fn(symbol)["price"])


def default_event_funding_fn(symbol: str) -> float:
    """Current perpetual funding rate for the event funding check.

    Reuses ``crypto_snapshot_tool``'s funding fetcher (OKX REST -> ccxt
    fallback). Raises when funding is unavailable (a NO_DATA sentinel or a
    missing rate), so the symbol gets no funding trigger this tick.
    """
    from src.tools import crypto_snapshot_tool

    result = crypto_snapshot_tool._build_funding_rate(
        symbol, crypto_snapshot_tool._fetch_row
    )
    if not isinstance(result, dict):
        raise RuntimeError(f"no funding rate for {symbol}: {result}")
    rate = result.get("current_rate")
    if rate is None:
        raise RuntimeError(f"funding rate missing for {symbol}")
    return float(rate)


def default_journal_ref_fn(store: PaperStore) -> Callable[[str], "float | None"]:
    """Build the reference-price resolver for the event price-move check.

    Returns a closure ``ref(symbol) -> float | None`` that reads the LAST
    committee decision for ``symbol`` from the decision journal and resolves
    its execution price:
      1. the ledger fill's ``fill_price`` for that decision (the actual paper
         execution price), else
      2. that journal entry's ``ref_price`` (the decision-time reference), else
      3. ``None`` (no decision on record — the caller falls back to the
         previous tick's stored price).
    """

    def _ref(symbol: str) -> float | None:
        from src.committee import journal

        try:
            entries = [e for e in journal.load_entries() if e.get("symbol") == symbol]
        except Exception:
            return None
        if not entries:
            return None
        decision_id = entries[-1].get("id")  # journal is oldest-first
        fill_price: float | None = None
        if decision_id:
            for row in store.iter_ledger():
                if row.get("decision_id") == decision_id and row.get("fill_price") is not None:
                    fill_price = float(row["fill_price"])  # last matching fill wins
        if fill_price is not None:
            return fill_price
        ref_price = entries[-1].get("ref_price")
        return float(ref_price) if ref_price is not None else None

    return _ref


def _watched_symbols(store: PaperStore) -> list[str]:
    """Union of open-position symbols and ``VIBE_COMMITTEE_SYMBOLS``.

    Reuses ``scheduled_routes._parse_committee_symbols`` (imported lazily to
    avoid a FastAPI import at module load) so the committee symbol universe is
    parsed exactly once, the same way for the scheduled committee job and the
    event trigger. Open positions come first; order is otherwise preserved and
    duplicates dropped.
    """
    from src.api.scheduled_routes import _parse_committee_symbols

    ordered = [p["symbol"] for p in store.load_positions()] + _parse_committee_symbols()
    seen: set[str] = set()
    result: list[str] = []
    for sym in ordered:
        if sym not in seen:
            seen.add(sym)
            result.append(sym)
    return result


def _run_event_check(
    store: PaperStore,
    state: dict,
    now: datetime,
    errors: list[dict],
    *,
    event_price_fn: Callable[[str], float] | None,
    event_funding_fn: Callable[[str], float] | None,
    journal_ref_fn: Callable[[str], "float | None"] | None,
    config: Any,
) -> tuple[list[dict], dict]:
    """Run the deterministic event check for the watched symbols.

    Wraps the (real or injected) price/funding fetchers so a per-symbol fetch
    failure is recorded in the tick ``errors`` list (spec: "error recorded in
    the tick result") and then re-raised — ``check_events`` catches it, emits
    no trigger, and never invents a price. Returns ``(triggers, new_state)``.
    """
    from src.paper.events import check_events

    symbols = _watched_symbols(store)
    if not symbols:
        return [], state

    raw_price = event_price_fn or default_event_price_fn
    raw_funding = event_funding_fn or default_event_funding_fn
    ref_fn = journal_ref_fn or default_journal_ref_fn(store)

    def _wrap(fn: Callable[[str], float], label: str) -> Callable[[str], float]:
        def wrapped(symbol: str) -> float:
            try:
                return fn(symbol)
            except Exception as exc:
                errors.append({"symbol": symbol, "error": f"event {label} fetch failed: {exc}"})
                raise

        return wrapped

    return check_events(
        symbols,
        state,
        price_fn=_wrap(raw_price, "price"),
        funding_fn=_wrap(raw_funding, "funding"),
        journal_ref_fn=ref_fn,
        now=now,
        config=config,
    )


def run_tick(
    store: PaperStore | None = None,
    *,
    bars_fn: BarsFn | None = None,
    price_fn: PriceFn | None = None,
    now: datetime | None = None,
    event_price_fn: Callable[[str], float] | None = None,
    event_funding_fn: Callable[[str], float] | None = None,
    journal_ref_fn: Callable[[str], "float | None"] | None = None,
) -> dict:
    """Evaluate conditional orders for every open position, then snapshot equity.

    Returns ``{"conditional_fills": [...], "equity_snapshot": {...}, "errors": [...]}``.

    Same-UTC-day idempotent: a second call on the same day re-evaluates
    conditionals harmlessly (already-executed stops/TPs were removed from
    position state on the first pass, so the same bar cannot refill them) but
    skips the ``equity.jsonl`` append if today's snapshot was already
    recorded — no duplicate rows, nothing overwritten.
    """
    # Kill switch: no-op fast when disabled (review cleanup 1) — no bar fetches,
    # no equity append, no account touched.
    from src.paper.translator import _paper_enabled

    if not _paper_enabled():
        return {
            "conditional_fills": [],
            "equity_snapshot": {},
            "errors": [],
            "notes": [],
            "retried_decisions": [],
            "event_triggers": [],
            "disabled": True,
        }

    store = store or PaperStore(paper_root())
    broker = PaperBroker(store, price_fn=price_fn)
    now = now or datetime.now(timezone.utc)
    today = _fmt_date(now)
    interval = _tick_interval()

    conditional_fills: list[dict] = []
    errors: list[dict] = []
    notes: list[str] = []
    marks: dict[str, float] = {}

    # Event trigger (Task 3): checked EVERY tick. To preserve Task 1's "no
    # tick_state.json in 1D mode" regression, tick_state is only loaded/saved
    # when it's actually needed — 1H mode (watermark) OR events enabled (event
    # bookkeeping). With both thresholds off in 1D mode, no tick_state file is
    # ever created (byte-identical to pre-Task-3 behavior). VIBE_EVENT_PRICE_
    # MOVE_PCT defaults ON (5), so a plain 1D deployment now DOES create
    # tick_state — carrying only event data; last_bar_ts stays empty.
    from src.paper.events import EventConfig

    event_config = EventConfig.from_env()
    need_state = interval == "1H" or event_config.enabled
    state = store.load_tick_state() if need_state else None

    event_triggers: list[dict] = []
    if event_config.enabled and state is not None:
        # Run BEFORE the bar evaluation so the price-move reference reads the
        # PREVIOUS tick's last_price (the 1H bar eval below refreshes it to the
        # current bar close for open-position symbols).
        event_triggers, state = _run_event_check(
            store, state, now, errors,
            event_price_fn=event_price_fn,
            event_funding_fn=event_funding_fn,
            journal_ref_fn=journal_ref_fn,
            config=event_config,
        )

    if interval == "1H":
        fetch_bar = bars_fn or default_bars_fn_1h
        _evaluate_1h(store, broker, fetch_bar, now, conditional_fills, errors, notes, marks, state)
    else:
        fetch_bar = bars_fn or default_bars_fn
        _evaluate_1d(store, broker, fetch_bar, now, conditional_fills, errors, notes, marks)

    if state is not None:
        store.save_tick_state(state)

    # Review I3: retry decisions whose only ledger rows are retriable noops,
    # now that prices may be available again. Bounded to the last 7 days.
    retried_decisions = _drive_retries(broker, now)

    # Idempotency keys on the persisted row's UTC date (derived from its ts,
    # which we stamp with the logical tick time). Transient bookkeeping keys
    # (date / already_recorded) are NOT persisted (review cleanup 4).
    already_recorded = any(
        (e.get("ts") or "")[:10] == today for e in store.iter_equity()
    )
    equity_snapshot = broker.equity(mark_prices=marks)
    equity_snapshot["ts"] = _iso(now)
    persist_row = dict(equity_snapshot)
    equity_snapshot["date"] = today
    equity_snapshot["already_recorded"] = already_recorded
    if not already_recorded:
        store.append_equity(persist_row)

    return {
        "conditional_fills": conditional_fills,
        "equity_snapshot": equity_snapshot,
        "errors": errors,
        "notes": notes,
        "retried_decisions": retried_decisions,
        "event_triggers": event_triggers,
    }
