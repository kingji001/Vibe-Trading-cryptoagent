"""Decision-level paper-trading PnL — the bridge from a journal decision to
its realized money outcome, consumed by the reflection officer (Task 6).

Persistence-read only: no trading logic (that's ``broker.py``), no mandates,
no fills. Reconstructs, from the append-only ``ledger.jsonl``, the full
lifecycle ("lineage") of the physical position a decision touched, and
reports realized/unrealized PnL, fees, and how (or whether) it exited.

Ledger attribution, binding (t6 brief + coordination notes):
  - Noop rows (``order_type == "noop"``) never contribute to money math
    (``realized_pnl`` is always ``None``, ``qty`` 0.0, ``fill_price`` None on
    those rows) but ARE evidence: a decision whose only ledger rows are
    noops is reported as ``executed: false`` / ``exit_kind: "not_executed"``,
    and the noop note(s) are folded into ``summary``.
  - Conditional fills (stop/take-profit, via ``broker.evaluate_conditionals``)
    carry the POSITION's decision_id, i.e. the id of whichever decision most
    recently OPENED the position from flat (``broker._apply_buy`` only sets
    ``decision_id`` when ``held is None`` -- a fresh open; it is left
    untouched on every subsequent add). So exit-kind attribution for the
    opening decision works directly off ``decision_id`` matches for that
    lineage. A closing MARKET sell, by contrast, carries the SELLING
    decision's own id (``translator._reduce`` passes the Sell/Underweight
    decision's ``decision_id``, not the position's) -- which differs from the
    opening decision's id whenever a *separate*, later Sell/Underweight
    decision is what closed the position. To resolve exit_kind for the
    OPENING decision in that case (and to fold that later realized PnL into
    the opening decision's outcome -- "did this call, followed to its
    conclusion, make money" is the whole point of PnL-aware reflection) this
    module replays the full ledger per-symbol into open/close "lineages"
    (see ``_replay_lineages``) rather than filtering by ``decision_id``
    alone. A query for a decision maps to the lineage containing that
    decision's own non-noop fill row -- whether that decision opened the
    lineage, added to it, or (partially/fully) reduced it -- and the
    reported realized/fees/exit_kind reflect that WHOLE lineage, not just
    the queried decision's own row.
  - ``position_open`` is true only when the queried decision's lineage is
    the SYMBOL's current (latest) lineage AND ``positions.json`` still holds
    it -- so a decision belonging to an older, already-fully-closed
    generation of the same symbol never falsely reports "open" just because
    the symbol was re-bought later under a different decision.
  - ``max_drawdown_pct`` is read from ``equity.jsonl`` per-position rows
    (written by the daily tick) whose timestamp falls within the lineage's
    open window [first row ts, last row ts or now-if-still-open]. Never
    invented: no snapshot in that window (e.g. the position opened and
    closed between two daily ticks) -> ``None``. When snapshots exist it is
    the worst (most negative) ``(mark - avg_entry) / avg_entry`` observed,
    clamped at 0.0 (a position that never went underwater has zero
    drawdown, not a positive number).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from src.paper.store import PaperStore, paper_root

MarkPriceFn = Callable[[str], Any]


# --------------------------------------------------------------------------- #
# ts parsing                                                                    #
# --------------------------------------------------------------------------- #
def _parse_ts(value: Any) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


# --------------------------------------------------------------------------- #
# ledger replay -> per-symbol open/close lineages                              #
# --------------------------------------------------------------------------- #
def _replay_lineages(rows: list[dict], symbol: str) -> list[dict]:
    """Replay ``rows`` (chronological, as stored) into a list of lineages for
    ``symbol``: each lineage is one continuous open-to-flat life of the
    position, ``{"rows": [...], "qty": float}``. A new lineage starts every
    time a buy fill arrives while the symbol is flat (no current lineage, or
    the current one's qty has been reduced to ~0 by prior sells) -- mirroring
    exactly how ``positions.json`` drops an entry when it empties and the
    broker creates a fresh one on the next buy (``PaperBroker._apply_buy`` /
    ``_execute_sell``). Noop rows never participate (no fill, nothing to
    replay)."""
    lineages: list[dict] = []
    current: dict | None = None
    for row in rows:
        if row.get("symbol") != symbol or row.get("order_type") == "noop":
            continue
        side = row.get("side")
        qty = float(row.get("qty") or 0.0)
        if side == "buy":
            if current is None or current["qty"] <= 1e-9:
                current = {"rows": [], "qty": 0.0}
                lineages.append(current)
            current["rows"].append(row)
            current["qty"] += qty
        elif side == "sell":
            if current is None:
                continue  # defensive: a sell fill implies a live lineage
            current["rows"].append(row)
            current["qty"] -= qty
    return lineages


# --------------------------------------------------------------------------- #
# mark price (never invented)                                                  #
# --------------------------------------------------------------------------- #
def _default_mark_price_fn(symbol: str) -> float | None:
    from src.paper.broker import PriceUnavailable, default_price_fn

    try:
        return float(default_price_fn(symbol)["price"])
    except PriceUnavailable:
        return None


def _resolve_mark(symbol: str, mark_price_fn: MarkPriceFn | None) -> float | None:
    fn = mark_price_fn or _default_mark_price_fn
    try:
        result = fn(symbol)
    except Exception:
        return None
    if result is None:
        return None
    if isinstance(result, dict):
        price = result.get("price")
        return None if price is None else float(price)
    return float(result)


# --------------------------------------------------------------------------- #
# max drawdown, from equity.jsonl per-position rows, within the lineage window #
# --------------------------------------------------------------------------- #
def _max_drawdown(
    store: PaperStore, symbol: str, lineage_rows: list[dict], position_open: bool
) -> float | None:
    if not lineage_rows:
        return None
    open_ts = _parse_ts(lineage_rows[0]["ts"])
    close_ts = None if position_open else _parse_ts(lineage_rows[-1]["ts"])

    pct_values: list[float] = []
    for snap in store.iter_equity():
        ts_raw = snap.get("ts")
        if not ts_raw:
            continue
        try:
            snap_ts = _parse_ts(ts_raw)
        except ValueError:
            continue
        if snap_ts < open_ts:
            continue
        if close_ts is not None and snap_ts > close_ts:
            continue
        for prow in snap.get("positions") or []:
            if prow.get("symbol") != symbol:
                continue
            avg_entry = prow.get("avg_entry")
            mark = prow.get("mark")
            if not avg_entry:
                continue
            pct_values.append((float(mark) - float(avg_entry)) / float(avg_entry) * 100.0)

    if not pct_values:
        return None
    return min(0.0, min(pct_values))


# --------------------------------------------------------------------------- #
# summary block (<=5 lines, quoted verbatim by the reflection officer)         #
# --------------------------------------------------------------------------- #
def _not_executed_result(decision_id: str, *, symbol: str | None, reason: str) -> dict:
    parts = [f"Decision {decision_id}" + (f" ({symbol})" if symbol else "") + ": not executed."]
    if reason:
        parts.append(f"Reason: {reason}.")
    return {
        "decision_id": decision_id,
        "executed": False,
        "realized_pnl": None,
        "fees_paid": 0.0,
        "unrealized_pnl": None,
        "position_open": False,
        "exit_kind": "not_executed",
        "max_drawdown_pct": None,
        "summary": "\n".join(parts),
    }


def _build_summary(
    decision_id: str,
    symbol: str,
    *,
    realized_pnl: float,
    fees_paid: float,
    unrealized_pnl: float | None,
    position_open: bool,
    exit_kind: str,
    max_drawdown_pct: float | None,
) -> str:
    lines = [f"Decision {decision_id} ({symbol}): executed -- exit_kind={exit_kind}."]
    lines.append(f"Realized PnL: {realized_pnl:.2f} | Fees paid: {fees_paid:.2f}")
    if position_open:
        if unrealized_pnl is not None:
            lines.append(f"Position OPEN -- unrealized PnL: {unrealized_pnl:.2f}")
        else:
            lines.append("Position OPEN -- unrealized PnL unavailable (no live price)")
    else:
        lines.append(f"Position CLOSED ({exit_kind})")
    if max_drawdown_pct is not None:
        lines.append(f"Max drawdown while open: {max_drawdown_pct:.2f}%")
    else:
        lines.append("Max drawdown: unavailable (insufficient equity history)")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# public entry point                                                           #
# --------------------------------------------------------------------------- #
def decision_pnl(
    decision_id: str,
    store: PaperStore | None = None,
    mark_price_fn: MarkPriceFn | None = None,
) -> dict:
    """Compute the realized/unrealized PnL outcome of one journaled decision.

    Returns (binding shape, t6 brief):
      {"decision_id", "executed": bool, "realized_pnl", "fees_paid",
       "unrealized_pnl", "position_open": bool,
       "exit_kind": "stopped"|"took_profit"|"closed_by_sell"|"open"|"not_executed",
       "max_drawdown_pct": float | None, "summary": str}

    Never raises for a missing paper account or an unexecuted decision --
    both simply resolve to ``executed: False`` / ``exit_kind: "not_executed"``
    (an empty/nonexistent ``store`` has no ledger rows at all, which is
    exactly that case).
    """
    store = store or PaperStore(paper_root())
    rows = list(store.iter_ledger())
    own_rows = [row for row in rows if row.get("decision_id") == decision_id]
    if not own_rows:
        return _not_executed_result(
            decision_id, symbol=None, reason="no paper-trading ledger rows for this decision"
        )

    symbol = own_rows[-1].get("symbol")
    non_noop = [row for row in own_rows if row.get("order_type") != "noop"]
    if not non_noop:
        notes = list(dict.fromkeys(row.get("note") for row in own_rows if row.get("note")))
        reason = "; ".join(notes) if notes else "no fill"
        return _not_executed_result(decision_id, symbol=symbol, reason=reason)

    target_trade_id = non_noop[0].get("trade_id")
    lineages = _replay_lineages(rows, symbol)
    lineage = next(
        (ln for ln in lineages if any(r.get("trade_id") == target_trade_id for r in ln["rows"])),
        None,
    )
    if lineage is None:
        # Defensive only: every non-noop buy/sell fill is tracked by the
        # replay, so this should be unreachable in practice.
        lineage = {"rows": list(non_noop), "qty": 0.0}

    positions = store.load_positions()
    held = next((p for p in positions if p.get("symbol") == symbol), None)
    is_latest_lineage = bool(lineages) and lineage is lineages[-1]
    position_open = is_latest_lineage and held is not None

    realized_pnl = sum(
        float(row["realized_pnl"]) for row in lineage["rows"] if row.get("realized_pnl") is not None
    )
    fees_paid = sum(float(row.get("fee_paid") or 0.0) for row in lineage["rows"])

    unrealized_pnl: float | None = None
    if position_open and held is not None:
        mark = _resolve_mark(symbol, mark_price_fn)
        if mark is not None:
            unrealized_pnl = (mark - held["avg_entry"]) * held["qty"]

    if position_open:
        exit_kind = "open"
    else:
        close_row = lineage["rows"][-1] if lineage["rows"] else None
        order_type = close_row.get("order_type") if close_row else None
        if order_type == "stop":
            exit_kind = "stopped"
        elif order_type == "take_profit":
            exit_kind = "took_profit"
        else:
            exit_kind = "closed_by_sell"

    max_drawdown_pct = _max_drawdown(store, symbol, lineage["rows"], position_open)

    summary = _build_summary(
        decision_id,
        symbol,
        realized_pnl=realized_pnl,
        fees_paid=fees_paid,
        unrealized_pnl=unrealized_pnl,
        position_open=position_open,
        exit_kind=exit_kind,
        max_drawdown_pct=max_drawdown_pct,
    )

    return {
        "decision_id": decision_id,
        "executed": True,
        "realized_pnl": realized_pnl,
        "fees_paid": fees_paid,
        "unrealized_pnl": unrealized_pnl,
        "position_open": position_open,
        "exit_kind": exit_kind,
        "max_drawdown_pct": max_drawdown_pct,
        "summary": summary,
    }
