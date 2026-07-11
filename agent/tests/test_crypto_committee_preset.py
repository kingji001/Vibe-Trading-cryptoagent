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


def test_preset_loads_and_lists(run):
    data = load_preset(PRESET)
    assert data["name"] == PRESET
    assert len(data["agents"]) == 13
    # Phase 4: the bull/bear debate and risk rotation now live under debates:
    # and are unrolled at build time. The raw YAML declares fewer tasks, but the
    # built run reproduces the historical 13-task graph when debate envs are
    # unset (rounds default to 1).
    assert len(data["tasks"]) == 8
    assert len(data["debates"]) == 2
    assert len(run.tasks) == 13


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


# --------------------------------------------------------------------------- #
# Phase 5 — anti-hallucination toolchain wiring
# --------------------------------------------------------------------------- #

SNAPSHOT_TOOL = "get_verified_crypto_snapshot"
SENTIMENT_TOOL = "get_crypto_sentiment_data"

# Seats the brief calls out: market analyst, funding-relevant seats
# (risky/safe/neutral risk debators + trader), research manager, and PM.
SNAPSHOT_SEATS = {
    "market_analyst",
    "research_manager",
    "trader",
    "risky_analyst",
    "safe_analyst",
    "neutral_analyst",
    "portfolio_manager",
}


def test_verified_snapshot_tool_registered_for_expected_seats(run):
    specs = {a.id: a for a in run.agents}
    for seat_id in SNAPSHOT_SEATS:
        assert SNAPSHOT_TOOL in specs[seat_id].tools, seat_id
    # No other seat carries it (keeps the whitelist tight / intentional).
    for spec in run.agents:
        if spec.id not in SNAPSHOT_SEATS:
            assert SNAPSHOT_TOOL not in spec.tools, spec.id


def test_sentiment_tool_replaces_read_url_for_sentiment_analyst(run):
    specs = {a.id: a for a in run.agents}
    sentiment_spec = specs["sentiment_analyst"]
    assert SENTIMENT_TOOL in sentiment_spec.tools
    # read_url must be gone — the tool now does the fetching in code.
    assert "read_url" not in sentiment_spec.tools
    assert SNAPSHOT_TOOL not in sentiment_spec.tools
    # No other seat carries the sentiment tool.
    for spec in run.agents:
        if spec.id != "sentiment_analyst":
            assert SENTIMENT_TOOL not in spec.tools, spec.id


def test_snapshot_seat_prompts_reference_get_verified_crypto_snapshot(run):
    """Every seat that carries the tool must actually call it by name in its
    system_prompt — carrying the tool with no prompt instruction is a
    registration that silently does nothing."""
    specs = {a.id: a for a in run.agents}
    for seat_id in SNAPSHOT_SEATS:
        assert SNAPSHOT_TOOL in specs[seat_id].system_prompt, seat_id


def test_lessons_to_manager_flag_off_by_default(run):
    """Upstream default (TradingAgents restricts memory to the PM): with
    VIBE_LESSONS_TO_MANAGER unset, the research manager's task does NOT
    receive past_lessons — only the PM does (see test_learning_loop_wiring)."""
    tasks = {t.id: t for t in run.tasks}
    assert "past_lessons" not in tasks["task-research-plan"].input_from


def test_lessons_to_manager_flag_on_injects_research_manager_input(monkeypatch):
    """VIBE_LESSONS_TO_MANAGER=1 mirrors the PM's past_lessons wiring onto the
    research manager (experiment knob), without disturbing the PM's own
    wiring or the reflection officer's task."""
    monkeypatch.setenv("VIBE_LESSONS_TO_MANAGER", "1")
    flagged_run = build_run_from_preset(PRESET, USER_VARS)
    tasks = {t.id: t for t in flagged_run.tasks}

    assert tasks["task-research-plan"].input_from.get("past_lessons") == "task-reflection"
    assert "task-reflection" in tasks["task-research-plan"].depends_on
    # PM wiring is unaffected.
    assert tasks["task-decision"].input_from.get("past_lessons") == "task-reflection"
    # DAG stays acyclic and reflection still runs task-reflection first.
    validate_dag(flagged_run.tasks)
    layers = topological_layers(flagged_run.tasks)
    order = {tid: i for i, layer in enumerate(layers) for tid in layer}
    assert order["task-reflection"] < order["task-research-plan"]


def test_pm_append_step_enumerates_execution_and_run_id_fields():
    """Final review I2/C3: the PM's step-2 decision_journal append enumeration
    must name the execution fields (stop_loss, take_profit, position_size_pct)
    and run_id — otherwise the prompt-requested execution data never reaches
    journal.append_decision / the paper executor."""
    from src.swarm.presets import PRESETS_DIR

    raw = (PRESETS_DIR / f"{PRESET}.yaml").read_text(encoding="utf-8")
    idx = raw.index('action "append"')
    segment = raw[idx : idx + 600]
    for field in ("stop_loss", "take_profit", "position_size_pct", "run_id"):
        assert field in segment, f"{field!r} missing from PM append enumeration"


def test_snapshot_tool_json_keys_match_prompt_references():
    """Prompt-contract: every top-level field name the snapshot tool's JSON
    envelope actually returns is referenced somewhere in the preset text —
    guards against a tool field rename silently orphaning what the prompts
    tell agents to look for."""
    from src.swarm.presets import PRESETS_DIR
    from src.tools.crypto_snapshot_tool import SNAPSHOT_FIELD_NAMES, build_snapshot

    raw_text = (PRESETS_DIR / f"{PRESET}.yaml").read_text(encoding="utf-8")
    for field in SNAPSHOT_FIELD_NAMES:
        assert field in raw_text, f"{field!r} not referenced anywhere in {PRESET}.yaml"

    # And the reverse direction: every field the prompts imply exists is
    # actually a key build_snapshot() returns (fixture-driven, no network).
    def _fetch_row(*, label, **kwargs):
        rows = {
            "spot ticker": {"last": "1", "ts": "1700000000000", "open24h": "1",
                             "high24h": "1", "low24h": "1", "vol24h": "1", "volCcy24h": "1"},
            "funding rate": {"fundingRate": "0.0001", "fundingTime": "1700000000000",
                              "nextFundingRate": "", "nextFundingTime": "1700000000000"},
            "open interest": {"oi": "1", "oiCcy": "1", "ts": "1700000000000"},
            "mark price": {"markPx": "1", "ts": "1700000000000"},
            "index price": {"idxPx": "1", "ts": "1700000000000"},
        }
        return rows[label], None

    snapshot = build_snapshot("BTC-USDT", fetch_row=_fetch_row)
    for field in SNAPSHOT_FIELD_NAMES:
        assert field in snapshot, field
