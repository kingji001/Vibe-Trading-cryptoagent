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
    PAPER_TICK_JOB_ID,
    PAPER_TICK_JOB_SCHEDULE,
    _ensure_decision_journal_job,
    _ensure_paper_trading_tick_job,
    _paper_trading_enabled,
    _start_scheduled_research_executor,
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


# ---------------------------------------------------------------------------
# Task 5 — paper-trading-tick scheduled job
#
# Same registration/idempotency/dispatch contract as the decision-journal job
# above, but double-gated: registered only when BOTH
# VIBE_TRADING_ENABLE_SCHEDULER and VIBE_PAPER_ENABLED are truthy (the paper
# kill switch's canonical rule — unset means enabled).
# ---------------------------------------------------------------------------


def test_ensure_paper_trading_tick_job_registers_with_expected_prompt(store):
    _ensure_paper_trading_tick_job(store)
    job = store.get(PAPER_TICK_JOB_ID)

    assert job is not None
    assert job.schedule == PAPER_TICK_JOB_SCHEDULE == "30 0 * * *"
    assert "paper_tick" in job.prompt


def test_ensure_paper_trading_tick_job_is_idempotent(store):
    _ensure_paper_trading_tick_job(store)
    first = store.get(PAPER_TICK_JOB_ID)

    _ensure_paper_trading_tick_job(store)  # e.g. a second server startup
    second = store.get(PAPER_TICK_JOB_ID)

    assert first.created_at == second.created_at  # not re-created


def test_ensure_paper_trading_tick_job_preserves_user_edits(store):
    _ensure_paper_trading_tick_job(store)
    job = store.get(PAPER_TICK_JOB_ID)
    job.schedule = "0 1 * * *"  # user (or operator) edits the schedule
    store.upsert(job)

    _ensure_paper_trading_tick_job(store)  # restart must not clobber the edit

    assert store.get(PAPER_TICK_JOB_ID).schedule == "0 1 * * *"


def test_paper_tick_job_dispatches_through_one_executor_tick(store):
    _ensure_paper_trading_tick_job(store)
    dispatched: list = []

    async def fake_dispatch(job) -> None:
        dispatched.append(job)

    job = store.get(PAPER_TICK_JOB_ID)
    executor = ScheduledResearchExecutor(store, fake_dispatch, now_fn=lambda: job.next_run_at + 1)

    asyncio.run(executor.tick())

    assert len(dispatched) == 1
    assert dispatched[0].id == PAPER_TICK_JOB_ID
    assert "paper_tick" in dispatched[0].prompt
    assert store.get(PAPER_TICK_JOB_ID).status == JobStatus.COMPLETED
    assert store.get(PAPER_TICK_JOB_ID).next_run_at > job.next_run_at


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, True),
        ("1", True),
        ("anything", True),
        ("0", False),
        ("false", False),
        ("False", False),
        ("", False),
    ],
)
def test_paper_trading_enabled_truthiness(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("VIBE_PAPER_ENABLED", raising=False)
    else:
        monkeypatch.setenv("VIBE_PAPER_ENABLED", value)
    assert _paper_trading_enabled() is expected


class _DummyExecutor:
    def __init__(self) -> None:
        self.started = False

    def start(self) -> None:
        self.started = True


def test_start_scheduled_research_executor_registers_paper_tick_when_both_enabled(
    monkeypatch, store
):
    import src.api.scheduled_routes as routes

    dummy_executor = _DummyExecutor()
    monkeypatch.setattr(routes, "_get_scheduled_research_store", lambda: store)
    monkeypatch.setattr(routes, "_get_scheduled_research_executor", lambda: dummy_executor)
    monkeypatch.setenv("VIBE_TRADING_ENABLE_SCHEDULER", "1")
    monkeypatch.setenv("VIBE_PAPER_ENABLED", "1")

    routes._start_scheduled_research_executor()

    assert store.get(DECISION_JOURNAL_JOB_ID) is not None
    assert store.get(PAPER_TICK_JOB_ID) is not None
    assert dummy_executor.started is True


def test_start_scheduled_research_executor_skips_paper_tick_when_paper_disabled(
    monkeypatch, store
):
    import src.api.scheduled_routes as routes

    dummy_executor = _DummyExecutor()
    monkeypatch.setattr(routes, "_get_scheduled_research_store", lambda: store)
    monkeypatch.setattr(routes, "_get_scheduled_research_executor", lambda: dummy_executor)
    monkeypatch.setenv("VIBE_TRADING_ENABLE_SCHEDULER", "1")
    monkeypatch.setenv("VIBE_PAPER_ENABLED", "0")

    routes._start_scheduled_research_executor()

    # the reflection job is unaffected by the paper gate
    assert store.get(DECISION_JOURNAL_JOB_ID) is not None
    assert store.get(PAPER_TICK_JOB_ID) is None
    assert dummy_executor.started is True


def test_start_scheduled_research_executor_skips_both_when_scheduler_disabled(
    monkeypatch, store
):
    import src.api.scheduled_routes as routes

    dummy_executor = _DummyExecutor()
    monkeypatch.setattr(routes, "_get_scheduled_research_store", lambda: store)
    monkeypatch.setattr(routes, "_get_scheduled_research_executor", lambda: dummy_executor)
    monkeypatch.delenv("VIBE_TRADING_ENABLE_SCHEDULER", raising=False)
    monkeypatch.setenv("VIBE_PAPER_ENABLED", "1")

    routes._start_scheduled_research_executor()

    assert store.get(DECISION_JOURNAL_JOB_ID) is None
    assert store.get(PAPER_TICK_JOB_ID) is None
    assert dummy_executor.started is False
