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
# Paper-trading loop, Task 5 — scheduled paper-trading-tick job
#
# The paper broker's conditional orders (stops/take-profits) and daily
# mark-to-market snapshot only advance when something calls
# ``src.paper.tick.run_tick`` once per UTC day. Rather than invent a new
# dispatch path, this reuses the exact same scheduled-research store/executor
# plumbing as the Phase 6 decision-journal job above: a lightweight agent
# tool (``paper_tick`` — ``src/tools/paper_tick_tool.py``, wraps ``run_tick``,
# no parameters) is called once by the scheduled agent turn, mirroring how
# the reflection job calls ``decision_journal``.
#
# Double-gated: registered only when the scheduler is enabled AND paper
# trading itself is enabled (``VIBE_PAPER_ENABLED``, same kill-switch rule as
# the rest of the paper package — unset means enabled). A user who disables
# paper trading after the job was already registered keeps the (now inert)
# job — ``paper_tick`` calls ``run_tick``, which itself honors the kill
# switch and no-ops fast with a disabled marker. Disabling therefore FREEZES
# existing positions: no conditional stop/TP evaluation, no mark-to-market
# snapshot, and no new trades, until the switch is turned back on.
# ---------------------------------------------------------------------------

PAPER_TICK_JOB_ID = "paper-trading-tick"
# 00:30 UTC — after the 00:00 decision-journal reflection job, so a paper
# position's mark-to-market/conditional-order tick runs once the day's
# reflections (if any) have already been written. Overridable at INITIAL
# registration only via VIBE_PAPER_TICK_SCHEDULE (see
# _ensure_paper_trading_tick_job); the recommended 2-hourly intraday
# deployment sets "30 */2 * * *" alongside VIBE_PAPER_TICK_INTERVAL=1H.
PAPER_TICK_JOB_SCHEDULE = "30 0 * * *"
_PAPER_TICK_SCHEDULE_ENV = "VIBE_PAPER_TICK_SCHEDULE"
PAPER_TICK_JOB_PROMPT = (
    "You are running the scheduled daily paper-trading tick. This is a "
    "mechanical maintenance run, not a trading decision — do not analyze "
    "the market and do not call any tool other than the one below.\n\n"
    "1. Call the paper_tick tool exactly once, with no arguments.\n"
    "2. Reply with a one-line summary of its result: how many conditional "
    "fills triggered, the current equity, and how many positions were "
    "marked stale. If the tool reported any errors, include them verbatim.\n"
    "Never fabricate results — report exactly what the tool returned."
)

_PAPER_ENABLED_ENV = "VIBE_PAPER_ENABLED"


def _paper_trading_enabled() -> bool:
    """``VIBE_PAPER_ENABLED`` truthiness: unset -> enabled; "0"/"false"/"" -> disabled.

    Same canonical kill-switch rule as ``src.paper.translator._paper_enabled``
    / ``src.paper.hook._paper_enabled``, duplicated locally (same pattern as
    this module's own ``_scheduled_research_scheduler_enabled``) so gating
    job registration never has to import the paper package.
    """
    val = os.environ.get(_PAPER_ENABLED_ENV)
    if val is None:
        return True
    return val.strip().lower() not in ("0", "false", "")


def _ensure_paper_trading_tick_job(store) -> None:
    """Register the paper_tick job if not already persisted.

    Idempotent and non-clobbering, identical contract to
    ``_ensure_decision_journal_job``: a job that already exists — whatever
    schedule or prompt it currently has, including a user's own edits — is
    left untouched on every subsequent call (e.g. a server restart).

    ``VIBE_PAPER_TICK_SCHEDULE`` (default ``"30 0 * * *"``) sets the cron
    schedule for the INITIAL registration only. Because registration is
    non-clobbering, changing that env after the job already exists does NOT
    rewrite the persisted schedule — the operator must edit the job (or delete
    it so a restart re-registers it) to change an existing job's cadence.
    """
    if store.get(PAPER_TICK_JOB_ID) is not None:
        return

    from src.scheduled_research.models import ScheduledResearchJob

    schedule = os.environ.get(_PAPER_TICK_SCHEDULE_ENV, "").strip() or PAPER_TICK_JOB_SCHEDULE
    store.upsert(
        ScheduledResearchJob(
            id=PAPER_TICK_JOB_ID,
            prompt=PAPER_TICK_JOB_PROMPT,
            schedule=schedule,
        )
    )


# ---------------------------------------------------------------------------
# Two-tier-cadence Task 2 — scheduled committee-run job
#
# The full 13-seat crypto_committee swarm is expensive (deep-tier LLM calls
# across 13 seats), so unlike the reflection/paper-tick jobs above it has NO
# built-in default cadence: it is registered ONLY when the operator opts in
# by setting VIBE_COMMITTEE_SCHEDULE. Leaving it unset is exactly today's
# behavior (no committee job at all) — fully additive.
#
# VIBE_COMMITTEE_SYMBOLS (comma list, default BTC-USDT) and
# VIBE_COMMITTEE_TIMEFRAME (default "72h swing") are resolved ONCE, at
# registration time, into the job's prompt text — see
# _build_committee_run_prompt. Because registration is non-clobbering (same
# contract as _ensure_decision_journal_job / _ensure_paper_trading_tick_job),
# changing either env after the job already exists has NO effect on a
# subsequent restart; the operator must delete the job (so a restart
# re-registers it with the new env) or hand-edit its persisted prompt to
# change the symbol universe or timeframe.
#
# The prompt is built as a literal, per-symbol instruction list (not a
# "for each symbol" natural-language loop) so a scheduled agent turn calls
# run_swarm(prompt=..., preset_name="crypto_committee") once per symbol with
# an EXPLICIT "... swarm on <SYMBOL> for a <TIMEFRAME> decision" sentence —
# this exact phrasing is what src.tools.swarm_tool._build_variables parses
# to set the crypto_committee preset's {target}/{timeframe} variables (see
# docs/crypto-committee.md "Running the committee"); it is what the run_swarm
# agent tool actually understands (it takes only prompt/preset_name, no
# structured variables argument — verified: SwarmTool.execute always derives
# variables from prompt text via _build_variables).
#
# run_swarm needs no special declaration to reach this job's session: unlike
# a hypothetical per-session allowlist, src.tools.build_registry registers
# every BaseTool subclass (including SwarmTool) unconditionally for every
# session — the same registry the paper-tick job's session already gets
# (which is how paper_tick reaches it today). Governance (governance_surface
# = "scheduler") gates *execution*, not registry membership, and defaults to
# "observe" mode (denies are logged, not blocking) unless the operator has
# set VIBE_TRADING_GOVERNANCE_MODE=enforce.
# ---------------------------------------------------------------------------

COMMITTEE_RUN_JOB_ID = "committee-run"
_COMMITTEE_SCHEDULE_ENV = "VIBE_COMMITTEE_SCHEDULE"
_COMMITTEE_SYMBOLS_ENV = "VIBE_COMMITTEE_SYMBOLS"
_COMMITTEE_TIMEFRAME_ENV = "VIBE_COMMITTEE_TIMEFRAME"
DEFAULT_COMMITTEE_SYMBOLS = "BTC-USDT"
DEFAULT_COMMITTEE_TIMEFRAME = "72h swing"


def _parse_committee_symbols() -> list[str]:
    """Parse ``VIBE_COMMITTEE_SYMBOLS`` into an ordered, deduped-by-nothing list.

    Comma-separated; each entry is whitespace-stripped and empties (e.g. a
    trailing comma or double comma) are dropped. Unset/blank -> the single
    default ``["BTC-USDT"]``.
    """
    raw = os.environ.get(_COMMITTEE_SYMBOLS_ENV, "")
    symbols = [s.strip() for s in raw.split(",")]
    symbols = [s for s in symbols if s]
    return symbols or [DEFAULT_COMMITTEE_SYMBOLS]


def _build_committee_run_prompt(symbols: list[str], timeframe: str) -> str:
    """Build the committee-run job prompt for a fixed symbol list/timeframe.

    Enumerates every symbol explicitly (not a natural-language "for each")
    so the instruction is unambiguous. Each step passes the instrument and
    horizon through run_swarm's structured ``variables`` parameter — the
    binding channel, validated against the preset and taking precedence
    over prompt extraction — so multi-symbol correctness never depends on
    the scheduling LLM reproducing the prose template verbatim. The prose
    "... swarm on <SYMBOL> for a <TIMEFRAME> decision" sentence is kept
    alongside for human readability (and as a redundant extraction path).
    """
    steps = "\n".join(
        f'{i}. Call run_swarm with preset_name="crypto_committee", '
        f'variables={{"target": "{symbol}", "timeframe": "{timeframe}"}}, and '
        f'prompt="Run the crypto_committee swarm on {symbol} for a '
        f'{timeframe} decision." Wait for it to finish before moving on.'
        for i, symbol in enumerate(symbols, start=1)
    )
    return (
        "You are running the scheduled full crypto investment committee "
        "cycle. This is a real 13-seat analysis + binding decision run for "
        "EACH symbol below, not a mechanical maintenance task.\n\n"
        "Do the following steps IN ORDER, one symbol at a time:\n\n"
        f"{steps}\n\n"
        "After each run_swarm call completes, note its run id and the "
        "portfolio manager's final rating (or, if the run failed or timed "
        "out, the failure reported) before starting the next symbol. If one "
        "symbol's run fails, report the failure verbatim and continue with "
        "the NEXT symbol — do not stop the whole job and do not fabricate a "
        "run id or rating for a run that did not actually complete.\n\n"
        "When every symbol above is done, reply with one line per symbol: "
        "the run id and final rating, or the failure reason. Report exactly "
        "what run_swarm returned — never invent a result."
    )


def _ensure_committee_run_job(store) -> None:
    """Register the committee-run job if not already persisted.

    Registered ONLY when ``VIBE_COMMITTEE_SCHEDULE`` is set (non-empty after
    stripping) — unset means no job at all, unlike the reflection/paper-tick
    jobs which have a built-in default schedule. Idempotent and
    non-clobbering, identical contract to the other ``_ensure_*`` jobs in
    this module: a job that already exists (whatever schedule or prompt it
    currently has, including a user's own edits) is left untouched on every
    subsequent call. ``VIBE_COMMITTEE_SYMBOLS``/``VIBE_COMMITTEE_TIMEFRAME``
    are read once, here, at registration time — see the module comment above
    for why changing them later requires deleting (or hand-editing) the job.
    """
    schedule = os.environ.get(_COMMITTEE_SCHEDULE_ENV, "").strip()
    if not schedule:
        return
    if store.get(COMMITTEE_RUN_JOB_ID) is not None:
        return

    from src.scheduled_research.models import ScheduledResearchJob

    symbols = _parse_committee_symbols()
    timeframe = os.environ.get(_COMMITTEE_TIMEFRAME_ENV, "").strip() or DEFAULT_COMMITTEE_TIMEFRAME
    store.upsert(
        ScheduledResearchJob(
            id=COMMITTEE_RUN_JOB_ID,
            prompt=_build_committee_run_prompt(symbols, timeframe),
            schedule=schedule,
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
    so daily resolve_due + reflect runs regardless of committee activity, the
    Task 5 paper-trading-tick job (idempotent, when paper trading is also
    enabled) so conditional orders and equity mark-to-market advance daily
    too, and — when VIBE_COMMITTEE_SCHEDULE is set — the two-tier-cadence
    committee-run job (idempotent) so the full committee runs on its own
    configured cadence independent of committee activity.
    """
    if not _scheduled_research_scheduler_enabled():
        return
    store = _get_scheduled_research_store()
    _ensure_decision_journal_job(store)
    if _paper_trading_enabled():
        _ensure_paper_trading_tick_job(store)
    _ensure_committee_run_job(store)
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
