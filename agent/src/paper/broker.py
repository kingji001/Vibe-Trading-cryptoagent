"""Paper-trading broker core — deterministic market fills, mandates, PnL.

Trading logic ONLY (persistence lives in ``store.py``). The broker simulates
market fills against live OKX last prices (via an injectable ``price_fn``; the
default reuses ``crypto_snapshot_tool``'s fetch path — no new HTTP), enforces
hard code-level mandates, and computes realized/unrealized PnL net of fees and
slippage.

Binding formulas (globals.md / design spec §3.1):
  buy fill  = price * (1 + slippage_bps/10000)
  sell fill = price * (1 - slippage_bps/10000)
  fee       = fill_notional * fee_bps/10000        (deducted from cash both sides)
  realized_pnl = (sell_fill - avg_entry) * qty_sold - sell_fee
                 (the buy-side fee already reduced cash at entry)

Binding dict shapes (for Tasks 3-7):
  position = {"symbol", "qty", "avg_entry", "stop",
              "take_profits": [{"price", "fraction"}], "opened_at", "decision_id"}
  ledger   = {"ts", "trade_id", "symbol", "side", "qty", "fill_price",
              "slippage_paid", "fee_paid", "order_type", "decision_id",
              "realized_pnl", "note"}

Spot long-only v1: no shorting; Sell/reduce on no position is not handled here
(the translator, Task 4, records that no-op) — ``market_sell`` returns ``None``.
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, TypedDict

from src.paper.store import PaperStore
from src.tools import crypto_snapshot_tool

logger = logging.getLogger(__name__)


class PriceQuote(TypedDict):
    price: float
    ts: str


PriceFn = Callable[[str], PriceQuote]


class MandateViolation(Exception):
    """Raised when a buy would breach a hard broker mandate (no state change)."""


class PriceUnavailable(Exception):
    """Raised when a live price cannot be fetched — the order is NOT filled."""


# --------------------------------------------------------------------------- #
# Config                                                                       #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class BrokerConfig:
    """Broker knobs, defaults per design spec §5 (all additive env vars)."""

    start_cash: float = 100_000.0
    slippage_bps: float = 5.0
    fee_bps: float = 10.0
    max_positions: int = 3
    max_symbol_pct: float = 25.0
    default_size_pct: float = 10.0
    default_stop_pct: float = 8.0

    @classmethod
    def from_env(cls) -> "BrokerConfig":
        env = os.environ
        return cls(
            start_cash=float(env.get("VIBE_PAPER_START_CASH", "100000")),
            slippage_bps=float(env.get("VIBE_PAPER_SLIPPAGE_BPS", "5")),
            fee_bps=float(env.get("VIBE_PAPER_FEE_BPS", "10")),
            max_positions=int(env.get("VIBE_PAPER_MAX_POSITIONS", "3")),
            max_symbol_pct=float(env.get("VIBE_PAPER_MAX_SYMBOL_PCT", "25")),
            default_size_pct=float(env.get("VIBE_PAPER_DEFAULT_SIZE_PCT", "10")),
            default_stop_pct=float(env.get("VIBE_PAPER_DEFAULT_STOP_PCT", "8")),
        )


# --------------------------------------------------------------------------- #
# Default price_fn — reuses the snapshot module's fetch path (no new HTTP)      #
# --------------------------------------------------------------------------- #
def default_price_fn(symbol: str) -> PriceQuote:
    """Fetch the live OKX last price via ``crypto_snapshot_tool``.

    Reuses the module-level last-price fetcher (``_build_last_price`` over the
    shared ``_fetch_row`` transport) rather than duplicating HTTP code, and
    translates its output into a ``PriceQuote``. Raises ``PriceUnavailable``
    when the underlying fetch returns a NO_DATA sentinel (a plain string).
    """
    last_price, _row = crypto_snapshot_tool._build_last_price(
        symbol, crypto_snapshot_tool._fetch_row
    )
    if not isinstance(last_price, dict):
        raise PriceUnavailable(f"no live price for {symbol}: {last_price}")
    return {"price": float(last_price["value"]), "ts": str(last_price["timestamp"])}


def _utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _fmt_ts(value: object) -> str:
    """Normalize a bar's ``ts`` (datetime or string) to an ISO-8601 UTC string."""
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        )
    return str(value)


def _parse_iso(value: object) -> datetime | None:
    """Parse a datetime or ISO-8601 string to a UTC datetime; None if unparseable."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# Daily conditional bars cover a one-UTC-day period starting at the bar ts.
_BAR_PERIOD = timedelta(days=1)


def bar_status_vs_entry(opened_at: object, bar_ts: object) -> str:
    """Classify a daily bar relative to a position's entry (review C2).

    Returns one of:
      - ``"evaluate"``  — the bar's whole period is AFTER entry (first full bar
        after the fill); conditional stops/TPs may be evaluated;
      - ``"entry_day"`` — the bar's period CONTAINS ``opened_at`` (the partially
        overlapping entry-day bar); skipped, conservatively, so pre-entry price
        action within the entry day cannot fire a fictitious stop/TP;
      - ``"pre_entry"`` — the bar's period ended before ``opened_at`` (entirely
        before the fill); skipped.

    Falls back to ``"evaluate"`` when either timestamp is missing/unparseable
    (legacy positions), preserving the pre-review behavior for those.
    """
    opened = _parse_iso(opened_at)
    start = _parse_iso(bar_ts)
    if opened is None or start is None:
        return "evaluate"
    if start > opened:
        return "evaluate"
    if start + _BAR_PERIOD <= opened:
        return "pre_entry"
    return "entry_day"


# --------------------------------------------------------------------------- #
# Broker                                                                        #
# --------------------------------------------------------------------------- #
class PaperBroker:
    """Deterministic paper broker: market fills, mandates, mark-to-market."""

    def __init__(self, store: PaperStore, price_fn: PriceFn | None = None) -> None:
        self.store = store
        self.price_fn: PriceFn = price_fn or default_price_fn
        self.config = BrokerConfig.from_env()

    # -- account ------------------------------------------------------------ #
    def _ensure_account(self) -> dict:
        account = self.store.load_account()
        if account is None:
            account = self.store.create_account(
                self.config.start_cash, asdict(self.config)
            )
        return account

    # -- fill math ---------------------------------------------------------- #
    def _buy_fill(self, price: float) -> float:
        return price * (1 + self.config.slippage_bps / 10_000)

    def _sell_fill(self, price: float) -> float:
        return price * (1 - self.config.slippage_bps / 10_000)

    def _fee(self, notional: float) -> float:
        return notional * self.config.fee_bps / 10_000

    # -- market buy --------------------------------------------------------- #
    def market_buy(
        self,
        symbol: str,
        notional_usdt: float,
        *,
        decision_id: str,
        stop: float | None,
        take_profit: float | None,
    ) -> dict:
        """Open or add to a long position for ``notional_usdt`` at the live fill.

        Mandates (hard, code-level):
          - reject (``MandateViolation``) when open positions >= MAX_POSITIONS
            and the symbol is not already held;
          - clamp ``notional_usdt`` so the symbol's exposure stays <=
            MAX_SYMBOL_PCT% of current equity (clamp, don't reject; the clamped
            notional is recorded in the ledger note).

        Raises ``PriceUnavailable`` (no state change) when the price fetch fails.
        Returns the persisted ledger entry.
        """
        positions = self.store.load_positions()
        held = self._find(positions, symbol)

        # -- mandate: max open positions (cheap reject before any fetch) ----- #
        if held is None and len(positions) >= self.config.max_positions:
            raise MandateViolation(
                f"max positions ({self.config.max_positions}) reached; "
                f"cannot open new symbol {symbol}"
            )

        # -- mandate: non-positive notional (review C1) ---------------------- #
        # A zero/negative notional (e.g. a mis-sized position_size_pct that the
        # translator turned into 0-or-negative notional) must NEVER mint cash or
        # a negative-qty position. Reject BEFORE the price fetch / account
        # creation so a bad first order leaves no account.json behind either.
        if float(notional_usdt) <= 0:
            raise MandateViolation(
                f"non-positive notional for {symbol}: {notional_usdt}"
            )

        # -- live price (never invent one) ----------------------------------- #
        quote = self.price_fn(symbol)  # raises PriceUnavailable -> no state change
        price = float(quote["price"])
        fill_price = self._buy_fill(price)

        # Account creation is deferred until here: mandate check passed and the
        # price fetch succeeded, so the order WILL execute. A failed/rejected
        # first order therefore never leaves an account.json behind.
        account = self._ensure_account()

        # -- mandate: per-symbol exposure clamp ------------------------------ #
        equity_now = self._equity_value(account, positions, {symbol: fill_price})
        cap = equity_now * self.config.max_symbol_pct / 100.0
        held_value = (held["qty"] * fill_price) if held else 0.0
        allowed = cap - held_value
        if allowed <= 0:
            # Zero headroom is a rejection, not a zero-qty ledger row: the
            # spec's "clamp, don't reject" applies to PARTIAL headroom only.
            raise MandateViolation(
                f"symbol at exposure cap: {symbol} already holds "
                f">= {self.config.max_symbol_pct}% of equity"
            )
        requested = float(notional_usdt)
        notional = min(requested, allowed)
        note: str | None = None
        if notional < requested:
            note = (
                f"clamped notional {requested:.8f} -> {notional:.8f} "
                f"(symbol cap {self.config.max_symbol_pct}% of equity)"
            )

        # -- cash floor: buys never drive cash negative (review I1) ---------- #
        # Clamp notional so notional + fee <= available cash (mirrors the
        # symbol-cap clamp: partial fill + a ledger note). Zero/negative cash is
        # a rejection, not a zero-qty ledger row.
        cash_avail = account["cash"]
        if cash_avail <= 0:
            raise MandateViolation(
                f"no cash available to buy {symbol} (cash={cash_avail:.8f})"
            )
        max_by_cash = cash_avail / (1 + self.config.fee_bps / 10_000)
        if notional > max_by_cash:
            notional = max_by_cash
            note = (note + "; " if note else "") + "clamped to available cash"

        qty = notional / fill_price
        fee = self._fee(qty * fill_price)
        slippage_paid = qty * (fill_price - price)

        # -- mutate cash + positions ----------------------------------------- #
        account["cash"] = account["cash"] - notional - fee
        self._apply_buy(positions, held, symbol, qty, fill_price, stop, take_profit,
                        decision_id, quote["ts"])

        entry = {
            "ts": quote["ts"],
            "trade_id": uuid.uuid4().hex,
            "symbol": symbol,
            "side": "buy",
            "qty": qty,
            "fill_price": fill_price,
            "slippage_paid": slippage_paid,
            "fee_paid": fee,
            "order_type": "market",
            "decision_id": decision_id,
            "realized_pnl": None,
            "note": note,
        }
        # NON-ATOMIC WINDOW: the three files are written by separate atomic
        # renames, so a crash can land between them. Invariant: NEVER PERSIST
        # A STATE RICHER THAN REALITY — which is side-dependent. On a BUY the
        # cash deduction goes first (account -> positions -> ledger): a crash
        # leaves cash gone with no position, never a free position that cash
        # didn't pay for. Full journal-then-apply machinery is out of scope
        # for a paper engine (review Important 2).
        self.store.save_account(account)
        self.store.save_positions(positions)
        self.store.append_ledger(entry)
        return entry

    # -- market sell -------------------------------------------------------- #
    def market_sell(
        self,
        symbol: str,
        fraction: float,
        *,
        decision_id: str,
        reason: str,
    ) -> dict | None:
        """Sell ``fraction`` (0-1) of the held position at the live fill.

        Returns ``None`` when there is no position (the no-op ledger note is the
        translator's responsibility, Task 4). Raises ``PriceUnavailable`` (no
        state change) when the price fetch fails.
        """
        positions = self.store.load_positions()
        held = self._find(positions, symbol)
        if held is None:
            return None

        quote = self.price_fn(symbol)  # raises PriceUnavailable -> no state change
        price = float(quote["price"])
        sell_fill = self._sell_fill(price)
        return self._execute_sell(
            symbol,
            fraction,
            fill_price=sell_fill,
            ref_price=price,
            decision_id=decision_id,
            reason=reason,
            order_type="market",
            ts=quote["ts"],
        )

    # -- shared sell persistence path (market + conditional fills) ---------- #
    def _execute_sell(
        self,
        symbol: str,
        fraction: float,
        *,
        fill_price: float,
        ref_price: float,
        decision_id: str | None,
        reason: str | None,
        order_type: str,
        ts: str,
    ) -> dict | None:
        """Persist a sell of ``fraction`` of the held ``symbol`` at ``fill_price``.

        Shared by ``market_sell`` (fill_price = live price with slippage,
        ``ref_price`` = the raw pre-slippage price) and
        ``evaluate_conditionals`` (fill_price = the triggered stop/TP price,
        ``ref_price == fill_price`` so ``slippage_paid`` is exactly 0 — bar
        prices are already conservative, per globals.md). Fee always applies.

        Returns ``None`` when there is no held position. Same non-atomic-window
        invariant/order as the historical ``market_sell``: on a SELL the
        position reduction persists BEFORE the cash credit (positions ->
        account -> ledger) — a crash leaves a shrunk position without the
        cash credit (conservative), never credited cash plus a still-live
        position (double-count).
        """
        positions = self.store.load_positions()
        held = self._find(positions, symbol)
        if held is None:
            return None

        # A position exists, so an account necessarily exists already; this is
        # a no-op load in practice (account creation stays deferred to the
        # point where an order actually executes).
        account = self._ensure_account()

        qty_sold = held["qty"] * float(fraction)
        avg_entry = held["avg_entry"]
        sell_notional = qty_sold * fill_price
        sell_fee = self._fee(sell_notional)
        realized_pnl = (fill_price - avg_entry) * qty_sold - sell_fee
        slippage_paid = qty_sold * (ref_price - fill_price)

        account["cash"] = account["cash"] + sell_notional - sell_fee
        remaining = held["qty"] - qty_sold
        if remaining <= 1e-12:
            positions.remove(held)
        else:
            held["qty"] = remaining

        entry = {
            "ts": ts,
            "trade_id": uuid.uuid4().hex,
            "symbol": symbol,
            "side": "sell",
            "qty": qty_sold,
            "fill_price": fill_price,
            "slippage_paid": slippage_paid,
            "fee_paid": sell_fee,
            "order_type": order_type,
            "decision_id": decision_id,
            "realized_pnl": realized_pnl,
            "note": reason,
        }
        self.store.save_positions(positions)
        self.store.save_account(account)
        self.store.append_ledger(entry)
        return entry

    # -- conditional orders (stop / take-profit), evaluated on the daily tick #
    def evaluate_conditionals(self, symbol: str, bar: dict) -> list[dict]:
        """Evaluate stop/take-profit triggers for ``symbol`` against one daily
        ``bar`` (``{"open", "high", "low", "close", "ts"}``) and execute fills.

        Binding fill rules (t3 brief / design spec §3.1):
          - stop triggers when ``bar["low"] <= stop``; fills at ``bar["open"]``
            if the bar gapped through (``open <= stop``), else at the stop
            price.
          - each take-profit ``{"price", "fraction"}`` triggers when
            ``bar["high"] >= price``; fills at ``price``, or at ``bar["open"]``
            if the bar gapped above (``open >= price``).
          - stop AND any TP inside the same bar -> stop wins (worse outcome):
            TPs are skipped entirely and the position is closed in full at
            the stop fill.
          - no slippage on conditional fills (bar prices are already
            conservative); fee still applies, via ``_execute_sell``.

        Executed take-profits are removed from the position's
        ``take_profits`` list (untriggered ones are preserved) so a same-day
        re-tick against the same bar cannot refill them. Returns the list of
        executed ledger entries — empty if nothing triggered or the symbol
        isn't held.
        """
        positions = self.store.load_positions()
        held = self._find(positions, symbol)
        if held is None:
            return []

        # Review C2: conditional evaluation begins on the first FULL bar after
        # entry. Bars that ended before entry, and the partially-overlapping
        # entry-day bar, are skipped so pre-entry price action within the entry
        # day can never fire a fictitious stop/TP (run_tick surfaces the note).
        if bar_status_vs_entry(held.get("opened_at"), bar.get("ts")) != "evaluate":
            return []

        open_ = float(bar["open"])
        high = float(bar["high"])
        low = float(bar["low"])
        ts = _fmt_ts(bar.get("ts"))
        decision_id = held.get("decision_id")

        stop = held.get("stop")
        if stop is not None and low <= stop:
            fill_price = open_ if open_ <= stop else stop
            entry = self._execute_sell(
                symbol,
                1.0,
                fill_price=fill_price,
                ref_price=fill_price,
                decision_id=decision_id,
                reason=f"stop triggered @ {stop}",
                order_type="stop",
                ts=ts,
            )
            return [entry] if entry else []

        fills: list[dict] = []
        remaining_tps: list[dict] = []
        triggered = False
        for tp in held.get("take_profits") or []:
            tp_price = float(tp["price"])
            if high >= tp_price:
                triggered = True
                fill_price = open_ if open_ >= tp_price else tp_price
                entry = self._execute_sell(
                    symbol,
                    float(tp["fraction"]),
                    fill_price=fill_price,
                    ref_price=fill_price,
                    decision_id=decision_id,
                    reason=f"take_profit triggered @ {tp_price}",
                    order_type="take_profit",
                    ts=ts,
                )
                if entry:
                    fills.append(entry)
            else:
                remaining_tps.append(tp)

        if triggered:
            positions = self.store.load_positions()
            held = self._find(positions, symbol)
            if held is not None:
                held["take_profits"] = remaining_tps
                self.store.save_positions(positions)

        return fills

    # -- risk management ---------------------------------------------------- #
    def set_risk(
        self, symbol: str, *, stop: float | None, take_profit: float | None
    ) -> bool:
        """Update stop / take-profit on a held position. Returns False if absent."""
        positions = self.store.load_positions()
        held = self._find(positions, symbol)
        if held is None:
            return False
        if stop is not None:
            held["stop"] = stop
        if take_profit is not None:
            held["take_profits"] = [{"price": take_profit, "fraction": 1.0}]
        self.store.save_positions(positions)
        return True

    # -- mark-to-market ----------------------------------------------------- #
    def equity(self, mark_prices: dict[str, float] | None = None) -> dict:
        """Mark the account to market. Returns cash / positions_value / equity /
        per-position unrealized. Missing marks fall back to ``price_fn`` and, if
        that fails, to ``avg_entry`` (unrealized 0) so this never raises — but
        the fallback is never silent: such rows carry ``stale: True`` and the
        top-level dict reports ``stale_positions`` (review Important 1)."""
        account = self._ensure_account()
        positions = self.store.load_positions()
        marks = dict(mark_prices or {})

        rows: list[dict] = []
        positions_value = 0.0
        stale_positions = 0
        for pos in positions:
            mark, stale = self._mark_for(pos, marks)
            if stale:
                stale_positions += 1
            value = pos["qty"] * mark
            positions_value += value
            rows.append(
                {
                    "symbol": pos["symbol"],
                    "qty": pos["qty"],
                    "avg_entry": pos["avg_entry"],
                    "mark": mark,
                    "value": value,
                    "unrealized": (mark - pos["avg_entry"]) * pos["qty"],
                    "stale": stale,
                }
            )

        cash = account["cash"]
        return {
            "ts": _utc_now_iso(),
            "cash": cash,
            "positions_value": positions_value,
            "equity": cash + positions_value,
            "positions": rows,
            "stale_positions": stale_positions,
        }

    # -- helpers ------------------------------------------------------------ #
    @staticmethod
    def _find(positions: list[dict], symbol: str) -> dict | None:
        for pos in positions:
            if pos["symbol"] == symbol:
                return pos
        return None

    def _mark_for(self, pos: dict, marks: dict[str, float]) -> tuple[float, bool]:
        """Resolve the mark for a position; returns ``(mark, stale)``.

        ``stale`` is True only when neither an explicit mark nor a live price
        was available and the position is valued at cost basis (avg_entry)."""
        symbol = pos["symbol"]
        if symbol in marks:
            return float(marks[symbol]), False
        try:
            return float(self.price_fn(symbol)["price"]), False
        except PriceUnavailable:
            logger.warning(
                "paper equity: no mark for %s; valuing at avg_entry (stale)", symbol
            )
            return float(pos["avg_entry"]), True

    def _equity_value(
        self, account: dict, positions: list[dict], marks: dict[str, float]
    ) -> float:
        """Equity for mandate sizing. Traded symbol uses the passed fill mark;
        other positions are valued at cost basis (avg_entry) to avoid extra
        network fetches during a buy."""
        value = account["cash"]
        for pos in positions:
            mark = marks.get(pos["symbol"], pos["avg_entry"])
            value += pos["qty"] * mark
        return value

    def _apply_buy(
        self,
        positions: list[dict],
        held: dict | None,
        symbol: str,
        qty: float,
        fill_price: float,
        stop: float | None,
        take_profit: float | None,
        decision_id: str,
        ts: str,
    ) -> None:
        take_profits = (
            [{"price": take_profit, "fraction": 1.0}] if take_profit is not None else []
        )
        if held is None:
            positions.append(
                {
                    "symbol": symbol,
                    "qty": qty,
                    "avg_entry": fill_price,
                    "stop": stop,
                    "take_profits": take_profits,
                    "opened_at": ts,
                    "decision_id": decision_id,
                }
            )
            return
        new_qty = held["qty"] + qty
        held["avg_entry"] = (held["qty"] * held["avg_entry"] + qty * fill_price) / new_qty
        held["qty"] = new_qty
        if stop is not None:
            held["stop"] = stop
        if take_profit is not None:
            held["take_profits"] = take_profits
