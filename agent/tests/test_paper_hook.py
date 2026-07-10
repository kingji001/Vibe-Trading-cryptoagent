"""Tests for the post-journal-append paper execution hook (Task 5).

``maybe_execute_paper`` is the seam between the decision journal's
``action="append"`` success path and the paper-trading translator/broker
(Tasks 2-4). Two contracts under test, both binding per the t5 brief:

  1. Kill switch: ``VIBE_PAPER_ENABLED`` falsy ("0"/"false"/"", unset means
     ENABLED) -> returns ``None`` immediately, no broker/store/import touched.
  2. Failure isolation: ANY exception raised anywhere downstream (translator,
     broker, price fetch) is caught and returned as ``{"error": str}`` —
     ``maybe_execute_paper`` itself never raises.

Socket-disabled: every enabled-path test monkeypatches
``src.paper.translator.execute_decision`` directly, except the one real
end-to-end test, which drives a Hold-with-no-position decision — a pure
no-op in the real translator that never reaches the price_fn (see
``translator._apply_hold``) — so it exercises the genuine
PaperBroker/PaperStore wiring with no network call and nothing to stub.
"""

from __future__ import annotations

import pytest

from src.paper.hook import maybe_execute_paper

pytestmark = pytest.mark.usefixtures("_paper_root")


@pytest.fixture()
def _paper_root(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBE_PAPER_ROOT", str(tmp_path))
    monkeypatch.setenv("VIBE_PAPER_START_CASH", "100000")
    monkeypatch.setenv("VIBE_PAPER_SLIPPAGE_BPS", "5")
    monkeypatch.setenv("VIBE_PAPER_FEE_BPS", "10")
    monkeypatch.setenv("VIBE_PAPER_MAX_POSITIONS", "3")
    monkeypatch.setenv("VIBE_PAPER_MAX_SYMBOL_PCT", "25")
    monkeypatch.setenv("VIBE_PAPER_DEFAULT_SIZE_PCT", "10")
    monkeypatch.setenv("VIBE_PAPER_DEFAULT_STOP_PCT", "8")


def _entry(**overrides) -> dict:
    base = {
        "id": "dec-1",
        "symbol": "ETH-USDT",
        "rating": "Hold",
        "time_horizon": "72h swing",
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# Kill switch: falsy -> None fast                                             #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value", ["0", "false", "False", ""])
def test_disabled_returns_none_fast(monkeypatch, value):
    monkeypatch.setenv("VIBE_PAPER_ENABLED", value)
    # If it weren't fast, this would try to import/construct a broker and
    # potentially touch the (non-existent, unmonkeypatched) network price_fn.
    assert maybe_execute_paper(_entry()) is None


def test_unset_env_defaults_to_enabled(monkeypatch):
    monkeypatch.delenv("VIBE_PAPER_ENABLED", raising=False)
    monkeypatch.setattr(
        "src.paper.translator.execute_decision",
        lambda entry, broker: {"decision_id": entry["id"], "actions": [], "skipped": None},
    )
    result = maybe_execute_paper(_entry())
    assert result == {"decision_id": "dec-1", "actions": [], "skipped": None}


def test_enabled_1_calls_through(monkeypatch):
    monkeypatch.setenv("VIBE_PAPER_ENABLED", "1")
    calls = []

    def fake_execute(entry, broker):
        calls.append(entry["id"])
        return {"decision_id": entry["id"], "actions": [], "skipped": None}

    monkeypatch.setattr("src.paper.translator.execute_decision", fake_execute)
    result = maybe_execute_paper(_entry())
    assert calls == ["dec-1"]
    assert result["decision_id"] == "dec-1"


# --------------------------------------------------------------------------- #
# Failure isolation: any downstream exception -> {"error": ...}, never raises #
# --------------------------------------------------------------------------- #
def test_execute_decision_exception_is_caught_and_reported(monkeypatch):
    monkeypatch.setenv("VIBE_PAPER_ENABLED", "1")

    def boom(entry, broker):
        raise RuntimeError("translator exploded")

    monkeypatch.setattr("src.paper.translator.execute_decision", boom)
    result = maybe_execute_paper(_entry())
    assert result == {"error": "translator exploded"}


def test_broker_construction_exception_is_caught_and_reported(monkeypatch):
    monkeypatch.setenv("VIBE_PAPER_ENABLED", "1")

    def boom(*args, **kwargs):
        raise RuntimeError("broker init exploded")

    monkeypatch.setattr("src.paper.broker.PaperBroker.__init__", boom)
    result = maybe_execute_paper(_entry())
    assert result == {"error": "broker init exploded"}


def test_never_raises_regardless_of_exception_type(monkeypatch):
    monkeypatch.setenv("VIBE_PAPER_ENABLED", "1")

    def boom(entry, broker):
        raise KeyError("some_missing_key")

    monkeypatch.setattr("src.paper.translator.execute_decision", boom)
    result = maybe_execute_paper(_entry())
    assert "error" in result


# --------------------------------------------------------------------------- #
# Real end-to-end success path (no monkeypatched execute_decision) — proves   #
# the default PaperBroker/PaperStore wiring actually works, with the network  #
# price fetch stubbed at the snapshot-module boundary (socket-disabled).      #
# --------------------------------------------------------------------------- #
def test_real_end_to_end_hold_no_position_is_noop(monkeypatch):
    monkeypatch.setenv("VIBE_PAPER_ENABLED", "1")
    result = maybe_execute_paper(_entry(rating="Hold"))
    # Hold with no existing position and no typed stop/TP is a pure no-op —
    # exercises the real translator/broker/store without any network call.
    assert result == {"decision_id": "dec-1", "actions": [], "skipped": None}
