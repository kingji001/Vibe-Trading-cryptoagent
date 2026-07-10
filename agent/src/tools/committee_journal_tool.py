"""decision_journal: the committee's decision journal as an agent tool.

One tool, five actions, so both swarm workers (reflection officer, portfolio
manager) and the main agent can drive the learning loop:

- append       PM records the freshly issued decision (pending).
- resolve_due  deterministic outcome math via okx/ccxt 1H bars: raw return,
               BTC-benchmark return, alpha at 24h/72h/7d. No LLM involved.
- reflect      attach a short written lesson to a resolved entry.
- lessons      render the prompt-injection block (same-symbol history +
               cross-symbol lessons).
- list         raw entries for inspection.
- pnl          decision-level paper-trading PnL (Task 6): was the decision
               actually executed as a paper trade, and what happened to the
               money (realized/unrealized PnL, fees, exit_kind). Consumed by
               the reflection officer to weigh the EXECUTED outcome against
               the pure directional call.

Learning-loop design adapted from TauricResearch/TradingAgents (Apache-2.0).

Resolution/reflection normally run inline in a committee run (the reflection
officer seat) or, when ``VIBE_TRADING_ENABLE_SCHEDULER=1``, via a daily
scheduled job (``src/api/scheduled_routes.py::_ensure_decision_journal_job``)
so outcomes resolve even on days with no committee run. If you keep the
scheduler off, see docs/minimax-migration-notes.md (Phase 6) for the
system-cron ``vibe-trading run`` equivalent.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

from src.agent.tools import BaseTool

BENCHMARK_ENV = "VIBE_COMMITTEE_BENCHMARK"
NOT_EXECUTED_MESSAGE = "not executed — no paper-trading data"


def _coerce_optional_float(value: Any) -> float | None:
    """Nullish-tolerant float coercion, same rule as the schema fields.

    Workers sometimes send "n/a" / "<unavailable>" / "$65,000" instead of a
    bare number; reuse the committee schemas' nullish coercion so those
    coerce to None / 65000.0 rather than erroring or journaling a string.
    Raises ValueError/TypeError on genuinely unparseable input.
    """
    from src.committee.schemas import _coerce_nullish

    coerced = _coerce_nullish(value)
    return None if coerced is None else float(coerced)


def _loader_fetch_bars(symbol: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Fetch 1H bars via the backtest loader registry (okx -> ccxt fallback)."""
    from backtest.loaders.registry import get_loader_cls_with_fallback

    last_exc: Exception | None = None
    for source in ("okx", "ccxt"):
        try:
            loader = get_loader_cls_with_fallback(source)()
            frames = loader.fetch(
                [symbol],
                start.strftime("%Y-%m-%d"),
                end.strftime("%Y-%m-%d"),
                None,
                interval="1H",
            )
            df = frames.get(symbol)
            if df is None or getattr(df, "empty", True):
                continue
            return _frame_to_bars(df)
        except Exception as exc:  # try the next source
            last_exc = exc
    raise RuntimeError(f"no bars for {symbol} via okx/ccxt: {last_exc}")


def _frame_to_bars(df: Any) -> list[dict[str, Any]]:
    """Normalize a loader OHLCV DataFrame to [{ts, open, close}, ...] UTC."""
    import pandas as pd

    frame = df.reset_index()
    frame.columns = [str(c).lower() for c in frame.columns]
    ts_col = next(
        (c for c in ("date", "datetime", "time", "timestamp", "index") if c in frame.columns),
        frame.columns[0],
    )
    bars: list[dict[str, Any]] = []
    for _, row in frame.iterrows():
        ts = pd.Timestamp(row[ts_col])
        ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
        bars.append(
            {
                "ts": ts.to_pydatetime(),
                "open": float(row["open"]),
                "close": float(row["close"]),
            }
        )
    return bars


class DecisionJournalTool(BaseTool):
    """Append, resolve, and read committee decisions."""

    name = "decision_journal"
    description = (
        "The committee decision journal (learning loop). Actions: "
        "'append' a new decision (symbol, rating, time_horizon, price_target?, "
        "stop_loss?, take_profit?, position_size_pct?, run_id?) — call after a "
        "portfolio_decision is accepted; "
        "'resolve_due' computes realized 24h/72h/7d returns and alpha vs the "
        "BTC benchmark for pending entries and returns entries needing a "
        "reflection; 'reflect' attaches a 2-4 sentence lesson (entry_id, "
        "reflection); 'lessons' renders past-decision context for a symbol; "
        "'list' returns raw entries; 'pnl' (decision_id or symbol) returns the "
        "decision's paper-trading PnL outcome — 'not executed — no "
        "paper-trading data' when nothing was actually traded."
    )
    is_readonly = False
    repeatable = True
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["append", "resolve_due", "reflect", "lessons", "list", "pnl"],
            },
            "symbol": {
                "type": "string",
                "description": "Asset in loader format, e.g. BTC-USDT (append/lessons/pnl — "
                "pnl resolves the most recent EXECUTED decision for the symbol).",
            },
            "decision_id": {
                "type": "string",
                "description": "Decision (journal entry) id to compute PnL for (pnl). "
                "Provide either decision_id or symbol.",
            },
            "rating": {
                "type": "string",
                "enum": ["Buy", "Overweight", "Hold", "Underweight", "Sell"],
                "description": "Final committee rating (append).",
            },
            "time_horizon": {
                "type": "string",
                "description": "Stated horizon, e.g. '72h swing' (append).",
            },
            "price_target": {"type": "number", "description": "Optional (append)."},
            "stop_loss": {
                "type": "number",
                "description": "Optional protective stop in quote currency (append). "
                "Omit when not determinable.",
            },
            "take_profit": {
                "type": "number",
                "description": "Optional target exit in quote currency (append). "
                "Omit when not determinable.",
            },
            "position_size_pct": {
                "type": "number",
                "description": "Optional position size as percent of equity, 0-100 "
                "(append). Omit when not determinable.",
            },
            "run_id": {"type": "string", "description": "Swarm run id (append)."},
            "entry_id": {"type": "string", "description": "Journal entry id (reflect)."},
            "reflection": {
                "type": "string",
                "description": "2-4 sentence lesson citing the realized alpha (reflect).",
            },
        },
        "required": ["action"],
    }

    @classmethod
    def check_available(cls) -> bool:
        try:
            from src.committee import journal  # noqa: F401
        except Exception:
            return False
        return True

    def execute(self, **kwargs: Any) -> str:
        from src.committee import journal

        action = kwargs.get("action", "")
        try:
            if action == "append":
                missing = [k for k in ("symbol", "rating", "time_horizon") if not kwargs.get(k)]
                if missing:
                    return self._err(f"append requires: {', '.join(missing)}")
                execution: dict[str, float | None] = {}
                for field in ("stop_loss", "take_profit", "position_size_pct"):
                    try:
                        execution[field] = _coerce_optional_float(kwargs.get(field))
                    except (TypeError, ValueError):
                        return self._err(
                            f"append: {field} must be a number (or a nullish string "
                            f"like 'n/a'), got {kwargs.get(field)!r}"
                        )
                entry = journal.append_decision(
                    symbol=kwargs["symbol"],
                    rating=kwargs["rating"],
                    time_horizon=kwargs["time_horizon"],
                    price_target=kwargs.get("price_target"),
                    run_id=kwargs.get("run_id"),
                    **execution,
                )
                result: dict[str, Any] = {
                    "status": "ok",
                    "entry_id": entry["id"],
                    "entry": entry,
                }
                # Paper-trading execution hook (Task 5): translate the
                # just-journaled decision into a paper order. Never fails the
                # append — maybe_execute_paper catches everything and returns
                # None fast when VIBE_PAPER_ENABLED is falsy, in which case
                # the paper_execution key is omitted entirely (not null).
                from src.paper.hook import maybe_execute_paper

                paper_execution = maybe_execute_paper(entry)
                if paper_execution is not None:
                    result["paper_execution"] = paper_execution
                return json.dumps(result, ensure_ascii=False, default=str)

            if action == "resolve_due":
                benchmark = os.getenv(BENCHMARK_ENV, journal.DEFAULT_BENCHMARK)
                result = journal.resolve_due(_loader_fetch_bars, benchmark=benchmark)
                return json.dumps(
                    {
                        "status": "ok",
                        "resolved": [f"{eid}@{h}" for eid, h in result["resolved"]],
                        "reflection_due": result["reflection_due"],
                        "errors": result["errors"],
                        "next_step": (
                            "For each entry in reflection_due, write a 2-4 sentence "
                            "reflection citing the realized alpha, then call this tool "
                            "with action='reflect'."
                        ),
                    },
                    ensure_ascii=False,
                    default=str,
                )

            if action == "reflect":
                if not kwargs.get("entry_id") or not kwargs.get("reflection"):
                    return self._err("reflect requires entry_id and reflection")
                entry = journal.write_reflection(kwargs["entry_id"], kwargs["reflection"])
                return json.dumps(
                    {"status": "ok", "entry_id": entry["id"]}, ensure_ascii=False
                )

            if action == "lessons":
                if not kwargs.get("symbol"):
                    return self._err("lessons requires symbol")
                block = journal.lessons_block(kwargs["symbol"])
                return json.dumps(
                    {"status": "ok", "lessons_markdown": block}, ensure_ascii=False
                )

            if action == "list":
                return json.dumps(
                    {"status": "ok", "entries": journal.load_entries()},
                    ensure_ascii=False,
                )

            if action == "pnl":
                return self._pnl(kwargs, journal)

            return self._err(
                "Unknown action {0!r}. Valid: append, resolve_due, reflect, lessons, "
                "list, pnl".format(action)
            )
        except KeyError as exc:
            return self._err(str(exc))

    @staticmethod
    def _err(message: str) -> str:
        return json.dumps({"status": "error", "error": message}, ensure_ascii=False)

    def _pnl(self, kwargs: dict[str, Any], journal: Any) -> str:
        """Handle action='pnl'. Instructive (not an error) when nothing was
        actually traded: 'not executed — no paper-trading data'."""
        decision_id = kwargs.get("decision_id")
        symbol = kwargs.get("symbol")
        if not decision_id and not symbol:
            return self._err("pnl requires decision_id or symbol")

        from src.paper.pnl import decision_pnl
        from src.paper.store import PaperStore, paper_root

        store = PaperStore(paper_root())

        if decision_id:
            result = decision_pnl(decision_id, store=store)
            if result.get("executed"):
                return json.dumps({"status": "ok", **result}, ensure_ascii=False, default=str)
            return json.dumps(
                {
                    "status": "ok",
                    "decision_id": decision_id,
                    "executed": False,
                    "summary": NOT_EXECUTED_MESSAGE,
                },
                ensure_ascii=False,
            )

        # symbol lookup: most recent EXECUTED decision for it, newest first.
        candidates = [e for e in journal.load_entries() if e.get("symbol") == symbol]
        candidates.sort(key=lambda e: e.get("decided_at") or "", reverse=True)
        for entry in candidates:
            result = decision_pnl(entry["id"], store=store)
            if result.get("executed"):
                return json.dumps({"status": "ok", **result}, ensure_ascii=False, default=str)

        return json.dumps(
            {
                "status": "ok",
                "symbol": symbol,
                "executed": False,
                "summary": NOT_EXECUTED_MESSAGE,
            },
            ensure_ascii=False,
        )
