"""Phase 4 — multi-round adversarial debate expansion tests.

The engine's DAG is single-pass and acyclic by construction; debate rounds are
unrolled at preset-build time (``debates:`` YAML sugar in presets.py) into plain
chained tasks. These tests pin:

- round-1 regression (unset envs == today's bull->bear->manager and
  risky->safe->neutral graph, byte-for-byte on task ids / input_from / prompts);
- round counts 1/2/3 for both the 2-seat alternation and 3-seat rotation;
- per-round ``input_from`` transcript accumulation;
- expansion determinism (same YAML + env -> same graph);
- cycle-freedom for rounds 1-4 x both debate shapes;
- rounds > 4 rejected at build time;
- a preset WITHOUT a ``debates:`` section builds identically to before;
- the Phase 2 layer-deadline math tolerates the longer serial chain.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.swarm import presets
from src.swarm.presets import build_run_from_preset, inspect_preset, load_preset
from src.swarm.runtime import compute_layer_deadline
from src.swarm.task_store import topological_layers, validate_dag

PRESET = "crypto_committee"
USER_VARS = {"target": "BTC-USDT", "timeframe": "72h swing"}

DEBATE_ENVS = ("VIBE_DEBATE_ROUNDS", "VIBE_RISK_ROUNDS")


@pytest.fixture(autouse=True)
def _clear_debate_envs(monkeypatch: pytest.MonkeyPatch):
    """Every test starts from unset debate envs (== today's single pass)."""
    for var in DEBATE_ENVS:
        monkeypatch.delenv(var, raising=False)


def _task_map(run):
    return {t.id: t for t in run.tasks}


def _signature(run):
    """Order-independent structural fingerprint for determinism comparison."""
    return sorted(
        (
            t.id,
            t.agent_id,
            t.prompt_template,
            tuple(sorted(t.depends_on)),
            tuple(sorted(t.input_from.items())),
        )
        for t in run.tasks
    )


# --------------------------------------------------------------- round-1 regression

# Golden wiring: exactly today's crypto_committee graph. Unset debate envs
# (rounds default to 1 via ${VIBE_DEBATE_ROUNDS:-1}) MUST reproduce this.
GOLDEN_R1 = {
    "task-bull": {
        "depends_on": {"task-market", "task-onchain", "task-news", "task-sentiment"},
        "input_from": {
            "market_report": "task-market",
            "onchain_report": "task-onchain",
            "news_report": "task-news",
            "sentiment_report": "task-sentiment",
        },
    },
    "task-bear": {
        "depends_on": {"task-bull"},
        "input_from": {
            "market_report": "task-market",
            "onchain_report": "task-onchain",
            "news_report": "task-news",
            "sentiment_report": "task-sentiment",
            "bull_argument": "task-bull",
        },
    },
    "task-research-plan": {
        "depends_on": {"task-bear"},
        "input_from": {
            "market_report": "task-market",
            "bull_argument": "task-bull",
            "bear_rebuttal": "task-bear",
        },
    },
    "task-risk-aggressive": {
        "depends_on": {"task-trader"},
        "input_from": {
            "trader_proposal": "task-trader",
            "research_plan": "task-research-plan",
        },
    },
    "task-risk-safe": {
        "depends_on": {"task-risk-aggressive"},
        "input_from": {
            "trader_proposal": "task-trader",
            "aggressive_argument": "task-risk-aggressive",
        },
    },
    "task-risk-neutral": {
        "depends_on": {"task-risk-safe"},
        "input_from": {
            "trader_proposal": "task-trader",
            "aggressive_argument": "task-risk-aggressive",
            "conservative_argument": "task-risk-safe",
        },
    },
    "task-decision": {
        "depends_on": {"task-risk-neutral", "task-reflection"},
        "input_from": {
            "past_lessons": "task-reflection",
            "research_plan": "task-research-plan",
            "trader_proposal": "task-trader",
            "risk_adjudication": "task-risk-neutral",
            "aggressive_argument": "task-risk-aggressive",
            "conservative_argument": "task-risk-safe",
        },
    },
}


def test_round1_regression_exact_wiring():
    """Crown jewel: unset envs reproduce today's graph exactly (ids/deps/inputs)."""
    run = build_run_from_preset(PRESET, USER_VARS)
    tasks = _task_map(run)

    # Same 13 task ids as before Phase 4.
    assert len(run.tasks) == 13
    for tid, want in GOLDEN_R1.items():
        assert tid in tasks, f"missing task {tid}"
        assert set(tasks[tid].depends_on) == want["depends_on"], tid
        assert tasks[tid].input_from == want["input_from"], tid


def test_round1_prompts_preserved():
    """Round-1 opener prompts match today's task prompt_templates verbatim."""
    run = build_run_from_preset(PRESET, USER_VARS)
    tasks = _task_map(run)
    assert tasks["task-bull"].prompt_template == (
        "Argue the bull case for {target} over the {timeframe} horizon, "
        "grounded in the four analyst reports."
    )
    assert tasks["task-bear"].prompt_template == (
        "Rebut the bull and argue the bear case for {target} over the "
        "{timeframe} horizon."
    )


def test_round1_dag_acyclic_and_sequential():
    run = build_run_from_preset(PRESET, USER_VARS)
    validate_dag(run.tasks)
    layers = topological_layers(run.tasks)
    assert layers[-1] == ["task-decision"]


# ------------------------------------------------------------------- round counts


@pytest.mark.parametrize("rounds", [1, 2, 3])
def test_research_debate_round_counts(monkeypatch, rounds):
    monkeypatch.setenv("VIBE_DEBATE_ROUNDS", str(rounds))
    run = build_run_from_preset(PRESET, USER_VARS)
    tasks = _task_map(run)

    # Round 1 keeps legacy ids; rounds >=2 append -r{n}.
    assert "task-bull" in tasks and "task-bear" in tasks
    for r in range(2, rounds + 1):
        assert f"task-bull-r{r}" in tasks, r
        assert f"task-bear-r{r}" in tasks, r
    assert f"task-bull-r{rounds + 1}" not in tasks

    validate_dag(run.tasks)


@pytest.mark.parametrize("rounds", [1, 2, 3])
def test_risk_rotation_round_counts(monkeypatch, rounds):
    monkeypatch.setenv("VIBE_RISK_ROUNDS", str(rounds))
    run = build_run_from_preset(PRESET, USER_VARS)
    tasks = _task_map(run)

    for seat in ("aggressive", "safe", "neutral"):
        assert f"task-risk-{seat}" in tasks
        for r in range(2, rounds + 1):
            assert f"task-risk-{seat}-r{r}" in tasks, (seat, r)
    validate_dag(run.tasks)


# ------------------------------------------------------------- per-round wiring


def test_round2_transcript_accumulation(monkeypatch):
    monkeypatch.setenv("VIBE_DEBATE_ROUNDS", "2")
    run = build_run_from_preset(PRESET, USER_VARS)
    tasks = _task_map(run)

    # bull r2 chains off bear r1 and carries the full r1 transcript + seeds.
    bull_r2 = tasks["task-bull-r2"]
    assert bull_r2.depends_on == ["task-bear"]
    assert bull_r2.input_from.get("bull_argument") == "task-bull"
    assert bull_r2.input_from.get("bear_rebuttal") == "task-bear"
    assert bull_r2.input_from.get("market_report") == "task-market"

    # bear r2 chains off bull r2 and sees bull's NEW round-2 material.
    bear_r2 = tasks["task-bear-r2"]
    assert bear_r2.depends_on == ["task-bull-r2"]
    assert bear_r2.input_from.get("bull_argument_r2") == "task-bull-r2"

    # rebuttal prompt tells the seat to respond to the latest opponent, not
    # restate its opener.
    assert "restate" in bull_r2.prompt_template.lower()
    assert "2" in bull_r2.prompt_template  # round number baked in

    # sink (research manager) receives the full alternation.
    sink = tasks["task-research-plan"]
    assert sink.depends_on == ["task-bear-r2"]
    for key in ("bull_argument", "bear_rebuttal", "bull_argument_r2", "bear_rebuttal_r2"):
        assert key in sink.input_from, key


def test_risk_round2_pm_sees_full_rotation(monkeypatch):
    monkeypatch.setenv("VIBE_RISK_ROUNDS", "2")
    run = build_run_from_preset(PRESET, USER_VARS)
    tasks = _task_map(run)
    pm = tasks["task-decision"]
    # PM depends on the final rotation task and sees both rounds' adjudications.
    assert pm.depends_on[-1] == "task-risk-neutral-r2" or "task-risk-neutral-r2" in pm.depends_on
    assert "risk_adjudication" in pm.input_from
    assert "risk_adjudication_r2" in pm.input_from


# ----------------------------------------------------------------- determinism


def test_expansion_is_deterministic(monkeypatch):
    monkeypatch.setenv("VIBE_DEBATE_ROUNDS", "3")
    monkeypatch.setenv("VIBE_RISK_ROUNDS", "2")
    a = build_run_from_preset(PRESET, USER_VARS)
    b = build_run_from_preset(PRESET, USER_VARS)
    assert _signature(a) == _signature(b)


# ---------------------------------------------------------- cycle-freedom property


@pytest.mark.parametrize("d_rounds", [1, 2, 3, 4])
@pytest.mark.parametrize("r_rounds", [1, 2, 3, 4])
def test_expanded_graph_is_acyclic(monkeypatch, d_rounds, r_rounds):
    monkeypatch.setenv("VIBE_DEBATE_ROUNDS", str(d_rounds))
    monkeypatch.setenv("VIBE_RISK_ROUNDS", str(r_rounds))
    run = build_run_from_preset(PRESET, USER_VARS)
    validate_dag(run.tasks)  # raises on cycle / dangling dep
    layers = topological_layers(run.tasks)
    assert layers  # non-empty


# ------------------------------------------------------------------- guardrails


@pytest.mark.parametrize("rounds", [5, 9])
def test_rounds_over_cap_rejected(monkeypatch, rounds):
    monkeypatch.setenv("VIBE_DEBATE_ROUNDS", str(rounds))
    with pytest.raises(ValueError, match="rounds"):
        build_run_from_preset(PRESET, USER_VARS)


@pytest.mark.parametrize("rounds", ["0", "-1"])
def test_rounds_below_one_rejected(monkeypatch, rounds):
    monkeypatch.setenv("VIBE_DEBATE_ROUNDS", rounds)
    with pytest.raises(ValueError, match="rounds"):
        build_run_from_preset(PRESET, USER_VARS)


def test_rounds_non_integer_rejected(monkeypatch):
    monkeypatch.setenv("VIBE_DEBATE_ROUNDS", "two")
    with pytest.raises(ValueError):
        build_run_from_preset(PRESET, USER_VARS)


# -------------------------------------------------------------------- inspect


def test_inspect_shows_expanded_tasks(monkeypatch):
    monkeypatch.setenv("VIBE_DEBATE_ROUNDS", "2")
    report = inspect_preset(PRESET)
    assert report["valid"], report["errors"]
    task_ids = {t["id"] for t in report["tasks"]}
    assert {"task-bull", "task-bear", "task-bull-r2", "task-bear-r2"} <= task_ids
    # every expanded round task appears in a layer (dry run shows the plan)
    layer_ids = {c["task_id"] for layer in report["layers"] for c in layer}
    assert "task-bull-r2" in layer_ids


# ----------------------------------------------------- no-debates regression


def test_preset_without_debates_unchanged(monkeypatch):
    """A preset with no ``debates:`` section builds exactly its raw tasks."""
    monkeypatch.setenv("VIBE_DEBATE_ROUNDS", "3")  # must be ignored
    data = load_preset("investment_committee")
    assert "debates" not in data
    run = build_run_from_preset("investment_committee", {"target": "X", "market": "Y"})
    raw_ids = [t["id"] for t in data["tasks"]]
    assert [t.id for t in run.tasks] == raw_ids


# --------------------------------------------------------------- budget check


def test_layer_deadline_absorbs_serial_chain(monkeypatch):
    """Serial debate chain = one runnable task per layer, so each layer gets the
    full single-task budget; more rounds just means more layers, never a
    squeezed per-layer deadline."""
    buffer = 300
    budget = 900 * 2  # timeout_seconds * (max_retries + 1)
    one = compute_layer_deadline(
        layer_budget=budget, runnable_tasks=1, max_workers=3, buffer_s=buffer
    )
    assert one == budget + buffer  # 1 wave, full budget

    # Longer chain -> strictly more layers, each still a single runnable task.
    monkeypatch.setenv("VIBE_DEBATE_ROUNDS", "1")
    short = topological_layers(build_run_from_preset(PRESET, USER_VARS).tasks)
    monkeypatch.setenv("VIBE_DEBATE_ROUNDS", "3")
    long = topological_layers(build_run_from_preset(PRESET, USER_VARS).tasks)
    assert len(long) > len(short)
    # Each debate layer holds exactly one task -> deadline == budget + buffer.
    for layer in long:
        if len(layer) == 1:
            assert (
                compute_layer_deadline(
                    layer_budget=budget,
                    runnable_tasks=1,
                    max_workers=3,
                    buffer_s=buffer,
                )
                == budget + buffer
            )
