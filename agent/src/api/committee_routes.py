"""Committee observatory REST routes (read-only, all GET).

Mounted by ``agent/api_server.py`` via ``register_committee_routes(app, ...)``.
Auth/host-symbol resolution mirrors ``register_scheduled_routes`` (host
``api_server`` via ``sys.modules``). Run listing REUSES ``SwarmStore``
(no re-globbing of ``.swarm/runs``); the journal join goes through
``src.committee.journal.load_entries``. Task R3 appends /journal/decisions,
/scheduler/health and /mcp/status to this same register function.
"""

from __future__ import annotations

import json
import sys as _sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from fastapi import Depends, FastAPI, HTTPException, Query

AuthDep = Callable[..., Awaitable[Any] | Any]

_COMMITTEE_PRESET = "crypto_committee"

# Safety ceiling on how many total swarm runs (all presets) /committee/runs
# scans looking for crypto_committee rows. SwarmStore.list_runs already reads
# every run.json under the runs root before sorting/truncating, so raising
# this ceiling costs nothing extra beyond that existing full scan — it only
# bounds how far back we're willing to look for committee rows among heavy
# non-committee swarm usage. A low, hardcoded cap here previously dropped
# committee runs older than the newest N runs overall even when the caller's
# `limit` was far from satisfied. Sized generously (thousands) so it only
# bites in extreme run-volume scenarios.
_RUN_SCAN_CEILING = 5000

# agent_id -> pipeline phase (analysts -> debate -> research_manager -> trader
# -> risk -> portfolio_manager -> reflection). Unknown seats fall back to "other".
_PHASE_BY_AGENT = {
    "market_analyst": "analysts", "news_analyst": "analysts",
    "onchain_analyst": "analysts", "sentiment_analyst": "analysts",
    "bull_researcher": "debate", "bear_researcher": "debate",
    "research_manager": "research_manager",
    "trader": "trader",
    "risky_analyst": "risk", "neutral_analyst": "risk", "safe_analyst": "risk",
    "portfolio_manager": "portfolio_manager",
    "reflection_officer": "reflection",
}


def _swarm_runs_root() -> Path:
    """Resolve the swarm runs directory (single source of truth).

    Wrapped so tests can monkeypatch it to a tmp dir without touching the real
    ``agent/.swarm/runs``.
    """
    from src.swarm.store import swarm_runs_root

    return swarm_runs_root()


def _swarm_store():
    """Construct a SwarmStore over the runs root (cheap; a Path holder)."""
    from src.swarm.store import SwarmStore

    return SwarmStore(base_dir=_swarm_runs_root())


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _wall_clock_s(created_at: str | None, completed_at: str | None) -> float | None:
    start, end = _parse_iso(created_at), _parse_iso(completed_at)
    if start is None or end is None:
        return None
    return (end - start).total_seconds()


def _debate_round(task_id: str) -> int:
    """Round number from the ``-r{n}`` debate suffix (default 1)."""
    marker = "-r"
    idx = task_id.rfind(marker)
    if idx != -1:
        suffix = task_id[idx + len(marker):]
        if suffix.isdigit():
            return int(suffix)
    return 1


def _journal_by_run_id() -> dict[str, dict]:
    """Map run_id -> latest journal entry for join (last write wins)."""
    from src.committee.journal import load_entries

    out: dict[str, dict] = {}
    for entry in load_entries():
        rid = entry.get("run_id")
        if rid:
            out[rid] = entry
    return out


def _pnl_summary_for(decision_id: str) -> str | None:
    from src.paper import pnl as pnl_mod
    from src.paper.store import PaperStore, paper_root

    try:
        return pnl_mod.decision_pnl(decision_id, PaperStore(paper_root())).get("summary")
    except Exception:  # never let a PnL read fail the list
        return None


def _list_item(run, journal_map: dict[str, dict]) -> dict[str, Any]:
    item: dict[str, Any] = {
        "run_id": run.id,
        "created_at": run.created_at,
        "status": run.status.value,
        "target": run.user_vars.get("target"),
        "wall_clock_s": _wall_clock_s(run.created_at, run.completed_at),
        "input_tokens": run.total_input_tokens,
        "output_tokens": run.total_output_tokens,
    }
    entry = journal_map.get(run.id)
    if entry:
        item["decision_id"] = entry["id"]
        item["rating"] = entry.get("rating")
        item["journal_status"] = entry.get("status")
        item["pnl_summary"] = _pnl_summary_for(entry["id"])
    return item


def _read_report(run_dir: Path, agent_id: str) -> dict[str, Any]:
    p = run_dir / "artifacts" / agent_id / "report.md"
    if not p.exists():
        return {"report_md": None, "missing": True}
    try:
        return {"report_md": p.read_text(encoding="utf-8")}
    except OSError as exc:  # corrupt/unreadable -> per-seat error, not 500
        return {"report_md": None, "error": str(exc)}


def _read_decision(run_dir: Path) -> dict[str, Any]:
    p = run_dir / "artifacts" / "portfolio_manager" / "decision.portfolio_decision.json"
    if not p.exists():
        return {"missing": True}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return {"error": str(exc)}


def register_committee_routes(app: FastAPI, require_auth: AuthDep | None = None) -> None:
    """Mount the committee observatory routes onto ``app``."""
    host = _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")
    if host is None:
        raise RuntimeError(
            "register_committee_routes: api_server module not in sys.modules; "
            "ensure api_server is imported before calling this function"
        )
    if require_auth is None:
        require_auth = host.require_auth

    def _host_validate_path_param(value: str, kind: str) -> None:
        h = _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")
        h._validate_path_param(value, kind)

    @app.get("/committee/runs", dependencies=[Depends(require_auth)])
    async def committee_runs(
        limit: int = Query(20, ge=1, le=100),
        status: Optional[str] = Query(None),
        symbol: Optional[str] = Query(None),
    ):
        """Newest-first crypto_committee runs joined with journal entries.

        A corrupt run.json is skipped by ``SwarmStore.list_runs`` (the list
        never fails because one run is unreadable).
        """
        journal_map = _journal_by_run_id()
        runs = _swarm_store().list_runs(limit=_RUN_SCAN_CEILING)
        sym = symbol.upper() if symbol else None
        items: list[dict[str, Any]] = []
        for run in runs:
            if run.preset_name != _COMMITTEE_PRESET:
                continue
            if status is not None and run.status.value != status:
                continue
            if sym is not None and (run.user_vars.get("target") or "").upper() != sym:
                continue
            items.append(_list_item(run, journal_map))
            if len(items) >= limit:
                break
        return items

    @app.get("/committee/runs/{run_id}", dependencies=[Depends(require_auth)])
    async def committee_run_detail(run_id: str):
        """Full discussion for one run: seats, debate structure, decision,
        journal outcome, and paper PnL. Missing artifacts -> explicit markers,
        never fabricated."""
        _host_validate_path_param(run_id, "run_id")
        run = _swarm_store().load_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

        run_dir = _swarm_runs_root() / run_id
        decision = _read_decision(run_dir)
        seats: list[dict[str, Any]] = []
        debate_order: list[str] = []
        debate_rounds = 1
        for task in run.tasks:
            phase = _PHASE_BY_AGENT.get(task.agent_id, "other")
            rnd = _debate_round(task.id)
            seat: dict[str, Any] = {
                "agent_id": task.agent_id,
                "phase": phase,
                "round": rnd,
                "status": task.status.value,
            }
            seat.update(_read_report(run_dir, task.agent_id))
            if task.agent_id == "portfolio_manager":
                seat["decision_json"] = decision
            seats.append(seat)
            if phase == "debate":
                debate_order.append(task.id)
                debate_rounds = max(debate_rounds, rnd)

        journal_map = _journal_by_run_id()
        entry = journal_map.get(run_id)
        journal_block = None
        pnl_block = None
        if entry:
            journal_block = {
                "horizons": entry.get("horizons"),
                "reflection": entry.get("reflection"),
                "reflected_at": entry.get("reflected_at"),
            }
            from src.paper import pnl as pnl_mod
            from src.paper.store import PaperStore, paper_root

            try:
                pnl_block = pnl_mod.decision_pnl(entry["id"], PaperStore(paper_root()))
            except Exception:
                pnl_block = None

        return {
            "run": {
                "run_id": run.id,
                "status": run.status.value,
                "target": run.user_vars.get("target"),
                "timeframe": run.user_vars.get("timeframe"),
                "created_at": run.created_at,
                "completed_at": run.completed_at,
                "wall_clock_s": _wall_clock_s(run.created_at, run.completed_at),
                "input_tokens": run.total_input_tokens,
                "output_tokens": run.total_output_tokens,
            },
            "seats": seats,
            "debate": {"rounds": debate_rounds, "order": debate_order},
            "decision": decision,
            "journal": journal_block,
            "pnl": pnl_block,
        }
