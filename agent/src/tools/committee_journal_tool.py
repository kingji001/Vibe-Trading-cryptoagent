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

Learning-loop design adapted from TauricResearch/TradingAgents (Apache-2.0).
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

from src.agent.tools import BaseTool

BENCHMARK_ENV = "VIBE_COMMITTEE_BENCHMARK"


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
        "run_id?) — call after a portfolio_decision is accepted; "
        "'resolve_due' computes realized 24h/72h/7d returns and alpha vs the "
        "BTC benchmark for pending entries and returns entries needing a "
        "reflection; 'reflect' attaches a 2-4 sentence lesson (entry_id, "
        "reflection); 'lessons' renders past-decision context for a symbol; "
        "'list' returns raw entries."
    )
    is_readonly = False
    repeatable = True
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["append", "resolve_due", "reflect", "lessons", "list"],
            },
            "symbol": {
                "type": "string",
                "description": "Asset in loader format, e.g. BTC-USDT (append/lessons).",
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
                entry = journal.append_decision(
                    symbol=kwargs["symbol"],
                    rating=kwargs["rating"],
                    time_horizon=kwargs["time_horizon"],
                    price_target=kwargs.get("price_target"),
                    run_id=kwargs.get("run_id"),
                )
                return json.dumps(
                    {"status": "ok", "entry_id": entry["id"], "entry": entry},
                    ensure_ascii=False,
                )

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

            return self._err(
                f"Unknown action {action!r}. Valid: append, resolve_due, reflect, lessons, list"
            )
        except KeyError as exc:
            return self._err(str(exc))

    @staticmethod
    def _err(message: str) -> str:
        return json.dumps({"status": "error", "error": message}, ensure_ascii=False)
