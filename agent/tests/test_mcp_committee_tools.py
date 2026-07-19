"""Gate + behavior tests for the committee READ tool group in mcp_server.

The tools are registered only when VIBE_MCP_COMMITTEE is truthy. Because
registration is an import-time side effect keyed on the env var, each test
sets the env then reloads the module and introspects / calls the tools via
the FastMCP instance (mcp.list_tools / mcp.call_tool), exactly the surface a
real MCP client drives.

Note: ``swarm_runs_root()`` (src/swarm/store.py) is a fixed path
(``<agent_root>/.swarm/runs``) with no env-var override -- unlike
``VIBE_TRADING_COMMITTEE_JOURNAL`` and ``VIBE_PAPER_ROOT``, which do honor
env overrides. ``mcp_server._get_swarm_store()`` resolves it via a *local*
`from src.swarm.store import swarm_runs_root` import inside the function
body, so monkeypatching the attribute on the `src.swarm.store` module (as
`committee_routes.py`'s own `_swarm_runs_root()` wrapper does for the REST
layer) redirects every call made after the patch is applied, regardless of
module reload order.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from datetime import datetime, timedelta, timezone

import pytest

COMMITTEE_TOOL_NAMES = {
    "committee_performance",
    "list_decisions",
    "get_decision",
    "list_committee_runs",
    "get_run_transcript",
    "paper_account",
}


def _reload_mcp(monkeypatch, **env):
    for key, val in env.items():
        if val is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, val)
    import mcp_server
    return importlib.reload(mcp_server)


def _tool_names(mod) -> set[str]:
    tools = asyncio.run(mod.mcp.list_tools())
    return {t.name for t in tools}


def _call(mod, name, **args) -> dict:
    result = asyncio.run(mod.mcp.call_tool(name, args))
    return json.loads(result.content[0].text)


def _seed_journal(path, *, run_id, symbol="BTC-USDT", decided_at=None,
                  rating="Buy", status="resolved", horizons=None):
    decided_at = decided_at or datetime.now(timezone.utc).isoformat()
    horizons = horizons if horizons is not None else {
        "24h": {"raw_return": 0.05, "benchmark_return": 0.0, "alpha": 0.05,
                "mark_price": 105.0, "direction_correct": True,
                "resolved_at": decided_at},
    }
    entry = {
        "id": "dec_" + run_id[-8:], "decided_at": decided_at, "symbol": symbol,
        "rating": rating, "time_horizon": "24h", "primary_horizon": "24h",
        "price_target": 110.0, "run_id": run_id, "status": status,
        "ref_price": 100.0, "horizons": horizons,
        "reflection": "Momentum held.", "reflected_at": decided_at,
    }
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
    return entry


def _seed_committee_run(swarm_root, *, run_id, symbol="BTC-USDT", status="completed",
                        report="## Bull\nlong.", decision=None):
    rd = swarm_root / run_id
    (rd / "artifacts" / "bull_researcher").mkdir(parents=True, exist_ok=True)
    (rd / "artifacts" / "portfolio_manager").mkdir(parents=True, exist_ok=True)
    (rd / "artifacts" / "bull_researcher" / "report.md").write_text(report, encoding="utf-8")
    if decision is not None:
        (rd / "artifacts" / "portfolio_manager" / "decision.portfolio_decision.json").write_text(
            json.dumps(decision), encoding="utf-8")
    now = datetime.now(timezone.utc)
    # SwarmStore.reconcile_run derives the *real* run status from task
    # statuses when every task is terminal (all-completed -> "completed"),
    # overriding a stale run.json "status" -- see store.py's
    # _recover_terminal. So a genuinely non-terminal "running" run must
    # leave its tasks non-terminal too, or the join/status-filter test would
    # see it promoted to "completed" underneath it.
    task_status = "completed" if status == "completed" else "in_progress"
    task_completed_at = now.isoformat() if task_status == "completed" else None
    run_json = {
        "id": run_id, "preset_name": "crypto_committee", "status": status,
        "user_vars": {"target": symbol, "timeframe": "24h"},
        "created_at": now.isoformat(),
        "completed_at": (now + timedelta(seconds=90)).isoformat() if status == "completed" else None,
        "total_input_tokens": 1000, "total_output_tokens": 500,
        "tasks": [
            {"id": "task-bull-r1", "agent_id": "bull_researcher", "prompt_template": "analyze {target}",
             "status": task_status, "summary": "bull", "worker_iterations": 1, "error": None,
             "started_at": now.isoformat(), "completed_at": task_completed_at,
             "depends_on": [], "blocked_by": []},
            {"id": "task-decision", "agent_id": "portfolio_manager", "prompt_template": "decide {target}",
             "status": task_status, "summary": "pm", "worker_iterations": 1, "error": None,
             "started_at": now.isoformat(), "completed_at": task_completed_at,
             "depends_on": ["task-bull-r1"], "blocked_by": []},
        ],
    }
    (rd / "run.json").write_text(json.dumps(run_json), encoding="utf-8")
    return run_id


@pytest.fixture
def committee_env(tmp_path, monkeypatch):
    journal = tmp_path / "journal.jsonl"
    swarm_root = tmp_path / "runs"
    paper_root = tmp_path / "paper"
    swarm_root.mkdir()
    paper_root.mkdir()
    monkeypatch.setenv("VIBE_TRADING_COMMITTEE_JOURNAL", str(journal))
    monkeypatch.setenv("VIBE_PAPER_ROOT", str(paper_root))
    # swarm_runs_root() has no env override; redirect it directly so
    # mcp_server._get_swarm_store()'s local `from src.swarm.store import
    # swarm_runs_root` import picks up the patched attribute on every call.
    import src.swarm.store as swarm_store_module
    monkeypatch.setattr(swarm_store_module, "swarm_runs_root", lambda: swarm_root)
    return {"journal": journal, "swarm_root": swarm_root, "paper_root": paper_root}


def test_committee_tools_absent_when_gate_off(monkeypatch):
    mod = _reload_mcp(monkeypatch, VIBE_MCP_COMMITTEE=None, VIBE_MCP_ALLOW_TRIGGER=None)
    names = _tool_names(mod)
    assert not (COMMITTEE_TOOL_NAMES & names), f"committee tools leaked with gate off: {COMMITTEE_TOOL_NAMES & names}"
    assert "run_committee" not in names
    assert {"analyze_options", "get_market_data", "list_skills"} <= names


def test_committee_read_tools_present_with_gate_on(monkeypatch):
    mod = _reload_mcp(monkeypatch, VIBE_MCP_COMMITTEE="1", VIBE_MCP_ALLOW_TRIGGER=None)
    names = _tool_names(mod)
    assert COMMITTEE_TOOL_NAMES <= names
    assert "run_committee" not in names  # gate 2 still off


def test_list_and_get_decision(monkeypatch, committee_env):
    _seed_journal(committee_env["journal"], run_id="swarm-aaa11111", symbol="BTC-USDT")
    _seed_journal(committee_env["journal"], run_id="swarm-bbb22222", symbol="ETH-USDT")
    mod = _reload_mcp(monkeypatch, VIBE_MCP_COMMITTEE="1")

    payload = _call(mod, "list_decisions", limit=10)
    assert payload["status"] == "ok"
    ids = [d["id"] for d in payload["decisions"]]
    assert ids == ["dec_bbb22222", "dec_aaa11111"]  # newest-first

    filtered = _call(mod, "list_decisions", limit=10, symbol="ETH-USDT")
    assert [d["symbol"] for d in filtered["decisions"]] == ["ETH-USDT"]

    got = _call(mod, "get_decision", decision_id="dec_aaa11111")
    assert got["status"] == "ok"
    assert got["decision"]["horizons"]["24h"]["alpha"] == 0.05
    assert got["decision"]["reflection"] == "Momentum held."

    missing = _call(mod, "get_decision", decision_id="dec_nope")
    assert missing["status"] == "error" and missing["error_type"] == "not_found"


def test_list_committee_runs_join_and_status_filter(monkeypatch, committee_env):
    _seed_committee_run(committee_env["swarm_root"], run_id="swarm-aaa11111", status="completed")
    _seed_committee_run(committee_env["swarm_root"], run_id="swarm-ccc33333", status="running")
    _seed_journal(committee_env["journal"], run_id="swarm-aaa11111", rating="Buy")
    mod = _reload_mcp(monkeypatch, VIBE_MCP_COMMITTEE="1")

    payload = _call(mod, "list_committee_runs", limit=10)
    assert payload["status"] == "ok"
    by_id = {r["run_id"]: r for r in payload["runs"]}
    assert by_id["swarm-aaa11111"]["rating"] == "Buy"
    assert by_id["swarm-aaa11111"]["decision_id"] == "dec_aaa11111"
    assert by_id["swarm-ccc33333"]["rating"] is None  # unjournaled
    assert by_id["swarm-aaa11111"]["target"] == "BTC-USDT"

    running = _call(mod, "list_committee_runs", limit=10, status="running")
    assert [r["run_id"] for r in running["runs"]] == ["swarm-ccc33333"]


def test_list_committee_runs_scans_past_hardcoded_cap_of_noncommittee_runs(
        monkeypatch, committee_env):
    """A committee run older than 200 non-committee runs must still surface.

    Regression for list_committee_runs (and, by the same code path,
    committee_performance) scanning only the newest N total runs (across all
    presets) before filtering to crypto_committee: a committee run that
    isn't in the newest N overall was silently dropped even though the
    caller's `limit` was far from satisfied.
    """
    from src.swarm.models import RunStatus, SwarmRun

    swarm_root = committee_env["swarm_root"]
    base = datetime(2026, 7, 19, 0, 0, 0, tzinfo=timezone.utc)

    # The committee run is older than every synthetic non-committee run
    # seeded below, so the old hardcoded 200-run cap would bury it.
    old_run = SwarmRun(
        id="committee-old", preset_name="crypto_committee",
        status=RunStatus.completed,
        user_vars={"target": "BTC-USDT"},
        created_at=(base - timedelta(hours=1)).isoformat(),
    )
    old_dir = swarm_root / "committee-old"
    old_dir.mkdir()
    (old_dir / "run.json").write_text(old_run.model_dump_json(), encoding="utf-8")

    for i in range(250):
        rd = swarm_root / f"other-{i:04d}"
        rd.mkdir()
        run = SwarmRun(
            id=f"other-{i:04d}", preset_name="research_team",
            status=RunStatus.completed,
            created_at=(base + timedelta(seconds=i)).isoformat(),
        )
        (rd / "run.json").write_text(run.model_dump_json(), encoding="utf-8")

    mod = _reload_mcp(monkeypatch, VIBE_MCP_COMMITTEE="1")
    payload = _call(mod, "list_committee_runs", limit=10)
    assert payload["status"] == "ok"
    assert [r["run_id"] for r in payload["runs"]] == ["committee-old"]


def test_get_run_transcript(monkeypatch, committee_env):
    _seed_committee_run(
        committee_env["swarm_root"], run_id="swarm-aaa11111",
        report="## Bull case\nGo long.",
        decision={"rating": "Buy", "price_target": 110.0},
    )
    mod = _reload_mcp(monkeypatch, VIBE_MCP_COMMITTEE="1")

    payload = _call(mod, "get_run_transcript", run_id="swarm-aaa11111")
    assert payload["status"] == "ok"
    # Run fields are nested under "run" so the run's own status can't clobber
    # the envelope's "status": "ok" — assert the contents, not just the shape.
    assert payload["run"] == {
        "run_id": "swarm-aaa11111", "status": "completed", "target": "BTC-USDT",
    }
    seats = {s["agent_id"]: s for s in payload["seats"]}
    assert "Bull case" in seats["bull_researcher"]["report_md"]
    assert seats["bull_researcher"]["round"] == 1
    assert payload["decision"]["rating"] == "Buy"
    assert payload["debate"]["rounds"] == 1

    only = _call(mod, "get_run_transcript", run_id="swarm-aaa11111", seat="bull_researcher")
    assert [s["agent_id"] for s in only["seats"]] == ["bull_researcher"]

    missing = _call(mod, "get_run_transcript", run_id="swarm-doesnotexist")
    assert missing["status"] == "error" and missing["error_type"] == "not_found"


def test_get_run_transcript_missing_artifact_marked_not_fabricated(monkeypatch, committee_env):
    rid = "swarm-aaa11111"
    _seed_committee_run(committee_env["swarm_root"], run_id=rid, decision=None)
    (committee_env["swarm_root"] / rid / "artifacts" / "bull_researcher" / "report.md").unlink()
    mod = _reload_mcp(monkeypatch, VIBE_MCP_COMMITTEE="1")

    payload = _call(mod, "get_run_transcript", run_id=rid)
    seats = {s["agent_id"]: s for s in payload["seats"]}
    assert seats["bull_researcher"]["report_md"] is None
    assert seats["bull_researcher"]["missing"] is True
    assert payload["decision"] is None


def test_committee_performance_single_symbol_caveat(monkeypatch, committee_env):
    now = datetime.now(timezone.utc)
    _seed_journal(committee_env["journal"], run_id="swarm-aaa11111", symbol="BTC-USDT",
                  decided_at=now.isoformat(), rating="Buy")
    _seed_committee_run(committee_env["swarm_root"], run_id="swarm-aaa11111")
    mod = _reload_mcp(monkeypatch, VIBE_MCP_COMMITTEE="1")

    payload = _call(mod, "committee_performance")
    assert payload["status"] == "ok"
    assert "definitionally ~0" in payload["alpha_caveat"]
    h = payload["horizons"]["24h"]
    assert h["count"] == 1
    assert h["direction_correct_rate"] == 1.0
    assert h["mean_alpha"] == 0.05
    assert payload["runs"]["count"] == 1
    assert payload["runs"]["avg_input_tokens"] == 1000


def test_committee_performance_window_filter(monkeypatch, committee_env):
    now = datetime.now(timezone.utc)
    _seed_journal(committee_env["journal"], run_id="swarm-old00000", symbol="BTC-USDT",
                  decided_at=(now - timedelta(hours=200)).isoformat())
    _seed_journal(committee_env["journal"], run_id="swarm-new11111", symbol="BTC-USDT",
                  decided_at=(now - timedelta(hours=1)).isoformat())
    mod = _reload_mcp(monkeypatch, VIBE_MCP_COMMITTEE="1")

    payload = _call(mod, "committee_performance", window_hours=24)
    assert payload["horizons"]["24h"]["count"] == 1  # only the recent decision


def test_paper_account(monkeypatch, committee_env):
    from src.paper.store import PaperStore
    store = PaperStore(committee_env["paper_root"])
    store.create_account(100_000.0, {"fee_bps": 10})
    store.append_ledger({"ts": "2026-07-19T00:00:00Z", "symbol": "BTC-USDT", "side": "buy",
                         "qty": 1.0, "fill_price": 100.0, "fee_paid": 0.1,
                         "realized_pnl": None, "order_type": "market"})
    mod = _reload_mcp(monkeypatch, VIBE_MCP_COMMITTEE="1")

    payload = _call(mod, "paper_account")
    assert payload["status"] == "ok"
    assert payload["equity"]["cash"] == 100_000.0
    assert payload["ledger_tail"][-1]["symbol"] == "BTC-USDT"
