"""Tests for conditional stop/TP evaluation + the daily mark-to-market tick (Task 3).

Socket-disabled: every test injects a fixture ``bars_fn``/``price_fn`` so no
test ever touches the network (``default_bars_fn`` itself is never exercised
here — same convention as ``test_paper_broker.py``'s fixture ``price_fn``).

Binding fill rules under test (t3 brief / globals.md, repeated for traceability):
  - stop triggers when ``bar["low"] <= stop``; fills at ``bar["open"]`` if the
    bar gapped through (``open <= stop``), else at the stop price.
  - each take-profit ``{"price", "fraction"}`` triggers when
    ``bar["high"] >= price``; fills at ``price``, or at ``bar["open"]`` if the
    bar gapped above (``open >= price``).
  - stop AND any TP inside the same bar -> stop wins: TPs skipped, position
    fully closed at the stop fill.
  - no slippage on conditional fills (bar prices already conservative); fee
    still applies.
  - realized_pnl = (fill - avg_entry) * qty_sold - fee (same formula as
    market_sell).
  - same-day idempotency: one equity.jsonl row per UTC date; executed
    conditionals are removed from the position so a re-tick can't refill them.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.paper.broker import PaperBroker, PriceUnavailable
from src.paper.store import PaperStore
from src.paper.tick import run_tick
from src.paper.translator import RETRIABLE_NOTE

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
    def __init__(self, price: float = 100.0, ts: str = "2026-07-11T00:00:00Z"):
        self.price = price
        self.ts = ts
        self.raises = False

    def __call__(self, symbol: str) -> dict:
        if self.raises:
            raise PriceUnavailable(f"fixture refused price for {symbol}")
        return {"price": float(self.price), "ts": self.ts}


@pytest.fixture
def price_fn():
    return FakePriceFn(price=100.0)


@pytest.fixture
def broker(monkeypatch, tmp_path, price_fn):
    _set_default_env(monkeypatch, tmp_path)
    store = PaperStore(tmp_path)
    return PaperBroker(store, price_fn=price_fn)


def _seed_position(store: PaperStore, **overrides) -> dict:
    """Seed a single open position directly (bypassing market_buy) so avg_entry
    / stop / take_profits can be controlled precisely for fill-rule tests."""
    pos = {
        "symbol": "BTC-USDT",
        "qty": 10.0,
        "avg_entry": 100.0,
        "stop": None,
        "take_profits": [],
        "opened_at": "2026-07-10T00:00:00Z",
        "decision_id": "d1",
    }
    pos.update(overrides)
    store.save_positions([pos])
    return pos


def _bar(**overrides) -> dict:
    bar = {
        "open": 100.0,
        "high": 105.0,
        "low": 95.0,
        "close": 102.0,
        "ts": "2026-07-11T00:00:00Z",
    }
    bar.update(overrides)
    return bar


# --------------------------------------------------------------------------- #
# evaluate_conditionals: stop                                                 #
# --------------------------------------------------------------------------- #
def test_stop_hit_exactly_fills_at_stop_price(broker):
    _seed_position(broker.store, stop=95.0, take_profits=[])
    bar = _bar(open=99.0, high=100.0, low=94.0, close=96.0)  # low <= stop, open > stop

    fills = broker.evaluate_conditionals("BTC-USDT", bar)

    assert len(fills) == 1
    entry = fills[0]
    assert entry["order_type"] == "stop"
    assert entry["side"] == "sell"
    assert entry["fill_price"] == pytest.approx(95.0, abs=ABS)  # no gap: fills at stop
    assert entry["qty"] == pytest.approx(10.0, abs=ABS)  # full close
    assert entry["slippage_paid"] == pytest.approx(0.0, abs=ABS)  # no slippage on conditionals
    expected_fee = (10.0 * 95.0) * 10 / 10_000
    assert entry["fee_paid"] == pytest.approx(expected_fee, abs=ABS)
    expected_pnl = (95.0 - 100.0) * 10.0 - expected_fee
    assert entry["realized_pnl"] == pytest.approx(expected_pnl, abs=ABS)
    assert entry["decision_id"] == "d1"

    # full close removes the position
    assert broker.store.load_positions() == []


def test_stop_gap_through_fills_at_bar_open(broker):
    _seed_position(broker.store, stop=95.0, take_profits=[])
    # bar gaps THROUGH the stop: open already below stop
    bar = _bar(open=90.0, high=91.0, low=85.0, close=88.0)

    fills = broker.evaluate_conditionals("BTC-USDT", bar)

    assert len(fills) == 1
    entry = fills[0]
    assert entry["order_type"] == "stop"
    assert entry["fill_price"] == pytest.approx(90.0, abs=ABS)  # fills at OPEN, not stop
    assert entry["slippage_paid"] == pytest.approx(0.0, abs=ABS)
    assert broker.store.load_positions() == []


def test_stop_not_triggered_when_low_above_stop(broker):
    _seed_position(broker.store, stop=90.0, take_profits=[])
    bar = _bar(open=100.0, high=102.0, low=91.0, close=99.0)  # low never reaches stop

    fills = broker.evaluate_conditionals("BTC-USDT", bar)

    assert fills == []
    pos = broker.store.load_positions()[0]
    assert pos["qty"] == pytest.approx(10.0, abs=ABS)
    assert pos["stop"] == pytest.approx(90.0, abs=ABS)


# --------------------------------------------------------------------------- #
# evaluate_conditionals: take-profit                                          #
# --------------------------------------------------------------------------- #
def test_take_profit_partial_fill_halves_qty_remaining_tp_preserved(broker):
    _seed_position(
        broker.store,
        stop=None,
        take_profits=[{"price": 110.0, "fraction": 0.5}, {"price": 130.0, "fraction": 1.0}],
    )
    # high reaches the first TP (110) but not the second (130)
    bar = _bar(open=101.0, high=112.0, low=99.0, close=111.0)

    fills = broker.evaluate_conditionals("BTC-USDT", bar)

    assert len(fills) == 1
    entry = fills[0]
    assert entry["order_type"] == "take_profit"
    assert entry["fill_price"] == pytest.approx(110.0, abs=ABS)  # no gap: fills at tp price
    assert entry["qty"] == pytest.approx(5.0, abs=ABS)  # fraction 0.5 of 10
    assert entry["slippage_paid"] == pytest.approx(0.0, abs=ABS)
    expected_fee = (5.0 * 110.0) * 10 / 10_000
    assert entry["fee_paid"] == pytest.approx(expected_fee, abs=ABS)
    expected_pnl = (110.0 - 100.0) * 5.0 - expected_fee
    assert entry["realized_pnl"] == pytest.approx(expected_pnl, abs=ABS)

    pos = broker.store.load_positions()[0]
    assert pos["qty"] == pytest.approx(5.0, abs=ABS)  # halved
    # untriggered TP preserved; the executed one is removed
    assert pos["take_profits"] == [{"price": 130.0, "fraction": 1.0}]


def test_take_profit_gap_above_fills_at_bar_open(broker):
    _seed_position(broker.store, stop=None, take_profits=[{"price": 110.0, "fraction": 1.0}])
    # bar gaps ABOVE the tp: open already past tp price
    bar = _bar(open=115.0, high=118.0, low=112.0, close=116.0)

    fills = broker.evaluate_conditionals("BTC-USDT", bar)

    assert len(fills) == 1
    entry = fills[0]
    assert entry["fill_price"] == pytest.approx(115.0, abs=ABS)  # fills at OPEN, not tp price
    assert entry["qty"] == pytest.approx(10.0, abs=ABS)  # fraction 1.0 -> full close
    assert broker.store.load_positions() == []


def test_no_trigger_bar_no_fills(broker):
    _seed_position(
        broker.store, stop=80.0, take_profits=[{"price": 150.0, "fraction": 1.0}]
    )
    bar = _bar(open=100.0, high=105.0, low=95.0, close=101.0)

    fills = broker.evaluate_conditionals("BTC-USDT", bar)

    assert fills == []
    pos = broker.store.load_positions()[0]
    assert pos["qty"] == pytest.approx(10.0, abs=ABS)
    assert pos["take_profits"] == [{"price": 150.0, "fraction": 1.0}]


def test_evaluate_conditionals_no_position_returns_empty(broker):
    assert broker.evaluate_conditionals("ETH-USDT", _bar()) == []


def test_multiple_take_profits_trigger_same_bar_fractions_apply_sequentially(broker):
    _seed_position(
        broker.store,
        stop=None,
        take_profits=[{"price": 110.0, "fraction": 0.5}, {"price": 120.0, "fraction": 1.0}],
    )
    # high clears both TPs in one bar
    bar = _bar(open=101.0, high=125.0, low=99.0, close=121.0)

    fills = broker.evaluate_conditionals("BTC-USDT", bar)

    assert len(fills) == 2
    assert fills[0]["order_type"] == "take_profit"
    assert fills[0]["qty"] == pytest.approx(5.0, abs=ABS)  # 0.5 of the original 10
    # second fraction (1.0) applies to what's LEFT after the first fill (5.0)
    assert fills[1]["qty"] == pytest.approx(5.0, abs=ABS)
    # both consumed -> position fully closed, no take_profits list survives
    assert broker.store.load_positions() == []


# --------------------------------------------------------------------------- #
# evaluate_conditionals: stop + TP same bar -> stop wins                      #
# --------------------------------------------------------------------------- #
def test_stop_and_tp_same_bar_stop_wins_tp_skipped_full_close(broker):
    _seed_position(
        broker.store,
        stop=95.0,
        take_profits=[{"price": 110.0, "fraction": 0.5}],
    )
    # wild bar: low <= stop AND high >= tp, both inside one bar
    bar = _bar(open=100.0, high=115.0, low=90.0, close=98.0)

    fills = broker.evaluate_conditionals("BTC-USDT", bar)

    assert len(fills) == 1  # only the stop fires, TP skipped entirely
    entry = fills[0]
    assert entry["order_type"] == "stop"
    assert entry["fill_price"] == pytest.approx(95.0, abs=ABS)  # open(100) > stop(95): no gap
    assert entry["qty"] == pytest.approx(10.0, abs=ABS)  # position fully closed
    assert broker.store.load_positions() == []


# --------------------------------------------------------------------------- #
# Write ordering (positions -> account -> ledger, same as market_sell)        #
# --------------------------------------------------------------------------- #
def test_conditional_fill_persists_positions_before_account_before_ledger(
    broker, monkeypatch
):
    _seed_position(broker.store, stop=95.0, take_profits=[])
    calls: list[str] = []
    for name in ("save_positions", "save_account", "append_ledger"):
        orig = getattr(broker.store, name)

        def wrapper(arg, _name=name, _orig=orig):
            calls.append(_name)
            return _orig(arg)

        monkeypatch.setattr(broker.store, name, wrapper)

    broker.evaluate_conditionals("BTC-USDT", _bar(open=99.0, high=100.0, low=90.0, close=91.0))
    assert calls == ["save_positions", "save_account", "append_ledger"]


# --------------------------------------------------------------------------- #
# run_tick                                                                    #
# --------------------------------------------------------------------------- #
def test_run_tick_executes_stop_and_records_fill_and_equity(broker):
    _seed_position(broker.store, stop=95.0, take_profits=[])
    bar = _bar(open=99.0, high=100.0, low=90.0, close=96.0)

    result = run_tick(broker.store, bars_fn=lambda symbol, now: bar, price_fn=broker.price_fn)

    assert len(result["conditional_fills"]) == 1
    assert result["conditional_fills"][0]["order_type"] == "stop"
    assert result["errors"] == []
    assert broker.store.load_positions() == []

    eq = result["equity_snapshot"]
    assert eq["positions"] == []  # position closed before the mark
    rows = list(broker.store.iter_equity())
    assert len(rows) == 1
    # Final review cleanup 4: transient bookkeeping keys are NOT persisted; the
    # row is self-describing via its ts (whose UTC date equals the tick date).
    assert "date" not in rows[0]
    assert "already_recorded" not in rows[0]
    assert rows[0]["ts"][:10] == eq["date"]


def test_run_tick_marks_open_positions_at_bar_close_not_stale(broker):
    _seed_position(broker.store, stop=None, take_profits=[])
    bar = _bar(open=100.0, high=105.0, low=98.0, close=103.0)

    result = run_tick(broker.store, bars_fn=lambda symbol, now: bar, price_fn=broker.price_fn)

    posrow = result["equity_snapshot"]["positions"][0]
    assert posrow["mark"] == pytest.approx(103.0, abs=ABS)  # bar close, explicit mark
    assert posrow["stale"] is False
    assert result["equity_snapshot"]["stale_positions"] == 0


def test_run_tick_bars_fn_failure_records_error_position_untouched(broker):
    _seed_position(broker.store, stop=95.0, take_profits=[{"price": 200.0, "fraction": 1.0}])

    def failing_bars_fn(symbol, now):
        raise RuntimeError("no bars for BTC-USDT via okx/ccxt")

    result = run_tick(broker.store, bars_fn=failing_bars_fn, price_fn=broker.price_fn)

    assert result["conditional_fills"] == []
    assert len(result["errors"]) == 1
    assert result["errors"][0]["symbol"] == "BTC-USDT"
    assert "no bars" in result["errors"][0]["error"]

    # position completely untouched (never invent a price)
    pos = broker.store.load_positions()[0]
    assert pos["qty"] == pytest.approx(10.0, abs=ABS)
    assert pos["stop"] == pytest.approx(95.0, abs=ABS)
    assert pos["take_profits"] == [{"price": 200.0, "fraction": 1.0}]


def test_run_tick_same_day_double_tick_single_equity_snapshot_no_duplicate_fills(broker):
    _seed_position(
        broker.store,
        stop=None,
        take_profits=[{"price": 110.0, "fraction": 0.5}, {"price": 130.0, "fraction": 1.0}],
    )
    bar = _bar(open=101.0, high=112.0, low=99.0, close=111.0)
    now = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)

    first = run_tick(
        broker.store, bars_fn=lambda symbol, n: bar, price_fn=broker.price_fn, now=now
    )
    second = run_tick(
        broker.store, bars_fn=lambda symbol, n: bar, price_fn=broker.price_fn, now=now
    )

    # only the first tick's TP trigger fills; the second tick's identical bar
    # cannot refill the already-executed TP (it was removed from the position)
    assert len(first["conditional_fills"]) == 1
    assert second["conditional_fills"] == []

    rows = list(broker.store.iter_equity())
    assert len(rows) == 1  # single snapshot for the UTC date, not two
    assert first["equity_snapshot"]["already_recorded"] is False
    assert second["equity_snapshot"]["already_recorded"] is True


def test_run_tick_no_positions_still_appends_equity_snapshot(broker):
    result = run_tick(broker.store, bars_fn=lambda symbol, now: _bar(), price_fn=broker.price_fn)
    assert result["conditional_fills"] == []
    assert result["errors"] == []
    assert result["equity_snapshot"]["positions"] == []
    assert len(list(broker.store.iter_equity())) == 1


def test_run_tick_defaults_to_new_store_when_none_passed(monkeypatch, tmp_path, price_fn):
    _set_default_env(monkeypatch, tmp_path)
    result = run_tick(bars_fn=lambda symbol, now: _bar(), price_fn=price_fn)
    assert result["conditional_fills"] == []
    assert result["errors"] == []


# --------------------------------------------------------------------------- #
# Final review C2 — entry-day bars are NOT evaluated (opened_at respected)     #
# --------------------------------------------------------------------------- #
def test_entry_day_bar_does_not_fill_stop(broker):
    """C2 reproduction: buy at 14:00 UTC (stop 95); the SAME-day bar
    (open 90 / low 88 / close 104) must NOT fire the stop for a fictitious
    loss — conditional evaluation begins on the first FULL bar after entry."""
    _seed_position(
        broker.store,
        stop=95.0,
        take_profits=[],
        avg_entry=100.05,
        opened_at="2026-07-10T14:00:00Z",
    )
    bar = _bar(open=90.0, high=104.0, low=88.0, close=104.0, ts="2026-07-10T00:00:00Z")

    fills = broker.evaluate_conditionals("BTC-USDT", bar)

    assert fills == []  # same-day bar skipped
    pos = broker.store.load_positions()[0]
    assert pos["qty"] == pytest.approx(10.0, abs=ABS)  # untouched


def test_first_full_bar_after_entry_fills_stop(broker):
    """A full bar on the day AFTER entry still fills the stop normally."""
    _seed_position(
        broker.store, stop=95.0, take_profits=[], opened_at="2026-07-10T14:00:00Z"
    )
    bar = _bar(open=99.0, high=100.0, low=90.0, close=96.0, ts="2026-07-11T00:00:00Z")

    fills = broker.evaluate_conditionals("BTC-USDT", bar)

    assert len(fills) == 1
    assert fills[0]["order_type"] == "stop"
    assert broker.store.load_positions() == []


def test_run_tick_records_entry_day_skip_note(broker):
    """C2: run_tick surfaces the entry-day skip as a note (no fill)."""
    _seed_position(
        broker.store, stop=95.0, take_profits=[], opened_at="2026-07-10T14:00:00Z"
    )
    bar = _bar(open=90.0, high=104.0, low=88.0, close=104.0, ts="2026-07-10T00:00:00Z")

    result = run_tick(
        broker.store,
        bars_fn=lambda symbol, now: bar,
        price_fn=broker.price_fn,
        now=datetime(2026, 7, 10, 15, 0, tzinfo=timezone.utc),
    )

    assert result["conditional_fills"] == []
    notes = result.get("notes", [])
    assert any("entry-day bar skipped" in n and "BTC-USDT" in n for n in notes)


# --------------------------------------------------------------------------- #
# Final review cleanup 1 — kill switch: run_tick no-ops fast when disabled     #
# --------------------------------------------------------------------------- #
def test_run_tick_disabled_no_ops(monkeypatch, tmp_path, price_fn):
    _set_default_env(monkeypatch, tmp_path, VIBE_PAPER_ENABLED="0")
    store = PaperStore(tmp_path)
    result = run_tick(store, bars_fn=lambda s, n: _bar(), price_fn=price_fn)
    assert result.get("disabled") is True
    assert result["conditional_fills"] == []
    assert list(store.iter_equity()) == []


# --------------------------------------------------------------------------- #
# Final review I3 — tick-driven retry of retriable (price-unavailable) noops   #
# --------------------------------------------------------------------------- #
def _seed_retriable_decision(store, jpath, monkeypatch, *, decided_at=None):
    from src.committee import journal

    monkeypatch.setenv(journal.JOURNAL_PATH_ENV, str(jpath))
    entry = journal.append_decision(
        symbol="BTC-USDT",
        rating="Buy",
        time_horizon="72h swing",
        path=jpath,
        run_id="run-retry",
        decided_at=decided_at,
    )
    store.append_ledger(
        {
            "ts": "2026-07-11T00:00:00Z",
            "trade_id": "noop1",
            "symbol": "BTC-USDT",
            "side": "buy",
            "qty": 0.0,
            "fill_price": None,
            "slippage_paid": 0.0,
            "fee_paid": 0.0,
            "order_type": "noop",
            "decision_id": entry["id"],
            "realized_pnl": None,
            "note": RETRIABLE_NOTE,
        }
    )
    return entry


def test_run_tick_retries_retriable_decision_when_price_available(
    monkeypatch, tmp_path, price_fn
):
    _set_default_env(monkeypatch, tmp_path)
    store = PaperStore(tmp_path)
    entry = _seed_retriable_decision(
        store, tmp_path / "journal.jsonl", monkeypatch,
        decided_at=datetime.now(timezone.utc),
    )

    result = run_tick(
        store, bars_fn=lambda s, n: _bar(), price_fn=price_fn,
        now=datetime.now(timezone.utc),
    )

    retried = result.get("retried_decisions", [])
    assert any(r["decision_id"] == entry["id"] for r in retried)
    # the retried Buy actually opened a position this tick
    assert len(store.load_positions()) == 1


def test_run_tick_does_not_retry_already_executed(monkeypatch, tmp_path, price_fn):
    _set_default_env(monkeypatch, tmp_path)
    from src.committee import journal

    jpath = tmp_path / "journal.jsonl"
    monkeypatch.setenv(journal.JOURNAL_PATH_ENV, str(jpath))
    store = PaperStore(tmp_path)
    entry = journal.append_decision(
        symbol="BTC-USDT", rating="Buy", time_horizon="72h swing",
        path=jpath, run_id="run-done",
    )
    store.append_ledger(
        {
            "ts": "2026-07-11T00:00:00Z",
            "trade_id": "fill1",
            "symbol": "BTC-USDT",
            "side": "buy",
            "qty": 10.0,
            "fill_price": 100.0,
            "slippage_paid": 0.0,
            "fee_paid": 1.0,
            "order_type": "market",
            "decision_id": entry["id"],
            "realized_pnl": None,
            "note": None,
        }
    )

    result = run_tick(
        store, bars_fn=lambda s, n: _bar(), price_fn=price_fn,
        now=datetime.now(timezone.utc),
    )
    assert result.get("retried_decisions", []) == []


def test_run_tick_skips_retriable_older_than_7_days(monkeypatch, tmp_path, price_fn):
    _set_default_env(monkeypatch, tmp_path)
    store = PaperStore(tmp_path)
    _seed_retriable_decision(
        store, tmp_path / "journal.jsonl", monkeypatch,
        decided_at=datetime.now(timezone.utc) - timedelta(days=8),
    )

    result = run_tick(
        store, bars_fn=lambda s, n: _bar(), price_fn=price_fn,
        now=datetime.now(timezone.utc),
    )
    assert result.get("retried_decisions", []) == []
    assert store.load_positions() == []
