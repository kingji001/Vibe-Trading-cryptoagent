"""Shared fixtures and sys.path setup for all tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure agent/ is on sys.path so imports like `backtest.*` and `src.*` work.
AGENT_DIR = Path(__file__).resolve().parent.parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))


# ---------------------------------------------------------------------------
# .env leak hermeticity guard (final whole-branch review finding).
#
# ``src.providers.llm._ensure_dotenv()`` searches, in order, ``~/.vibe-
# trading/.env``, ``agent/.env``, and ``$CWD/.env``, latching a module-level
# ``_dotenv_loaded`` flag so it only ever runs once per process. Any test
# that spins up the real FastAPI app (e.g. via ``starlette.testclient
# .TestClient``, which runs the app's startup lifespan) can be the FIRST
# thing in a session to call it -- and since pytest is invoked from the repo
# root, ``$CWD/.env`` resolves to the operator's real, repo-root ``.env``.
# ``load_dotenv(..., override=False)`` only sets keys not already in
# ``os.environ``, but for keys that ARE unset it mutates the real process
# environment directly (not via monkeypatch), so the values leak into every
# test that runs afterward in the same session and never get cleaned up.
#
# With a real ``.env`` that sets VIBE_LLM_MAX_CONCURRENT, VIBE_PAPER_TICK_*,
# VIBE_COMMITTEE_*, etc. (a normal operator config, not a test fixture) this
# deterministically broke tests in test_paper_tick.py,
# test_concurrency_governance.py, and test_scheduled_reflection_job.py that
# assume those vars are unset unless they set them themselves.
#
# Fix: latch the flag to already-loaded (and defensively strip the known
# leak-prone vars) BEFORE any test gets a chance to trigger the real load.
# ``cli._legacy`` imports ``_ensure_dotenv`` from this same module and shares
# the same module-level flag, so there is only one loader to guard -- no
# sibling flag exists. ``cli.main``'s own ``load_dotenv`` call (onboarding
# wizard) only fires when NO ``.env`` exists anywhere, which cannot be true
# once ``.env`` is present, so it needs no guard here.
#
# Per-test fixtures still setenv/delenv whatever they need -- this only
# prevents the ACCIDENTAL, uncontrolled load of the real file.
# ---------------------------------------------------------------------------

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
    "VIBE_MCP_COMMITTEE",
    "VIBE_MCP_ALLOW_TRIGGER",
    "VIBE_MCP_TRIGGER_BUDGET",
    "VIBE_MCP_TRIGGER_AUDIT",
)


@pytest.fixture(scope="session", autouse=True)
def _dotenv_hermeticity_guard(tmp_path_factory):
    """Latch dotenv loading as already-done before any test can trigger it.

    Must be the first autouse session fixture pytest sets up (hence its
    placement at the very top of this file, ahead of the paper-executor
    guard below) so nothing gets a window to load the real repo-root .env.
    """
    import src.providers.llm as llm_module

    mp = pytest.MonkeyPatch()
    mp.setattr(llm_module, "_dotenv_loaded", True)
    for name in _LEAK_PRONE_ENV_VARS:
        mp.delenv(name, raising=False)
    yield
    mp.undo()


# ---------------------------------------------------------------------------
# Paper-executor hermeticity guard (paper-trading loop, Task 5 incident fix).
#
# The decision_journal tool's append action fires the paper execution hook
# (src/paper/hook.py), and VIBE_PAPER_ENABLED is default-ENABLED per spec
# when unset. Before this guard existed, any test that exercised an append
# while isolating only the JOURNAL path executed REAL paper trades: a default
# PaperBroker fetching live OKX prices and writing to the production
# ~/.vibe-trading/paper store (a live run left ~55 orphaned ledger entries).
#
# Two layers, both autouse:
#   - session-scoped: a hard backstop so the real home-dir store is
#     unreachable for the entire run, whatever fixture ordering does;
#   - function-scoped (applied via monkeypatch BEFORE the test body): a
#     per-test tmp VIBE_PAPER_ROOT + VIBE_PAPER_ENABLED=0. Tests that WANT
#     the executor (test_paper_*.py, the hook/journal-seam tests) override
#     with their own monkeypatch.setenv/delenv — autouse fixtures are set up
#     first, so test-local writes win, and monkeypatch teardown unwinds in
#     LIFO order back to the guard values, then to the pre-test environment.
#
# Regression coverage: tests/test_paper_env_guard.py.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def _paper_env_session_backstop(tmp_path_factory):
    """Session-wide backstop: never let a test see the real paper store."""
    mp = pytest.MonkeyPatch()
    mp.setenv("VIBE_PAPER_ROOT", str(tmp_path_factory.mktemp("paper-session-backstop")))
    mp.setenv("VIBE_PAPER_ENABLED", "0")
    mp.setenv("VIBE_OPS_ROOT", str(tmp_path_factory.mktemp("ops-session-backstop")))
    yield
    mp.undo()


@pytest.fixture(autouse=True)
def _paper_env_guard(monkeypatch, tmp_path):
    """Per-test guard: paper executor disabled, store rooted in this test's tmp.

    Also pins VIBE_OPS_ROOT (scripts/ops/run72.sh + `vibe-trading ops report`
    artifacts root) to this test's tmp — same hermeticity rule as
    VIBE_PAPER_ROOT above: any new env var with filesystem/network side
    effects gets a guard entry in the same task that introduces it.
    """
    monkeypatch.setenv("VIBE_PAPER_ROOT", str(tmp_path / "paper-guard"))
    monkeypatch.setenv("VIBE_PAPER_ENABLED", "0")
    monkeypatch.setenv("VIBE_OPS_ROOT", str(tmp_path / "ops-guard"))
    for _mcp_var in (
        "VIBE_MCP_COMMITTEE",
        "VIBE_MCP_ALLOW_TRIGGER",
        "VIBE_MCP_TRIGGER_BUDGET",
    ):
        monkeypatch.delenv(_mcp_var, raising=False)
    # VIBE_MCP_TRIGGER_AUDIT is a file PATH (committee_routes._mcp_triggers_path /
    # mcp_server._mcp_triggers_path both fall back to the real
    # ~/.vibe-trading/committee/mcp_triggers.jsonl when unset) -- delenv alone
    # leaves every test reading/appending to that real, live operational
    # audit log. Point it at this test's own tmp dir instead so no test ever
    # touches the real file. Tests that want a specific seeded audit log
    # still override with their own monkeypatch.setenv/setattr.
    monkeypatch.setenv(
        "VIBE_MCP_TRIGGER_AUDIT", str(tmp_path / "mcp-audit-guard" / "mcp_triggers.jsonl")
    )
    yield
