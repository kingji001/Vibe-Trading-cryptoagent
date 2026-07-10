"""Hermeticity regression tests for the paper-executor test guard (Task 5 fix).

Driving incident: after the Task 5 post-append hook landed (paper trading is
default-ENABLED per spec when ``VIBE_PAPER_ENABLED`` is unset), every test
that exercised ``decision_journal action="append"`` while isolating only the
JOURNAL path (``VIBE_COMMITTEE_JOURNAL`` / tmp jpath) — but not the PAPER
env — fired ``maybe_execute_paper`` against a REAL default broker: live OKX
price fetches and writes to the production ``~/.vibe-trading/paper`` store.
A live run left ~55 orphaned ledger entries (real ETH-USDT market buys at
live prices, cap clamps, retriable noops) with decision_ids that exist in no
journal.

The fix is a pair of autouse guards in ``conftest.py``:
  - session-scoped: ``VIBE_PAPER_ROOT`` -> a session tmp dir and
    ``VIBE_PAPER_ENABLED=0`` for the entire run, so even import-time or
    fixture-ordering surprises can never reach the real home-dir store;
  - function-scoped (yield-based, applied via ``monkeypatch`` BEFORE the test
    body): a per-test tmp ``VIBE_PAPER_ROOT`` + ``VIBE_PAPER_ENABLED=0`` that
    test-local ``monkeypatch.setenv``/``delenv`` calls override cleanly —
    tests that WANT the executor (test_paper_*.py, the hook tests) opt back
    in explicitly, exactly as they already do.

These tests intentionally do NO paper env setup of their own: they assert the
world an arbitrary, paper-oblivious test now lives in.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from src.committee import journal
from src.paper.store import paper_root

_REAL_PAPER_DIR = Path.home() / ".vibe-trading" / "paper"


def test_guard_disables_paper_executor_by_default():
    """VIBE_PAPER_ENABLED must be "0" for any test that doesn't opt in —
    unset means ENABLED per spec, so the guard must set it explicitly."""
    assert os.environ.get("VIBE_PAPER_ENABLED") == "0"


def test_guard_points_paper_root_at_per_test_tmp(tmp_path):
    """paper_root() must resolve inside pytest-managed tmp, never the real
    home-dir store."""
    root = paper_root()
    assert root != _REAL_PAPER_DIR
    assert not str(root).startswith(str(_REAL_PAPER_DIR))
    # per-test guard wins over the session guard: the resolved root lives
    # under this test's own tmp_path
    assert str(root).startswith(str(tmp_path))


def test_journal_tool_append_without_paper_env_is_hermetic(tmp_path, monkeypatch):
    """The exact leaking shape from the incident: a Task-1-style journal tool
    append (Buy + stop/TP/size) that isolates ONLY the journal path. It must
    succeed, produce NO paper_execution key (executor disabled by the guard),
    and leave both the per-test paper root and the real home-dir store
    untouched."""
    from src.tools.committee_journal_tool import DecisionJournalTool

    monkeypatch.setenv(journal.JOURNAL_PATH_ENV, str(tmp_path / "journal.jsonl"))
    real_before = sorted(
        (p.name, p.stat().st_size, p.stat().st_mtime)
        for p in _REAL_PAPER_DIR.glob("*")
    ) if _REAL_PAPER_DIR.exists() else None

    out = json.loads(
        DecisionJournalTool().execute(
            action="append",
            symbol="ETH-USDT",
            rating="Buy",
            time_horizon="72h swing",
            stop_loss=1700.0,
            take_profit=2000.0,
            position_size_pct=10.0,
            run_id="run-hermeticity-guard",
        )
    )

    assert out["status"] == "ok"
    assert "paper_execution" not in out  # hook returned None fast

    # zero paper side effects: disabled path never constructs a store, so the
    # guard's tmp root has no account/positions/ledger files either
    guard_root = paper_root()
    assert not (guard_root / "account.json").exists()
    assert not (guard_root / "ledger.jsonl").exists()

    real_after = sorted(
        (p.name, p.stat().st_size, p.stat().st_mtime)
        for p in _REAL_PAPER_DIR.glob("*")
    ) if _REAL_PAPER_DIR.exists() else None
    assert real_after == real_before  # production store byte-untouched


def test_opt_in_override_wins_over_guard(monkeypatch, tmp_path):
    """A test-local monkeypatch.setenv must beat the autouse guard (the paper
    test files rely on this to re-enable the executor against their own tmp
    root)."""
    my_root = tmp_path / "my-paper-root"
    monkeypatch.setenv("VIBE_PAPER_ENABLED", "1")
    monkeypatch.setenv("VIBE_PAPER_ROOT", str(my_root))

    from src.paper.translator import _paper_enabled

    assert _paper_enabled() is True
    assert paper_root() == my_root
