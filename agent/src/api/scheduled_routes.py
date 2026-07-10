"""Scheduled research HTTP routes.

Mounted by ``agent/api_server.py`` via ``register_scheduled_routes(app, ...)``.
"""

from __future__ import annotations

import logging
import os
import sys as _sys
import time
import uuid
from typing import Any, Awaitable, Callable, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, status
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SCHEDULED_RESEARCH_SCHEDULER_ENV = "VIBE_TRADING_ENABLE_SCHEDULER"
_SCHEDULED_RESEARCH_TRUE_VALUES = {"1", "true", "yes", "on"}

# ---------------------------------------------------------------------------
# Phase 6 — decision-journal reflection job
#
# The committee's decision journal (src/committee/journal.py) previously
# resolved past decisions (24h/72h/7d realized returns + alpha) only when the
# next committee run's reflection officer happened to fire — so on a day
# with no committee run, due outcomes just sat unresolved and no lesson
# accrued. When the scheduler is enabled this well-known job is registered
# once (idempotent — a restart never clobbers a user's edited schedule) so
# resolution and reflection happen daily regardless of committee activity.
# The prompt is dispatched through the ordinary session runtime (see
# ``_dispatch_scheduled_research_job`` below), so it runs with a real agent
# turn — writing an actual lesson (action='reflect') needs judgment, not
# just arithmetic (action='resolve_due' is pure and lookahead-safe, but the
# reflection text is not mechanically derivable).
#
# Users who keep VIBE_TRADING_ENABLE_SCHEDULER unset/0 get no automatic job;
# the CLI equivalent for a system-cron entry is documented in
# docs/minimax-migration-notes.md (Phase 6) — that doc is the more
# discoverable spot for an end-user config decision than this module's
# docstring, since it's the file cfg.md and the other phase docs already
# point users to for env var behavior.
# ---------------------------------------------------------------------------

DECISION_JOURNAL_JOB_ID = "decision-journal-reflection"
# Daily at 00:00 UTC. Overridable for operators who want a different time of
# day without touching code — the job is upserted with whatever schedule is
# in the store already (see _ensure_decision_journal_job), so changing this
# default only affects brand-new installs, never an existing job.
DECISION_JOURNAL_JOB_SCHEDULE = "0 0 * * *"
DECISION_JOURNAL_JOB_PROMPT = (
    "You are running the committee's scheduled reflection pass. There is no "
    "live debate today — you are only closing the loop on PAST decisions.\n\n"
    "1. Call the decision_journal tool with action='resolve_due'. This computes "
    "realized 24h/72h/7d returns and alpha vs the configured benchmark for every "
    "pending decision that has reached a due horizon.\n"
    "2. For EACH entry in the returned reflection_due list: write a 2-4 sentence "
    "reflection citing the realized raw return and alpha at the primary horizon, "
    "and state plainly whether the rating was directionally right. End with one "
    "transferable lesson. Save it via decision_journal action='reflect' "
    "(entry_id, reflection).\n"
    "3. Reply with a one-line summary: how many horizons were resolved and how "
    "many reflections were written this run. If resolve_due reported any "
    "errors, include them verbatim.\n"
    "If nothing was due, say so and stop — do not fabricate a decision or a "
    "reflection for an entry that isn't due."
)


def _ensure_decision_journal_job(store) -> None:
    """Register the daily resolve_due + reflect job if not already persisted.

    Idempotent and non-clobbering: called on every startup while the
    scheduler is enabled, but a job that already exists (whatever schedule
    or prompt it currently has — including a user's own edits) is left
    untouched, so a restart never resets ``next_run_at`` or discards an
    edit.
    """
    if store.get(DECISION_JOURNAL_JOB_ID) is not None:
        return

    from src.scheduled_research.models import ScheduledResearchJob

    store.upsert(
        ScheduledResearchJob(
            id=DECISION_JOURNAL_JOB_ID,
            prompt=DECISION_JOURNAL_JOB_PROMPT,
            schedule=DECISION_JOURNAL_JOB_SCHEDULE,
        )
    )


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_scheduled_research_store: Any = None
_scheduled_research_executor: Any = None


def _scheduled_research_scheduler_enabled() -> bool:
    """Return whether scheduled research execution is enabled."""
    return (
        os.getenv(_SCHEDULED_RESEARCH_SCHEDULER_ENV, "").strip().lower()
        in _SCHEDULED_RESEARCH_TRUE_VALUES
    )


def _get_scheduled_research_store():
    """Return the singleton ScheduledResearchJobStore, creating it on first call."""
    global _scheduled_research_store
    if _scheduled_research_store is None:
        from src.scheduled_research.store import ScheduledResearchJobStore

        _scheduled_research_store = ScheduledResearchJobStore()
    return _scheduled_research_store


async def _dispatch_scheduled_research_job(job) -> None:
    """Enqueue one scheduled research job through the session runtime.

    ``send_message`` queues the agent attempt and returns once accepted; it
    does not wait for that agent run to reach a terminal status. The executor's
    ``COMPLETED`` state for this dispatch path means "successfully enqueued."
    """
    host = _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")
    svc = host._get_session_service()
    if not svc:
        raise RuntimeError("Session runtime not enabled")
    # Pass a copy so the session runtime's internal config writes (e.g.
    # include_shell_tools) do not mutate the persisted scheduled-run config.
    job_config = dict(job.config)
    job_config["governance_surface"] = "scheduler"
    session = svc.create_session(
        title=f"scheduled-research:{job.id}", config=job_config
    )
    logger.info(
        "dispatching scheduled research job %s via session %s",
        job.id,
        session.session_id,
    )
    await svc.send_message(session.session_id, job.prompt)


def _get_scheduled_research_executor():
    """Return the singleton scheduled research executor."""
    global _scheduled_research_executor
    if _scheduled_research_executor is None:
        from src.scheduled_research.executor import ScheduledResearchExecutor

        _scheduled_research_executor = ScheduledResearchExecutor(
            _get_scheduled_research_store(),
            _dispatch_scheduled_research_job,
            enabled=_scheduled_research_scheduler_enabled(),
        )
    return _scheduled_research_executor


def _start_scheduled_research_executor() -> None:
    """Start scheduled research execution when explicitly enabled.

    Also registers the Phase 6 decision-journal reflection job (idempotent)
    so daily resolve_due + reflect runs regardless of committee activity.
    """
    if not _scheduled_research_scheduler_enabled():
        return
    _ensure_decision_journal_job(_get_scheduled_research_store())
    _get_scheduled_research_executor().start()


async def _stop_scheduled_research_executor() -> None:
    """Stop scheduled research execution if it was started."""
    executor = _scheduled_research_executor
    if executor is not None:
        await executor.stop()


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class CreateScheduledRunRequest(BaseModel):
    """Request body for POST /scheduled-runs."""

    id: Optional[str] = Field(
        None, description="Job id; auto-generated UUID when omitted"
    )
    prompt: str = Field(
        ..., min_length=1, description="Research prompt or backtest description"
    )
    schedule: str = Field(
        ..., min_length=1, description="Interval-ms or 5-field cron expression"
    )
    next_run_at: Optional[int] = Field(
        None, description="Epoch-ms for next run; defaults to now"
    )
    config: Dict[str, Any] = Field(
        default_factory=dict, description="Optional backtest parameters"
    )


class ScheduledRunResponse(BaseModel):
    """API response for a single scheduled job."""

    id: str
    prompt: str
    schedule: str
    next_run_at: int
    status: str
    created_at: int
    config: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

AuthDep = Callable[..., Awaitable[Any] | Any]


def register_scheduled_routes(
    app: FastAPI,
    require_auth: AuthDep | None = None,
) -> None:
    """Mount the scheduled routes onto ``app``.

    Resolves ``require_auth`` from the host ``api_server`` module via
    ``sys.modules`` when not passed explicitly.
    """
    host = _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")

    if host is None:
        raise RuntimeError(
            "register_scheduled_routes: api_server module not in sys.modules; "
            "ensure api_server is imported before calling this function"
        )

    if require_auth is None:
        require_auth = host.require_auth

    def _host_validate_path_param(value: str, kind: str) -> None:
        h = _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")
        h._validate_path_param(value, kind)

    # --- Routes ---

    @app.post(
        "/scheduled-runs",
        response_model=ScheduledRunResponse,
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(require_auth)],
    )
    async def create_scheduled_run(
        request: CreateScheduledRunRequest,
    ) -> ScheduledRunResponse:
        """Create (or replace) a scheduled research job.

        The job is persisted immediately. No execution is triggered.
        """
        from src.scheduled_research.models import (
            JobStatus,
            ScheduledResearchJob,
            validate_schedule,
        )

        try:
            validate_schedule(request.schedule)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        now_ms = int(time.time() * 1000)
        job = ScheduledResearchJob(
            id=request.id or str(uuid.uuid4()),
            prompt=request.prompt,
            schedule=request.schedule,
            next_run_at=request.next_run_at if request.next_run_at is not None else now_ms,
            status=JobStatus.PENDING,
            created_at=now_ms,
            config=request.config,
        )
        _get_scheduled_research_store().upsert(job)
        return ScheduledRunResponse(**job.to_dict())

    @app.get(
        "/scheduled-runs",
        response_model=List[ScheduledRunResponse],
        dependencies=[Depends(require_auth)],
    )
    async def list_scheduled_runs(
        status_filter: Optional[str] = Query(None, alias="status"),
        limit: int = Query(50, ge=1, le=200),
    ) -> List[ScheduledRunResponse]:
        """List scheduled research jobs, optionally filtered by status."""
        jobs = _get_scheduled_research_store().list_jobs(
            status=status_filter, limit=limit
        )
        return [ScheduledRunResponse(**j.to_dict()) for j in jobs]

    @app.delete(
        "/scheduled-runs/{job_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=[Depends(require_auth)],
    )
    async def delete_scheduled_run(job_id: str) -> None:
        """Cancel (delete) a scheduled research job by id."""
        _host_validate_path_param(job_id, "job_id")
        removed = _get_scheduled_research_store().delete(job_id)
        if not removed:
            raise HTTPException(
                status_code=404, detail=f"scheduled run {job_id} not found"
            )
