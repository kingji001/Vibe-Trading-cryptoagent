"""Tests for the ``paper_tick`` agent tool (Task 5).

Thin wrapper around ``src.paper.tick.run_tick`` so the scheduled
``paper-trading-tick`` job (``src/api/scheduled_routes.py``) can drive the
daily tick through the ordinary prompt -> tool-call executor path — same
shape as the Phase 6 reflection job calling ``decision_journal``. Not a
committee-seat tool (never registered in crypto_committee.yaml).
"""

from __future__ import annotations

import json

from src.tools.paper_tick_tool import PaperTickTool


def test_tool_metadata() -> None:
    tool = PaperTickTool()
    assert tool.name == "paper_tick"
    assert tool.parameters["properties"] == {}
    assert tool.parameters["required"] == []


def test_tool_is_auto_discovered() -> None:
    from src.tools import build_registry

    registry = build_registry()
    assert "paper_tick" in registry.tool_names


def test_execute_summarizes_run_tick_result(monkeypatch):
    fake_result = {
        "conditional_fills": [{"symbol": "ETH-USDT"}],
        "equity_snapshot": {
            "equity": 105_000.0,
            "stale_positions": 1,
            "date": "2026-07-11",
            "already_recorded": False,
        },
        "errors": [{"symbol": "SOL-USDT", "error": "no bar"}],
    }
    monkeypatch.setattr(
        "src.paper.tick.run_tick", lambda: fake_result
    )
    tool = PaperTickTool()
    out = json.loads(tool.execute())
    assert out["status"] == "ok"
    assert out["fills"] == 1
    assert out["equity"] == 105_000.0
    assert out["stale_positions"] == 1
    assert out["date"] == "2026-07-11"
    assert out["already_recorded"] is False
    assert out["errors"] == [{"symbol": "SOL-USDT", "error": "no bar"}]


def test_execute_no_params_required(monkeypatch):
    """The tool must run with zero arguments (the scheduled job calls it bare)."""
    monkeypatch.setattr(
        "src.paper.tick.run_tick",
        lambda: {"conditional_fills": [], "equity_snapshot": {}, "errors": []},
    )
    tool = PaperTickTool()
    out = json.loads(tool.execute())
    assert out["status"] == "ok"
    assert out["fills"] == 0


def test_execute_disabled_returns_disabled_marker(monkeypatch):
    """Final review cleanup 1: the tool surfaces run_tick's disabled marker."""
    monkeypatch.setattr(
        "src.paper.tick.run_tick",
        lambda: {"conditional_fills": [], "equity_snapshot": {}, "errors": [],
                 "notes": [], "retried_decisions": [], "disabled": True},
    )
    tool = PaperTickTool()
    out = json.loads(tool.execute())
    assert out["status"] == "disabled"


def test_execute_catches_run_tick_exception(monkeypatch):
    def boom():
        raise RuntimeError("tick blew up")

    monkeypatch.setattr("src.paper.tick.run_tick", boom)
    tool = PaperTickTool()
    out = json.loads(tool.execute())
    assert out["status"] == "error"
    assert "tick blew up" in out["error"]
