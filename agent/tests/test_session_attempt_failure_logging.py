"""A crashing agent turn must reach the server log, not just attempt.json.

Regression: ``SessionService._run_attempt`` caught every exception and only
recorded it on the attempt record + event bus. On an unattended run nobody
reads either, so six consecutive scheduled turns died with an AttributeError
while run72.log stayed completely clean and the scheduler reported the jobs
as "completed" (it only ever means "enqueued").
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from src.session.events import EventBus
from src.session.models import AttemptStatus
from src.session.service import SessionService
from src.session.store import SessionStore


@pytest.fixture
def service(tmp_path):
    return SessionService(
        store=SessionStore(base_dir=tmp_path / "sessions"),
        event_bus=EventBus(),
        runs_dir=tmp_path / "runs",
    )


def test_failing_attempt_is_logged_with_traceback(service, monkeypatch, caplog):
    """The exception that killed the turn must appear in the log."""

    async def _boom(*args, **kwargs):
        raise AttributeError("'GovernedToolRegistry' object has no attribute '_tools'")

    monkeypatch.setattr(service, "_run_with_agent", _boom)

    async def scenario():
        session = service.create_session(title="scheduled-research:committee-run")
        result = await service.send_message(session.session_id, "run the committee")
        # send_message fire-and-forgets the attempt task; let it finish.
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        return session.session_id, result["attempt_id"]

    with caplog.at_level(logging.ERROR, logger="src.session.service"):
        session_id, attempt_id = asyncio.run(scenario())

    assert service.store.get_attempt(session_id, attempt_id).status == AttemptStatus.FAILED

    assert "_tools" in caplog.text, "the root-cause exception never reached the log"
    assert any(r.exc_info for r in caplog.records), "logged without a traceback"
