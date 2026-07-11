"""Regression coverage for SwarmTool natural-language preset routing."""

from __future__ import annotations

import json

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
# crypto_committee variable extraction (two-tier-cadence Task 2)
#
# _build_variables previously hardcoded {"target": "BTC-USDT", "timeframe":
# "72h swing"} for crypto_committee regardless of prompt content, silently
# ignoring any other symbol a caller (e.g. the scheduled committee-run job,
# which iterates VIBE_COMMITTEE_SYMBOLS) asked for. It now extracts the
# instrument and horizon from the documented "... swarm on <SYMBOL> for a
# <TIMEFRAME> decision" phrasing (docs/crypto-committee.md), falling back to
# the historical defaults when a prompt doesn't use that phrasing.
# ---------------------------------------------------------------------------


def test_build_variables_extracts_explicit_symbol_and_timeframe() -> None:
    prompt = "Run the crypto_committee swarm on ETH-USDT for a 24h swing decision."

    assert swarm_tool._build_variables("crypto_committee", prompt) == {
        "target": "ETH-USDT",
        "timeframe": "24h swing",
    }


def test_build_variables_falls_back_to_default_target_and_timeframe() -> None:
    prompt = "Analyze crypto markets broadly."

    assert swarm_tool._build_variables("crypto_committee", prompt) == {
        "target": "BTC-USDT",
        "timeframe": "72h swing",
    }
