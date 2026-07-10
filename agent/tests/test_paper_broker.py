"""Tests for the paper store + broker core (Task 2).

Socket-disabled: every test injects a fixture ``price_fn`` (or monkeypatches
the snapshot module's ``_fetch_row``) so no test ever touches the network.
Money math is asserted to 8 decimal places via ``pytest.approx`` with absolute
tolerances, mirroring the journal's alpha-math precision conventions.

Fill/fee/realized-pnl formulas under test (binding, from globals.md):
  buy fill  = price * (1 + slippage_bps/10000)
  sell fill = price * (1 - slippage_bps/10000)
  fee       = fill_notional * fee_bps/10000     (deducted from cash both sides)
  realized_pnl = (sell_fill - avg_entry) * qty_sold - sell_fee
"""

from __future__ import annotations

import json
import os

import pytest

from src.paper import broker as broker_mod
from src.paper.broker import BrokerConfig, MandateViolation, PaperBroker, PriceUnavailable
from src.paper.store import PaperStore, paper_root

ABS = 1e-8


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #
def _set_default_env(monkeypatch, tmp_path, **overrides):
    env = {
        "VIBE_PAPER_ENABLED": "1",
        "VIBE_PAPER_START_CASH": "100000",
        "VIBE_PAPER_SLIPPAGE_BPS": "5",
        "VIBE_PAPER_FEE_BPS": "10",
        "VIBE_PAPER_MAX_POSITIONS": "3",
        "VIBE_PAPER_MAX_SYMBOL_PCT": "25",
        "VIBE_PAPER_DEFAULT_SIZE_PCT": "10",
        "VIBE_PAPER_DEFAULT_STOP_PCT": "8",
        "VIBE_PAPER_ROOT": str(tmp_path),
    }
    env.update(overrides)
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))


class FakePriceFn:
    """Deterministic price_fn: returns a fixed price for any symbol.

    ``.raises`` forces a PriceUnavailable to exercise the fetch-failure path.
    """

    def __init__(self, price: float = 100.0, ts: str = "2026-07-11T00:00:00Z"):
        self.price = price
        self.ts = ts
        self.raises = False
        self.prices: dict[str, float] = {}

    def __call__(self, symbol: str) -> dict:
        if self.raises:
            raise PriceUnavailable(f"fixture refused price for {symbol}")
        price = self.prices.get(symbol, self.price)
        return {"price": float(price), "ts": self.ts}


@pytest.fixture
def price_fn():
    return FakePriceFn(price=100.0)


@pytest.fixture
def broker(monkeypatch, tmp_path, price_fn):
    _set_default_env(monkeypatch, tmp_path)
    store = PaperStore(tmp_path)
    return PaperBroker(store, price_fn=price_fn)


# --------------------------------------------------------------------------- #
# Exact fill math                                                             #
# --------------------------------------------------------------------------- #
def test_market_buy_exact_fill_math(broker):
    entry = broker.market_buy(
        "BTC-USDT", 10_000.0, decision_id="d1", stop=None, take_profit=None
    )

    # fill = 100 * (1 + 5/10000) = 100.05
    expected_fill = 100.0 * (1 + 5 / 10_000)
    expected_qty = 10_000.0 / expected_fill  # 99.95002498750625
    expected_fee = (expected_qty * expected_fill) * 10 / 10_000  # 10.0 on 10k notional
    expected_slip = expected_qty * (expected_fill - 100.0)

    assert entry["side"] == "buy"
    assert entry["order_type"] == "market"
    assert entry["decision_id"] == "d1"
    assert entry["realized_pnl"] is None
    assert entry["fill_price"] == pytest.approx(100.05, abs=ABS)
    assert entry["qty"] == pytest.approx(expected_qty, abs=ABS)
    assert entry["fee_paid"] == pytest.approx(expected_fee, abs=ABS)
    assert entry["slippage_paid"] == pytest.approx(expected_slip, abs=ABS)

    # cash reduced by notional + buy fee; avg_entry recorded at the fill price
    account = broker.store.load_account()
    assert account["cash"] == pytest.approx(100_000.0 - 10_000.0 - expected_fee, abs=ABS)
    positions = broker.store.load_positions()
    assert len(positions) == 1
    pos = positions[0]
    assert pos["symbol"] == "BTC-USDT"
    assert pos["qty"] == pytest.approx(expected_qty, abs=ABS)
    assert pos["avg_entry"] == pytest.approx(expected_fill, abs=ABS)
    assert pos["decision_id"] == "d1"

    # one ledger row persisted
    rows = list(broker.store.iter_ledger())
    assert len(rows) == 1
    assert rows[0]["trade_id"] == entry["trade_id"]


def test_position_shape_carries_stops_and_take_profits(broker):
    broker.market_buy(
        "BTC-USDT", 10_000.0, decision_id="d1", stop=92.0, take_profit=130.0
    )
    pos = broker.store.load_positions()[0]
    # binding shape for Tasks 3-7
    assert set(pos) == {
        "symbol",
        "qty",
        "avg_entry",
        "stop",
        "take_profits",
        "opened_at",
        "decision_id",
    }
    assert pos["stop"] == pytest.approx(92.0, abs=ABS)
    assert pos["take_profits"] == [{"price": 130.0, "fraction": 1.0}]


# --------------------------------------------------------------------------- #
# Sell realizes PnL net of both-side costs                                    #
# --------------------------------------------------------------------------- #
def test_market_sell_realizes_pnl_net_of_both_costs(broker, price_fn):
    broker.market_buy("BTC-USDT", 10_000.0, decision_id="d1", stop=None, take_profit=None)

    buy_fill = 100.0 * (1 + 5 / 10_000)  # 100.05 == avg_entry
    qty = 10_000.0 / buy_fill
    buy_fee = (qty * buy_fill) * 10 / 10_000

    price_fn.price = 110.0
    entry = broker.market_sell("BTC-USDT", 1.0, decision_id="d2", reason="take gains")

    sell_fill = 110.0 * (1 - 5 / 10_000)  # 109.945
    sell_notional = qty * sell_fill
    sell_fee = sell_notional * 10 / 10_000
    expected_pnl = (sell_fill - buy_fill) * qty - sell_fee

    assert entry["side"] == "sell"
    assert entry["order_type"] == "market"
    assert entry["note"] == "take gains"
    assert entry["fill_price"] == pytest.approx(sell_fill, abs=ABS)
    assert entry["qty"] == pytest.approx(qty, abs=ABS)
    assert entry["fee_paid"] == pytest.approx(sell_fee, abs=ABS)
    assert entry["realized_pnl"] == pytest.approx(expected_pnl, abs=ABS)

    # full close removes the position
    assert broker.store.load_positions() == []

    # account P&L = realized_pnl - buy_fee (buy fee already hit cash at entry)
    account = broker.store.load_account()
    assert account["cash"] == pytest.approx(
        100_000.0 + expected_pnl - buy_fee, abs=ABS
    )


def test_market_sell_no_position_returns_none(broker):
    assert broker.market_sell("ETH-USDT", 1.0, decision_id="d9", reason="sell") is None
    assert list(broker.store.iter_ledger()) == []


def test_partial_sell_keeps_reduced_position(broker, price_fn):
    broker.market_buy("BTC-USDT", 10_000.0, decision_id="d1", stop=None, take_profit=None)
    qty = 10_000.0 / (100.0 * (1 + 5 / 10_000))
    price_fn.price = 120.0
    broker.market_sell("BTC-USDT", 0.5, decision_id="d2", reason="trim")
    pos = broker.store.load_positions()[0]
    assert pos["qty"] == pytest.approx(qty * 0.5, abs=ABS)


# --------------------------------------------------------------------------- #
# Mandates                                                                    #
# --------------------------------------------------------------------------- #
def test_fourth_new_symbol_rejected(broker):
    for i, sym in enumerate(["AAA-USDT", "BBB-USDT", "CCC-USDT"]):
        broker.market_buy(sym, 10_000.0, decision_id=f"d{i}", stop=None, take_profit=None)
    with pytest.raises(MandateViolation):
        broker.market_buy("DDD-USDT", 10_000.0, decision_id="d4", stop=None, take_profit=None)
    # rejection leaves state untouched: still 3 positions, 3 ledger rows
    assert len(broker.store.load_positions()) == 3
    assert len(list(broker.store.iter_ledger())) == 3


def test_adding_to_held_symbol_allowed_at_max_positions(broker):
    for i, sym in enumerate(["AAA-USDT", "BBB-USDT", "CCC-USDT"]):
        broker.market_buy(sym, 5_000.0, decision_id=f"d{i}", stop=None, take_profit=None)
    # already-held symbol may be added to even at MAX_POSITIONS
    entry = broker.market_buy(
        "AAA-USDT", 5_000.0, decision_id="d3b", stop=None, take_profit=None
    )
    assert entry["side"] == "buy"
    assert len(broker.store.load_positions()) == 3


def test_oversize_notional_clamped_to_symbol_cap(broker):
    # fresh account: equity == 100_000; cap = 25% => 25_000 max symbol exposure
    # PARTIAL headroom: clamp, don't reject — the allowed amount still fills.
    entry = broker.market_buy(
        "BTC-USDT", 50_000.0, decision_id="d1", stop=None, take_profit=None
    )
    fill = 100.0 * (1 + 5 / 10_000)
    expected_qty = 25_000.0 / fill
    assert entry["qty"] == pytest.approx(expected_qty, abs=ABS)
    assert entry["note"] is not None and "clamp" in entry["note"].lower()
    # spent 25_000 notional, not 50_000
    account = broker.store.load_account()
    expected_fee = 25_000.0 * 10 / 10_000
    assert account["cash"] == pytest.approx(100_000.0 - 25_000.0 - expected_fee, abs=ABS)


def test_zero_headroom_at_exposure_cap_raises(broker):
    """Review Minor 2: zero headroom is a rejection, not a zero-qty ledger row."""
    broker.market_buy("BTC-USDT", 25_000.0, decision_id="d1", stop=None, take_profit=None)
    with pytest.raises(MandateViolation, match="exposure cap"):
        broker.market_buy("BTC-USDT", 1_000.0, decision_id="d2", stop=None, take_profit=None)
    # no zero-qty row appended; position untouched
    rows = list(broker.store.iter_ledger())
    assert len(rows) == 1
    assert all(r["qty"] > 0 for r in rows)
    assert len(broker.store.load_positions()) == 1


# --------------------------------------------------------------------------- #
# Write ordering: cash persisted first (review Important 2)                    #
# --------------------------------------------------------------------------- #
def _record_store_calls(store, monkeypatch):
    calls: list[str] = []
    for name in ("save_account", "save_positions", "append_ledger"):
        orig = getattr(store, name)

        def wrapper(arg, _name=name, _orig=orig):
            calls.append(_name)
            return _orig(arg)

        monkeypatch.setattr(store, name, wrapper)
    return calls


def test_market_buy_persists_cash_before_positions_before_ledger(
    broker, monkeypatch
):
    calls = _record_store_calls(broker.store, monkeypatch)
    broker.market_buy("BTC-USDT", 10_000.0, decision_id="d1", stop=None, take_profit=None)
    assert calls == ["save_account", "save_positions", "append_ledger"]


def test_market_sell_persists_positions_before_cash_before_ledger(
    broker, monkeypatch, price_fn
):
    """Sell-side conservative order: position reduction persisted BEFORE the
    cash credit — a crash leaves a shrunk position without credited cash,
    never credited cash plus a still-live position (double-count)."""
    broker.market_buy("BTC-USDT", 10_000.0, decision_id="d1", stop=None, take_profit=None)
    calls = _record_store_calls(broker.store, monkeypatch)
    price_fn.price = 110.0
    broker.market_sell("BTC-USDT", 1.0, decision_id="d2", reason="close")
    assert calls == ["save_positions", "save_account", "append_ledger"]


# --------------------------------------------------------------------------- #
# Price unavailable: no fill, no state change                                 #
# --------------------------------------------------------------------------- #
def test_price_unavailable_no_ledger_no_position_change(broker, price_fn):
    price_fn.raises = True
    with pytest.raises(PriceUnavailable):
        broker.market_buy("BTC-USDT", 10_000.0, decision_id="d1", stop=None, take_profit=None)
    assert broker.store.load_positions() == []
    assert list(broker.store.iter_ledger()) == []
    # Review Minor 1: failed FIRST order must not leave an account.json behind
    assert broker.store.load_account() is None


def test_rejected_first_order_creates_no_account(monkeypatch, tmp_path, price_fn):
    """Review Minor 1: account creation is deferred until an order will execute."""
    _set_default_env(monkeypatch, tmp_path, VIBE_PAPER_MAX_POSITIONS="0")
    store = PaperStore(tmp_path)
    b = PaperBroker(store, price_fn=price_fn)
    with pytest.raises(MandateViolation):
        b.market_buy("BTC-USDT", 1_000.0, decision_id="d1", stop=None, take_profit=None)
    assert store.load_account() is None


def test_price_unavailable_on_sell_leaves_position(broker, price_fn):
    broker.market_buy("BTC-USDT", 10_000.0, decision_id="d1", stop=None, take_profit=None)
    price_fn.raises = True
    with pytest.raises(PriceUnavailable):
        broker.market_sell("BTC-USDT", 1.0, decision_id="d2", reason="sell")
    assert len(broker.store.load_positions()) == 1
    assert len(list(broker.store.iter_ledger())) == 1  # only the buy


# --------------------------------------------------------------------------- #
# Account auto-create                                                         #
# --------------------------------------------------------------------------- #
def test_account_auto_create_with_start_cash(monkeypatch, tmp_path, price_fn):
    _set_default_env(monkeypatch, tmp_path, VIBE_PAPER_START_CASH="50000")
    store = PaperStore(tmp_path)
    assert store.load_account() is None
    b = PaperBroker(store, price_fn=price_fn)
    # any operation lazily creates the account
    eq = b.equity(mark_prices={})
    assert eq["cash"] == pytest.approx(50_000.0, abs=ABS)
    account = store.load_account()
    assert account is not None
    assert account["cash"] == pytest.approx(50_000.0, abs=ABS)
    assert account["config"]["start_cash"] == pytest.approx(50_000.0, abs=ABS)


# --------------------------------------------------------------------------- #
# set_risk / equity                                                           #
# --------------------------------------------------------------------------- #
def test_set_risk_updates_position(broker):
    broker.market_buy("BTC-USDT", 10_000.0, decision_id="d1", stop=None, take_profit=None)
    assert broker.set_risk("BTC-USDT", stop=90.0, take_profit=140.0) is True
    pos = broker.store.load_positions()[0]
    assert pos["stop"] == pytest.approx(90.0, abs=ABS)
    assert pos["take_profits"] == [{"price": 140.0, "fraction": 1.0}]


def test_set_risk_missing_symbol_returns_false(broker):
    assert broker.set_risk("ETH-USDT", stop=90.0, take_profit=None) is False


def test_equity_reports_unrealized(broker):
    broker.market_buy("BTC-USDT", 10_000.0, decision_id="d1", stop=None, take_profit=None)
    fill = 100.0 * (1 + 5 / 10_000)
    qty = 10_000.0 / fill
    fee = 10_000.0 * 10 / 10_000
    eq = broker.equity(mark_prices={"BTC-USDT": 120.0})
    assert eq["cash"] == pytest.approx(100_000.0 - 10_000.0 - fee, abs=ABS)
    assert eq["positions_value"] == pytest.approx(qty * 120.0, abs=ABS)
    assert eq["equity"] == pytest.approx(eq["cash"] + eq["positions_value"], abs=ABS)
    posrow = eq["positions"][0]
    assert posrow["unrealized"] == pytest.approx((120.0 - fill) * qty, abs=ABS)
    # explicit-mark row is NOT stale (review Important 1)
    assert posrow["stale"] is False
    assert eq["stale_positions"] == 0


def test_equity_live_price_row_not_stale(broker, price_fn):
    broker.market_buy("BTC-USDT", 10_000.0, decision_id="d1", stop=None, take_profit=None)
    price_fn.price = 115.0
    eq = broker.equity()  # no explicit mark: falls through to price_fn (live)
    posrow = eq["positions"][0]
    assert posrow["mark"] == pytest.approx(115.0, abs=ABS)
    assert posrow["stale"] is False
    assert eq["stale_positions"] == 0


def test_equity_cost_basis_fallback_flagged_stale(broker, price_fn):
    """Review Important 1: when no mark is available and price_fn fails, the
    avg_entry fallback must be visibly flagged, not silent."""
    broker.market_buy("BTC-USDT", 10_000.0, decision_id="d1", stop=None, take_profit=None)
    fill = 100.0 * (1 + 5 / 10_000)
    price_fn.raises = True
    eq = broker.equity(mark_prices={})  # no-raise semantics preserved
    posrow = eq["positions"][0]
    assert posrow["mark"] == pytest.approx(fill, abs=ABS)  # valued at avg_entry
    assert posrow["unrealized"] == pytest.approx(0.0, abs=ABS)
    assert posrow["stale"] is True
    assert eq["stale_positions"] == 1


# --------------------------------------------------------------------------- #
# Atomicity: interrupted write leaves old state intact                        #
# --------------------------------------------------------------------------- #
def test_atomic_write_interrupted_keeps_old_state(monkeypatch, tmp_path):
    store = PaperStore(tmp_path)
    store.save_positions([{"symbol": "BTC-USDT", "qty": 1.0, "avg_entry": 100.0,
                           "stop": None, "take_profits": [], "opened_at": "t",
                           "decision_id": "d0"}])
    original = store.load_positions()

    def boom(src, dst):
        raise OSError("simulated crash during rename")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        store.save_positions([{"symbol": "ETH-USDT", "qty": 2.0, "avg_entry": 50.0,
                               "stop": None, "take_profits": [], "opened_at": "t",
                               "decision_id": "d1"}])
    monkeypatch.undo()
    # old state survived; no stray tmp file left behind
    assert store.load_positions() == original
    tmp_files = list(tmp_path.glob("*.tmp"))
    assert tmp_files == []


def test_append_ledger_atomic_interrupt_keeps_old_rows(monkeypatch, tmp_path):
    store = PaperStore(tmp_path)
    store.append_ledger({"trade_id": "t1", "side": "buy"})

    def boom(src, dst):
        raise OSError("crash")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        store.append_ledger({"trade_id": "t2", "side": "sell"})
    monkeypatch.undo()
    rows = list(store.iter_ledger())
    assert [r["trade_id"] for r in rows] == ["t1"]


# --------------------------------------------------------------------------- #
# Store: root resolution + archive_and_reset                                  #
# --------------------------------------------------------------------------- #
def test_paper_root_honors_env(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBE_PAPER_ROOT", str(tmp_path / "custom"))
    assert paper_root() == tmp_path / "custom"


def test_paper_root_default(monkeypatch):
    monkeypatch.delenv("VIBE_PAPER_ROOT", raising=False)
    root = paper_root()
    assert root.name == "paper"
    assert root.parent.name == ".vibe-trading"


def test_archive_and_reset_moves_state(broker):
    broker.market_buy("BTC-USDT", 10_000.0, decision_id="d1", stop=None, take_profit=None)
    archive = broker.store.archive_and_reset()
    assert archive.exists()
    assert (archive / "ledger.jsonl").exists()
    # live state cleared
    assert broker.store.load_account() is None
    assert broker.store.load_positions() == []
    assert list(broker.store.iter_ledger()) == []


# --------------------------------------------------------------------------- #
# BrokerConfig.from_env                                                        #
# --------------------------------------------------------------------------- #
def test_broker_config_from_env(monkeypatch, tmp_path):
    _set_default_env(monkeypatch, tmp_path, VIBE_PAPER_FEE_BPS="20",
                     VIBE_PAPER_MAX_POSITIONS="5")
    cfg = BrokerConfig.from_env()
    assert cfg.fee_bps == pytest.approx(20.0, abs=ABS)
    assert cfg.slippage_bps == pytest.approx(5.0, abs=ABS)
    assert cfg.max_positions == 5
    assert cfg.max_symbol_pct == pytest.approx(25.0, abs=ABS)
    assert cfg.start_cash == pytest.approx(100_000.0, abs=ABS)
    assert cfg.default_size_pct == pytest.approx(10.0, abs=ABS)
    assert cfg.default_stop_pct == pytest.approx(8.0, abs=ABS)
    assert cfg.enabled is True


# --------------------------------------------------------------------------- #
# Default price_fn reuses the snapshot fetch path (no network)                #
# --------------------------------------------------------------------------- #
def test_default_price_fn_translates_snapshot(monkeypatch):
    from src.tools import crypto_snapshot_tool as snap

    def fake_fetch_row(**kwargs):
        return {"last": "100.5", "ts": "1700000000000"}, None

    monkeypatch.setattr(snap, "_fetch_row", fake_fetch_row)
    quote = broker_mod.default_price_fn("BTC-USDT")
    assert quote["price"] == pytest.approx(100.5, abs=ABS)
    assert isinstance(quote["ts"], str) and quote["ts"]


def test_default_price_fn_raises_on_no_data(monkeypatch):
    from src.tools import crypto_snapshot_tool as snap

    def fake_fetch_row(**kwargs):
        return None, "OKX down"

    monkeypatch.setattr(snap, "_fetch_row", fake_fetch_row)
    with pytest.raises(PriceUnavailable):
        broker_mod.default_price_fn("BTC-USDT")
