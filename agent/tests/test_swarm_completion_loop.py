"""Loop 2 (Completion) unit coverage — degraded status, markers, gate.

A seat that dies mid-work had its partial scratch output marked ``completed``
and propagated downstream as a binding deliverable
(swarm-20260728-180010-cd9f563d, spec §4.1). These tests pin the pieces of
the fix; the end-to-end replay lives in
``test_swarm_completion_loop_anchor.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import src.providers.backoff as backoff_mod
import src.swarm.worker as worker_mod
from src.providers.chat import LLMResponse, ToolCallRequest
from src.swarm.models import (
    SwarmAgentSpec,
    SwarmTask,
    TaskStatus,
    WorkerResult,
    WorkerStatus,
)
from src.swarm.worker import _recurring_tool_errors, run_worker


# --- Task 1: enum ----------------------------------------------------------


def test_task_status_has_degraded_and_round_trips():
    assert TaskStatus.degraded.value == "degraded"
    task = SwarmTask(
        id="t1", agent_id="a", prompt_template="x", status=TaskStatus.degraded
    )
    revived = SwarmTask.model_validate_json(task.model_dump_json())
    assert revived.status is TaskStatus.degraded


def test_worker_status_has_degraded():
    assert WorkerStatus.degraded.value == "degraded"
    assert WorkerResult(status="degraded", summary="x").status is WorkerStatus.degraded


LONG_NOTE = (
    "Verified snapshot in hand: spot 63,717.20, 24h low 62,741.10 held, funding "
    "+0.00280%/8h positive, OI 3.17M contracts. These live prints materially "
    "affect the ruling. Proceeding to score and submit."
)

VALIDATION_ERROR = json.dumps(
    {
        "status": "error",
        "error": "Decision payload failed validation.",
        "issues": [
            {"field": "strategic_actions", "problem": "Input should be a valid list"}
        ],
    }
)


# --- Task 2/3: worker terminal path ---------------------------------------


class _ErrorRegistry:
    def get_definitions(self) -> list[dict]:
        return [{"type": "function", "function": {"name": "submit_decision"}}]

    def execute(self, name: str, args: dict) -> str:
        return VALIDATION_ERROR

    def get(self, name: str):
        return None


class _NeverStopsLLM:
    """Always returns a tool call, so the worker never terminates itself."""

    def __init__(self, model_name: str | None = None, **kwargs) -> None:
        self.model_name = model_name
        self._n = 0

    def __call__(self, *args, **kwargs) -> "_NeverStopsLLM":
        return _NeverStopsLLM(**kwargs)

    def stream_chat(self, messages, tools=None, on_text_chunk=None, timeout=None):
        self._n += 1
        return LLMResponse(
            content=LONG_NOTE,
            tool_calls=[
                ToolCallRequest(
                    id=f"tc{self._n}", name="submit_decision", arguments={"schema": "x"}
                )
            ],
        )


def _spec(**overrides) -> SwarmAgentSpec:
    base = dict(
        id="research_manager",
        role="Research Manager",
        system_prompt="Judge.\n\n{upstream_context}",
        # tool-less: this file drives the terminal path, not the data-agent
        # output contract (that is test_swarm_output_contract.py's job).
        tools=[],
        skills=[],
        max_iterations=4,
        timeout_seconds=60,
        model_name="judge-model",
        max_retries=0,
    )
    base.update(overrides)
    return SwarmAgentSpec(**base)


def _drive_to_iteration_limit(monkeypatch, tmp_path: Path) -> WorkerResult:
    monkeypatch.setattr(backoff_mod.time, "sleep", lambda *_: None)
    with (
        patch.object(
            worker_mod, "build_swarm_registry", lambda *a, **k: _ErrorRegistry()
        ),
        patch.object(worker_mod, "ChatLLM", _NeverStopsLLM()),
    ):
        return run_worker(
            agent_spec=_spec(),
            task=SwarmTask(
                id="t1", agent_id="research_manager", prompt_template="Judge."
            ),
            upstream_summaries={},
            user_vars={},
            run_dir=tmp_path,
        )


def test_iteration_limit_returns_degraded_with_error(monkeypatch, tmp_path):
    result = _drive_to_iteration_limit(monkeypatch, tmp_path)
    assert result.status is WorkerStatus.degraded
    assert result.error and "iteration limit" in result.error


def test_failure_context_records_recurring_schema_error(monkeypatch, tmp_path):
    _drive_to_iteration_limit(monkeypatch, tmp_path)
    ctx = json.loads(
        (
            tmp_path / "artifacts" / "research_manager" / "failure_context.json"
        ).read_text(encoding="utf-8")
    )
    assert ctx["reason"] == "iteration_limit"
    assert len(ctx["tool_errors"]) >= 2
    assert ctx["tool_errors"][0]["tool"] == "submit_decision"
    assert ctx["tool_errors"][0]["issues"][0]["field"] == "strategic_actions"
    assert ctx["recurring_tool_errors"][0]["count"] >= 2


def test_recurring_tool_errors_ignores_one_offs():
    errors = [
        {"tool": "submit_decision", "issues": [{"field": "a", "problem": "p"}]},
        {"tool": "submit_decision", "issues": [{"field": "a", "problem": "p"}]},
        {"tool": "get_market_data", "error": "timeout"},
    ]
    recurring = _recurring_tool_errors(errors)
    assert len(recurring) == 1
    assert recurring[0]["tool"] == "submit_decision"
    assert recurring[0]["count"] == 2
