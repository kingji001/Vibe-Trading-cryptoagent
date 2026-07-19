"""API tests for the read-only paper REST surface (register_paper_routes).

Socket-free: /paper/status with no open positions never calls price_fn; the
stale-path test monkeypatches src.paper.broker.default_price_fn to raise so no
socket opens. Loopback TestClient (127.0.0.1) bypasses dev-mode auth, matching
tests/test_alpha_compare_api.py. VIBE_PAPER_ROOT is pinned to this test's tmp
by the conftest autouse guard, so PaperStore(paper_root()) reads the seed here.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import api_server
from src.paper.broker import PriceUnavailable
from src.paper.store import PaperStore, paper_root


def _client() -> TestClient:
    return TestClient(api_server.app, client=("127.0.0.1", 50000))


def _seed_store() -> PaperStore:
    store = PaperStore(paper_root())
    store.create_account(10_000.0, {"fee_bps": 5.0})
    store.append_ledger({
        "ts": "2026-07-18T00:00:00Z", "trade_id": "t1", "symbol": "BTC-USDT",
        "side": "buy", "qty": 0.1, "fill_price": 60000.0, "slippage_paid": 1.0,
        "fee_paid": 3.0, "order_type": "market", "decision_id": "dec_abc123",
        "realized_pnl": None, "note": None,
    })
    store.append_ledger({
        "ts": "2026-07-18T01:00:00Z", "trade_id": "t2", "symbol": "BTC-USDT",
        "side": "sell", "qty": 0.1, "fill_price": 61000.0, "slippage_paid": 1.0,
        "fee_paid": 3.0, "order_type": "market", "decision_id": "dec_abc123",
        "realized_pnl": 94.0, "note": None,
    })
    store.append_equity({
        "ts": "2026-07-18T00:00:00Z", "cash": 10_000.0, "positions_value": 0.0,
        "equity": 10_000.0, "positions": [], "stale_positions": 0,
    })
    return store


def test_status_no_positions_returns_equity_shape():
    _seed_store()
    resp = _client().get("/paper/status")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) >= {"ts", "cash", "positions_value", "equity",
                         "positions", "stale_positions"}
    assert body["cash"] == 10_000.0
    assert body["positions"] == []
    assert body["stale_positions"] == 0


def test_status_stale_position_renders_stale_flag(monkeypatch):
    store = _seed_store()
    store.save_positions([{
        "symbol": "BTC-USDT", "qty": 0.1, "avg_entry": 60000.0,
        "stop": 58000.0, "take_profits": [{"price": 65000.0, "fraction": 1.0}],
        "opened_at": "2026-07-18T00:00:00Z", "decision_id": "dec_abc123",
    }])

    def _boom(symbol):
        raise PriceUnavailable(symbol)

    monkeypatch.setattr("src.paper.broker.default_price_fn", _boom)
    body = _client().get("/paper/status").json()
    assert body["stale_positions"] == 1
    row = body["positions"][0]
    assert row["stale"] is True
    assert row["mark"] == 60000.0  # valued at avg_entry, not fabricated


def test_ledger_tail_and_limit():
    _seed_store()
    rows = _client().get("/paper/ledger", params={"limit": 1}).json()
    assert len(rows) == 1
    assert rows[0]["trade_id"] == "t2"  # newest-last tail slice
    full = _client().get("/paper/ledger").json()
    assert [r["trade_id"] for r in full] == ["t1", "t2"]


def test_equity_returns_all_snapshots():
    _seed_store()
    rows = _client().get("/paper/equity").json()
    assert len(rows) == 1
    assert rows[0]["equity"] == 10_000.0


def test_pnl_executed_decision_matches_decision_pnl():
    _seed_store()
    body = _client().get("/paper/pnl/dec_abc123").json()
    assert body["decision_id"] == "dec_abc123"
    assert body["executed"] is True
    assert body["realized_pnl"] == pytest.approx(94.0)


def test_pnl_unknown_decision_is_not_executed_not_404():
    _seed_store()
    resp = _client().get("/paper/pnl/dec_missing")
    assert resp.status_code == 200
    assert resp.json()["executed"] is False


def test_pnl_rejects_path_traversal_decision_id():
    # %2f decodes to "/" before routing -> two segments, no route: 404.
    assert _client().get("/paper/pnl/..%2f..").status_code == 404
    # Backslash payload reaches the route as one segment -> the validator
    # itself must fire with 400 (see test_committee_api counterpart).
    assert _client().get("/paper/pnl/..%5c..").status_code == 400
