"""Tests for the `vibe-trading paper` CLI subcommands (Task 7).

Invokes the ``cmd_paper_*`` functions directly against a fixture account
rooted at ``VIBE_PAPER_ROOT`` (``PaperStore``/``PaperBroker`` both honor that
env var via ``src.paper.store.paper_root``) and captures rich console output
via ``capsys`` — same convention as ``agent/tests/test_cli_live.py``.

Socket-disabled: ``cmd_paper_status`` builds a default ``PaperBroker`` (no
injectable ``price_fn`` parameter on the CLI surface), so any test exercising
a live mark stubs the network boundary one level down —
``src.tools.crypto_snapshot_tool._fetch_row`` — exactly like
``test_paper_broker.py::test_default_price_fn_translates_snapshot`` /
``test_default_price_fn_raises_on_no_data``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cli._legacy import EXIT_SUCCESS, EXIT_USAGE_ERROR, cmd_paper_ledger, cmd_paper_reset, cmd_paper_status, cmd_paper_tick
from src.paper.store import PaperStore


def _stub_price(monkeypatch, *, price: float | None = None, fail: bool = False) -> None:
    """Stub the network boundary ``default_price_fn`` reads from."""
    from src.tools import crypto_snapshot_tool as snap

    def fake_fetch_row(**kwargs):
        if fail:
            return None, "no data (test stub)"
        return {"last": str(price), "ts": "1700000000000"}, None

    monkeypatch.setattr(snap, "_fetch_row", fake_fetch_row)


def _set_env(monkeypatch, tmp_path, **overrides):
    env = {
        "VIBE_PAPER_ROOT": str(tmp_path),
        "VIBE_PAPER_ENABLED": "1",
        "VIBE_PAPER_START_CASH": "100000",
        "VIBE_PAPER_SLIPPAGE_BPS": "5",
        "VIBE_PAPER_FEE_BPS": "10",
        "VIBE_PAPER_MAX_POSITIONS": "3",
        "VIBE_PAPER_MAX_SYMBOL_PCT": "25",
        "VIBE_PAPER_DEFAULT_SIZE_PCT": "10",
        "VIBE_PAPER_DEFAULT_STOP_PCT": "8",
    }
    env.update(overrides)
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))


def _seed_account(tmp_path: Path, *, cash: float = 95000.0) -> PaperStore:
    store = PaperStore(tmp_path)
    store.create_account(100000.0, {"slippage_bps": 5.0, "fee_bps": 10.0})
    account = store.load_account()
    account["cash"] = cash
    store.save_account(account)
    return store


# --------------------------------------------------------------------------- #
# status                                                                       #
# --------------------------------------------------------------------------- #
class TestStatus:
    def test_no_account_yet(self, monkeypatch, tmp_path, capsys):
        _set_env(monkeypatch, tmp_path)
        rc = cmd_paper_status()
        out = capsys.readouterr().out
        assert rc == EXIT_SUCCESS
        assert "No paper account" in out

    def test_shows_equity_and_mandate_headroom(self, monkeypatch, tmp_path, capsys):
        _set_env(monkeypatch, tmp_path)
        _stub_price(monkeypatch, price=60000.0)
        store = _seed_account(tmp_path, cash=95000.0)
        store.save_positions(
            [
                {
                    "symbol": "BTC-USDT",
                    "qty": 0.1,
                    "avg_entry": 50000.0,
                    "stop": 46000.0,
                    "take_profits": [{"price": 55000.0, "fraction": 1.0}],
                    "opened_at": "2026-07-10T00:00:00Z",
                    "decision_id": "dec_1",
                }
            ]
        )
        rc = cmd_paper_status()
        out = capsys.readouterr().out
        assert rc == EXIT_SUCCESS
        assert "Equity" in out
        assert "BTC-USDT" in out
        # mandate headroom: positions used/max
        assert "1/3" in out
        assert "46000" in out  # stop shown
        assert "55000" in out  # take-profit shown
        # live mark (60000.0) used, not flagged stale
        assert "60000" in out
        assert "STALE" not in out.upper()

    def test_status_shows_stale_flag_explicitly(self, monkeypatch, tmp_path, capsys):
        """Regression: equity() stale rows (valued at avg_entry) must be
        visibly flagged in `paper status`, per the broker's Important-1 review
        note (stale marks are never silent)."""
        _set_env(monkeypatch, tmp_path)
        _stub_price(monkeypatch, fail=True)
        store = _seed_account(tmp_path)
        store.save_positions(
            [
                {
                    "symbol": "ETH-USDT",
                    "qty": 1.0,
                    "avg_entry": 3000.0,
                    "stop": None,
                    "take_profits": [],
                    "opened_at": "2026-07-10T00:00:00Z",
                    "decision_id": "dec_2",
                }
            ]
        )
        rc = cmd_paper_status()
        out = capsys.readouterr().out
        assert rc == EXIT_SUCCESS
        assert "STALE" in out.upper()
        # stale rows are valued at avg_entry (3000.00), per broker._mark_for
        assert "3000.00" in out


# --------------------------------------------------------------------------- #
# ledger                                                                       #
# --------------------------------------------------------------------------- #
class TestLedger:
    def _seed_ledger(self, store: PaperStore, n: int = 5) -> None:
        for i in range(n):
            store.append_ledger(
                {
                    "ts": f"2026-07-{10 + i:02d}T00:00:00Z",
                    "trade_id": f"t{i}",
                    "symbol": "BTC-USDT" if i % 2 == 0 else "ETH-USDT",
                    "side": "buy",
                    "qty": 1.0,
                    "fill_price": 100.0 + i,
                    "slippage_paid": 0.0,
                    "fee_paid": 1.0,
                    "order_type": "market",
                    "decision_id": f"dec_{i}",
                    "realized_pnl": None,
                    "note": None,
                }
            )

    def test_ledger_limit_truncates(self, monkeypatch, tmp_path, capsys):
        _set_env(monkeypatch, tmp_path)
        store = PaperStore(tmp_path)
        self._seed_ledger(store, n=5)
        rc = cmd_paper_ledger(limit=2)
        out = capsys.readouterr().out
        assert rc == EXIT_SUCCESS
        assert "2 entr" in out
        # rows are appended chronologically; limit=2 keeps the LAST two fills
        # (i=3 -> fill 103.00, i=4 -> fill 104.00), drops the earliest (i=0).
        assert "103.00" in out and "104.00" in out
        assert "100.00" not in out

    def test_ledger_symbol_filter(self, monkeypatch, tmp_path, capsys):
        _set_env(monkeypatch, tmp_path)
        store = PaperStore(tmp_path)
        self._seed_ledger(store, n=5)
        rc = cmd_paper_ledger(limit=20, symbol="ETH-USDT")
        out = capsys.readouterr().out
        assert rc == EXIT_SUCCESS
        assert "ETH-USDT" in out
        assert "BTC-USDT" not in out

    def test_ledger_empty(self, monkeypatch, tmp_path, capsys):
        _set_env(monkeypatch, tmp_path)
        rc = cmd_paper_ledger(limit=20)
        out = capsys.readouterr().out
        assert rc == EXIT_SUCCESS
        assert "No ledger entries" in out


# --------------------------------------------------------------------------- #
# tick                                                                         #
# --------------------------------------------------------------------------- #
class TestTick:
    def test_tick_prints_run_summary(self, monkeypatch, tmp_path, capsys):
        _set_env(monkeypatch, tmp_path)
        _seed_account(tmp_path)

        def fake_run_tick(*args, **kwargs):
            return {
                "conditional_fills": [{"trade_id": "x"}],
                "equity_snapshot": {
                    "equity": 101000.0,
                    "cash": 90000.0,
                    "positions_value": 11000.0,
                    "stale_positions": 1,
                    "date": "2026-07-11",
                    "already_recorded": False,
                },
                "errors": [{"symbol": "SOL-USDT", "error": "no bar"}],
            }

        monkeypatch.setattr("src.paper.tick.run_tick", fake_run_tick)
        rc = cmd_paper_tick()
        out = capsys.readouterr().out
        assert rc == EXIT_SUCCESS
        assert "101000" in out
        assert "1" in out  # fills / stale count somewhere
        assert "SOL-USDT" in out
        assert "no bar" in out


# --------------------------------------------------------------------------- #
# reset                                                                        #
# --------------------------------------------------------------------------- #
class TestReset:
    def test_reset_without_confirm_refuses(self, monkeypatch, tmp_path, capsys):
        _set_env(monkeypatch, tmp_path)
        _seed_account(tmp_path)
        rc = cmd_paper_reset(confirm=False)
        out = capsys.readouterr().out
        assert rc == EXIT_USAGE_ERROR
        assert rc != 0
        assert "--confirm" in out
        # nothing archived, account file untouched
        assert (tmp_path / "account.json").exists()
        assert not any(p.name.startswith("archive-") for p in tmp_path.iterdir())

    def test_reset_with_confirm_archives(self, monkeypatch, tmp_path, capsys):
        _set_env(monkeypatch, tmp_path)
        _seed_account(tmp_path)
        rc = cmd_paper_reset(confirm=True)
        out = capsys.readouterr().out
        assert rc == EXIT_SUCCESS
        assert "archived" in out.lower()
        assert not (tmp_path / "account.json").exists()
        archives = [p for p in tmp_path.iterdir() if p.name.startswith("archive-")]
        assert len(archives) == 1
        assert (archives[0] / "account.json").exists()
