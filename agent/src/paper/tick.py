"""Daily mark-to-market tick: conditional stop/TP evaluation + equity snapshot.

Runs once per UTC day (idempotent — see ``run_tick``): for every open
position, fetches the latest confirmed daily OHLC bar (reusing the journal's
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

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from src.paper.broker import PaperBroker, PriceFn, bar_status_vs_entry
from src.paper.store import PaperStore, paper_root

BarsFn = Callable[[str, datetime], dict[str, Any]]

# Retriable noops older than this are not re-driven by the tick (the spec's
# "retried on next tick" promise is bounded so stale decisions don't resurrect).
_RETRY_MAX_AGE = timedelta(days=7)


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


def run_tick(
    store: PaperStore | None = None,
    *,
    bars_fn: BarsFn | None = None,
    price_fn: PriceFn | None = None,
    now: datetime | None = None,
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
            "disabled": True,
        }

    store = store or PaperStore(paper_root())
    broker = PaperBroker(store, price_fn=price_fn)
    fetch_bar = bars_fn or default_bars_fn
    now = now or datetime.now(timezone.utc)
    today = _fmt_date(now)

    conditional_fills: list[dict] = []
    errors: list[dict] = []
    notes: list[str] = []
    marks: dict[str, float] = {}

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
    }
