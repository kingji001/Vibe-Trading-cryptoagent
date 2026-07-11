"""Regression tests for the .env leak hermeticity guard (final whole-branch
review finding).

Driving incident: ``src.providers.llm._ensure_dotenv()`` searches ``$CWD/.env``
among its candidates, and pytest is invoked from the repo root -- so the
FIRST test in a session to trigger it (observed: a FastAPI app startup via
``starlette.testclient.TestClient``) loaded the operator's real, repo-root
``.env`` into ``os.environ`` process-wide (``load_dotenv(..., override=
False)`` sets any key not already present, and that mutation is never undone
because it isn't routed through ``monkeypatch``). With a real ``.env`` that
sets VIBE_LLM_MAX_CONCURRENT, VIBE_PAPER_TICK_*, VIBE_COMMITTEE_*, etc. (an
ordinary operator config, not a test fixture) this deterministically broke
13 tests across test_paper_tick.py, test_concurrency_governance.py, and
test_scheduled_reflection_job.py that assume those vars are unset unless they
set them explicitly.

The fix is a session-scoped autouse fixture in conftest.py
(``_dotenv_hermeticity_guard``) that latches ``_dotenv_loaded`` to True and
strips the known leak-prone vars before any test gets a chance to trigger the
real load. Reproducing the leak itself needs a real repo-root ``.env`` (can't
easily be faked as a fixture without touching the operator's actual file), so
this file instead asserts the guard's own state directly. The full regression
proof is running the previously-failing clusters WITH the real repo-root
``.env`` present and confirming they're green (see the CLAUDE-facing report
for that run).
"""

from __future__ import annotations

import os

import src.providers.llm as llm_module

# Kept as a literal, independent list (rather than importing conftest's
# private tuple) -- conftest.py isn't reliably importable as a plain module
# name once multiple conftest.py files exist in the tree (agent/tests/ and
# agent/tests/factors/), and duplicating this short list keeps the test
# self-contained and honest about exactly what it checks.
_LEAK_PRONE_ENV_VARS = (
    "VIBE_PAPER_TICK_INTERVAL",
    "VIBE_PAPER_TICK_SCHEDULE",
    "VIBE_LLM_MAX_CONCURRENT",
    "VIBE_COMMITTEE_SCHEDULE",
    "VIBE_COMMITTEE_SYMBOLS",
    "VIBE_COMMITTEE_TIMEFRAME",
    "VIBE_EVENT_PRICE_MOVE_PCT",
    "VIBE_EVENT_FUNDING_ABS",
    "VIBE_EVENT_COOLDOWN_H",
)


def test_dotenv_loaded_flag_latched_at_session_start():
    """The guard must have already flipped the latch before this test body
    runs -- proving no test gets a window to trigger a real dotenv load."""
    assert llm_module._dotenv_loaded is True


def test_leak_prone_env_vars_absent_by_default():
    """None of the known leak-prone vars should be present unless a test
    explicitly (re-)set them via its own monkeypatch -- this test does no
    setup of its own, so a clean slate here is exactly the guard's job."""
    for name in _LEAK_PRONE_ENV_VARS:
        assert name not in os.environ, f"{name} leaked into the test process"
