"""Decision -> order translator with per-decision idempotency.

Consumes a decision-journal entry dict (Task 1 shape; ``stop_loss`` /
``take_profit`` / ``position_size_pct`` are conditionally-keyed — absent on
legacy entries, so always read via ``.get``) and a ``PaperBroker`` (Task 2),
and issues the corresponding paper order.

Rating -> action mapping (binding; design spec / t4 brief; the real 5-tier
enum is ``Buy | Overweight | Hold | Underweight | Sell`` per
``committee/schemas.py::parse_rating`` — matched case-insensitively since the
journal stores it Title-cased, e.g. "Hold"):

  Buy,        no position -> market_buy sized position_size_pct (default
                              DEFAULT_SIZE_PCT) percent of current equity.
  Overweight, no position -> same, at HALF that size.
  Buy/Overweight, existing position -> add, same sizing rule (broker enforces
                              the symbol exposure cap; MandateViolation on
                              zero headroom or max-open-positions is caught,
                              see below).
  stop = stop_loss if provided, else fill_price * (1 - DEFAULT_STOP_PCT/100)
         ("fill_price" = this order's own fill -- the position's avg_entry
         for a fresh open, or the incremental fill for an add).
  tp    = take_profit if provided, else price_target (single TP, fraction 1.0).
  Hold                     -> set_risk with any provided (typed) stop/TP;
                              nothing else. price_target is NOT used as a TP
                              fallback for Hold (only explicit stop_loss /
                              take_profit apply) -- a Hold on an entry that
                              only carries price_target is a pure no-op.
  Underweight              -> market_sell fraction 0.5.
  Sell                     -> market_sell fraction 1.0.
  Underweight/Sell, no position -> ledger noop, note="sell signal with no
                              position" (spot long-only v1: no shorting).

Idempotency: before acting, scan the ledger for an entry whose
``decision_id`` equals this entry's ``id``. EXACT RULE: a decision counts as
"already executed" -- and a repeat call is skipped -- if the ledger has ANY
row with that decision_id, EXCEPT rows with ``order_type == "noop"`` AND
``note == RETRIABLE_NOTE`` ("price unavailable — not executed"). Those
retriable rows are the sole exception: a price-fetch failure never fills, so
it must not block a later retry once prices are available again. Every other
row -- real fills, MandateViolation noops (max positions / symbol exposure
cap), sell-with-no-position noops, and Hold risk-update noops -- permanently
marks the decision as executed.

Kill switch: ``VIBE_PAPER_ENABLED`` -- unset means ENABLED (default "1" per
spec); "0" / "false" / "" (case-insensitive) means DISABLED. When disabled,
no broker method is called at all, so no account is ever auto-created.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Any

from src.paper.broker import MandateViolation, PaperBroker, PriceUnavailable
from src.paper.store import PaperStore

RETRIABLE_NOTE = "price unavailable — not executed"
_SELL_NO_POSITION_NOTE = "sell signal with no position"

_POSITIVE_RATINGS = {"buy", "overweight"}
_REDUCE_RATINGS = {"underweight", "sell"}


def _utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _coerce_float(value: Any) -> float | None:
    """Best-effort numeric coercion; ``None``/non-numeric -> ``None``.

    Journal entries may carry a non-numeric ``price_target`` (e.g. the
    literal string ``"n/a"``) because the tool layer does not coerce it --
    treat that as absent rather than raising, falling through to no TP (or
    the stop-only default).
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _paper_enabled() -> bool:
    """``VIBE_PAPER_ENABLED`` truthiness: unset -> enabled; "0"/"false"/"" -> disabled."""
    val = os.environ.get("VIBE_PAPER_ENABLED")
    if val is None:
        return True
    return val.strip().lower() not in ("0", "false", "")


def _find_position(positions: list[dict], symbol: str) -> dict | None:
    for pos in positions:
        if pos["symbol"] == symbol:
            return pos
    return None


def _noop_entry(
    store: PaperStore,
    *,
    decision_id: str,
    symbol: str,
    note: str,
    side: str | None = None,
) -> dict:
    """Build and persist a noop ledger row (no fill, no cash/position change).

    Persisted (not just returned) so the idempotency scan can see it on a
    later call -- see the module docstring's exact rule for the one
    exception (retriable price-unavailable noops).
    """
    entry = {
        "ts": _utc_now_iso(),
        "trade_id": uuid.uuid4().hex,
        "symbol": symbol,
        "side": side,
        "qty": 0.0,
        "fill_price": None,
        "slippage_paid": 0.0,
        "fee_paid": 0.0,
        "order_type": "noop",
        "decision_id": decision_id,
        "realized_pnl": None,
        "note": note,
    }
    store.append_ledger(entry)
    return entry


def _is_retriable_noop(row: dict) -> bool:
    return row.get("order_type") == "noop" and row.get("note") == RETRIABLE_NOTE


def _already_executed(store: PaperStore, decision_id: str) -> bool:
    for row in store.iter_ledger():
        if row.get("decision_id") != decision_id:
            continue
        if _is_retriable_noop(row):
            continue
        return True
    return False


# --------------------------------------------------------------------------- #
# Rating handlers                                                             #
# --------------------------------------------------------------------------- #
def _buy_or_add(
    broker: PaperBroker,
    *,
    decision_id: str,
    symbol: str,
    size_pct: float | None,
    size_frac: float,
    stop_loss: float | None,
    take_profit: float | None,
    price_target: float | None,
) -> list[dict]:
    effective_pct = size_pct if size_pct is not None else broker.config.default_size_pct
    equity_snapshot = broker.equity()
    notional = equity_snapshot["equity"] * (effective_pct / 100.0) * size_frac

    tp = take_profit if take_profit is not None else price_target

    try:
        result = broker.market_buy(
            symbol,
            notional,
            decision_id=decision_id,
            stop=stop_loss,
            take_profit=tp,
        )
    except MandateViolation as exc:
        return [
            _noop_entry(
                broker.store, decision_id=decision_id, symbol=symbol, note=str(exc), side="buy"
            )
        ]
    except PriceUnavailable:
        return [
            _noop_entry(
                broker.store,
                decision_id=decision_id,
                symbol=symbol,
                note=RETRIABLE_NOTE,
                side="buy",
            )
        ]

    if stop_loss is None:
        # Only apply the default stop when the position has NO stop yet (a fresh
        # open, or an add onto a stopless position). An add onto a position that
        # already carries a stop keeps it (review cleanup 3) — an explicit
        # stop_loss still always applies via market_buy above.
        positions = broker.store.load_positions()
        pos = _find_position(positions, symbol)
        if pos is not None and pos.get("stop") is None:
            default_stop = result["fill_price"] * (1 - broker.config.default_stop_pct / 100.0)
            broker.set_risk(symbol, stop=default_stop, take_profit=None)

    return [result]


def _apply_hold(
    broker: PaperBroker,
    *,
    decision_id: str,
    symbol: str,
    stop_loss: float | None,
    take_profit: float | None,
) -> list[dict]:
    if stop_loss is None and take_profit is None:
        return []
    positions = broker.store.load_positions()
    if _find_position(positions, symbol) is None:
        return []
    broker.set_risk(symbol, stop=stop_loss, take_profit=take_profit)
    return [
        _noop_entry(
            broker.store,
            decision_id=decision_id,
            symbol=symbol,
            note="risk parameters updated (Hold)",
        )
    ]


def _reduce(
    broker: PaperBroker, *, decision_id: str, symbol: str, fraction: float
) -> list[dict]:
    try:
        result = broker.market_sell(
            symbol, fraction, decision_id=decision_id, reason="rating signal"
        )
    except PriceUnavailable:
        return [
            _noop_entry(
                broker.store,
                decision_id=decision_id,
                symbol=symbol,
                note=RETRIABLE_NOTE,
                side="sell",
            )
        ]

    if result is None:
        return [
            _noop_entry(
                broker.store,
                decision_id=decision_id,
                symbol=symbol,
                note=_SELL_NO_POSITION_NOTE,
                side="sell",
            )
        ]
    return [result]


# --------------------------------------------------------------------------- #
# Entry point                                                                  #
# --------------------------------------------------------------------------- #
def execute_decision(entry: dict, broker: PaperBroker) -> dict:
    """Translate one journal decision into paper order(s). Never raises for
    business-rule outcomes (MandateViolation / PriceUnavailable / no-position
    sell are all caught and recorded as ledger noops)."""
    decision_id = entry["id"]

    if not _paper_enabled():
        return {"decision_id": decision_id, "actions": [], "skipped": "paper trading disabled"}

    if _already_executed(broker.store, decision_id):
        return {"decision_id": decision_id, "actions": [], "skipped": "already executed"}

    symbol = entry["symbol"]
    rating = str(entry.get("rating") or "").strip().lower()

    stop_loss = _coerce_float(entry.get("stop_loss"))
    take_profit = _coerce_float(entry.get("take_profit"))
    price_target = _coerce_float(entry.get("price_target"))
    size_pct = _coerce_float(entry.get("position_size_pct"))

    if rating in _POSITIVE_RATINGS:
        size_frac = 0.5 if rating == "overweight" else 1.0
        actions = _buy_or_add(
            broker,
            decision_id=decision_id,
            symbol=symbol,
            size_pct=size_pct,
            size_frac=size_frac,
            stop_loss=stop_loss,
            take_profit=take_profit,
            price_target=price_target,
        )
    elif rating == "hold":
        actions = _apply_hold(
            broker,
            decision_id=decision_id,
            symbol=symbol,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )
    elif rating in _REDUCE_RATINGS:
        fraction = 0.5 if rating == "underweight" else 1.0
        actions = _reduce(broker, decision_id=decision_id, symbol=symbol, fraction=fraction)
    else:
        # Defensive only: schemas.parse_rating always returns one of the
        # 5-tier enum values (defaulting to Hold on unparseable text), so
        # this branch should be unreachable in practice.
        actions = []

    return {"decision_id": decision_id, "actions": actions, "skipped": None}
