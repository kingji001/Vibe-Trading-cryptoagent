"""Loop 2 (Completion) unit coverage — degraded status, markers, gate.

A seat that dies mid-work had its partial scratch output marked ``completed``
and propagated downstream as a binding deliverable
(swarm-20260728-180010-cd9f563d, spec §4.1). These tests pin the pieces of
the fix; the end-to-end replay lives in
``test_swarm_completion_loop_anchor.py``.
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
from src.swarm.models import (
    RunStatus,
    SwarmAgentSpec,
    SwarmRun,
    SwarmTask,
    TaskStatus,
    WorkerResult,
    WorkerStatus,
)
from src.swarm.store import SwarmStore
from src.swarm.task_store import TaskStore
from src.swarm.worker import (
    INCOMPLETE_UPSTREAM_MARKER,
    _recurring_tool_errors,
    build_worker_prompt,
    run_worker,
)


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


def test_degraded_worker_writes_upstream_marker_file(monkeypatch, tmp_path):
    monkeypatch.setattr(backoff_mod.time, "sleep", lambda *_: None)
    with (
        patch.object(
            worker_mod, "build_swarm_registry", lambda *a, **k: _ErrorRegistry()
        ),
        patch.object(worker_mod, "ChatLLM", _NeverStopsLLM()),
    ):
        run_worker(
            agent_spec=_spec(id="portfolio_manager"),
            task=SwarmTask(
                id="t1", agent_id="portfolio_manager", prompt_template="Decide."
            ),
            upstream_summaries={"research_plan": "partial note"},
            user_vars={},
            run_dir=tmp_path,
            degraded_upstreams={
                "research_plan": "upstream task task-research-plan: x"
            },
        )
    payload = json.loads(
        (
            tmp_path / "artifacts" / "portfolio_manager" / "upstream_degradation.json"
        ).read_text(encoding="utf-8")
    )
    assert payload["degraded_upstreams"] == [
        {
            "context_key": "research_plan",
            "reason": "upstream task task-research-plan: x",
        }
    ]


# --- Task 3: prompt marker -------------------------------------------------


def test_prompt_tags_degraded_section_and_adds_banner():
    prompt = build_worker_prompt(
        _spec(id="trader"),
        {"research_plan": "a partial note", "market_report": "a full report"},
        "(no matching skills)",
        degraded_upstreams={"research_plan": "upstream task task-research-plan: died"},
    )
    assert f"### research_plan {INCOMPLETE_UPSTREAM_MARKER}" in prompt
    assert "### market_report\n" in prompt
    assert "Upstream Integrity Warning" in prompt
    assert "Buy / Overweight / Sell / Underweight" in prompt


def test_prompt_unchanged_when_nothing_degraded():
    args = (
        _spec(id="trader"),
        {"research_plan": "a full plan"},
        "(no matching skills)",
    )

    def _stable(text: str) -> str:
        # The trailing "Current Date & Time" section is clock-derived; drop it
        # so this comparison cannot flake across a minute boundary.
        return text.split("## Current Date & Time")[0]

    assert _stable(build_worker_prompt(*args)) == _stable(
        build_worker_prompt(*args, degraded_upstreams={})
    )
    assert INCOMPLETE_UPSTREAM_MARKER not in build_worker_prompt(*args)


# --- Task 4: runtime transitions ------------------------------------------


def _two_task_run(tmp_path: Path) -> tuple[SwarmStore, rt.SwarmRuntime, SwarmRun]:
    store = SwarmStore(base_dir=tmp_path)
    runtime = rt.SwarmRuntime(store=store)
    run = SwarmRun(
        id="r-deg",
        preset_name="demo",
        created_at="2026-07-28T18:00:10+00:00",
        agents=[
            SwarmAgentSpec(id="judge", role="judge", system_prompt="x", max_retries=0),
            SwarmAgentSpec(id="trader", role="trader", system_prompt="x", max_retries=0),
        ],
        tasks=[
            SwarmTask(id="task-judge", agent_id="judge", prompt_template="judge"),
            SwarmTask(
                id="task-trader",
                agent_id="trader",
                prompt_template="trade",
                depends_on=["task-judge"],
                blocked_by=["task-judge"],
                input_from={"research_plan": "task-judge"},
            ),
        ],
    )
    store.create_run(run)
    return store, runtime, run


def test_degraded_result_marks_task_degraded_and_run_failed(tmp_path, monkeypatch):
    seen: list[dict] = []

    def fake_worker(agent_spec, task, **kwargs):
        seen.append({"task": task.id, "degraded": kwargs.get("degraded_upstreams")})
        if task.id == "task-judge":
            return WorkerResult(
                status="degraded",
                summary="a partial working note",
                error="hit iteration limit (15) without completing the deliverable",
                iterations=15,
            )
        return WorkerResult(status="completed", summary="trader done", iterations=3)

    monkeypatch.setattr(rt, "run_worker", fake_worker)
    store, runtime, run = _two_task_run(tmp_path)
    runtime._execute_run(run, threading.Event())

    reloaded = store.load_run(run.id)
    by_id = {t.id: t for t in reloaded.tasks}
    assert by_id["task-judge"].status is TaskStatus.degraded
    assert by_id["task-judge"].summary == "a partial working note"
    assert "iteration limit" in by_id["task-judge"].error
    # fail-visible: the consumer runs, and it is told.
    assert by_id["task-trader"].status is TaskStatus.completed
    assert seen[-1]["degraded"] == {
        "research_plan": "upstream task task-judge: hit iteration limit (15) "
        "without completing the deliverable"
    }
    # run-level status still reports the degradation
    assert reloaded.status is RunStatus.failed


def test_degraded_emits_task_degraded_event(tmp_path, monkeypatch):
    monkeypatch.setattr(
        rt,
        "run_worker",
        lambda *a, **k: WorkerResult(
            status="degraded",
            summary="partial",
            error="hit iteration limit",
            iterations=15,
        ),
    )
    store, runtime, run = _two_task_run(tmp_path)
    runtime._execute_run(run, threading.Event())

    events = [
        json.loads(line)
        for line in (tmp_path / run.id / "events.jsonl").read_text().splitlines()
        if line.strip()
    ]
    degraded = [e for e in events if e["type"] == "task_degraded"]
    assert {e["task_id"] for e in degraded} == {"task-judge", "task-trader"}
    assert degraded[0]["data"]["iterations"] == 15
    assert "task_completed" not in {e["type"] for e in events}


def test_failed_upstream_still_blocks_downstream(tmp_path, monkeypatch):
    """Guard: degraded is permissive, failed is not — the 5/27 gate stands."""
    monkeypatch.setattr(
        rt,
        "run_worker",
        lambda *a, **k: WorkerResult(status="failed", summary="", error="boom"),
    )
    store, runtime, run = _two_task_run(tmp_path)
    runtime._execute_run(run, threading.Event())

    by_id = {t.id: t for t in store.load_run(run.id).tasks}
    assert by_id["task-judge"].status is TaskStatus.failed
    assert by_id["task-trader"].status is TaskStatus.blocked


def test_marker_is_always_co_delivered_with_the_content(tmp_path, monkeypatch):
    """A consumer can never receive degraded content without its marker.

    ``input_from`` creates no DAG edge (``topological_layers`` reads only
    ``depends_on``, task_store.py:220), so a consumer CAN be scheduled before
    a task it reads from. That is survivable only because ``upstream`` and
    ``degraded_upstreams`` are populated from the same loop over the same two
    dicts, and ``_execute_run`` writes ``task_summaries[tid]`` and
    ``degraded_tasks[tid]`` together in the degraded branch. The unordered
    case therefore yields NEITHER content nor marker — never content alone.
    """
    seen: list[dict] = []

    def fake_worker(agent_spec, task, **kwargs):
        seen.append(
            {
                "task": task.id,
                "upstream": set(kwargs.get("upstream_summaries") or {}),
                "degraded": set(kwargs.get("degraded_upstreams") or {}),
            }
        )
        if task.id == "task-judge":
            return WorkerResult(
                status="degraded",
                summary="",
                error="hit iteration limit",
                iterations=15,
            )
        return WorkerResult(status="completed", summary="ok", iterations=1)

    monkeypatch.setattr(rt, "run_worker", fake_worker)
    store, runtime, run = _two_task_run(tmp_path)
    runtime._execute_run(run, threading.Event())

    for call in seen:
        assert call["degraded"] <= call["upstream"], call
    trader = next(c for c in seen if c["task"] == "task-trader")
    # even an EMPTY degraded summary still delivers the key, so the marker
    # is never orphaned by a seat that produced no text at all.
    assert trader["upstream"] == {"research_plan"}
    assert trader["degraded"] == {"research_plan"}


def test_cancellation_does_not_erase_a_degraded_task(tmp_path):
    """A degraded task is terminal: cancelling the run must not overwrite it."""
    store, runtime, run = _two_task_run(tmp_path)
    task_store = TaskStore(tmp_path / run.id)
    for t in run.tasks:
        task_store.save_task(t)
    task_store.update_status(
        "task-judge", TaskStatus.degraded, error="hit iteration limit"
    )
    runtime._cancel_remaining_tasks(task_store, ["task-trader"], task_store.load_all())
    assert task_store.load_task("task-judge").status is TaskStatus.degraded
    assert task_store.load_task("task-trader").status is TaskStatus.cancelled


# --- Task 5: store reconciliation -----------------------------------------


def test_reconcile_treats_degraded_as_terminal(tmp_path):
    store = SwarmStore(base_dir=tmp_path)
    run = SwarmRun(
        id="r-rec",
        preset_name="demo",
        status=RunStatus.running,
        created_at="2026-07-28T18:00:10+00:00",
        tasks=[
            SwarmTask(
                id="t1", agent_id="a", prompt_template="x", status=TaskStatus.degraded
            ),
            SwarmTask(
                id="t2", agent_id="b", prompt_template="x", status=TaskStatus.completed
            ),
        ],
    )
    store.create_run(run)
    reconciled = store.reconcile_run(store.load_run(run.id), write=True)

    assert reconciled.status is RunStatus.failed
    assert reconciled.completed_at
    # a degraded task must survive reconciliation unmodified
    assert {t.id: t.status for t in reconciled.tasks}["t1"] is TaskStatus.degraded
