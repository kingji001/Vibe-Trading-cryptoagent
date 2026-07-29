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

# Loop 2 (Completion) decision gate. ``run_dir`` injected into every swarm
# tool call is the calling seat's artifact dir, and the worker mirrors its
# degraded-upstream set there as ``upstream_degradation.json``; the worker
# also passes that set directly into the kwargs of the two gated tools
# (``submit_decision`` and ``decision_journal``), which takes precedence --
# see ``_degraded_upstreams``. The file fallback still exists for callers
# outside the swarm (tests, the single-agent loop) but is never the source of
# truth inside a swarm run. A sized directional call on a run whose required
# upstream never completed is what committed $16,965 on a truncated scratch
# note (spec §4.1); Hold stays open so a degraded run yields a no-op rather
# than a blind trade.
_UPSTREAM_DEGRADATION_FILENAME = "upstream_degradation.json"
_DIRECTIONAL_RATINGS = frozenset({"Buy", "Overweight", "Sell", "Underweight"})


def _degraded_upstreams(run_dir: Any, injected: Any = None) -> list[dict]:
    """Return the degraded-upstream entries recorded for this seat, if any.

    Args:
        run_dir: The seat's artifact directory. Used only as the fallback
            source when ``injected`` is absent, for backward compatibility
            with callers that never pass the kwarg.
        injected: The seat's degraded-upstream set passed out-of-band by the
            swarm worker (worker.py's tool-call loop injects this for the
            gated tools specifically). Preferred over the
            on-disk marker: ``upstream_degradation.json`` lives inside the
            model-writable ``run_dir``, and ``write_file`` can overwrite any
            filename there (``resolve_safe_path`` has no filename blocklist),
            so a model can clear the file itself to unlock a sized
            directional call on a run whose upstream never completed.
            Trusting the file alone makes this gate cooperative rather than
            enforced.
    """
    if injected is not None:
        if not isinstance(injected, list):
            return []
        return [e for e in injected if isinstance(e, dict)]
    if not run_dir:
        return []
    try:
        path = Path(run_dir) / _UPSTREAM_DEGRADATION_FILENAME
        if not path.is_file():
            return []
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(payload, dict):
        return []
    entries = payload.get("degraded_upstreams")
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict)]


def degraded_gate_error(rating: str, degraded: list[dict]) -> dict:
    """The gate's refusal payload, shared by both tools that can commit capital.

    ``submit_decision`` validates and renders; ``decision_journal(action=
    "append")`` is what actually reaches the broker (append_decision ->
    paper.hook.maybe_execute_paper -> paper.translator.execute_decision, which
    sizes the order from the journal entry's rating). The portfolio manager
    holds both, so both gate — and returning one message from one place keeps
    the two refusals byte-identical rather than drifting apart.
    """
    names = ", ".join(
        str(e.get("context_key") or e.get("task_id") or "?") for e in degraded
    )
    return {
        "status": "error",
        "error": (
            f"Decision gate: rating '{rating}' is a sized directional call, "
            "but a required upstream deliverable on this run is degraded "
            f"({names}). Resubmit with rating 'Hold' — it is the only rating "
            "accepted while an upstream is incomplete."
        ),
        "degraded_upstreams": degraded,
    }


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

        if schema_name == "portfolio_decision":
            degraded = _degraded_upstreams(run_dir, kwargs.get("degraded_upstreams"))
            rating = getattr(model, "rating", None)
            rating_value = getattr(rating, "value", None) or str(rating)
            if degraded and rating_value in _DIRECTIONAL_RATINGS:
                return json.dumps(
                    degraded_gate_error(rating_value, degraded),
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
