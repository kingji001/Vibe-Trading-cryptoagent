"""Loop 2 (Completion) anchor regression — replays swarm-20260728-180010-cd9f563d.

The research_manager seat burned its 15-iteration budget retrying
``submit_decision`` against a ``strategic_actions`` schema error, produced no
``report.md`` and no ``decision.research_plan.json``, and its 821-byte
mid-thought scratch note was marked ``status: "completed"`` and handed to the
trader and the PM as the binding research plan.

Four things must hold (spec §4.3):
  1. a worker that hits the iteration limit does not report ``completed``;
  2. the triggering validation error survives in the task's artifacts;
  3. every downstream consumer's rendered prompt carries an explicit
     incomplete-upstream marker (fail-visible, not fail-closed — the run
     continues rather than burning 0.9M tokens on an abort);
  4. the PM gate rejects a sized directional decision on a degraded run and
     still accepts Hold.

Every assertion below except the Hold guard fails on pre-fix code.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import patch

import src.providers.backoff as backoff_mod
import src.swarm.runtime as rt
import src.swarm.worker as worker_mod
from src.providers.chat import LLMResponse, ToolCallRequest
from src.swarm.models import SwarmAgentSpec, SwarmRun, SwarmTask, TaskStatus
from src.swarm.store import SwarmStore
from src.swarm.worker import run_worker
from src.tools.committee_decision_tool import SubmitDecisionTool

# The real 821-byte scratch note pattern: long enough to survive
# _best_summary and _classify_deliverable, ending mid-thought.
SCRATCH_NOTE = (
    "I have the verified live snapshot. Critical new facts neither side had at "
    "debate time: spot 63,717.20, the 24h low touched 62,741.10 and S2 HELD, "
    "funding +0.00280%/8h is slightly POSITIVE, open interest 3.17M contracts. "
    "These live prints materially affect the ruling. Proceeding to score and submit."
)

# Verbatim from the incident's messages.json, three consecutive times.
VALIDATION_ERROR = json.dumps(
    {
        "status": "error",
        "error": "Decision payload failed validation. Fix these fields "
        "and call submit_decision again.",
        "issues": [
            {"field": "strategic_actions", "problem": "Input should be a valid list"}
        ],
    },
    ensure_ascii=False,
)

SNAPSHOT_OK = json.dumps({"status": "ok", "mark_price": 63717.2})

DOWNSTREAM_REPORT = (
    "# BTC-USDT — Trader Proposal\n\n"
    "Spot 63,717.2 verified. Entry 63,700, stop 62,300, target 64,773.\n"
    "**FINAL TRANSACTION PROPOSAL: HOLD** pending a complete research plan."
)


class _ScriptedRegistry:
    """Registry that replays the incident's tool results."""

    def get_definitions(self) -> list[dict]:
        return [{"type": "function", "function": {"name": "submit_decision"}}]

    def execute(self, name: str, args: dict) -> str:
        if name == "submit_decision":
            return VALIDATION_ERROR
        return SNAPSHOT_OK

    def get(self, name: str):
        return None


class _IncidentLLM:
    """One snapshot call, then submit_decision forever — never terminates.

    The first, successful data-tool call is load-bearing: it is what made
    ``_classify_deliverable`` return ``None`` for the real research_manager
    (a data agent with no report.md), which is why the seat took the
    ``completed`` branch rather than ``incomplete``.
    """

    def __init__(self, model_name: str | None = None, **kwargs) -> None:
        self.model_name = model_name
        self._calls = 0

    def __call__(self, *args, **kwargs) -> "_IncidentLLM":
        return _IncidentLLM(**kwargs)

    def stream_chat(self, messages, tools=None, on_text_chunk=None, timeout=None):
        if self.model_name == "downstream-model":
            return LLMResponse(content=DOWNSTREAM_REPORT)
        self._calls += 1
        tool = "get_verified_crypto_snapshot" if self._calls == 1 else "submit_decision"
        return LLMResponse(
            content=SCRATCH_NOTE,
            tool_calls=[
                ToolCallRequest(
                    id=f"tc{self._calls}", name=tool, arguments={"symbol": "BTC-USDT"}
                )
            ],
        )


def _judge_spec() -> SwarmAgentSpec:
    return SwarmAgentSpec(
        id="research_manager",
        role="Research Manager (debate judge)",
        system_prompt="Judge the debate.\n\n{upstream_context}",
        tools=["write_file", "submit_decision", "get_verified_crypto_snapshot"],
        skills=[],
        max_iterations=6,
        timeout_seconds=60,
        model_name="judge-model",
        max_retries=0,
    )


def _trader_spec() -> SwarmAgentSpec:
    return SwarmAgentSpec(
        id="trader",
        role="Trader",
        system_prompt="Convert the plan.\n\n{upstream_context}",
        tools=[],
        skills=[],
        max_iterations=4,
        timeout_seconds=60,
        model_name="downstream-model",
        max_retries=0,
    )


# --------------------------------------------------------------------------
# 1 + 2: the seat that burned its budget is not "completed", and the schema
#        error it died on is diagnosable from the run archive.
# --------------------------------------------------------------------------


def test_iteration_limit_worker_is_degraded_not_completed(monkeypatch, tmp_path):
    monkeypatch.setattr(backoff_mod.time, "sleep", lambda *_: None)
    task = SwarmTask(
        id="task-research-plan",
        agent_id="research_manager",
        prompt_template="Judge the debate.",
    )
    with (
        patch.object(
            worker_mod, "build_swarm_registry", lambda *a, **k: _ScriptedRegistry()
        ),
        patch.object(worker_mod, "ChatLLM", _IncidentLLM()),
    ):
        result = run_worker(
            agent_spec=_judge_spec(),
            task=task,
            upstream_summaries={},
            user_vars={},
            run_dir=tmp_path,
        )

    assert result.status != "completed", (
        "a seat that burned its iteration budget without a deliverable was "
        f"reported {result.status!r} — this is the 07-28 incident"
    )
    assert result.status == "degraded"


def test_triggering_validation_error_is_persisted(monkeypatch, tmp_path):
    monkeypatch.setattr(backoff_mod.time, "sleep", lambda *_: None)
    task = SwarmTask(
        id="task-research-plan",
        agent_id="research_manager",
        prompt_template="Judge the debate.",
    )
    with (
        patch.object(
            worker_mod, "build_swarm_registry", lambda *a, **k: _ScriptedRegistry()
        ),
        patch.object(worker_mod, "ChatLLM", _IncidentLLM()),
    ):
        run_worker(
            agent_spec=_judge_spec(),
            task=task,
            upstream_summaries={},
            user_vars={},
            run_dir=tmp_path,
        )

    ctx_path = tmp_path / "artifacts" / "research_manager" / "failure_context.json"
    assert ctx_path.is_file(), "no structured failure context written"
    ctx = json.loads(ctx_path.read_text(encoding="utf-8"))
    assert ctx["tool_errors"], ctx
    fields = {
        issue["field"]
        for err in ctx["tool_errors"]
        for issue in err.get("issues", [])
    }
    assert "strategic_actions" in fields, ctx


# --------------------------------------------------------------------------
# 3: fail-visible — the run continues, and every consumer sees the marker.
# --------------------------------------------------------------------------


def test_degraded_upstream_marker_reaches_downstream_prompt(monkeypatch, tmp_path):
    monkeypatch.setattr(backoff_mod.time, "sleep", lambda *_: None)
    monkeypatch.setenv("VIBE_SWARM_PERSIST_TRANSCRIPTS", "1")

    store = SwarmStore(base_dir=tmp_path)
    runtime = rt.SwarmRuntime(store=store)
    run = SwarmRun(
        # NOT "crypto_committee": that preset is in grounding.IDENTITY_ANCHOR_VARS
        # and would fail instrument resolution before any worker ran.
        id="r-loop2",
        preset_name="demo",
        created_at="2026-07-28T18:00:10+00:00",
        agents=[_judge_spec(), _trader_spec()],
        tasks=[
            SwarmTask(
                id="task-research-plan",
                agent_id="research_manager",
                prompt_template="Judge the debate.",
            ),
            SwarmTask(
                id="task-trader",
                agent_id="trader",
                prompt_template="Convert the plan.",
                depends_on=["task-research-plan"],
                blocked_by=["task-research-plan"],
                input_from={"research_plan": "task-research-plan"},
            ),
        ],
    )
    store.create_run(run)

    with (
        patch.object(
            worker_mod, "build_swarm_registry", lambda *a, **k: _ScriptedRegistry()
        ),
        patch.object(worker_mod, "ChatLLM", _IncidentLLM()),
    ):
        runtime._execute_run(run, threading.Event())

    reloaded = store.load_run(run.id)
    by_id = {t.id: t for t in reloaded.tasks}

    assert by_id["task-research-plan"].status != TaskStatus.completed
    assert by_id["task-research-plan"].status == TaskStatus.degraded
    # fail-visible, not fail-closed: the trader still runs.
    assert by_id["task-trader"].status == TaskStatus.completed

    transcript = json.loads(
        (tmp_path / run.id / "artifacts" / "trader" / "messages.json").read_text(
            encoding="utf-8"
        )
    )
    system_prompt = transcript[0]["content"]
    assert "INCOMPLETE UPSTREAM" in system_prompt, system_prompt


# --------------------------------------------------------------------------
# 4: the PM may not size a directional bet on a degraded run; Hold stays open.
# --------------------------------------------------------------------------

_LONG = "x" * 120

_SIZED_BUY = {
    "rating": "Buy",
    "executive_summary": _LONG,
    "investment_thesis": _LONG * 2,
    "time_horizon": "72h swing",
    "price_target": 64773,
    "stop_loss": 62300,
    "take_profit": 64773,
    "position_size_pct": 34.0,
}

_HOLD = {
    "rating": "Hold",
    "executive_summary": _LONG,
    "investment_thesis": _LONG * 2,
    "time_horizon": "72h swing",
}


def _degraded_artifact_dir(tmp_path: Path) -> Path:
    artifact_dir = tmp_path / "artifacts" / "portfolio_manager"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "upstream_degradation.json").write_text(
        json.dumps(
            {
                "degraded_upstreams": [
                    {
                        "context_key": "research_plan",
                        "reason": "upstream task task-research-plan: hit iteration "
                        "limit (15) without completing the deliverable",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return artifact_dir


def test_pm_gate_rejects_sized_directional_on_degraded_run(tmp_path):
    artifact_dir = _degraded_artifact_dir(tmp_path)
    result = json.loads(
        SubmitDecisionTool().execute(
            schema="portfolio_decision",
            payload=_SIZED_BUY,
            run_dir=str(artifact_dir),
        )
    )
    assert result["status"] == "error", result
    assert "degraded" in result["error"].lower(), result
    assert not (artifact_dir / "decision.portfolio_decision.json").exists()


def test_pm_gate_accepts_hold_on_degraded_run(tmp_path):
    artifact_dir = _degraded_artifact_dir(tmp_path)
    result = json.loads(
        SubmitDecisionTool().execute(
            schema="portfolio_decision",
            payload=_HOLD,
            run_dir=str(artifact_dir),
        )
    )
    assert result["status"] == "ok", result
    assert (artifact_dir / "decision.portfolio_decision.json").exists()
