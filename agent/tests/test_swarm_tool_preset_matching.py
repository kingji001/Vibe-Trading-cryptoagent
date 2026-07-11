"""Regression coverage for SwarmTool natural-language preset routing."""

from __future__ import annotations

import json

import pytest

import src.tools.swarm_tool as swarm_tool


def test_explicit_preset_name_wins_over_keyword_scoring() -> None:
    prompt = (
        "[Swarm Team Mode] Use the investment_committee preset to evaluate "
        "whether to go long or short on NVDA given current market conditions"
    )

    assert swarm_tool._match_preset(prompt) == "investment_committee"


def test_plain_given_does_not_trigger_iv_derivatives_match() -> None:
    prompt = "Evaluate whether to go long or short on NVDA given current market conditions"

    assert swarm_tool._match_preset(prompt) != "derivatives_strategy_desk"


def test_explicit_preset_name_accepts_spaces() -> None:
    prompt = "Use the investment committee preset for NVDA"

    assert swarm_tool._match_preset(prompt) == "investment_committee"


def test_explicit_preset_parameter_is_normalized() -> None:
    preset, error = swarm_tool._resolve_preset(
        "Continue and finish the report.",
        explicit_preset="Investment Committee",
    )

    assert error is None
    assert preset == "investment_committee"


def test_ambiguous_continuation_does_not_fallback_to_equity_team() -> None:
    preset, error = swarm_tool._resolve_preset(
        "Continue and finish report. Continue from 'Trim 25% of position if price r'."
    )

    assert preset is None
    assert error is not None
    assert "equity_research_team" in error


def test_swarm_tool_rejects_ambiguous_continuation_before_starting_run() -> None:
    payload = json.loads(
        swarm_tool.SwarmTool().execute(
            prompt="Continue and finish report. Continue from 'Trim 25% of position if price r'."
        )
    )

    assert payload["status"] == "error"
    assert "Ambiguous continuation" in payload["error"]


# ---------------------------------------------------------------------------
# crypto_committee variable extraction (two-tier-cadence Task 2 + review fix)
#
# _build_variables previously hardcoded {"target": "BTC-USDT", "timeframe":
# "72h swing"} for crypto_committee regardless of prompt content, silently
# ignoring any other symbol a caller (e.g. the scheduled committee-run job,
# which iterates VIBE_COMMITTEE_SYMBOLS) asked for. It now extracts the
# instrument and horizon from the documented "... swarm on <SYMBOL> for a
# <TIMEFRAME> decision" phrasing (docs/crypto-committee.md). Post-review
# hardening: a prompt that names NO target (and no structured `variables`
# override, see below) raises an instructive error instead of silently
# defaulting to BTC-USDT — the silent default was the wrong-asset failure
# mode, just non-deterministic (any paraphrase triggered it).
# ---------------------------------------------------------------------------


def test_build_variables_extracts_explicit_symbol_and_timeframe() -> None:
    prompt = "Run the crypto_committee swarm on ETH-USDT for a 24h swing decision."

    assert swarm_tool._build_variables("crypto_committee", prompt) == {
        "target": "ETH-USDT",
        "timeframe": "24h swing",
    }


def test_build_variables_defaults_timeframe_when_only_symbol_named() -> None:
    prompt = "Run the crypto_committee swarm on ETH-USDT."

    assert swarm_tool._build_variables("crypto_committee", prompt) == {
        "target": "ETH-USDT",
        "timeframe": "72h swing",
    }


def test_build_variables_raises_instructive_error_when_no_target_named() -> None:
    with pytest.raises(swarm_tool.CryptoTargetMissingError) as excinfo:
        swarm_tool._build_variables("crypto_committee", "Analyze crypto markets broadly.")

    message = str(excinfo.value)
    assert "variables" in message  # names the structured channel
    assert "on <SYMBOL>" in message  # names the prose phrasing


# ---------------------------------------------------------------------------
# Structured `variables` parameter (post-review fix)
#
# run_swarm's agent tool gains an optional `variables` object that is
# validated against the preset's declared YAML variables and WINS over
# prompt extraction, so callers (e.g. the scheduled committee-run job) do
# not depend on the LLM reproducing the prose template verbatim.
# ---------------------------------------------------------------------------


class _ForbiddenRuntime:
    """SwarmRuntime stand-in that fails the test if a run is ever started."""

    def __init__(self, *args, **kwargs) -> None:
        raise AssertionError("SwarmRuntime must not be constructed for this input")


class _CapturingRuntime:
    """SwarmRuntime stand-in that records start_run args then aborts the run."""

    captured: dict = {}

    def __init__(self, *args, **kwargs) -> None:
        pass

    def start_run(self, preset, variables, **kwargs):
        _CapturingRuntime.captured = {"preset": preset, "variables": variables}
        raise ValueError("captured; aborting before any worker starts")


def _patch_runtime(monkeypatch, runtime_cls) -> None:
    import src.swarm.runtime as swarm_runtime

    monkeypatch.setattr(swarm_runtime, "SwarmRuntime", runtime_cls)


def test_variables_param_reaches_start_run_and_wins_over_prompt_extraction(monkeypatch) -> None:
    _patch_runtime(monkeypatch, _CapturingRuntime)
    _CapturingRuntime.captured = {}

    payload = json.loads(
        swarm_tool.SwarmTool().execute(
            prompt="Run the crypto_committee swarm on ETH-USDT for a 24h swing decision.",
            preset_name="crypto_committee",
            variables={"target": "SOL-USDT", "timeframe": "1 week position"},
        )
    )

    assert payload["status"] == "error"  # the capture stub aborts the run
    assert _CapturingRuntime.captured == {
        "preset": "crypto_committee",
        "variables": {"target": "SOL-USDT", "timeframe": "1 week position"},
    }


def test_variables_param_satisfies_target_requirement_for_paraphrased_prompt(monkeypatch) -> None:
    _patch_runtime(monkeypatch, _CapturingRuntime)
    _CapturingRuntime.captured = {}

    json.loads(
        swarm_tool.SwarmTool().execute(
            prompt="Please convene the crypto committee for Solana.",
            preset_name="crypto_committee",
            variables={"target": "SOL-USDT"},
        )
    )

    captured = _CapturingRuntime.captured
    assert captured["variables"]["target"] == "SOL-USDT"
    assert captured["variables"]["timeframe"] == "72h swing"  # default retained


def test_variables_param_rejects_unknown_keys_before_any_run(monkeypatch) -> None:
    _patch_runtime(monkeypatch, _ForbiddenRuntime)

    payload = json.loads(
        swarm_tool.SwarmTool().execute(
            prompt="Run the crypto_committee swarm on BTC-USDT for a 72h swing decision.",
            preset_name="crypto_committee",
            variables={"target": "BTC-USDT", "bogus": "x"},
        )
    )

    assert payload["status"] == "error"
    assert "bogus" in payload["error"]
    assert "target" in payload["error"]  # lists the declared variables


def test_variables_param_rejects_non_string_values_before_any_run(monkeypatch) -> None:
    _patch_runtime(monkeypatch, _ForbiddenRuntime)

    payload = json.loads(
        swarm_tool.SwarmTool().execute(
            prompt="Run the crypto_committee swarm on BTC-USDT for a 72h swing decision.",
            preset_name="crypto_committee",
            variables={"target": 42},
        )
    )

    assert payload["status"] == "error"
    assert "string" in payload["error"]


def test_paraphrased_prompt_without_variables_errors_and_starts_no_run(monkeypatch) -> None:
    _patch_runtime(monkeypatch, _ForbiddenRuntime)

    payload = json.loads(
        swarm_tool.SwarmTool().execute(
            prompt="Convene the crypto committee and issue a decision.",
            preset_name="crypto_committee",
        )
    )

    assert payload["status"] == "error"
    assert "variables" in payload["error"]
    assert "on <SYMBOL>" in payload["error"]
