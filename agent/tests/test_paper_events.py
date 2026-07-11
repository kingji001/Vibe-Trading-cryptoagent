"""Tests for the deterministic event trigger (two-tier-cadence Task 3).

``check_events`` is a PURE function: all market/journal I/O is injected via
``price_fn`` / ``funding_fn`` / ``journal_ref_fn`` callables, so every test
here is socket-free by construction. It flags a watched symbol when the last
price has moved >= the configured percent from a reference price, or when the
absolute funding rate is >= the configured threshold, and enforces a
per-symbol cooldown so a sustained move triggers exactly once.

Reference-price resolution order (binding, spec 2.3):
  1. the last committee decision's execution price (``journal_ref_fn``),
  2. else the previous tick's stored ``last_price``,
  3. else no price trigger this tick — but the observed price is stored so the
     NEXT tick can compare.

The run_tick integration (events checked every tick; 1D creates tick_state
ONLY when a threshold is enabled) and the fetch-failure -> error-in-tick-result
behavior are pinned in test_paper_tick.py; this file owns the pure check.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.paper.events import EventConfig, check_events

NOW = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)


def _empty_state() -> dict:
    return {"last_bar_ts": {}, "last_event_trigger_ts": {}, "last_price": {}}


def _price_fn(prices: dict[str, float]):
    def fn(symbol: str) -> float:
        if symbol not in prices:
            raise RuntimeError(f"fixture has no price for {symbol}")
        return prices[symbol]

    return fn


def _funding_fn(fundings: dict[str, float]):
    def fn(symbol: str) -> float:
        if symbol not in fundings:
            raise RuntimeError(f"fixture has no funding for {symbol}")
        return fundings[symbol]

    return fn


def _ref_fn(refs: dict[str, float | None]):
    def fn(symbol: str) -> float | None:
        return refs.get(symbol)

    return fn


def _cfg(**overrides) -> EventConfig:
    base = {"price_move_pct": 5.0, "funding_abs": 0.001, "cooldown_h": 12.0}
    base.update(overrides)
    return EventConfig(**base)


# --------------------------------------------------------------------------- #
# EventConfig.from_env                                                         #
# --------------------------------------------------------------------------- #
def test_config_defaults_when_env_unset(monkeypatch):
    for key in ("VIBE_EVENT_PRICE_MOVE_PCT", "VIBE_EVENT_FUNDING_ABS", "VIBE_EVENT_COOLDOWN_H"):
        monkeypatch.delenv(key, raising=False)
    cfg = EventConfig.from_env()
    assert cfg.price_move_pct == 5.0
    assert cfg.funding_abs == 0.001
    assert cfg.cooldown_h == 12.0
    assert cfg.enabled is True


def test_config_zero_disables_each_threshold(monkeypatch):
    monkeypatch.setenv("VIBE_EVENT_PRICE_MOVE_PCT", "0")
    monkeypatch.setenv("VIBE_EVENT_FUNDING_ABS", "0")
    cfg = EventConfig.from_env()
    assert cfg.price_move_pct == 0.0
    assert cfg.funding_abs == 0.0
    assert cfg.enabled is False  # both off


def test_config_partial_enable(monkeypatch):
    monkeypatch.setenv("VIBE_EVENT_PRICE_MOVE_PCT", "0")
    monkeypatch.setenv("VIBE_EVENT_FUNDING_ABS", "0.002")
    cfg = EventConfig.from_env()
    assert cfg.enabled is True  # funding still on


def test_config_empty_string_falls_back_to_default(monkeypatch):
    """An empty env value must not crash float() nor be read as 0."""
    monkeypatch.setenv("VIBE_EVENT_PRICE_MOVE_PCT", "")
    cfg = EventConfig.from_env()
    assert cfg.price_move_pct == 5.0


# --------------------------------------------------------------------------- #
# Price-move threshold boundaries                                             #
# --------------------------------------------------------------------------- #
def test_price_move_exactly_at_threshold_triggers():
    # ref 100 -> 105 is exactly +5%
    triggers, state = check_events(
        ["BTC-USDT"], _empty_state(),
        price_fn=_price_fn({"BTC-USDT": 105.0}),
        funding_fn=_funding_fn({"BTC-USDT": 0.0}),
        journal_ref_fn=_ref_fn({"BTC-USDT": 100.0}),
        now=NOW, config=_cfg(),
    )
    assert len(triggers) == 1
    t = triggers[0]
    assert t["symbol"] == "BTC-USDT"
    assert t["metric"] == "price_move_pct"
    assert t["value"] == pytest.approx(5.0)
    assert t["threshold"] == pytest.approx(5.0)
    assert state["last_event_trigger_ts"]["BTC-USDT"]  # cooldown armed


def test_price_move_just_below_threshold_does_not_trigger():
    # ref 100 -> 104.99 is +4.99%
    triggers, state = check_events(
        ["BTC-USDT"], _empty_state(),
        price_fn=_price_fn({"BTC-USDT": 104.99}),
        funding_fn=_funding_fn({"BTC-USDT": 0.0}),
        journal_ref_fn=_ref_fn({"BTC-USDT": 100.0}),
        now=NOW, config=_cfg(),
    )
    assert triggers == []
    assert "BTC-USDT" not in state["last_event_trigger_ts"]
    # observed price stored for next tick even though no trigger
    assert state["last_price"]["BTC-USDT"] == pytest.approx(104.99)


def test_price_move_downward_uses_absolute_move():
    # ref 100 -> 94 is -6% -> |move| 6% >= 5%
    triggers, _ = check_events(
        ["BTC-USDT"], _empty_state(),
        price_fn=_price_fn({"BTC-USDT": 94.0}),
        funding_fn=_funding_fn({"BTC-USDT": 0.0}),
        journal_ref_fn=_ref_fn({"BTC-USDT": 100.0}),
        now=NOW, config=_cfg(),
    )
    assert len(triggers) == 1
    assert triggers[0]["value"] == pytest.approx(6.0)


# --------------------------------------------------------------------------- #
# Reference-price resolution order                                            #
# --------------------------------------------------------------------------- #
def test_reference_uses_journal_decision_price_first():
    """Decision price (journal_ref_fn) wins even when a stale last_price exists."""
    state = _empty_state()
    state["last_price"]["BTC-USDT"] = 999.0  # would give a huge move if used
    triggers, _ = check_events(
        ["BTC-USDT"], state,
        price_fn=_price_fn({"BTC-USDT": 103.0}),  # +3% off decision price 100 -> no trigger
        funding_fn=_funding_fn({"BTC-USDT": 0.0}),
        journal_ref_fn=_ref_fn({"BTC-USDT": 100.0}),
        now=NOW, config=_cfg(),
    )
    assert triggers == []  # decision price 100 used, not last_price 999


def test_reference_falls_back_to_previous_tick_price():
    """No decision price -> previous tick's stored last_price is the reference."""
    state = _empty_state()
    state["last_price"]["BTC-USDT"] = 100.0
    triggers, _ = check_events(
        ["BTC-USDT"], state,
        price_fn=_price_fn({"BTC-USDT": 106.0}),  # +6% vs prev-tick 100
        funding_fn=_funding_fn({"BTC-USDT": 0.0}),
        journal_ref_fn=_ref_fn({"BTC-USDT": None}),  # no decision
        now=NOW, config=_cfg(),
    )
    assert len(triggers) == 1
    assert triggers[0]["value"] == pytest.approx(6.0)


def test_reference_absent_no_trigger_but_stores_price_for_next_tick():
    """No decision AND no previous-tick price -> no price trigger this tick, but
    the observed price is stored so the NEXT tick can compare."""
    triggers, state = check_events(
        ["BTC-USDT"], _empty_state(),
        price_fn=_price_fn({"BTC-USDT": 100.0}),
        funding_fn=_funding_fn({"BTC-USDT": 0.0}),
        journal_ref_fn=_ref_fn({"BTC-USDT": None}),
        now=NOW, config=_cfg(),
    )
    assert triggers == []
    assert state["last_price"]["BTC-USDT"] == pytest.approx(100.0)


# --------------------------------------------------------------------------- #
# Funding threshold                                                           #
# --------------------------------------------------------------------------- #
def test_funding_exactly_at_threshold_triggers():
    triggers, _ = check_events(
        ["BTC-USDT"], _empty_state(),
        price_fn=_price_fn({"BTC-USDT": 100.0}),
        funding_fn=_funding_fn({"BTC-USDT": 0.001}),
        journal_ref_fn=_ref_fn({"BTC-USDT": 100.0}),  # no price move
        now=NOW, config=_cfg(),
    )
    assert len(triggers) == 1
    assert triggers[0]["metric"] == "funding_abs"
    assert triggers[0]["value"] == pytest.approx(0.001)


def test_funding_negative_uses_absolute_value():
    triggers, _ = check_events(
        ["BTC-USDT"], _empty_state(),
        price_fn=_price_fn({"BTC-USDT": 100.0}),
        funding_fn=_funding_fn({"BTC-USDT": -0.0015}),
        journal_ref_fn=_ref_fn({"BTC-USDT": 100.0}),
        now=NOW, config=_cfg(),
    )
    assert len(triggers) == 1
    assert triggers[0]["metric"] == "funding_abs"


def test_funding_just_below_threshold_does_not_trigger():
    triggers, _ = check_events(
        ["BTC-USDT"], _empty_state(),
        price_fn=_price_fn({"BTC-USDT": 100.0}),
        funding_fn=_funding_fn({"BTC-USDT": 0.0009}),
        journal_ref_fn=_ref_fn({"BTC-USDT": 100.0}),
        now=NOW, config=_cfg(),
    )
    assert triggers == []


def test_price_move_takes_precedence_single_trigger_per_symbol():
    """When BOTH metrics breach, only ONE trigger (price) is emitted for the
    symbol and it enters cooldown once."""
    triggers, _ = check_events(
        ["BTC-USDT"], _empty_state(),
        price_fn=_price_fn({"BTC-USDT": 110.0}),  # +10%
        funding_fn=_funding_fn({"BTC-USDT": 0.01}),  # also breaches
        journal_ref_fn=_ref_fn({"BTC-USDT": 100.0}),
        now=NOW, config=_cfg(),
    )
    assert len(triggers) == 1
    assert triggers[0]["metric"] == "price_move_pct"


# --------------------------------------------------------------------------- #
# Disabled thresholds                                                         #
# --------------------------------------------------------------------------- #
def test_price_disabled_only_funding_checked():
    triggers, _ = check_events(
        ["BTC-USDT"], _empty_state(),
        price_fn=_price_fn({"BTC-USDT": 200.0}),  # +100% but price disabled
        funding_fn=_funding_fn({"BTC-USDT": 0.002}),
        journal_ref_fn=_ref_fn({"BTC-USDT": 100.0}),
        now=NOW, config=_cfg(price_move_pct=0.0),
    )
    assert len(triggers) == 1
    assert triggers[0]["metric"] == "funding_abs"


def test_funding_disabled_only_price_checked():
    triggers, _ = check_events(
        ["BTC-USDT"], _empty_state(),
        price_fn=_price_fn({"BTC-USDT": 110.0}),
        funding_fn=_funding_fn({"BTC-USDT": 0.05}),  # huge but funding disabled
        journal_ref_fn=_ref_fn({"BTC-USDT": 100.0}),
        now=NOW, config=_cfg(funding_abs=0.0),
    )
    assert len(triggers) == 1
    assert triggers[0]["metric"] == "price_move_pct"


def test_both_disabled_no_triggers_no_state_change():
    state = _empty_state()
    triggers, new_state = check_events(
        ["BTC-USDT"], state,
        price_fn=_price_fn({"BTC-USDT": 200.0}),
        funding_fn=_funding_fn({"BTC-USDT": 0.05}),
        journal_ref_fn=_ref_fn({"BTC-USDT": 100.0}),
        now=NOW, config=_cfg(price_move_pct=0.0, funding_abs=0.0),
    )
    assert triggers == []
    assert new_state["last_price"] == {}  # nothing fetched/stored


# --------------------------------------------------------------------------- #
# Fetch failure -> no trigger, observed price NOT invented                    #
# --------------------------------------------------------------------------- #
def test_price_fetch_failure_no_trigger_and_no_price_stored():
    def raising_price(symbol):
        raise RuntimeError("okx down")

    triggers, state = check_events(
        ["BTC-USDT"], _empty_state(),
        price_fn=raising_price,
        funding_fn=_funding_fn({"BTC-USDT": 0.0}),
        journal_ref_fn=_ref_fn({"BTC-USDT": 100.0}),
        now=NOW, config=_cfg(),
    )
    assert triggers == []
    assert "BTC-USDT" not in state["last_price"]  # never invent a price


def test_funding_fetch_failure_does_not_block_price_trigger():
    def raising_funding(symbol):
        raise RuntimeError("funding endpoint down")

    triggers, _ = check_events(
        ["BTC-USDT"], _empty_state(),
        price_fn=_price_fn({"BTC-USDT": 110.0}),
        funding_fn=raising_funding,
        journal_ref_fn=_ref_fn({"BTC-USDT": 100.0}),
        now=NOW, config=_cfg(),
    )
    assert len(triggers) == 1
    assert triggers[0]["metric"] == "price_move_pct"


# --------------------------------------------------------------------------- #
# Cooldown lifecycle                                                          #
# --------------------------------------------------------------------------- #
def test_cooldown_suppresses_re_trigger_within_window():
    state = _empty_state()
    # armed 1h ago; cooldown is 12h -> still cooling
    state["last_event_trigger_ts"]["BTC-USDT"] = (NOW - timedelta(hours=1)).isoformat()
    triggers, new_state = check_events(
        ["BTC-USDT"], state,
        price_fn=_price_fn({"BTC-USDT": 200.0}),  # would trigger big
        funding_fn=_funding_fn({"BTC-USDT": 0.05}),
        journal_ref_fn=_ref_fn({"BTC-USDT": 100.0}),
        now=NOW, config=_cfg(),
    )
    assert triggers == []  # suppressed
    # cooldown timestamp unchanged
    assert new_state["last_event_trigger_ts"]["BTC-USDT"] == state["last_event_trigger_ts"]["BTC-USDT"]


def test_cooldown_rearms_after_window_elapses():
    state = _empty_state()
    # armed 13h ago; cooldown 12h -> re-armed
    state["last_event_trigger_ts"]["BTC-USDT"] = (NOW - timedelta(hours=13)).isoformat()
    triggers, new_state = check_events(
        ["BTC-USDT"], state,
        price_fn=_price_fn({"BTC-USDT": 110.0}),
        funding_fn=_funding_fn({"BTC-USDT": 0.0}),
        journal_ref_fn=_ref_fn({"BTC-USDT": 100.0}),
        now=NOW, config=_cfg(),
    )
    assert len(triggers) == 1
    # cooldown re-armed to NOW
    assert new_state["last_event_trigger_ts"]["BTC-USDT"] != state["last_event_trigger_ts"]["BTC-USDT"]


def test_cooldown_is_per_symbol():
    state = _empty_state()
    state["last_event_trigger_ts"]["BTC-USDT"] = (NOW - timedelta(hours=1)).isoformat()
    triggers, _ = check_events(
        ["BTC-USDT", "ETH-USDT"], state,
        price_fn=_price_fn({"BTC-USDT": 200.0, "ETH-USDT": 110.0}),
        funding_fn=_funding_fn({"BTC-USDT": 0.0, "ETH-USDT": 0.0}),
        journal_ref_fn=_ref_fn({"BTC-USDT": 100.0, "ETH-USDT": 100.0}),
        now=NOW, config=_cfg(),
    )
    # BTC in cooldown -> suppressed; ETH fresh -> triggers
    assert [t["symbol"] for t in triggers] == ["ETH-USDT"]


def test_input_state_not_mutated():
    """check_events is pure: it must not mutate the passed-in state dict."""
    state = _empty_state()
    check_events(
        ["BTC-USDT"], state,
        price_fn=_price_fn({"BTC-USDT": 110.0}),
        funding_fn=_funding_fn({"BTC-USDT": 0.0}),
        journal_ref_fn=_ref_fn({"BTC-USDT": 100.0}),
        now=NOW, config=_cfg(),
    )
    assert state == _empty_state()  # untouched


def test_empty_symbols_no_triggers():
    triggers, new_state = check_events(
        [], _empty_state(),
        price_fn=_price_fn({}),
        funding_fn=_funding_fn({}),
        journal_ref_fn=_ref_fn({}),
        now=NOW, config=_cfg(),
    )
    assert triggers == []
    assert new_state == _empty_state()
