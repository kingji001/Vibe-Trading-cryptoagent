"""Tests for decision-level PnL aggregation (Task 6).

Ledger/equity rows are constructed directly as fixtures (bypassing the
broker) so every test pins exact money math and exit-kind attribution
without touching the network. Money math asserted via ``pytest.approx``.
"""

from __future__ import annotations

import pytest

from src.paper.pnl import decision_pnl
from src.paper.store import PaperStore

ABS = 1e-8


@pytest.fixture
def store(tmp_path):
    return PaperStore(tmp_path)


def _ledger_row(**overrides) -> dict:
    row = {
        "ts": "2026-07-01T00:00:00Z",
        "trade_id": "t-default",
        "symbol": "BTC-USDT",
        "side": "buy",
        "qty": 10.0,
        "fill_price": 100.0,
        "slippage_paid": 0.0,
        "fee_paid": 1.0,
        "order_type": "market",
        "decision_id": "dec1",
        "realized_pnl": None,
        "note": None,
    }
    row.update(overrides)
    return row


def _noop_row(**overrides) -> dict:
    row = {
        "ts": "2026-07-01T00:00:00Z",
        "trade_id": "t-noop",
        "symbol": "BTC-USDT",
        "side": None,
        "qty": 0.0,
        "fill_price": None,
        "slippage_paid": 0.0,
        "fee_paid": 0.0,
        "order_type": "noop",
        "decision_id": "dec1",
        "realized_pnl": None,
        "note": "sell signal with no position",
    }
    row.update(overrides)
    return row


def _equity_row(*, ts: str, symbol: str, avg_entry: float, mark: float) -> dict:
    return {
        "ts": ts,
        "date": ts[:10],
        "cash": 0.0,
        "positions_value": 0.0,
        "equity": 0.0,
        "positions": [
            {
                "symbol": symbol,
                "qty": 10.0,
                "avg_entry": avg_entry,
                "mark": mark,
                "value": mark * 10.0,
                "unrealized": (mark - avg_entry) * 10.0,
                "stale": False,
            }
        ],
        "stale_positions": 0,
        "already_recorded": False,
    }


# --------------------------------------------------------------------------- #
# never executed                                                               #
# --------------------------------------------------------------------------- #
def test_no_ledger_rows_at_all_not_executed(store):
    result = decision_pnl("dec_missing", store=store)
    assert result["decision_id"] == "dec_missing"
    assert result["executed"] is False
    assert result["realized_pnl"] is None
    assert result["fees_paid"] == 0.0
    assert result["unrealized_pnl"] is None
    assert result["position_open"] is False
    assert result["exit_kind"] == "not_executed"
    assert result["max_drawdown_pct"] is None
    assert "not executed" in result["summary"]
    assert result["summary"].count("\n") <= 4


def test_only_noop_rows_not_executed_note_surfaces_in_summary(store):
    store.append_ledger(_noop_row(note="sell signal with no position"))
    result = decision_pnl("dec1", store=store)
    assert result["executed"] is False
    assert result["exit_kind"] == "not_executed"
    assert result["position_open"] is False
    assert "sell signal with no position" in result["summary"]


def test_only_retriable_noop_not_executed_reason_in_summary(store):
    store.append_ledger(_noop_row(note="price unavailable — not executed"))
    result = decision_pnl("dec1", store=store)
    assert result["executed"] is False
    assert "price unavailable" in result["summary"]


# --------------------------------------------------------------------------- #
# stopped                                                                       #
# --------------------------------------------------------------------------- #
def test_buy_then_stop_full_close_exit_kind_stopped(store):
    store.append_ledger(
        _ledger_row(trade_id="t1", ts="2026-07-01T00:00:00Z", fee_paid=1.0)
    )
    store.append_ledger(
        _ledger_row(
            trade_id="t2",
            ts="2026-07-02T00:00:00Z",
            side="sell",
            qty=10.0,
            fill_price=90.0,
            fee_paid=0.9,
            order_type="stop",
            realized_pnl=(90.0 - 100.0) * 10.0 - 0.9,
            note="stop triggered @ 90.0",
        )
    )

    result = decision_pnl("dec1", store=store)
    assert result["executed"] is True
    assert result["exit_kind"] == "stopped"
    assert result["position_open"] is False
    assert result["realized_pnl"] == pytest.approx(-100.9, abs=ABS)
    assert result["fees_paid"] == pytest.approx(1.9, abs=ABS)
    assert result["unrealized_pnl"] is None
    assert result["max_drawdown_pct"] is None  # no equity snapshots seeded
    assert "stopped" in result["summary"]


# --------------------------------------------------------------------------- #
# took_profit (multiple TP fills exhausting qty)                               #
# --------------------------------------------------------------------------- #
def test_buy_then_two_take_profits_exhaust_qty_exit_kind_took_profit(store):
    store.append_ledger(_ledger_row(trade_id="t1", ts="2026-07-01T00:00:00Z", fee_paid=1.0))
    store.append_ledger(
        _ledger_row(
            trade_id="t2",
            ts="2026-07-02T00:00:00Z",
            side="sell",
            qty=5.0,
            fill_price=120.0,
            fee_paid=0.6,
            order_type="take_profit",
            realized_pnl=(120.0 - 100.0) * 5.0 - 0.6,
            note="take_profit triggered @ 120.0",
        )
    )
    store.append_ledger(
        _ledger_row(
            trade_id="t3",
            ts="2026-07-03T00:00:00Z",
            side="sell",
            qty=5.0,
            fill_price=130.0,
            fee_paid=0.65,
            order_type="take_profit",
            realized_pnl=(130.0 - 100.0) * 5.0 - 0.65,
            note="take_profit triggered @ 130.0",
        )
    )

    result = decision_pnl("dec1", store=store)
    assert result["executed"] is True
    assert result["exit_kind"] == "took_profit"
    assert result["position_open"] is False
    assert result["realized_pnl"] == pytest.approx(99.4 + 149.35, abs=ABS)
    assert result["fees_paid"] == pytest.approx(1.0 + 0.6 + 0.65, abs=ABS)


# --------------------------------------------------------------------------- #
# closed_by_sell -- same decision, and a LATER, DIFFERENT decision             #
# --------------------------------------------------------------------------- #
def test_buy_then_market_sell_same_decision_closed_by_sell(store):
    store.append_ledger(_ledger_row(trade_id="t1", ts="2026-07-01T00:00:00Z", fee_paid=1.0))
    store.append_ledger(
        _ledger_row(
            trade_id="t2",
            ts="2026-07-02T00:00:00Z",
            side="sell",
            qty=10.0,
            fill_price=110.0,
            fee_paid=1.1,
            order_type="market",
            decision_id="dec1",
            realized_pnl=(110.0 - 100.0) * 10.0 - 1.1,
            note="rating signal",
        )
    )

    result = decision_pnl("dec1", store=store)
    assert result["exit_kind"] == "closed_by_sell"
    assert result["position_open"] is False
    assert result["realized_pnl"] == pytest.approx(98.9, abs=ABS)


def test_buy_closed_by_later_different_decision_sell_attributed_to_opener(store):
    """A separate, later Sell decision (different decision_id) closes the
    position -- exit_kind and the realized PnL of that close must still be
    attributed back to the OPENING decision's PnL report (t6 coordination
    note 2)."""
    store.append_ledger(_ledger_row(trade_id="t1", ts="2026-07-01T00:00:00Z", fee_paid=1.0))
    store.append_ledger(
        _ledger_row(
            trade_id="t2",
            ts="2026-07-02T00:00:00Z",
            side="sell",
            qty=10.0,
            fill_price=110.0,
            fee_paid=1.1,
            order_type="market",
            decision_id="dec2",  # a different, later decision
            realized_pnl=(110.0 - 100.0) * 10.0 - 1.1,
            note="rating signal",
        )
    )

    opener = decision_pnl("dec1", store=store)
    assert opener["exit_kind"] == "closed_by_sell"
    assert opener["position_open"] is False
    assert opener["realized_pnl"] == pytest.approx(98.9, abs=ABS)
    assert opener["fees_paid"] == pytest.approx(2.1, abs=ABS)

    closer = decision_pnl("dec2", store=store)
    assert closer["executed"] is True
    assert closer["exit_kind"] == "closed_by_sell"
    assert closer["realized_pnl"] == pytest.approx(98.9, abs=ABS)


# --------------------------------------------------------------------------- #
# still open                                                                    #
# --------------------------------------------------------------------------- #
def test_position_still_open_unrealized_from_mark_price_fn(store):
    store.append_ledger(_ledger_row(trade_id="t1", ts="2026-07-01T00:00:00Z", fee_paid=1.0))
    store.save_positions(
        [
            {
                "symbol": "BTC-USDT",
                "qty": 10.0,
                "avg_entry": 100.0,
                "stop": 92.0,
                "take_profits": [],
                "opened_at": "2026-07-01T00:00:00Z",
                "decision_id": "dec1",
            }
        ]
    )

    result = decision_pnl("dec1", store=store, mark_price_fn=lambda symbol: 120.0)
    assert result["executed"] is True
    assert result["exit_kind"] == "open"
    assert result["position_open"] is True
    assert result["realized_pnl"] == pytest.approx(0.0, abs=ABS)
    assert result["fees_paid"] == pytest.approx(1.0, abs=ABS)
    assert result["unrealized_pnl"] == pytest.approx(200.0, abs=ABS)


def test_position_still_open_mark_unavailable_unrealized_none_never_invented(store):
    store.append_ledger(_ledger_row(trade_id="t1", ts="2026-07-01T00:00:00Z", fee_paid=1.0))
    store.save_positions(
        [
            {
                "symbol": "BTC-USDT",
                "qty": 10.0,
                "avg_entry": 100.0,
                "stop": 92.0,
                "take_profits": [],
                "opened_at": "2026-07-01T00:00:00Z",
                "decision_id": "dec1",
            }
        ]
    )

    def _boom(symbol):
        raise RuntimeError("no price")

    result = decision_pnl("dec1", store=store, mark_price_fn=_boom)
    assert result["position_open"] is True
    assert result["unrealized_pnl"] is None
    assert "unavailable" in result["summary"]


def test_older_generation_of_reopened_symbol_is_not_open(store):
    """The symbol closed under dec1, then reopened later under dec2. dec1's
    own lineage must report closed/not-open even though the SYMBOL currently
    has a live position (owned by dec2's lineage)."""
    store.append_ledger(_ledger_row(trade_id="t1", ts="2026-07-01T00:00:00Z", fee_paid=1.0))
    store.append_ledger(
        _ledger_row(
            trade_id="t2",
            ts="2026-07-02T00:00:00Z",
            side="sell",
            qty=10.0,
            fill_price=90.0,
            fee_paid=0.9,
            order_type="stop",
            realized_pnl=(90.0 - 100.0) * 10.0 - 0.9,
        )
    )
    store.append_ledger(
        _ledger_row(
            trade_id="t3",
            ts="2026-07-05T00:00:00Z",
            decision_id="dec2",
            fill_price=95.0,
            fee_paid=0.95,
        )
    )
    store.save_positions(
        [
            {
                "symbol": "BTC-USDT",
                "qty": 10.0,
                "avg_entry": 95.0,
                "stop": None,
                "take_profits": [],
                "opened_at": "2026-07-05T00:00:00Z",
                "decision_id": "dec2",
            }
        ]
    )

    dec1_result = decision_pnl("dec1", store=store)
    assert dec1_result["position_open"] is False
    assert dec1_result["exit_kind"] == "stopped"

    dec2_result = decision_pnl("dec2", store=store, mark_price_fn=lambda s: 100.0)
    assert dec2_result["position_open"] is True
    assert dec2_result["exit_kind"] == "open"


# --------------------------------------------------------------------------- #
# max_drawdown_pct                                                             #
# --------------------------------------------------------------------------- #
def test_max_drawdown_pct_worst_dip_while_open(store):
    store.append_ledger(_ledger_row(trade_id="t1", ts="2026-07-01T00:00:00Z", fee_paid=1.0))
    store.save_positions(
        [
            {
                "symbol": "BTC-USDT",
                "qty": 10.0,
                "avg_entry": 100.0,
                "stop": None,
                "take_profits": [],
                "opened_at": "2026-07-01T00:00:00Z",
                "decision_id": "dec1",
            }
        ]
    )
    # before the position opened -- must be excluded
    store.append_equity(
        _equity_row(ts="2026-06-30T00:00:00Z", symbol="BTC-USDT", avg_entry=100.0, mark=50.0)
    )
    store.append_equity(
        _equity_row(ts="2026-07-01T12:00:00Z", symbol="BTC-USDT", avg_entry=100.0, mark=95.0)
    )
    store.append_equity(
        _equity_row(ts="2026-07-02T12:00:00Z", symbol="BTC-USDT", avg_entry=100.0, mark=80.0)
    )
    store.append_equity(
        _equity_row(ts="2026-07-03T12:00:00Z", symbol="BTC-USDT", avg_entry=100.0, mark=110.0)
    )
    # different symbol -- must be excluded
    store.append_equity(
        _equity_row(ts="2026-07-02T13:00:00Z", symbol="ETH-USDT", avg_entry=100.0, mark=1.0)
    )

    result = decision_pnl("dec1", store=store, mark_price_fn=lambda s: 110.0)
    assert result["max_drawdown_pct"] == pytest.approx(-20.0, abs=ABS)


def test_max_drawdown_pct_never_underwater_clamped_to_zero(store):
    store.append_ledger(_ledger_row(trade_id="t1", ts="2026-07-01T00:00:00Z", fee_paid=1.0))
    store.save_positions(
        [
            {
                "symbol": "BTC-USDT",
                "qty": 10.0,
                "avg_entry": 100.0,
                "stop": None,
                "take_profits": [],
                "opened_at": "2026-07-01T00:00:00Z",
                "decision_id": "dec1",
            }
        ]
    )
    store.append_equity(
        _equity_row(ts="2026-07-01T12:00:00Z", symbol="BTC-USDT", avg_entry=100.0, mark=105.0)
    )

    result = decision_pnl("dec1", store=store, mark_price_fn=lambda s: 110.0)
    assert result["max_drawdown_pct"] == pytest.approx(0.0, abs=ABS)


def test_max_drawdown_pct_sparse_data_returns_none(store):
    """Position opened and closed between two daily ticks -- no equity
    snapshot lands inside the lineage window -- must return None, never a
    fabricated number."""
    store.append_ledger(_ledger_row(trade_id="t1", ts="2026-07-01T10:00:00Z", fee_paid=1.0))
    store.append_ledger(
        _ledger_row(
            trade_id="t2",
            ts="2026-07-01T11:00:00Z",
            side="sell",
            qty=10.0,
            fill_price=101.0,
            fee_paid=1.01,
            order_type="market",
            realized_pnl=(101.0 - 100.0) * 10.0 - 1.01,
        )
    )
    # equity snapshot exists, but it's from a day BEFORE the lineage opened.
    store.append_equity(
        _equity_row(ts="2026-06-30T00:00:00Z", symbol="BTC-USDT", avg_entry=100.0, mark=99.0)
    )

    result = decision_pnl("dec1", store=store)
    assert result["max_drawdown_pct"] is None
