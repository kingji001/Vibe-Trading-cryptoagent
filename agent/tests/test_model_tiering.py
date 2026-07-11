"""Tests for Phase 3 model tiering: ${ENV_VAR} placeholder resolution in
preset ``model_name`` fields.

Covers:
- ``${VAR}`` resolves to the env value when set.
- ``${VAR}`` resolves to ``None`` (-> global model) when unset/empty and no
  default is given.
- ``${VAR:-default}`` resolves to the default when the env var is unset or
  empty, and to the env value when set.
- Literal (non-placeholder) ``model_name`` values pass through untouched —
  the regression guard for presets with no placeholders at all.
- ``inspect_preset`` renders the resolved model, not the raw placeholder.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.swarm import presets

FIXTURE_PRESET = """
name: model_tiering_fixture
title: Model tiering fixture
description: "Fixture preset for Phase 3 model-tiering placeholder tests."

agents:
  - id: agent_env_set
    role: Env Set
    system_prompt: "x"
    model_name: "${VIBE_TEST_MODEL_SET}"
  - id: agent_env_unset
    role: Env Unset
    system_prompt: "x"
    model_name: "${VIBE_TEST_MODEL_UNSET}"
  - id: agent_env_default
    role: Env Default
    system_prompt: "x"
    model_name: "${VIBE_TEST_MODEL_UNSET:-fallback-model}"
  - id: agent_literal
    role: Literal
    system_prompt: "x"
    model_name: "literal-model-name"
  - id: agent_no_model
    role: No Model
    system_prompt: "x"

tasks:
  - id: task-a
    agent_id: agent_env_set
    prompt_template: "do {target}"
    depends_on: []

variables:
  - name: target
"""


@pytest.fixture()
def fixture_preset_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    preset_dir = tmp_path / "presets"
    preset_dir.mkdir()
    (preset_dir / "model_tiering_fixture.yaml").write_text(FIXTURE_PRESET, encoding="utf-8")
    monkeypatch.setattr(presets, "PRESETS_DIR", preset_dir)
    monkeypatch.delenv("VIBE_TEST_MODEL_SET", raising=False)
    monkeypatch.delenv("VIBE_TEST_MODEL_UNSET", raising=False)
    return preset_dir


def _build(user_vars: dict | None = None):
    return presets.build_run_from_preset("model_tiering_fixture", user_vars or {"target": "BTC-USDT"})


# --------------------------------------------------------------------- build_run_from_preset


def test_placeholder_resolves_when_env_set(fixture_preset_dir, monkeypatch):
    monkeypatch.setenv("VIBE_TEST_MODEL_SET", "resolved-model")
    run = _build()
    specs = {a.id: a for a in run.agents}
    assert specs["agent_env_set"].model_name == "resolved-model"


def test_placeholder_unset_without_default_resolves_to_none(fixture_preset_dir):
    run = _build()
    specs = {a.id: a for a in run.agents}
    assert specs["agent_env_unset"].model_name is None


def test_placeholder_empty_env_value_resolves_to_none(fixture_preset_dir, monkeypatch):
    monkeypatch.setenv("VIBE_TEST_MODEL_SET", "")
    run = _build()
    specs = {a.id: a for a in run.agents}
    assert specs["agent_env_set"].model_name is None


def test_placeholder_with_default_uses_default_when_unset(fixture_preset_dir):
    run = _build()
    specs = {a.id: a for a in run.agents}
    assert specs["agent_env_default"].model_name == "fallback-model"


def test_placeholder_with_default_uses_env_value_when_set(fixture_preset_dir, monkeypatch):
    monkeypatch.setenv("VIBE_TEST_MODEL_UNSET", "overridden")
    run = _build()
    specs = {a.id: a for a in run.agents}
    assert specs["agent_env_default"].model_name == "overridden"


def test_literal_model_name_passes_through_unchanged(fixture_preset_dir):
    """Regression guard: a preset with NO placeholders behaves identically."""
    run = _build()
    specs = {a.id: a for a in run.agents}
    assert specs["agent_literal"].model_name == "literal-model-name"


def test_agent_with_no_model_name_stays_none(fixture_preset_dir):
    run = _build()
    specs = {a.id: a for a in run.agents}
    assert specs["agent_no_model"].model_name is None


# ------------------------------------------------------------------------------ inspect_preset


def test_inspect_preset_renders_resolved_models(fixture_preset_dir, monkeypatch):
    monkeypatch.setenv("VIBE_TEST_MODEL_SET", "resolved-model")
    report = presets.inspect_preset("model_tiering_fixture")
    models = {a["id"]: a["model"] for a in report["agents"]}
    assert models["agent_env_set"] == "resolved-model"
    assert models["agent_env_unset"] is None
    assert models["agent_env_default"] == "fallback-model"
    assert models["agent_literal"] == "literal-model-name"
    assert models["agent_no_model"] is None


# ------------------------------------------------------------------------------ crypto_committee


def test_crypto_committee_deep_and_quick_tiers_resolve_from_env(monkeypatch):
    """Deliverable 2: research_manager/portfolio_manager use VIBE_DEEP_MODEL;
    analyst/researcher/trader/risk/reflection seats use VIBE_QUICK_MODEL."""
    monkeypatch.setenv("VIBE_DEEP_MODEL", "deep-tier-model")
    monkeypatch.setenv("VIBE_QUICK_MODEL", "quick-tier-model")
    run = presets.build_run_from_preset(
        "crypto_committee", {"target": "BTC-USDT", "timeframe": "72h swing"}
    )
    specs = {a.id: a for a in run.agents}

    for deep_seat in ("research_manager", "portfolio_manager"):
        assert specs[deep_seat].model_name == "deep-tier-model", deep_seat

    for quick_seat in (
        "market_analyst",
        "onchain_analyst",
        "news_analyst",
        "sentiment_analyst",
        "reflection_officer",
        "bull_researcher",
        "bear_researcher",
        "trader",
        "risky_analyst",
        "safe_analyst",
        "neutral_analyst",
    ):
        assert specs[quick_seat].model_name == "quick-tier-model", quick_seat


def test_crypto_committee_falls_back_to_global_model_when_unset(monkeypatch):
    """Intentional Phase 3 behavior change: unset VIBE_DEEP_MODEL/VIBE_QUICK_MODEL
    means every seat (including the former deepseek-v4-pro pins) resolves to
    ``None`` and falls back to the run's global model, not deepseek-v4-pro."""
    monkeypatch.delenv("VIBE_DEEP_MODEL", raising=False)
    monkeypatch.delenv("VIBE_QUICK_MODEL", raising=False)
    run = presets.build_run_from_preset(
        "crypto_committee", {"target": "BTC-USDT", "timeframe": "72h swing"}
    )
    for spec in run.agents:
        assert spec.model_name is None, spec.id
