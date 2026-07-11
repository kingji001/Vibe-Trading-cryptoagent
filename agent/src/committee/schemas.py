"""Typed decision schemas for the crypto committee swarm preset.

Ported/adapted from TauricResearch/TradingAgents (Apache-2.0,
arXiv:2412.20138): ResearchPlan, TraderProposal, PortfolioDecision,
SentimentReport, the 5-tier rating scale, nullish-string coercion for
optional numeric fields, and deterministic (regex, non-LLM) rating
extraction.

Design notes for the swarm engine:
- Workers have no native structured-output mode, so validation happens in
  the ``submit_decision`` tool (src/tools/committee_decision_tool.py),
  which returns actionable errors the worker can retry against.
- Each ``render_markdown`` emits a deterministic ``**Rating**:`` /
  ``**Action**:`` header line so downstream consumers parse decisions
  without an LLM call.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# Rating scale (5-tier, TradingAgents-compatible)
# ---------------------------------------------------------------------------


class Rating(str, Enum):
    BUY = "Buy"
    OVERWEIGHT = "Overweight"
    HOLD = "Hold"
    UNDERWEIGHT = "Underweight"
    SELL = "Sell"


_RATING_PATTERN = re.compile(
    r"\*\*Rating\*\*\s*:\s*(Buy|Overweight|Hold|Underweight|Sell)\b",
    re.IGNORECASE,
)
_RATING_FALLBACK = re.compile(
    r"\b(Buy|Overweight|Hold|Underweight|Sell)\b", re.IGNORECASE
)

_CANONICAL = {r.value.lower(): r for r in Rating}


def parse_rating(text: str) -> Rating:
    """Deterministically extract a Rating from rendered markdown.

    Prefers the ``**Rating**: X`` header line; falls back to the first
    bare tier word; defaults to Hold. Never calls an LLM.
    """
    if not text:
        return Rating.HOLD
    m = _RATING_PATTERN.search(text) or _RATING_FALLBACK.search(text)
    if not m:
        return Rating.HOLD
    return _CANONICAL.get(m.group(1).lower(), Rating.HOLD)


# ---------------------------------------------------------------------------
# Nullish coercion (TradingAgents #1058): models emit "n/a"/"tbd"/"-"/""
# in optional numeric fields; coerce to None instead of failing validation.
# ---------------------------------------------------------------------------

_NULLISH = {"", "n/a", "na", "none", "null", "tbd", "-", "unknown", "nil", "<unavailable>"}


def _coerce_nullish(value: Any) -> Any:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.lower() in _NULLISH:
            return None
        # "65000 USDT" / "$65,000" style — keep digits, dot, minus.
        cleaned = re.sub(r"[^\d.\-eE]", "", stripped)
        return cleaned if cleaned else None
    return value


class _CommitteeModel(BaseModel):
    """Base with shared config; forbid unknown fields so typos surface."""

    model_config = {"extra": "forbid"}


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ResearchPlan(_CommitteeModel):
    """Research Manager's ruling on the bull/bear debate."""

    recommendation: Rating = Field(
        description="5-tier call: Buy | Overweight | Hold | Underweight | Sell"
    )
    rationale: str = Field(
        min_length=50,
        description="Why this side of the debate won; cite specific arguments and data.",
    )
    strategic_actions: list[str] = Field(
        min_length=1,
        description="Concrete next actions for the trader (entries, invalidations, hedges).",
    )


class TraderProposal(_CommitteeModel):
    """Trader's executable proposal. Action is Buy/Hold/Sell only —
    sizing/tilt granularity is the Portfolio Manager's job."""

    action: Literal["Buy", "Hold", "Sell"]
    reasoning: str = Field(min_length=50)
    entry_price: float | None = Field(
        default=None, description="Proposed entry in quote currency (e.g. USDT)."
    )
    stop_loss: float | None = None
    take_profit: float | None = None
    position_sizing: str | None = Field(
        default=None, description="Sizing note, e.g. 'half size until funding normalizes'."
    )

    @field_validator("entry_price", "stop_loss", "take_profit", mode="before")
    @classmethod
    def _nullish(cls, v: Any) -> Any:
        return _coerce_nullish(v)

    @field_validator("action", mode="before")
    @classmethod
    def _title_case(cls, v: Any) -> Any:
        return v.strip().title() if isinstance(v, str) else v


class PortfolioDecision(_CommitteeModel):
    """Portfolio Manager's final, binding call."""

    rating: Rating
    executive_summary: str = Field(min_length=50)
    investment_thesis: str = Field(min_length=100)
    price_target: float | None = None
    time_horizon: str = Field(
        description="Stated horizon, e.g. '72h swing' or '2-4 week position'."
    )
    stop_loss: float | None = Field(
        default=None,
        description="Protective stop in quote currency, grounded in the trader's proposal "
        "and verified snapshot prices. Omit when not determinable — never invent.",
    )
    take_profit: float | None = Field(
        default=None,
        description="Target exit in quote currency, grounded in the trader's proposal and "
        "verified snapshot prices. Omit when not determinable — never invent.",
    )
    position_size_pct: float | None = Field(
        default=None,
        ge=0,
        le=100,
        description="Position size as a percent of equity (0-100). Omit when not "
        "determinable — never invent.",
    )

    @field_validator(
        "price_target", "stop_loss", "take_profit", "position_size_pct", mode="before"
    )
    @classmethod
    def _nullish(cls, v: Any) -> Any:
        return _coerce_nullish(v)


class SentimentReport(_CommitteeModel):
    """Sentiment analyst's structured read of pre-fetched social data."""

    sentiment: Literal[
        "very_bearish", "bearish", "neutral", "mixed", "bullish", "very_bullish"
    ]
    score_0_10: float = Field(ge=0, le=10)
    confidence: Literal["low", "medium", "high"] = Field(
        description="Must be down-rated when any source was <unavailable>."
    )
    narrative: str = Field(min_length=50)

    @field_validator("score_0_10", mode="before")
    @classmethod
    def _nullish_score(cls, v: Any) -> Any:
        coerced = _coerce_nullish(v)
        return 5.0 if coerced is None else coerced


SCHEMAS: dict[str, type[_CommitteeModel]] = {
    "research_plan": ResearchPlan,
    "trader_proposal": TraderProposal,
    "portfolio_decision": PortfolioDecision,
    "sentiment_report": SentimentReport,
}


# ---------------------------------------------------------------------------
# Markdown rendering (downstream prose consumers stay unchanged)
# ---------------------------------------------------------------------------


def _fmt_num(v: float | None) -> str:
    return f"{v:,.6g}" if isinstance(v, (int, float)) else "n/a"


def render_markdown(schema_name: str, model: _CommitteeModel) -> str:
    if isinstance(model, ResearchPlan):
        actions = "\n".join(f"- {a}" for a in model.strategic_actions)
        return (
            f"## Research Plan\n\n**Rating**: {model.recommendation.value}\n\n"
            f"### Rationale\n{model.rationale}\n\n### Strategic Actions\n{actions}\n"
        )
    if isinstance(model, TraderProposal):
        return (
            f"## Trader Proposal\n\n**Action**: {model.action}\n\n"
            f"### Reasoning\n{model.reasoning}\n\n"
            f"| Entry | Stop Loss | Take Profit |\n|---|---|---|\n"
            f"| {_fmt_num(model.entry_price)} | {_fmt_num(model.stop_loss)} "
            f"| {_fmt_num(model.take_profit)} |\n\n"
            f"**Sizing note**: {model.position_sizing or 'n/a'}\n\n"
            f"FINAL TRANSACTION PROPOSAL: **{model.action.upper()}**\n"
        )
    if isinstance(model, PortfolioDecision):
        return (
            f"## Portfolio Decision\n\n**Rating**: {model.rating.value}\n\n"
            f"### Executive Summary\n{model.executive_summary}\n\n"
            f"### Investment Thesis\n{model.investment_thesis}\n\n"
            f"**Price target**: {_fmt_num(model.price_target)}\n"
            f"**Time horizon**: {model.time_horizon}\n"
        )
    if isinstance(model, SentimentReport):
        return (
            f"## Sentiment Report\n\n"
            f"**Sentiment**: {model.sentiment}  |  **Score**: {model.score_0_10:.1f}/10"
            f"  |  **Confidence**: {model.confidence}\n\n{model.narrative}\n"
        )
    raise ValueError(f"No renderer for schema '{schema_name}'")
