"""API tests for the committee observatory REST surface.

Socket-free: swarm runs are seeded under a tmp root (committee_routes.
_swarm_runs_root monkeypatched); the journal env points at a tmp file. Loopback
TestClient bypasses dev-mode auth (see tests/test_alpha_compare_api.py).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api_server
from src.api import committee_routes
from src.swarm.models import RunStatus, SwarmRun, SwarmTask, TaskStatus


def _client() -> TestClient:
    return TestClient(api_server.app, client=("127.0.0.1", 50000))


@pytest.fixture(autouse=True)
def _tmp_swarm_and_journal(tmp_path, monkeypatch):
    runs_root = tmp_path / "swarm-runs"
    runs_root.mkdir()
    monkeypatch.setattr(committee_routes, "_swarm_runs_root", lambda: runs_root)
    monkeypatch.setenv("VIBE_TRADING_COMMITTEE_JOURNAL", str(tmp_path / "journal.jsonl"))
    return runs_root


def _seed_run(runs_root: Path, run_id: str, *, target="BTC-USDT",
              status=RunStatus.completed, with_reports=True, corrupt_decision=False):
    rd = runs_root / run_id
    (rd / "artifacts" / "portfolio_manager").mkdir(parents=True)
    tasks = [
        SwarmTask(id="task-market", agent_id="market_analyst", prompt_template="",
                  status=TaskStatus.completed),
        SwarmTask(id="task-bull", agent_id="bull_researcher", prompt_template="",
                  status=TaskStatus.completed),
        SwarmTask(id="task-bull-r2", agent_id="bull_researcher", prompt_template="",
                  status=TaskStatus.completed),
        SwarmTask(id="task-bear", agent_id="bear_researcher", prompt_template="",
                  status=TaskStatus.completed),
        SwarmTask(id="task-decision", agent_id="portfolio_manager", prompt_template="",
                  status=TaskStatus.completed),
    ]
    run = SwarmRun(
        id=run_id, preset_name="crypto_committee", status=status,
        user_vars={"target": target, "timeframe": "72h swing"}, tasks=tasks,
        created_at="2026-07-18T20:00:58+00:00",
        completed_at="2026-07-18T20:18:29+00:00",
        total_input_tokens=704573, total_output_tokens=104805,
    )
    (rd / "run.json").write_text(run.model_dump_json(indent=2), encoding="utf-8")
    if with_reports:
        for agent in ("market_analyst", "bull_researcher", "bear_researcher",
                      "portfolio_manager"):
            (rd / "artifacts" / agent).mkdir(parents=True, exist_ok=True)
            (rd / "artifacts" / agent / "report.md").write_text(
                f"# {agent} report\n", encoding="utf-8")
    dec_path = rd / "artifacts" / "portfolio_manager" / "decision.portfolio_decision.json"
    if corrupt_decision:
        dec_path.write_text("{ not json", encoding="utf-8")
    else:
        dec_path.write_text(json.dumps({
            "rating": "Hold", "price_target": 65500.0, "stop_loss": 61800.0,
            "take_profit": 65500.0, "position_size_pct": 80.0,
        }), encoding="utf-8")
    return rd


def _seed_journal(run_id: str, symbol="BTC-USDT"):
    from src.committee import journal
    return journal.append_decision(
        symbol=symbol, rating="Hold", time_horizon="72h swing",
        run_id=run_id, decided_at="2026-07-18T20:18:29+00:00",
    )


def test_runs_list_newest_first_with_shape(_tmp_swarm_and_journal):
    _seed_run(_tmp_swarm_and_journal, "swarm-20260718-200058-aaa")
    _seed_run(_tmp_swarm_and_journal, "swarm-20260719-100000-bbb")
    rows = _client().get("/committee/runs").json()
    assert [r["run_id"] for r in rows] == [
        "swarm-20260719-100000-bbb", "swarm-20260718-200058-aaa"]
    r = rows[0]
    assert set(r) >= {"run_id", "created_at", "status", "target",
                      "wall_clock_s", "input_tokens", "output_tokens"}
    assert r["target"] == "BTC-USDT"
    assert r["status"] == "completed"
    assert r["input_tokens"] == 704573
    assert r["wall_clock_s"] == pytest.approx(1050.0, abs=1.0)


def test_runs_list_joins_journal_by_run_id(_tmp_swarm_and_journal):
    _seed_run(_tmp_swarm_and_journal, "swarm-run-x")
    entry = _seed_journal("swarm-run-x")
    row = next(r for r in _client().get("/committee/runs").json()
               if r["run_id"] == "swarm-run-x")
    assert row["decision_id"] == entry["id"]
    assert row["rating"] == "Hold"
    assert row["journal_status"] == "pending"


def test_runs_list_filters_by_symbol_and_status(_tmp_swarm_and_journal):
    _seed_run(_tmp_swarm_and_journal, "run-btc", target="BTC-USDT")
    _seed_run(_tmp_swarm_and_journal, "run-eth", target="ETH-USDT",
              status=RunStatus.failed)
    only_eth = _client().get("/committee/runs", params={"symbol": "ETH-USDT"}).json()
    assert [r["run_id"] for r in only_eth] == ["run-eth"]
    only_failed = _client().get("/committee/runs", params={"status": "failed"}).json()
    assert [r["run_id"] for r in only_failed] == ["run-eth"]


def test_runs_list_ignores_non_committee_presets(_tmp_swarm_and_journal):
    rd = _tmp_swarm_and_journal / "run-other"
    rd.mkdir()
    run = SwarmRun(id="run-other", preset_name="research_team",
                   status=RunStatus.completed, created_at="2026-07-18T20:00:00+00:00")
    (rd / "run.json").write_text(run.model_dump_json(), encoding="utf-8")
    assert _client().get("/committee/runs").json() == []


def test_runs_list_scans_past_hardcoded_cap_of_noncommittee_runs(_tmp_swarm_and_journal):
    """A committee run older than 200 non-committee runs must still surface.

    Regression for the /committee/runs endpoint scanning only the newest N
    total runs (across all presets) before filtering to crypto_committee: a
    committee run that isn't in the newest N overall was silently dropped
    even though the caller's limit was far from satisfied.
    """
    runs_root = _tmp_swarm_and_journal
    _seed_run(runs_root, "committee-old", target="BTC-USDT")

    base = datetime(2026, 7, 19, 0, 0, 0, tzinfo=timezone.utc)
    for i in range(250):
        rd = runs_root / f"other-{i:04d}"
        rd.mkdir()
        run = SwarmRun(
            id=f"other-{i:04d}", preset_name="research_team",
            status=RunStatus.completed,
            created_at=(base + timedelta(seconds=i)).isoformat(),
        )
        (rd / "run.json").write_text(run.model_dump_json(), encoding="utf-8")

    rows = _client().get("/committee/runs").json()
    assert [r["run_id"] for r in rows] == ["committee-old"]


def test_run_detail_shape(_tmp_swarm_and_journal):
    _seed_run(_tmp_swarm_and_journal, "run-detail")
    entry = _seed_journal("run-detail")
    body = _client().get("/committee/runs/run-detail").json()
    assert body["run"]["run_id"] == "run-detail"
    seats = {(s["agent_id"], s["round"]): s for s in body["seats"]}
    assert seats[("bull_researcher", 2)]["phase"] == "debate"
    assert seats[("market_analyst", 1)]["report_md"].startswith("# market_analyst")
    assert body["debate"]["rounds"] == 2
    assert body["decision"]["rating"] == "Hold"
    assert body["journal"]["reflection"] is None
    assert body["pnl"]["decision_id"] == entry["id"]


def test_run_detail_missing_report_marks_missing(_tmp_swarm_and_journal):
    rd = _seed_run(_tmp_swarm_and_journal, "run-nomd", with_reports=False)
    (rd / "artifacts" / "market_analyst").mkdir(parents=True, exist_ok=True)
    body = _client().get("/committee/runs/run-nomd").json()
    market = next(s for s in body["seats"] if s["agent_id"] == "market_analyst")
    assert market["report_md"] is None
    assert market["missing"] is True


def test_run_detail_corrupt_decision_reports_error_not_500(_tmp_swarm_and_journal):
    _seed_run(_tmp_swarm_and_journal, "run-baddec", corrupt_decision=True)
    resp = _client().get("/committee/runs/run-baddec")
    assert resp.status_code == 200
    assert "error" in resp.json()["decision"]


def test_run_detail_unknown_run_id_404(_tmp_swarm_and_journal):
    assert _client().get("/committee/runs/swarm-does-not-exist").status_code == 404


def test_run_detail_rejects_path_traversal(_tmp_swarm_and_journal):
    assert _client().get("/committee/runs/..%2f..").status_code in (400, 404)
