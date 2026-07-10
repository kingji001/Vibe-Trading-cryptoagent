"""paper_tick: lightweight agent-tool wrapper around ``run_tick`` (Task 5).

Not a committee seat tool — it exists solely so the scheduled
``paper-trading-tick`` job (``src/api/scheduled_routes.py``) can drive the
daily mark-to-market / conditional-order tick through the ordinary
prompt -> tool-call executor path, the same shape the Phase 6 reflection job
uses for ``decision_journal``. Takes no parameters and never invents a price
(see ``src.paper.tick.run_tick``): a bar-fetch failure for a symbol is
recorded in ``errors`` and that position is left untouched, retried next tick.
"""

from __future__ import annotations

import json
from typing import Any

from src.agent.tools import BaseTool


class PaperTickTool(BaseTool):
    """Run one paper-trading daily tick: conditional orders + equity snapshot."""

    name = "paper_tick"
    description = (
        "Run one paper-trading daily tick: evaluates stop/take-profit "
        "conditional orders for every open paper position against the latest "
        "confirmed daily bar, then marks the paper account to market and "
        "records an equity snapshot. Idempotent per UTC day. No parameters. "
        "Returns a summary of fills, current equity, stale positions, and any "
        "per-symbol bar-fetch errors (those positions are left untouched and "
        "retried on the next tick)."
    )
    parameters = {"type": "object", "properties": {}, "required": []}
    is_readonly = False
    repeatable = True

    @classmethod
    def check_available(cls) -> bool:
        try:
            from src.paper import tick  # noqa: F401
        except Exception:
            return False
        return True

    def execute(self, **kwargs: Any) -> str:
        from src.paper.tick import run_tick

        try:
            result = run_tick()
        except Exception as exc:
            return json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)

        equity = result.get("equity_snapshot") or {}
        summary = {
            "status": "ok",
            "fills": len(result.get("conditional_fills") or []),
            "equity": equity.get("equity"),
            "stale_positions": equity.get("stale_positions"),
            "date": equity.get("date"),
            "already_recorded": equity.get("already_recorded"),
            "errors": result.get("errors") or [],
        }
        return json.dumps(summary, ensure_ascii=False, default=str)
