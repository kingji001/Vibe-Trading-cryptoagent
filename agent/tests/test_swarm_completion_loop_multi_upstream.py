"""Multi-upstream degraded projection — companion to the anchor regression.

test_swarm_completion_loop_anchor.py replays swarm-20260728-180010-cd9f563d,
which has exactly one degraded seat feeding exactly one consumer. That shape
cannot exercise ``_execute_layer``'s ``input_from`` projection loop
(``src/swarm/runtime.py``, "Build upstream summaries from input_from mapping")
across more than one key or one source. Real presets do: e.g.
``crypto_committee.yaml``'s ``task-decision`` reads three upstream keys
(``past_lessons``, ``research_plan``, ``trader_proposal``), any of which may
be degraded independently. These tests pin that projection directly.

They also document a known gap: degradation is projected only for a
consumer's *direct* ``input_from`` sources, not transitively. If task A
degrades and task B (which consumes A) nonetheless reports ``completed``,
task C consuming B's summary receives no marker — the taint is silent past
one hop. See ``test_transitive_degradation_is_not_projected_past_one_hop``.
The incident replay happens to not need this: ``research_plan`` feeds
``task-decision`` directly from the degraded seat, with no intermediate
"completed" hop between them. That is a property of today's DAG shape, not
a structural guarantee — a two-hop chain in a different preset would go
silent.
"""

from __future__ import annotations

import threading

import src.swarm.runtime as rt
from src.swarm.models import SwarmAgentSpec, SwarmRun, SwarmTask, WorkerResult
from src.swarm.store import SwarmStore

_DEGRADED_REASON_A = "hit iteration limit (15) without completing the deliverable"
_DEGRADED_REASON_B = "validation failed after 3 consecutive attempts"


def _spec(agent_id: str) -> SwarmAgentSpec:
    return SwarmAgentSpec(
        id=agent_id,
        role=agent_id,
        system_prompt="x\n\n{upstream_context}",
        max_retries=0,
    )


def _make_runtime(tmp_path):
    store = SwarmStore(base_dir=tmp_path)
    return store, rt.SwarmRuntime(store=store)


def test_two_degraded_sources_both_projected_to_one_consumer(tmp_path, monkeypatch):
    """Two independently-degraded upstreams feeding one consumer: both of the
    consumer's context keys must carry their own, correctly-formatted
    reason — neither may be dropped or overwritten by the other."""

    calls: dict[str, dict] = {}

    def fake_run_worker(agent_spec, task, degraded_upstreams=None, **kwargs):
        calls[task.id] = degraded_upstreams
        if task.id == "task-A":
            return WorkerResult(
                status="degraded", summary="partial-a", error=_DEGRADED_REASON_A
            )
        if task.id == "task-B":
            return WorkerResult(
                status="degraded", summary="partial-b", error=_DEGRADED_REASON_B
            )
        return WorkerResult(status="completed", summary="ok")

    monkeypatch.setattr(rt, "run_worker", fake_run_worker)

    store, runtime = _make_runtime(tmp_path)
    run = SwarmRun(
        id="r-two-sources",
        preset_name="demo",
        created_at="2026-07-28T18:00:10+00:00",
        agents=[_spec("a"), _spec("b"), _spec("c")],
        tasks=[
            SwarmTask(id="task-A", agent_id="a", prompt_template="x"),
            SwarmTask(id="task-B", agent_id="b", prompt_template="x"),
            SwarmTask(
                id="task-C",
                agent_id="c",
                prompt_template="x",
                depends_on=["task-A", "task-B"],
                blocked_by=["task-A", "task-B"],
                input_from={"research_plan": "task-A", "trader_proposal": "task-B"},
            ),
        ],
    )
    store.create_run(run)
    runtime._execute_run(run, threading.Event())

    degraded_seen = calls["task-C"]
    assert degraded_seen == {
        "research_plan": f"upstream task task-A: {_DEGRADED_REASON_A}",
        "trader_proposal": f"upstream task task-B: {_DEGRADED_REASON_B}",
    }, degraded_seen


def test_two_context_keys_from_the_same_degraded_source_are_both_marked(
    tmp_path, monkeypatch
):
    """One degraded upstream feeding a consumer under two different context
    keys (a preset can legitimately alias the same source into two prompt
    slots): both keys must be marked, not just the first one seen."""

    calls: dict[str, dict] = {}

    def fake_run_worker(agent_spec, task, degraded_upstreams=None, **kwargs):
        calls[task.id] = degraded_upstreams
        if task.id == "task-A":
            return WorkerResult(
                status="degraded", summary="partial-a", error=_DEGRADED_REASON_A
            )
        return WorkerResult(status="completed", summary="ok")

    monkeypatch.setattr(rt, "run_worker", fake_run_worker)

    store, runtime = _make_runtime(tmp_path)
    run = SwarmRun(
        id="r-aliased-source",
        preset_name="demo",
        created_at="2026-07-28T18:00:10+00:00",
        agents=[_spec("a"), _spec("c")],
        tasks=[
            SwarmTask(id="task-A", agent_id="a", prompt_template="x"),
            SwarmTask(
                id="task-C",
                agent_id="c",
                prompt_template="x",
                depends_on=["task-A"],
                blocked_by=["task-A"],
                input_from={"research_plan": "task-A", "risk_view": "task-A"},
            ),
        ],
    )
    store.create_run(run)
    runtime._execute_run(run, threading.Event())

    degraded_seen = calls["task-C"]
    assert degraded_seen == {
        "research_plan": f"upstream task task-A: {_DEGRADED_REASON_A}",
        "risk_view": f"upstream task task-A: {_DEGRADED_REASON_A}",
    }, degraded_seen


def test_transitive_degradation_is_not_projected_past_one_hop(tmp_path, monkeypatch):
    """Documents a real limitation, not a bug to fix here: degradation
    projection (src/swarm/runtime.py, _execute_layer) only checks whether a
    consumer's *direct* input_from source is in the run's degraded_tasks
    map. If A degrades and B (which consumes A) nonetheless reports
    "completed", B is never added to degraded_tasks — so C, which consumes
    B, sees no marker at all. The taint is real (B's summary was built on a
    degraded A) but silent past one hop.

    This is what makes the anchor's single-hop replay
    (research_manager -> {trader, portfolio_manager}) an incomplete proof
    for any preset with a longer chain: the gate and the marker are only as
    good as the DAG happening to route every consumer directly off the
    degraded seat.
    """

    def fake_run_worker(agent_spec, task, degraded_upstreams=None, **kwargs):
        if task.id == "task-A":
            return WorkerResult(
                status="degraded", summary="partial-a", error=_DEGRADED_REASON_A
            )
        if task.id == "task-B":
            # B consumed A's (degraded) summary but itself reports completed —
            # e.g. it judged the partial input sufficient. This is the
            # realistic failure mode: nothing forces B to notice.
            return WorkerResult(status="completed", summary="b-summary")
        return WorkerResult(status="completed", summary="c-summary")

    calls: dict[str, dict] = {}
    real_fake = fake_run_worker

    def recording_fake(agent_spec, task, degraded_upstreams=None, **kwargs):
        calls[task.id] = degraded_upstreams
        return real_fake(agent_spec, task, degraded_upstreams=degraded_upstreams, **kwargs)

    monkeypatch.setattr(rt, "run_worker", recording_fake)

    store, runtime = _make_runtime(tmp_path)
    run = SwarmRun(
        id="r-transitive",
        preset_name="demo",
        created_at="2026-07-28T18:00:10+00:00",
        agents=[_spec("a"), _spec("b"), _spec("c")],
        tasks=[
            SwarmTask(id="task-A", agent_id="a", prompt_template="x"),
            SwarmTask(
                id="task-B",
                agent_id="b",
                prompt_template="x",
                depends_on=["task-A"],
                blocked_by=["task-A"],
                input_from={"research_plan": "task-A"},
            ),
            SwarmTask(
                id="task-C",
                agent_id="c",
                prompt_template="x",
                depends_on=["task-B"],
                blocked_by=["task-B"],
                input_from={"research_plan": "task-B"},
            ),
        ],
    )
    store.create_run(run)
    runtime._execute_run(run, threading.Event())

    # B (the direct consumer of degraded A) does see the marker...
    assert calls["task-B"] == {
        "research_plan": f"upstream task task-A: {_DEGRADED_REASON_A}"
    }
    # ...but C (which only ever sees B, itself "completed") does not, even
    # though C's context ultimately descends from a degraded seat. This
    # assertion is the documented limitation, not a bug to fix in Loop 2 —
    # closing it would require projecting degraded status transitively
    # through every "completed" hop, which is out of scope here.
    assert calls["task-C"] in (None, {})
