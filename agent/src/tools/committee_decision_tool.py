"""submit_decision: schema-validated structured decisions for swarm workers.

The swarm engine has no structured-output enforcement (workers are free-form
ReAct loops), so this tool is the validation gate: a worker submits a JSON
payload against a named committee schema; validation errors come back as
actionable messages the worker can fix and retry within its iteration budget.

On success the tool:
  1. persists the typed object as ``decision.<schema>.json`` in the worker's
     artifact dir (``run_dir`` is injected per-call by the swarm worker), and
  2. returns rendered markdown the worker MUST include in its ``report.md``
     so downstream ``input_from`` consumers and ``final_report`` see it.

Decision mechanics adapted from TauricResearch/TradingAgents (Apache-2.0,
arXiv:2412.20138).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.agent.tools import BaseTool


class SubmitDecisionTool(BaseTool):
    """Validate a committee decision against its Pydantic schema."""

    name = "submit_decision"
    description = (
        "Validate and record a structured committee decision. Call with the "
        "schema name and a JSON payload. On validation errors, fix the listed "
        "fields and call again. On success, copy the returned "
        "rendered_markdown into your report.md. Schemas: research_plan "
        "(recommendation: Buy|Overweight|Hold|Underweight|Sell, rationale, "
        "strategic_actions[]), trader_proposal (action: Buy|Hold|Sell, "
        "reasoning, entry_price?, stop_loss?, take_profit?, position_sizing?), "
        "portfolio_decision (rating, executive_summary, investment_thesis, "
        "price_target?, time_horizon), sentiment_report (sentiment, "
        "score_0_10, confidence, narrative)."
    )
    is_readonly = False
    repeatable = True
    parameters = {
        "type": "object",
        "properties": {
            "schema": {
                "type": "string",
                "enum": [
                    "research_plan",
                    "trader_proposal",
                    "portfolio_decision",
                    "sentiment_report",
                ],
                "description": "Which decision schema to validate against.",
            },
            "payload": {
                "type": "object",
                "description": "The decision fields as a JSON object.",
            },
        },
        "required": ["schema", "payload"],
    }

    @classmethod
    def check_available(cls) -> bool:
        try:
            import pydantic  # noqa: F401

            from src.committee import schemas  # noqa: F401
        except Exception:
            return False
        return True

    def execute(self, **kwargs: Any) -> str:
        from pydantic import ValidationError

        from src.committee.schemas import SCHEMAS, render_markdown

        schema_name = kwargs.get("schema", "")
        payload = kwargs.get("payload")
        run_dir = kwargs.get("run_dir")

        model_cls = SCHEMAS.get(schema_name)
        if model_cls is None:
            return json.dumps(
                {
                    "status": "error",
                    "error": f"Unknown schema '{schema_name}'. Valid: {sorted(SCHEMAS)}",
                },
                ensure_ascii=False,
            )
        if not isinstance(payload, dict):
            return json.dumps(
                {
                    "status": "error",
                    "error": "payload must be a JSON object of decision fields",
                },
                ensure_ascii=False,
            )

        try:
            model = model_cls.model_validate(payload)
        except ValidationError as exc:
            issues = [
                {
                    "field": ".".join(str(p) for p in err["loc"]) or "(root)",
                    "problem": err["msg"],
                }
                for err in exc.errors()
            ]
            return json.dumps(
                {
                    "status": "error",
                    "error": "Decision payload failed validation. Fix these fields "
                    "and call submit_decision again.",
                    "issues": issues,
                },
                ensure_ascii=False,
            )

        rendered = render_markdown(schema_name, model)

        saved_to = None
        if run_dir:
            try:
                out = Path(run_dir) / f"decision.{schema_name}.json"
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(
                    json.dumps(model.model_dump(mode="json"), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                saved_to = str(out)
            except Exception:
                saved_to = None  # persistence is best-effort; decision still valid

        return json.dumps(
            {
                "status": "ok",
                "schema": schema_name,
                "decision": model.model_dump(mode="json"),
                "saved_to": saved_to,
                "rendered_markdown": rendered,
                "next_step": "Include rendered_markdown verbatim in your report.md.",
            },
            ensure_ascii=False,
        )
