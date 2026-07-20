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
import re
from datetime import datetime
from typing import Any

from src.agent.tools import BaseTool

BENCHMARK_ENV = "VIBE_COMMITTEE_BENCHMARK"
NOT_EXECUTED_MESSAGE = "not executed — no paper-trading data"


def _derive_run_id(run_dir: Any) -> str | None:
    """Extract the swarm run id from an injected ``run_dir`` path (review C3).

    The swarm worker injects only ``run_dir`` (``.swarm/runs/<run_id>/...``),
    never ``run_id``. Without a run_id the journal's (run_id, symbol)
    idempotency can't fire, so a retried PM task re-appends and the paper hook
    buys again. Anchored to the ``.swarm/runs/`` segment — a checkout path that
    merely contains ``/runs/`` must not match (it would derive a constant wrong
    run_id and silently dedupe across runs). None when not derivable.
    """
    if not run_dir:
        return None
    match = re.search(r"\.swarm[\\/]runs[\\/]([^\\/]+)", str(run_dir))
    return match.group(1) if match else None


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
                # A price level of 0 (or negative) is not a price — it is the
                # model declining to specify one. Carried downstream it becomes
                # a stop that can never trigger, so resolve it to None (the
                # "not specified" contract the translator already handles) and
                # report the drop instead of silently keeping a fake level.
                # position_size_pct is NOT a price: 0 legitimately means "no
                # position" and must survive.
                dropped_price_fields: list[str] = []
                execution: dict[str, float | None] = {}
                for field in ("stop_loss", "take_profit", "position_size_pct"):
                    try:
                        value = _coerce_optional_float(kwargs.get(field))
                    except (TypeError, ValueError):
                        return self._err(
                            f"append: {field} must be a number (or a nullish string "
                            f"like 'n/a'), got {kwargs.get(field)!r}"
                        )
                    if field != "position_size_pct" and value is not None and value <= 0:
                        dropped_price_fields.append(field)
                        value = None
                    # Review C1: an out-of-range position_size_pct must be
                    # rejected fail-before-write (the schema enforces [0,100] but
                    # the PM reaches the journal directly through this tool).
                    if (
                        field == "position_size_pct"
                        and value is not None
                        and not (0.0 <= value <= 100.0)
                    ):
                        return self._err(
                            f"append: position_size_pct must be within [0, 100], "
                            f"got {value}"
                        )
                    execution[field] = value
                # Review C3 + live incident 2026-07-19: the runtime-injected
                # run_dir is ground truth for run identity. A model-supplied
                # run_id that disagrees is overridden (the PM once mutated its
                # run_id — "-corrected-no-execution", "-final" — to defeat
                # (run_id, symbol) idempotency and spam six rows for one run).
                # Without a derivable run_dir (CLI/manual), the caller's
                # run_id stands.
                # Same rule for price_target (kept on its own path so a valid
                # value passes through in its original numeric shape).
                price_target = kwargs.get("price_target")
                try:
                    pt_value = _coerce_optional_float(price_target)
                except (TypeError, ValueError):
                    pt_value = None  # append_decision keeps its own tolerance
                if pt_value is not None and pt_value <= 0:
                    dropped_price_fields.append("price_target")
                    price_target = None

                supplied = kwargs.get("run_id") or None
                derived = _derive_run_id(kwargs.get("run_dir"))
                corrected = None
                if derived and supplied and supplied != derived:
                    corrected = {"from": supplied, "to": derived}
                run_id = derived or supplied
                already = bool(run_id) and any(
                    e.get("run_id") == run_id and e.get("symbol") == kwargs["symbol"]
                    for e in journal.load_entries()
                )
                entry = journal.append_decision(
                    symbol=kwargs["symbol"],
                    rating=kwargs["rating"],
                    time_horizon=kwargs["time_horizon"],
                    price_target=price_target,
                    run_id=run_id,
                    **execution,
                )
                result: dict[str, Any] = {
                    "status": "ok",
                    "entry_id": entry["id"],
                    "entry": entry,
                }
                if dropped_price_fields:
                    result["dropped_price_fields"] = dropped_price_fields
                    result["note_price_fields"] = (
                        "Dropped as not-a-price (must be > 0): "
                        + ", ".join(dropped_price_fields)
                        + ". Supply a real level or omit the field."
                    )
                if corrected:
                    result["run_id_corrected"] = corrected
                if already:
                    # Explicit stop signal — a silent identical-looking success
                    # is what sent the PM into its re-append "correction" loop.
                    result["deduplicated"] = True
                    result["note"] = (
                        "This (run_id, symbol) decision was already journaled — "
                        "no new entry was created. Do NOT append again this run."
                    )
                    return json.dumps(result, ensure_ascii=False, default=str)
                # Paper-trading execution hook (Task 5): translate the
                # just-journaled decision into a paper order. Never fails the
                # append — maybe_execute_paper catches everything and returns
                # None fast when VIBE_PAPER_ENABLED is falsy, in which case
                # the paper_execution key is omitted entirely (not null).
                # Deduped repeats return above: the original append already
                # ran the hook.
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
                    # Instructive headline + the function's evidence-bearing
                    # summary (WHY nothing executed: noop notes such as "sell
                    # signal with no position", a mandate message, or "price
                    # unavailable — not executed").
                    "summary": NOT_EXECUTED_MESSAGE + "\n" + result["summary"],
                },
                ensure_ascii=False,
            )

        # symbol lookup: most recent EXECUTED decision for it, newest first.
        candidates = [e for e in journal.load_entries() if e.get("symbol") == symbol]
        candidates.sort(key=lambda e: e.get("decided_at") or "", reverse=True)
        newest_result: dict | None = None
        for entry in candidates:
            result = decision_pnl(entry["id"], store=store)
            if result.get("executed"):
                return json.dumps({"status": "ok", **result}, ensure_ascii=False, default=str)
            if newest_result is None:
                newest_result = result

        summary = NOT_EXECUTED_MESSAGE
        if newest_result is not None:
            # Surface the newest candidate's evidence (noop notes) too.
            summary += "\n" + newest_result["summary"]
        return json.dumps(
            {
                "status": "ok",
                "symbol": symbol,
                "executed": False,
                "summary": summary,
            },
            ensure_ascii=False,
        )
