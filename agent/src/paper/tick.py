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

from src.paper.broker import PaperBroker, PriceFn
from src.paper.store import PaperStore, paper_root

BarsFn = Callable[[str, datetime], dict[str, Any]]


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
    store = store or PaperStore(paper_root())
    broker = PaperBroker(store, price_fn=price_fn)
    fetch_bar = bars_fn or default_bars_fn
    now = now or datetime.now(timezone.utc)
    today = _fmt_date(now)

    conditional_fills: list[dict] = []
    errors: list[dict] = []
    marks: dict[str, float] = {}

    for pos in store.load_positions():
        symbol = pos["symbol"]
        try:
            bar = fetch_bar(symbol, now)
        except Exception as exc:  # never invent a price; position stays untouched
            errors.append({"symbol": symbol, "error": str(exc)})
            continue
        marks[symbol] = float(bar["close"])
        conditional_fills.extend(broker.evaluate_conditionals(symbol, bar))

    already_recorded = any(e.get("date") == today for e in store.iter_equity())
    equity_snapshot = broker.equity(mark_prices=marks)
    equity_snapshot["date"] = today
    equity_snapshot["already_recorded"] = already_recorded
    if not already_recorded:
        store.append_equity(equity_snapshot)

    return {
        "conditional_fills": conditional_fills,
        "equity_snapshot": equity_snapshot,
        "errors": errors,
    }
