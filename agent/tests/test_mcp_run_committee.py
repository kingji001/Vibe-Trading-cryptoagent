"""Trigger tool (gate 2): budget, audit, grounding validation, dispatch.

run_committee is registered only when VIBE_MCP_COMMITTEE and
VIBE_MCP_ALLOW_TRIGGER are both truthy. The swarm dispatch is faked (spec
§3.5 permits the operator to veto the token spend), and the grounding
network fetch is stubbed, so these tests never place a real run or touch
the network.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import threading
from datetime import datetime, timezone

import pytest


def _reload_mcp(monkeypatch, **env):
    for key, val in env.items():
        if val is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, val)
    import mcp_server
    return importlib.reload(mcp_server)


def _call(mod, name, **args) -> dict:
    return json.loads(asyncio.run(mod.mcp.call_tool(name, args)).content[0].text)


def _tool_names(mod) -> set[str]:
    return {t.name for t in asyncio.run(mod.mcp.list_tools())}


@pytest.fixture
def trigger_mod(tmp_path, monkeypatch):
    audit = tmp_path / "mcp_triggers.jsonl"
    monkeypatch.setenv("VIBE_MCP_TRIGGER_AUDIT", str(audit))
    mod = _reload_mcp(monkeypatch, VIBE_MCP_COMMITTEE="1",
                      VIBE_MCP_ALLOW_TRIGGER="1", VIBE_MCP_TRIGGER_BUDGET="2")
    # Fake grounding: BTC-USDT resolves + has data; UNREAL-USDT resolves shape
    # but returns no market data; a junk value resolves to no symbol at all.
    monkeypatch.setattr(mod, "_grounding_resolve",
                        lambda s: None if s == "???" else s.upper())
    monkeypatch.setattr(mod, "_grounding_fetch",
                        lambda sym: {sym: [{"close": 1.0}]} if sym != "UNREAL-USDT" else {})
    # Fake dispatch: never starts a real swarm; returns a deterministic id.
    # Guarded by a lock so it's safe to call from multiple threads at once
    # (the concurrency test below drives it that way).
    calls = []
    calls_lock = threading.Lock()
    def _fake_dispatch(symbol, timeframe):
        with calls_lock:
            calls.append((symbol, timeframe))
            return f"swarm-fake-{len(calls)}"
    monkeypatch.setattr(mod, "_dispatch_committee_run", _fake_dispatch)
    mod._test_dispatch_calls = calls
    mod._test_audit_path = audit
    return mod


def _audit_rows(mod) -> list[dict]:
    p = mod._test_audit_path
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def test_run_committee_absent_without_trigger_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBE_MCP_TRIGGER_AUDIT", str(tmp_path / "a.jsonl"))
    mod = _reload_mcp(monkeypatch, VIBE_MCP_COMMITTEE="1", VIBE_MCP_ALLOW_TRIGGER=None)
    assert "run_committee" not in _tool_names(mod)


def test_run_committee_present_with_both_gates(trigger_mod):
    assert "run_committee" in _tool_names(trigger_mod)


def test_accepted_trigger_dispatches_and_audits(trigger_mod):
    payload = _call(trigger_mod, "run_committee", symbol="BTC-USDT", note="hi")
    assert payload["status"] == "ok"
    assert payload["run_id"] == "swarm-fake-1"
    assert trigger_mod._test_dispatch_calls == [("BTC-USDT", "72h swing")]
    rows = _audit_rows(trigger_mod)
    assert len(rows) == 1
    assert rows[0]["accepted"] is True
    assert rows[0]["symbol"] == "BTC-USDT" and rows[0]["note"] == "hi"
    assert rows[0]["run_id"] == "swarm-fake-1"


def test_unresolvable_symbol_refused_and_audited(trigger_mod):
    payload = _call(trigger_mod, "run_committee", symbol="???")
    assert payload["status"] == "error" and payload["error_type"] == "validation"
    assert trigger_mod._test_dispatch_calls == []
    rows = _audit_rows(trigger_mod)
    assert rows[0]["accepted"] is False and "resolve" in rows[0]["reason"].lower()


def test_ungrounded_symbol_refused(trigger_mod):
    payload = _call(trigger_mod, "run_committee", symbol="UNREAL-USDT")
    assert payload["status"] == "error" and payload["error_type"] == "validation"
    assert trigger_mod._test_dispatch_calls == []
    assert _audit_rows(trigger_mod)[0]["accepted"] is False


def test_budget_exhausted_after_cap(trigger_mod):
    assert _call(trigger_mod, "run_committee", symbol="BTC-USDT")["status"] == "ok"
    assert _call(trigger_mod, "run_committee", symbol="ETH-USDT")["status"] == "ok"
    payload = _call(trigger_mod, "run_committee", symbol="SOL-USDT")  # 3rd > budget 2
    assert payload["status"] == "error"
    assert payload["error_type"] == "budget_exhausted"
    assert "resets_at" in payload
    assert len(trigger_mod._test_dispatch_calls) == 2
    rows = _audit_rows(trigger_mod)
    assert [r["accepted"] for r in rows] == [True, True, False]
    assert rows[-1]["reason"] == "budget_exhausted"


def test_budget_is_file_backed_across_reload(trigger_mod, monkeypatch):
    now = datetime.now(timezone.utc).isoformat()
    with trigger_mod._test_audit_path.open("w", encoding="utf-8") as fh:
        for i in range(2):
            fh.write(json.dumps({"ts": now, "symbol": "BTC-USDT", "note": None,
                                 "accepted": True, "run_id": f"seed-{i}"}) + "\n")
    mod = _reload_mcp(monkeypatch, VIBE_MCP_COMMITTEE="1",
                      VIBE_MCP_ALLOW_TRIGGER="1", VIBE_MCP_TRIGGER_BUDGET="2")
    monkeypatch.setattr(mod, "_grounding_resolve", lambda s: s.upper())
    monkeypatch.setattr(mod, "_grounding_fetch", lambda sym: {sym: [{"close": 1.0}]})
    monkeypatch.setattr(mod, "_dispatch_committee_run", lambda s, t: "should-not-run")
    payload = _call(mod, "run_committee", symbol="BTC-USDT")
    assert payload["error_type"] == "budget_exhausted"


def test_stale_yesterday_rows_do_not_count(trigger_mod):
    old = "2020-01-01T00:00:00+00:00"
    with trigger_mod._test_audit_path.open("w", encoding="utf-8") as fh:
        for i in range(5):
            fh.write(json.dumps({"ts": old, "symbol": "BTC-USDT", "note": None,
                                 "accepted": True, "run_id": f"old-{i}"}) + "\n")
    assert _call(trigger_mod, "run_committee", symbol="BTC-USDT")["status"] == "ok"


def test_concurrent_triggers_never_exceed_budget(trigger_mod):
    """budget=2 (from the trigger_mod fixture); fire 6 concurrent calls and
    require exactly 2 accepted + dispatched, the rest refused as
    budget_exhausted. Without a lock around the budget-read -> audit-append
    section, concurrent threads can all observe the same used-count and all
    dispatch, exceeding the budget (TOCTOU)."""
    n = 6
    results: list[dict] = [None] * n
    barrier = threading.Barrier(n)

    def _worker(i):
        barrier.wait()  # maximize overlap so the race window is actually hit
        results[i] = _call(trigger_mod, "run_committee", symbol="BTC-USDT")

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    accepted = [r for r in results if r["status"] == "ok"]
    refused = [r for r in results if r["status"] == "error"]
    assert len(accepted) == 2
    assert len(refused) == n - 2
    assert all(r["error_type"] == "budget_exhausted" for r in refused)
    assert len(trigger_mod._test_dispatch_calls) == 2

    rows = _audit_rows(trigger_mod)
    assert len(rows) == n
    assert sum(1 for r in rows if r["accepted"]) == 2
    assert sum(1 for r in rows if not r["accepted"]) == n - 2
    assert all(r["reason"] == "budget_exhausted" for r in rows if not r["accepted"])


def test_accepted_audit_records_resolved_symbol(trigger_mod):
    """The audit row for an accepted trigger must record the canonicalized
    (resolved) symbol used for dispatch, plus the raw caller input under
    raw_symbol when they differ -- not just the raw string twice."""
    payload = _call(trigger_mod, "run_committee", symbol="btc-usdt")
    assert payload["status"] == "ok"
    assert payload["symbol"] == "BTC-USDT"
    rows = _audit_rows(trigger_mod)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "BTC-USDT"
    assert rows[0]["raw_symbol"] == "btc-usdt"
