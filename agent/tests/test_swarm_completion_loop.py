"""Loop 2 (Completion) unit coverage — degraded status, markers, gate.

A seat that dies mid-work had its partial scratch output marked ``completed``
and propagated downstream as a binding deliverable
(swarm-20260728-180010-cd9f563d, spec §4.1). These tests pin the pieces of
the fix; the end-to-end replay lives in
``test_swarm_completion_loop_anchor.py``.
"""

from __future__ import annotations

from src.swarm.models import SwarmTask, TaskStatus, WorkerResult, WorkerStatus


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
