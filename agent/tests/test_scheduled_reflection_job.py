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
    COMMITTEE_RUN_JOB_ID,
    DECISION_JOURNAL_JOB_ID,
    DECISION_JOURNAL_JOB_SCHEDULE,
    PAPER_TICK_JOB_ID,
    PAPER_TICK_JOB_SCHEDULE,
    _ensure_committee_run_job,
    _ensure_decision_journal_job,
    _ensure_paper_trading_tick_job,
    _paper_trading_enabled,
    _parse_committee_symbols,
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


def test_paper_tick_job_prompt_has_event_trigger_run_swarm_followup(monkeypatch, store):
    """Prompt-contract (Task 3): the tick job prompt references paper_tick AND
    the event-trigger run_swarm follow-up, using the STRUCTURED variables param
    (target/timeframe) mirroring the committee-run job — not prompt extraction
    alone. The timeframe is resolved from VIBE_COMMITTEE_TIMEFRAME at
    registration."""
    monkeypatch.setenv("VIBE_COMMITTEE_TIMEFRAME", "24h swing")
    _ensure_paper_trading_tick_job(store)
    prompt = store.get(PAPER_TICK_JOB_ID).prompt

    assert "paper_tick" in prompt
    assert "event_triggers" in prompt
    assert "run_swarm" in prompt
    assert "crypto_committee" in prompt
    # structured variables channel with the resolved timeframe baked in
    assert '"target"' in prompt and '"timeframe"' in prompt
    assert "24h swing" in prompt
    # honest constraint carried through
    assert "never invent" in prompt.lower() or "never fabricate" in prompt.lower()


def test_paper_tick_job_prompt_defaults_timeframe_when_env_unset(monkeypatch, store):
    monkeypatch.delenv("VIBE_COMMITTEE_TIMEFRAME", raising=False)
    _ensure_paper_trading_tick_job(store)
    assert "72h swing" in store.get(PAPER_TICK_JOB_ID).prompt


def test_paper_tick_job_honors_schedule_env(monkeypatch, store):
    """VIBE_PAPER_TICK_SCHEDULE overrides the initial registration schedule
    (the recommended 2-hourly deployment sets "30 */2 * * *")."""
    monkeypatch.setenv("VIBE_PAPER_TICK_SCHEDULE", "30 */2 * * *")
    _ensure_paper_trading_tick_job(store)
    assert store.get(PAPER_TICK_JOB_ID).schedule == "30 */2 * * *"


def test_paper_tick_job_schedule_env_does_not_clobber_existing(monkeypatch, store):
    """Non-clobbering: once the job exists, changing the env does NOT rewrite
    its schedule on a later startup."""
    _ensure_paper_trading_tick_job(store)  # default "30 0 * * *"
    assert store.get(PAPER_TICK_JOB_ID).schedule == "30 0 * * *"
    monkeypatch.setenv("VIBE_PAPER_TICK_SCHEDULE", "30 */2 * * *")
    _ensure_paper_trading_tick_job(store)  # restart with new env
    assert store.get(PAPER_TICK_JOB_ID).schedule == "30 0 * * *"  # unchanged


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


# ---------------------------------------------------------------------------
# Two-tier-cadence Task 2 — committee-run scheduled job
#
# Unlike the reflection/paper-tick jobs, this one is registered ONLY when
# VIBE_COMMITTEE_SCHEDULE is explicitly set (unset = fully additive, no job
# at all — there is no built-in default cadence for the full committee,
# since it is the expensive tier). VIBE_COMMITTEE_SYMBOLS and
# VIBE_COMMITTEE_TIMEFRAME are resolved once, at registration time, into the
# job's prompt text; because registration is non-clobbering (same contract
# as the other two jobs), changing either env after the job already exists
# has no effect until the job is deleted (or hand-edited) so a restart can
# re-register it with the new values.
# ---------------------------------------------------------------------------


def test_parse_committee_symbols_defaults_to_btc_usdt(monkeypatch):
    monkeypatch.delenv("VIBE_COMMITTEE_SYMBOLS", raising=False)
    assert _parse_committee_symbols() == ["BTC-USDT"]


def test_parse_committee_symbols_splits_strips_and_drops_empties(monkeypatch):
    monkeypatch.setenv("VIBE_COMMITTEE_SYMBOLS", " BTC-USDT, ETH-USDT ,, SOL-USDT,")
    assert _parse_committee_symbols() == ["BTC-USDT", "ETH-USDT", "SOL-USDT"]


def test_parse_committee_symbols_uppercases_mixed_case_entries(monkeypatch):
    """A lowercase/mixed-case symbol (e.g. an operator typo like ``eth-usdt``)
    must normalize to the same identity a position's uppercase symbol uses —
    otherwise the event trigger's watch-list union creates a parallel
    lowercase identity with its own cooldown key (final review item 4)."""
    monkeypatch.setenv("VIBE_COMMITTEE_SYMBOLS", "eth-usdt, Btc-Usdt")
    assert _parse_committee_symbols() == ["ETH-USDT", "BTC-USDT"]


def test_committee_run_job_not_registered_when_schedule_env_unset(monkeypatch, store):
    monkeypatch.delenv("VIBE_COMMITTEE_SCHEDULE", raising=False)
    _ensure_committee_run_job(store)
    assert store.get(COMMITTEE_RUN_JOB_ID) is None


def test_committee_run_job_registered_when_schedule_env_set(monkeypatch, store):
    monkeypatch.setenv("VIBE_COMMITTEE_SCHEDULE", "0 8 * * *")
    _ensure_committee_run_job(store)
    job = store.get(COMMITTEE_RUN_JOB_ID)

    assert job is not None
    assert job.schedule == "0 8 * * *"


def test_committee_run_job_prompt_names_every_symbol_preset_and_timeframe(monkeypatch, store):
    monkeypatch.setenv("VIBE_COMMITTEE_SCHEDULE", "0 8 * * *")
    monkeypatch.setenv("VIBE_COMMITTEE_SYMBOLS", "BTC-USDT, ETH-USDT")
    monkeypatch.setenv("VIBE_COMMITTEE_TIMEFRAME", "24h swing")
    _ensure_committee_run_job(store)
    job = store.get(COMMITTEE_RUN_JOB_ID)

    assert "BTC-USDT" in job.prompt
    assert "ETH-USDT" in job.prompt
    assert "crypto_committee" in job.prompt
    assert "run_swarm" in job.prompt
    assert "24h swing" in job.prompt
    # Structured channel (post-review fix): each step instructs an explicit
    # variables object per symbol so multi-symbol correctness never depends
    # on the scheduling LLM reproducing the prose template verbatim.
    assert 'variables={"target": "BTC-USDT", "timeframe": "24h swing"}' in job.prompt
    assert 'variables={"target": "ETH-USDT", "timeframe": "24h swing"}' in job.prompt


def test_committee_run_job_prompt_uppercases_mixed_case_symbols_env(monkeypatch, store):
    """A mixed-case VIBE_COMMITTEE_SYMBOLS still lands in the registered job's
    prompt as uppercase symbols (final review item 4 — normalization happens
    in _parse_committee_symbols, which this job's registration calls)."""
    monkeypatch.setenv("VIBE_COMMITTEE_SCHEDULE", "0 8 * * *")
    monkeypatch.setenv("VIBE_COMMITTEE_SYMBOLS", "eth-usdt, btc-usdt")
    _ensure_committee_run_job(store)
    job = store.get(COMMITTEE_RUN_JOB_ID)

    assert "ETH-USDT" in job.prompt
    assert "BTC-USDT" in job.prompt
    assert "eth-usdt" not in job.prompt
    assert 'variables={"target": "ETH-USDT"' in job.prompt


def test_committee_run_job_uses_defaults_when_symbols_and_timeframe_unset(monkeypatch, store):
    monkeypatch.setenv("VIBE_COMMITTEE_SCHEDULE", "0 8 * * *")
    monkeypatch.delenv("VIBE_COMMITTEE_SYMBOLS", raising=False)
    monkeypatch.delenv("VIBE_COMMITTEE_TIMEFRAME", raising=False)
    _ensure_committee_run_job(store)
    job = store.get(COMMITTEE_RUN_JOB_ID)

    assert "BTC-USDT" in job.prompt
    assert "72h swing" in job.prompt


def test_committee_run_job_is_idempotent(monkeypatch, store):
    monkeypatch.setenv("VIBE_COMMITTEE_SCHEDULE", "0 8 * * *")
    _ensure_committee_run_job(store)
    first = store.get(COMMITTEE_RUN_JOB_ID)

    _ensure_committee_run_job(store)  # e.g. a second server startup
    second = store.get(COMMITTEE_RUN_JOB_ID)

    assert first.created_at == second.created_at  # not re-created


def test_committee_run_job_preserves_user_edits_across_restart(monkeypatch, store):
    monkeypatch.setenv("VIBE_COMMITTEE_SCHEDULE", "0 8 * * *")
    _ensure_committee_run_job(store)
    job = store.get(COMMITTEE_RUN_JOB_ID)
    job.schedule = "0 6 * * *"  # user (or operator) edits the schedule
    store.upsert(job)

    monkeypatch.setenv("VIBE_COMMITTEE_SYMBOLS", "ETH-USDT")  # env changes too
    _ensure_committee_run_job(store)  # restart must not clobber either edit

    reloaded = store.get(COMMITTEE_RUN_JOB_ID)
    assert reloaded.schedule == "0 6 * * *"
    assert "ETH-USDT" not in reloaded.prompt  # prompt was NOT rebuilt from the new env


def test_start_scheduled_research_executor_registers_committee_run_when_schedule_set(
    monkeypatch, store
):
    import src.api.scheduled_routes as routes

    dummy_executor = _DummyExecutor()
    monkeypatch.setattr(routes, "_get_scheduled_research_store", lambda: store)
    monkeypatch.setattr(routes, "_get_scheduled_research_executor", lambda: dummy_executor)
    monkeypatch.setenv("VIBE_TRADING_ENABLE_SCHEDULER", "1")
    monkeypatch.setenv("VIBE_COMMITTEE_SCHEDULE", "0 8 * * *")

    routes._start_scheduled_research_executor()

    assert store.get(COMMITTEE_RUN_JOB_ID) is not None


def test_start_scheduled_research_executor_skips_committee_run_when_schedule_unset(
    monkeypatch, store
):
    import src.api.scheduled_routes as routes

    dummy_executor = _DummyExecutor()
    monkeypatch.setattr(routes, "_get_scheduled_research_store", lambda: store)
    monkeypatch.setattr(routes, "_get_scheduled_research_executor", lambda: dummy_executor)
    monkeypatch.setenv("VIBE_TRADING_ENABLE_SCHEDULER", "1")
    monkeypatch.delenv("VIBE_COMMITTEE_SCHEDULE", raising=False)

    routes._start_scheduled_research_executor()

    assert store.get(COMMITTEE_RUN_JOB_ID) is None


def test_start_scheduled_research_executor_skips_committee_run_when_scheduler_disabled(
    monkeypatch, store
):
    import src.api.scheduled_routes as routes

    dummy_executor = _DummyExecutor()
    monkeypatch.setattr(routes, "_get_scheduled_research_store", lambda: store)
    monkeypatch.setattr(routes, "_get_scheduled_research_executor", lambda: dummy_executor)
    monkeypatch.delenv("VIBE_TRADING_ENABLE_SCHEDULER", raising=False)
    monkeypatch.setenv("VIBE_COMMITTEE_SCHEDULE", "0 8 * * *")

    routes._start_scheduled_research_executor()

    assert store.get(COMMITTEE_RUN_JOB_ID) is None
    assert dummy_executor.started is False


def test_committee_run_job_dispatches_through_one_executor_tick(monkeypatch, store):
    monkeypatch.setenv("VIBE_COMMITTEE_SCHEDULE", "0 8 * * *")
    _ensure_committee_run_job(store)
    dispatched: list = []

    async def fake_dispatch(job) -> None:
        dispatched.append(job)

    job = store.get(COMMITTEE_RUN_JOB_ID)
    executor = ScheduledResearchExecutor(store, fake_dispatch, now_fn=lambda: job.next_run_at + 1)

    asyncio.run(executor.tick())

    assert len(dispatched) == 1
    assert dispatched[0].id == COMMITTEE_RUN_JOB_ID
    assert store.get(COMMITTEE_RUN_JOB_ID).status == JobStatus.COMPLETED
    assert store.get(COMMITTEE_RUN_JOB_ID).next_run_at > job.next_run_at
