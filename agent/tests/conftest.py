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
    yield
    mp.undo()


@pytest.fixture(autouse=True)
def _paper_env_guard(monkeypatch, tmp_path):
    """Per-test guard: paper executor disabled, store rooted in this test's tmp."""
    monkeypatch.setenv("VIBE_PAPER_ROOT", str(tmp_path / "paper-guard"))
    monkeypatch.setenv("VIBE_PAPER_ENABLED", "0")
    yield
