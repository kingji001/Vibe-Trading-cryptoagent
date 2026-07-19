"""The /mcp streamable-HTTP mount is gated by VIBE_MCP_COMMITTEE.

Gate off -> no /mcp route (serve routes byte-identical to today).
Gate on  -> /mcp mounted and an MCP 'initialize' handshake succeeds under
            TestClient (which runs the wired lifespan / session manager).
"""

from __future__ import annotations

import importlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _reload_api_server(monkeypatch, gate):
    if gate is None:
        monkeypatch.delenv("VIBE_MCP_COMMITTEE", raising=False)
    else:
        monkeypatch.setenv("VIBE_MCP_COMMITTEE", gate)
    import mcp_server
    importlib.reload(mcp_server)   # register/unregister committee tools for this env
    import api_server
    return importlib.reload(api_server)


def test_no_mcp_mount_when_gate_off(monkeypatch):
    mod = _reload_api_server(monkeypatch, None)
    app = FastAPI()
    assert mod._maybe_mount_committee_mcp(app) is False
    assert not any(getattr(r, "path", "") == "/mcp" for r in app.routes)


def test_mcp_mounted_when_gate_on(monkeypatch):
    mod = _reload_api_server(monkeypatch, "1")
    app = FastAPI()
    assert mod._maybe_mount_committee_mcp(app) is True
    assert any(getattr(r, "path", "") == "/mcp" for r in app.routes)
    # Idempotent: a second call does not double-mount.
    assert mod._maybe_mount_committee_mcp(app) is True
    assert sum(1 for r in app.routes if getattr(r, "path", "") == "/mcp") == 1


def test_mcp_initialize_handshake_over_http(monkeypatch):
    mod = _reload_api_server(monkeypatch, "1")
    app = FastAPI()
    assert mod._maybe_mount_committee_mcp(app) is True

    with TestClient(app) as client:  # enters the wired MCP lifespan
        resp = client.post(
            "/mcp/",
            headers={"Accept": "application/json, text/event-stream",
                     "Content-Type": "application/json"},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                             "clientInfo": {"name": "mount-test", "version": "1"}}},
        )
        assert resp.status_code == 200, resp.text
        assert "protocolVersion" in resp.text  # SSE or JSON body carries the result
