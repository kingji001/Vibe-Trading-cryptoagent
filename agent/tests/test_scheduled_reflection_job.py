"""Tests for the Phase 6 scheduled decision-journal reflection job.

``src.api.scheduled_routes`` owns the scheduled-research store/executor
singletons and the ``VIBE_TRADING_ENABLE_SCHEDULER`` gate (see
``_start_scheduled_research_executor``). Phase 6 adds one more piece of
startup wiring: when the scheduler is enabled, a daily job is registered
that instructs the agent to run ``decision_journal action=resolve_due`` then
``action=reflect`` for every entry that needs it — so 24h/72h/7d outcomes
resolve and lessons accrue even on days with no committee run.

Registration (``_ensure_decision_journal_job``) and dispatch reuse the
already-tested generic store/executor plumbing (``ScheduledResearchJobStore``,
``ScheduledResearchExecutor``) unchanged — these tests only cover the new
piece: the job gets created, is idempotent across restarts (never resets a
user's edited schedule), and its prompt drives both protocol steps through
one ordinary executor tick. No network, no session runtime involved.
"""

from __future__ import annotations

import asyncio

import pytest

from src.api.scheduled_routes import (
    DECISION_JOURNAL_JOB_ID,
    DECISION_JOURNAL_JOB_SCHEDULE,
    _ensure_decision_journal_job,
)
from src.scheduled_research.executor import ScheduledResearchExecutor
from src.scheduled_research.models import JobStatus
from src.scheduled_research.store import ScheduledResearchJobStore


@pytest.fixture()
def store(tmp_path):
    return ScheduledResearchJobStore(path=tmp_path / "scheduled_research_jobs.json")


def test_ensure_decision_journal_job_registers_with_expected_prompt(store):
    _ensure_decision_journal_job(store)
    job = store.get(DECISION_JOURNAL_JOB_ID)

    assert job is not None
    assert job.schedule == DECISION_JOURNAL_JOB_SCHEDULE
    assert "resolve_due" in job.prompt
    assert "reflect" in job.prompt
    assert "decision_journal" in job.prompt


def test_ensure_decision_journal_job_is_idempotent(store):
    _ensure_decision_journal_job(store)
    first = store.get(DECISION_JOURNAL_JOB_ID)

    _ensure_decision_journal_job(store)  # e.g. a second server startup
    second = store.get(DECISION_JOURNAL_JOB_ID)

    assert first.created_at == second.created_at  # not re-created


def test_ensure_decision_journal_job_preserves_user_edits(store):
    _ensure_decision_journal_job(store)
    job = store.get(DECISION_JOURNAL_JOB_ID)
    job.schedule = "0 6 * * *"  # user (or operator) edits the schedule
    store.upsert(job)

    _ensure_decision_journal_job(store)  # restart must not clobber the edit

    assert store.get(DECISION_JOURNAL_JOB_ID).schedule == "0 6 * * *"


def test_registered_job_dispatches_through_one_executor_tick(store):
    _ensure_decision_journal_job(store)
    dispatched: list = []

    async def fake_dispatch(job) -> None:
        dispatched.append(job)

    job = store.get(DECISION_JOURNAL_JOB_ID)
    executor = ScheduledResearchExecutor(store, fake_dispatch, now_fn=lambda: job.next_run_at + 1)

    asyncio.run(executor.tick())

    assert len(dispatched) == 1
    assert dispatched[0].id == DECISION_JOURNAL_JOB_ID
    assert "resolve_due" in dispatched[0].prompt and "reflect" in dispatched[0].prompt
    assert store.get(DECISION_JOURNAL_JOB_ID).status == JobStatus.COMPLETED
    # advanced to the next due time per its own cron schedule, not deleted
    assert store.get(DECISION_JOURNAL_JOB_ID).next_run_at > job.next_run_at
