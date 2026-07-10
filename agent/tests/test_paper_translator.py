"""Tests for the decision -> order translator with per-decision idempotency (Task 4).

Socket-disabled: every test injects a fixture ``price_fn`` (same convention as
``test_paper_broker.py`` / ``test_paper_tick.py``) so no test ever touches the
network. Money math is asserted to 8 decimal places via ``pytest.approx``.

Binding mapping under test (t4 brief + Tasks 1-3 review addenda, repeated for
traceability):
  Buy/Overweight   -> market_buy sized position_size_pct (default
                       DEFAULT_SIZE_PCT) percent of current equity;
                       Overweight at HALF that size (open or add).
  stop             -> stop_loss if provided, else fill_price*(1-DEFAULT_STOP_PCT/100).
  tp               -> take_profit if provided, else price_target.
  Hold             -> set_risk with any provided (typed) stop/TP; nothing else
                       (price_target is NOT a TP fallback for Hold).
  Underweight/Sell -> market_sell fraction 0.5 / 1.0; no position -> noop
                       ("sell signal with no position").
  Idempotency      -> ledger scan by decision_id; ANY row counts as executed
                       EXCEPT a noop row with note == RETRIABLE_NOTE (a
                       price-fetch failure), which must NOT block a retry.
  Kill switch      -> VIBE_PAPER_ENABLED: unset -> enabled; "0"/"false"/"" -> disabled.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.paper.broker import PaperBroker, PriceUnavailable
from src.paper.store import PaperStore
from src.paper.translator import RETRIABLE_NOTE, execute_decision

ABS = 1e-8


# --------------------------------------------------------------------------- #
# Fixtures (mirrors test_paper_broker.py / test_paper_tick.py conventions)     #
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
    """Deterministic price_fn: fixed price per symbol (default 100.0).

    ``.raises`` forces PriceUnavailable to exercise the retriable-noop path.
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


def _seed_position(store: PaperStore, **overrides) -> dict:
    pos = {
        "symbol": "BTC-USDT",
        "qty": 10.0,
        "avg_entry": 100.0,
        "stop": None,
        "take_profits": [],
        "opened_at": "2026-07-10T00:00:00Z",
        "decision_id": "prior",
    }
    pos.update(overrides)
    store.save_positions([pos])
    return pos


def _decision(rating: str, **overrides) -> dict:
    """Build a Task-1-shaped journal entry dict; typed fields default absent
    (conditionally-keyed on real entries -- see translator.py docstring)."""
    entry = {
        "id": "dec_test",
        "decided_at": "2026-07-11T00:00:00+00:00",
        "symbol": "BTC-USDT",
        "rating": rating,
        "time_horizon": "72h swing",
        "primary_horizon": "72h",
        "price_target": None,
        "run_id": None,
        "status": "pending",
        "ref_price": None,
        "horizons": {},
        "reflection": None,
        "reflected_at": None,
    }
    entry.update(overrides)
    return entry


LEGACY_HOLD_FIXTURE = {
    "id": "dec_1f0b2866628e",
    "decided_at": "2026-07-10T14:09:11.922094+00:00",
    "symbol": "BTC-USDT",
    "rating": "Hold",
    "time_horizon": "72h swing",
    "primary_horizon": "72h",
    "price_target": 65000,
    "run_id": None,
    "status": "pending",
    "ref_price": None,
    "horizons": {},
    "reflection": None,
    "reflected_at": None,
}


# --------------------------------------------------------------------------- #
# 10-cell rating x position matrix                                            #
# --------------------------------------------------------------------------- #
MATRIX = [
    ("Buy", False, "open_buy", 1.0),
    ("Buy", True, "add_buy", 1.0),
    ("Overweight", False, "open_buy", 0.5),
    ("Overweight", True, "add_buy", 0.5),
    ("Hold", False, "hold_noop", None),
    ("Hold", True, "hold_noop", None),
    ("Underweight", False, "sell_noop", None),
    ("Underweight", True, "sell_reduce", 0.5),
    ("Sell", False, "sell_noop", None),
    ("Sell", True, "sell_reduce", 1.0),
]


@pytest.mark.parametrize("rating,has_position,kind,frac", MATRIX)
def test_rating_position_matrix(broker, rating, has_position, kind, frac):
    if has_position:
        _seed_position(broker.store)

    entry = _decision(rating, id="dec_matrix")
    result = execute_decision(entry, broker)

    assert result["decision_id"] == "dec_matrix"
    assert result["skipped"] is None
    actions = result["actions"]

    if kind in ("open_buy", "add_buy"):
        assert len(actions) == 1
        action = actions[0]
        assert action["side"] == "buy"
        assert action["order_type"] == "market"

        cash = 100_000.0
        position_value = 10.0 * 100.0 if has_position else 0.0  # mark == avg_entry (100.0)
        equity = cash + position_value
        expected_notional = equity * 0.10 * frac
        expected_fill = 100.0 * (1 + 5 / 10_000)
        expected_qty = expected_notional / expected_fill
        # for an add, qty added this order == expected_qty regardless of
        # prior held qty (avg_entry re-blends, doesn't change this order's fill qty)
        assert action["qty"] == pytest.approx(expected_qty, abs=ABS)

    elif kind == "hold_noop":
        assert actions == []

    elif kind == "sell_noop":
        assert len(actions) == 1
        assert actions[0]["order_type"] == "noop"
        assert actions[0]["note"] == "sell signal with no position"

    elif kind == "sell_reduce":
        assert len(actions) == 1
        action = actions[0]
        assert action["side"] == "sell"
        assert action["order_type"] == "market"
        assert action["qty"] == pytest.approx(10.0 * frac, abs=ABS)


# --------------------------------------------------------------------------- #
# Defaults: sizing + stop + TP fallback                                       #
# --------------------------------------------------------------------------- #
def test_buy_no_position_default_sizing_and_stop(broker):
    entry = _decision("Buy", id="dec_1")
    result = execute_decision(entry, broker)

    assert result["skipped"] is None
    action = result["actions"][0]
    expected_fill = 100.0 * (1 + 5 / 10_000)  # 100.05
    expected_notional = 100_000.0 * 0.10  # DEFAULT_SIZE_PCT=10, equity=cash only
    expected_qty = expected_notional / expected_fill
    assert action["qty"] == pytest.approx(expected_qty, abs=ABS)
    assert action["fill_price"] == pytest.approx(expected_fill, abs=ABS)

    pos = broker.store.load_positions()[0]
    expected_stop = expected_fill * (1 - 8 / 100)  # DEFAULT_STOP_PCT=8 -> entry*0.92
    assert pos["stop"] == pytest.approx(expected_stop, abs=ABS)
    assert pos["stop"] == pytest.approx(pos["avg_entry"] * 0.92, abs=ABS)
    assert pos["take_profits"] == []  # no take_profit, no price_target


def test_overweight_no_position_half_sizing(broker):
    entry = _decision("Overweight", id="dec_2")
    execute_decision(entry, broker)

    expected_fill = 100.0 * (1 + 5 / 10_000)
    expected_notional = 100_000.0 * 0.10 * 0.5
    expected_qty = expected_notional / expected_fill
    pos = broker.store.load_positions()[0]
    assert pos["qty"] == pytest.approx(expected_qty, abs=ABS)


def test_buy_position_size_pct_override(broker):
    entry = _decision("Buy", id="dec_3", position_size_pct=20)
    execute_decision(entry, broker)

    expected_fill = 100.0 * (1 + 5 / 10_000)
    expected_notional = 100_000.0 * 0.20
    expected_qty = expected_notional / expected_fill
    pos = broker.store.load_positions()[0]
    assert pos["qty"] == pytest.approx(expected_qty, abs=ABS)


def test_buy_explicit_stop_and_take_profit_no_default_applied(broker):
    entry = _decision("Buy", id="dec_4", stop_loss=88.0, take_profit=130.0)
    execute_decision(entry, broker)

    pos = broker.store.load_positions()[0]
    assert pos["stop"] == pytest.approx(88.0, abs=ABS)
    assert pos["take_profits"] == [{"price": 130.0, "fraction": 1.0}]


def test_buy_tp_falls_back_to_price_target(broker):
    entry = _decision("Buy", id="dec_5", price_target=150.0)
    execute_decision(entry, broker)

    pos = broker.store.load_positions()[0]
    assert pos["take_profits"] == [{"price": 150.0, "fraction": 1.0}]


def test_buy_explicit_take_profit_wins_over_price_target(broker):
    entry = _decision("Buy", id="dec_6", take_profit=140.0, price_target=150.0)
    execute_decision(entry, broker)

    pos = broker.store.load_positions()[0]
    assert pos["take_profits"] == [{"price": 140.0, "fraction": 1.0}]


def test_buy_price_target_non_numeric_string_treated_as_absent(broker):
    """The tool layer doesn't coerce price_target -- e.g. a literal 'n/a'."""
    entry = _decision("Buy", id="dec_7", price_target="n/a")
    result = execute_decision(entry, broker)

    assert result["skipped"] is None
    pos = broker.store.load_positions()[0]
    assert pos["take_profits"] == []  # falls through to no TP, not a crash


# --------------------------------------------------------------------------- #
# Hold                                                                          #
# --------------------------------------------------------------------------- #
def test_hold_real_legacy_fixture_no_position_empty_actions_no_set_risk(broker):
    """The real 2026-07-10 fixture: Hold, price_target=65000, no typed
    stop_loss/take_profit, no position -> empty actions regardless of
    price_target (Hold never uses it as a TP fallback; and set_risk requires
    a live position anyway)."""
    monkeypatch_set_risk = MagicMock(wraps=broker.set_risk)
    broker.set_risk = monkeypatch_set_risk

    result = execute_decision(LEGACY_HOLD_FIXTURE, broker)

    assert result == {"decision_id": "dec_1f0b2866628e", "actions": [], "skipped": None}
    monkeypatch_set_risk.assert_not_called()
    assert broker.store.load_positions() == []
    assert list(broker.store.iter_ledger()) == []


def test_hold_no_typed_fields_existing_position_empty_actions(broker):
    _seed_position(broker.store)
    entry = _decision("Hold", id="dec_8")
    result = execute_decision(entry, broker)

    assert result["actions"] == []
    pos = broker.store.load_positions()[0]
    assert pos["stop"] is None  # untouched


def test_hold_with_typed_stop_and_tp_existing_position_updates_risk(broker):
    _seed_position(broker.store)
    entry = _decision("Hold", id="dec_9", stop_loss=90.0, take_profit=120.0)
    result = execute_decision(entry, broker)

    assert len(result["actions"]) == 1
    assert result["actions"][0]["order_type"] == "noop"
    assert result["actions"][0]["note"] == "risk parameters updated (Hold)"

    pos = broker.store.load_positions()[0]
    assert pos["stop"] == pytest.approx(90.0, abs=ABS)
    assert pos["take_profits"] == [{"price": 120.0, "fraction": 1.0}]

    # permanent noop -> idempotent
    result2 = execute_decision(entry, broker)
    assert result2 == {"decision_id": "dec_9", "actions": [], "skipped": "already executed"}
    assert len(list(broker.store.iter_ledger())) == 1


# --------------------------------------------------------------------------- #
# Idempotency                                                                  #
# --------------------------------------------------------------------------- #
def test_same_decision_twice_second_call_skipped_ledger_unchanged(broker):
    entry = _decision("Buy", id="dec_dup")
    result1 = execute_decision(entry, broker)
    assert result1["skipped"] is None
    ledger_after_first = list(broker.store.iter_ledger())
    assert len(ledger_after_first) == 1

    result2 = execute_decision(entry, broker)
    assert result2 == {"decision_id": "dec_dup", "actions": [], "skipped": "already executed"}
    ledger_after_second = list(broker.store.iter_ledger())
    assert len(ledger_after_second) == 1
    assert ledger_after_second == ledger_after_first


def test_sell_no_position_noop_is_permanent_and_idempotent(broker):
    entry = _decision("Sell", id="dec_noop_sell")
    result1 = execute_decision(entry, broker)
    assert result1["actions"][0]["order_type"] == "noop"

    result2 = execute_decision(entry, broker)
    assert result2["skipped"] == "already executed"
    assert len(list(broker.store.iter_ledger())) == 1


# --------------------------------------------------------------------------- #
# MandateViolation -> noop (max positions / symbol exposure cap)              #
# --------------------------------------------------------------------------- #
def test_buy_max_positions_reached_recorded_as_noop_not_raised(broker):
    positions = [
        {
            "symbol": sym,
            "qty": 1.0,
            "avg_entry": 100.0,
            "stop": None,
            "take_profits": [],
            "opened_at": "2026-07-10T00:00:00Z",
            "decision_id": f"prior-{sym}",
        }
        for sym in ("BTC-USDT", "ETH-USDT", "SOL-USDT")
    ]
    broker.store.save_positions(positions)

    entry = _decision("Buy", id="dec_maxpos", symbol="XRP-USDT")
    result = execute_decision(entry, broker)

    assert result["skipped"] is None
    assert len(result["actions"]) == 1
    action = result["actions"][0]
    assert action["order_type"] == "noop"
    assert "max positions" in action["note"]
    assert action["side"] == "buy"

    # never raised, and is a permanent (non-retriable) noop
    result2 = execute_decision(entry, broker)
    assert result2["skipped"] == "already executed"


def test_buy_symbol_exposure_cap_recorded_as_noop_not_raised(broker):
    _seed_position(broker.store, qty=400.0, avg_entry=100.0)

    entry = _decision("Buy", id="dec_expcap")
    result = execute_decision(entry, broker)

    assert result["skipped"] is None
    assert len(result["actions"]) == 1
    action = result["actions"][0]
    assert action["order_type"] == "noop"
    assert "exposure cap" in action["note"]


# --------------------------------------------------------------------------- #
# PriceUnavailable -> retriable noop, NOT counted as executed                 #
# --------------------------------------------------------------------------- #
def test_buy_price_unavailable_retriable_noop_then_retry_succeeds(broker, price_fn):
    price_fn.raises = True
    entry = _decision("Buy", id="dec_retry")
    result1 = execute_decision(entry, broker)

    assert result1["skipped"] is None
    assert len(result1["actions"]) == 1
    assert result1["actions"][0]["order_type"] == "noop"
    assert result1["actions"][0]["note"] == RETRIABLE_NOTE
    assert broker.store.load_positions() == []

    price_fn.raises = False
    result2 = execute_decision(entry, broker)
    assert result2["skipped"] is None  # NOT "already executed" -- retriable
    assert result2["actions"][0]["order_type"] == "market"
    assert len(broker.store.load_positions()) == 1
    assert len(list(broker.store.iter_ledger())) == 2  # retriable noop + real fill


def test_sell_price_unavailable_retriable_noop(broker, price_fn):
    _seed_position(broker.store)
    price_fn.raises = True

    entry = _decision("Sell", id="dec_sell_retry")
    result = execute_decision(entry, broker)

    assert len(result["actions"]) == 1
    assert result["actions"][0]["order_type"] == "noop"
    assert result["actions"][0]["note"] == RETRIABLE_NOTE
    assert len(broker.store.load_positions()) == 1  # untouched


# --------------------------------------------------------------------------- #
# Rating case-insensitivity                                                    #
# --------------------------------------------------------------------------- #
def test_rating_read_case_insensitively(broker):
    entry = _decision("buy", id="dec_lower")  # lowercase, as raw text could be
    result = execute_decision(entry, broker)
    assert result["actions"][0]["side"] == "buy"

    _seed_position(broker.store, symbol="ETH-USDT")
    entry2 = _decision("SELL", id="dec_upper", symbol="ETH-USDT")
    result2 = execute_decision(entry2, broker)
    assert result2["actions"][0]["side"] == "sell"


def test_unrecognized_rating_defensive_noop(broker):
    entry = _decision("Neutral", id="dec_unknown")
    result = execute_decision(entry, broker)
    assert result == {"decision_id": "dec_unknown", "actions": [], "skipped": None}


# --------------------------------------------------------------------------- #
# Kill switch                                                                  #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value", ["0", "false", "False", ""])
def test_disabled_env_skips_and_no_account_created(monkeypatch, tmp_path, price_fn, value):
    _set_default_env(monkeypatch, tmp_path, VIBE_PAPER_ENABLED=value)
    store = PaperStore(tmp_path)
    broker = PaperBroker(store, price_fn=price_fn)

    entry = _decision("Buy", id="dec_disabled")
    result = execute_decision(entry, broker)

    assert result == {"decision_id": "dec_disabled", "actions": [], "skipped": "paper trading disabled"}
    assert broker.store.load_account() is None


def test_unset_env_defaults_to_enabled(monkeypatch, tmp_path, price_fn):
    _set_default_env(monkeypatch, tmp_path)
    monkeypatch.delenv("VIBE_PAPER_ENABLED", raising=False)
    store = PaperStore(tmp_path)
    broker = PaperBroker(store, price_fn=price_fn)

    entry = _decision("Buy", id="dec_unset")
    result = execute_decision(entry, broker)

    assert result["skipped"] is None
    assert len(result["actions"]) == 1
