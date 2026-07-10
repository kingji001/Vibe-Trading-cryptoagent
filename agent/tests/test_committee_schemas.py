"""Tests for src/committee/schemas.py (typed committee decisions).

Guards the TradingAgents-ported decision mechanics: 5-tier rating,
nullish-string coercion on optional numerics, deterministic (non-LLM)
rating extraction, and render/parse round-trips.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.committee.schemas import (
    SCHEMAS,
    PortfolioDecision,
    Rating,
    ResearchPlan,
    SentimentReport,
    TraderProposal,
    parse_rating,
    render_markdown,
)

LONG = "x" * 120


# ---------------------------------------------------------------- parse_rating


def test_parse_rating_prefers_header_line():
    text = "prose mentions Sell earlier\n\n**Rating**: Overweight\n\nmore prose"
    assert parse_rating(text) is Rating.OVERWEIGHT


def test_parse_rating_fallback_first_tier_word():
    assert parse_rating("The committee leans Underweight into the event.") is Rating.UNDERWEIGHT


def test_parse_rating_defaults_to_hold():
    assert parse_rating("") is Rating.HOLD
    assert parse_rating("no tier words here") is Rating.HOLD


def test_parse_rating_case_insensitive():
    assert parse_rating("**rating**: buy") is Rating.BUY


# ------------------------------------------------------------ nullish coercion


@pytest.mark.parametrize("nullish", ["", "n/a", "N/A", "tbd", "-", "none", "null", "  NA "])
def test_trader_proposal_nullish_numerics_coerce_to_none(nullish):
    p = TraderProposal(
        action="Hold",
        reasoning=LONG,
        entry_price=nullish,
        stop_loss=nullish,
        take_profit=nullish,
    )
    assert p.entry_price is None and p.stop_loss is None and p.take_profit is None


def test_trader_proposal_currency_strings_coerce():
    p = TraderProposal(action="Buy", reasoning=LONG, entry_price="$65,000", stop_loss="61200 USDT")
    assert p.entry_price == pytest.approx(65000.0)
    assert p.stop_loss == pytest.approx(61200.0)


def test_trader_proposal_action_title_cased_and_bounded():
    assert TraderProposal(action="buy", reasoning=LONG).action == "Buy"
    with pytest.raises(ValidationError):
        TraderProposal(action="Overweight", reasoning=LONG)  # PM-only tier


# ------------------------------------------------------------------ validation


def test_research_plan_requires_substantive_rationale_and_actions():
    with pytest.raises(ValidationError):
        ResearchPlan(recommendation="Buy", rationale="too short", strategic_actions=["a"])
    with pytest.raises(ValidationError):
        ResearchPlan(recommendation="Buy", rationale=LONG, strategic_actions=[])


def test_portfolio_decision_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        PortfolioDecision(
            rating="Hold",
            executive_summary=LONG,
            investment_thesis=LONG,
            time_horizon="72h",
            surprise_field=1,
        )


def test_sentiment_score_bounds_and_nullish_default():
    with pytest.raises(ValidationError):
        SentimentReport(sentiment="bullish", score_0_10=11, confidence="low", narrative=LONG)
    s = SentimentReport(sentiment="mixed", score_0_10="n/a", confidence="low", narrative=LONG)
    assert s.score_0_10 == pytest.approx(5.0)


# --------------------------------------------------------- execution fields (paper loop Task 1)


def test_portfolio_decision_without_execution_fields_still_validates():
    """Regression: pre-existing PM submissions carry no stop/TP/size and must
    still validate now that the fields are declared as optional additions."""
    old_payload = {
        "rating": "Hold",
        "executive_summary": LONG,
        "investment_thesis": LONG,
        "price_target": 65000,
        "time_horizon": "72h swing",
    }
    d = PortfolioDecision(**old_payload)
    assert d.stop_loss is None
    assert d.take_profit is None
    assert d.position_size_pct is None


def test_portfolio_decision_position_size_pct_bounded_0_100():
    with pytest.raises(ValidationError):
        PortfolioDecision(
            rating="Buy",
            executive_summary=LONG,
            investment_thesis=LONG,
            time_horizon="72h swing",
            position_size_pct=125,
        )


@pytest.mark.parametrize("nullish", ["", "n/a", "tbd", "-", "none", "<unavailable>"])
def test_portfolio_decision_execution_fields_nullish_coerce(nullish):
    """Mirrors TraderProposal's _nullish coercion pattern for entry_price/etc."""
    d = PortfolioDecision(
        rating="Buy",
        executive_summary=LONG,
        investment_thesis=LONG,
        time_horizon="72h swing",
        stop_loss=nullish,
        take_profit=nullish,
        position_size_pct=nullish,
    )
    assert d.stop_loss is None
    assert d.take_profit is None
    assert d.position_size_pct is None


def test_portfolio_decision_execution_fields_accept_valid_values():
    d = PortfolioDecision(
        rating="Buy",
        executive_summary=LONG,
        investment_thesis=LONG,
        time_horizon="72h swing",
        stop_loss="61200 USDT",
        take_profit=70000,
        position_size_pct=10,
    )
    assert d.stop_loss == pytest.approx(61200.0)
    assert d.take_profit == pytest.approx(70000.0)
    assert d.position_size_pct == pytest.approx(10.0)


# ------------------------------------------------------------- render round-trip


@pytest.mark.parametrize("rating", list(Rating))
def test_portfolio_decision_render_parse_round_trip(rating):
    d = PortfolioDecision(
        rating=rating,
        executive_summary=LONG,
        investment_thesis=LONG,
        price_target=None,
        time_horizon="72h swing",
    )
    md = render_markdown("portfolio_decision", d)
    assert f"**Rating**: {rating.value}" in md
    assert parse_rating(md) is rating


def test_trader_proposal_render_keeps_legacy_final_line():
    p = TraderProposal(action="Sell", reasoning=LONG, entry_price=100.0)
    md = render_markdown("trader_proposal", p)
    assert "FINAL TRANSACTION PROPOSAL: **SELL**" in md


def test_schema_registry_covers_all_four():
    assert set(SCHEMAS) == {
        "research_plan",
        "trader_proposal",
        "portfolio_decision",
        "sentiment_report",
    }
