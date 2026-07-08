"""Structural tests for the crypto_committee swarm preset.

Guards the engine contracts learned in recon:
- the DAG must be acyclic and layer into the intended sequential debate;
- every task using ``input_from`` needs ``{upstream_context}`` in its agent's
  ``system_prompt`` (otherwise upstream reports are silently dropped);
- decision workers must carry the ``submit_decision`` tool;
- ``prompt_template`` may only reference declared preset variables.
"""

from __future__ import annotations

import pytest

from src.swarm.presets import build_run_from_preset, inspect_preset, load_preset
from src.swarm.task_store import topological_layers, validate_dag

PRESET = "crypto_committee"
USER_VARS = {"target": "BTC-USDT", "timeframe": "72h swing"}

DECISION_AGENTS = {
    "sentiment_analyst",
    "research_manager",
    "trader",
    "portfolio_manager",
}


@pytest.fixture(scope="module")
def run():
    return build_run_from_preset(PRESET, USER_VARS)


def test_preset_loads_and_lists():
    data = load_preset(PRESET)
    assert data["name"] == PRESET
    assert len(data["agents"]) == 13
    assert len(data["tasks"]) == 13


def test_dag_is_acyclic_and_debate_is_sequential(run):
    validate_dag(run.tasks)  # raises on cycles
    layers = topological_layers(run.tasks)
    order = {tid: i for i, layer in enumerate(layers) for tid in layer}

    # 4 analysts + the reflection officer run first, in parallel.
    analyst_layer = {
        order[t]
        for t in (
            "task-market",
            "task-onchain",
            "task-news",
            "task-sentiment",
            "task-reflection",
        )
    }
    assert analyst_layer == {0}

    # Sequential decision spine: bull -> bear -> plan -> trader -> risk x3 -> PM.
    spine = [
        "task-bull",
        "task-bear",
        "task-research-plan",
        "task-trader",
        "task-risk-aggressive",
        "task-risk-safe",
        "task-risk-neutral",
        "task-decision",
    ]
    positions = [order[t] for t in spine]
    assert positions == sorted(positions) and len(set(positions)) == len(spine)

    # Final report comes from the PM (last layer).
    assert layers[-1] == ["task-decision"]


def test_input_from_agents_declare_upstream_context_placeholder(run):
    specs = {a.id: a for a in run.agents}
    for task in run.tasks:
        if task.input_from:
            prompt = specs[task.agent_id].system_prompt
            assert "{upstream_context}" in prompt, (
                f"agent {task.agent_id!r} consumes input_from but its system_prompt "
                "lacks {upstream_context}; upstream reports would be silently dropped"
            )


def test_bear_sees_bull_and_pm_sees_full_risk_rotation(run):
    tasks = {t.id: t for t in run.tasks}
    assert tasks["task-bear"].input_from.get("bull_argument") == "task-bull"
    pm_inputs = set(tasks["task-decision"].input_from.values())
    assert {"task-research-plan", "task-trader", "task-risk-neutral"} <= pm_inputs


def test_decision_agents_carry_submit_decision_tool(run):
    for spec in run.agents:
        if spec.id in DECISION_AGENTS:
            assert "submit_decision" in spec.tools, spec.id
        else:
            assert "submit_decision" not in spec.tools, spec.id


def test_learning_loop_wiring(run):
    """Reflection officer feeds the PM; both carry decision_journal."""
    specs = {a.id: a for a in run.agents}
    tasks = {t.id: t for t in run.tasks}

    assert "decision_journal" in specs["reflection_officer"].tools
    assert "decision_journal" in specs["portfolio_manager"].tools
    # nobody else touches the journal
    for spec in run.agents:
        if spec.id not in {"reflection_officer", "portfolio_manager"}:
            assert "decision_journal" not in spec.tools, spec.id

    # PM receives the lessons report; reflection runs with no upstream inputs
    assert tasks["task-decision"].input_from.get("past_lessons") == "task-reflection"
    assert tasks["task-reflection"].depends_on == []
    assert not tasks["task-reflection"].input_from


def test_all_agents_can_write_their_report(run):
    for spec in run.agents:
        assert "write_file" in spec.tools, spec.id


def test_inspect_reports_no_variable_issues():
    report = inspect_preset(PRESET)
    declared = {v["name"] for v in load_preset(PRESET).get("variables", [])}
    assert declared == {"target", "timeframe"}
    # No template var referenced without being declared (engine would degrade
    # it to an LLM "infer this" hint).
    assert not report.get("undeclared_variables"), report
