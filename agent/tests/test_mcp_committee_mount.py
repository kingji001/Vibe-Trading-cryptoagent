"""The /mcp streamable-HTTP mount is gated by VIBE_MCP_COMMITTEE.

Gate off -> no /mcp route (serve routes byte-identical to today).
Gate on  -> /mcp mounted and an MCP 'initialize' handshake succeeds under
            TestClient (which runs the wired lifespan / session manager).
"""

from __future__ import annotations

import importlib
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _restore_api_server_module():
    """Undo the `importlib.reload(api_server)` pollution this file causes.

    `api_server.py` reads `API_AUTH_KEY` into a module-level `_API_KEY`
    global at import time (its `_configured_api_key()` fallback). Reloading
    `api_server` while a test has `monkeypatch.setenv("API_AUTH_KEY", ...)`
    active re-executes that line and bakes the key into the reloaded
    module's globals. `monkeypatch.undo()` only reverts `os.environ`, not
    that already-executed module attribute, so the key leaked into every
    later test importing `api_server` in the same process (breaking
    unrelated tests in test_settings_api.py). Reload once more here with a
    guaranteed-clean environment so the shared module is left as found.
    """
    yield
    os.environ.pop("API_AUTH_KEY", None)
    os.environ.pop("VIBE_MCP_COMMITTEE", None)
    import api_server
    import mcp_server

    importlib.reload(mcp_server)
    importlib.reload(api_server)


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


def _mcp_initialize_payload():
    return {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                   "clientInfo": {"name": "mount-test", "version": "1"}},
    }


_MCP_HEADERS = {"Accept": "application/json, text/event-stream",
                "Content-Type": "application/json"}


def test_mcp_initialize_handshake_over_http(monkeypatch):
    mod = _reload_api_server(monkeypatch, "1")
    app = FastAPI()
    assert mod._maybe_mount_committee_mcp(app) is True

    with TestClient(app) as client:  # enters the wired MCP lifespan
        resp = client.post("/mcp/", headers=_MCP_HEADERS, json=_mcp_initialize_payload())
        assert resp.status_code == 200, resp.text
        assert "protocolVersion" in resp.text  # SSE or JSON body carries the result


def test_mcp_rejects_non_loopback_client_without_key(monkeypatch):
    """/mcp must enforce the same non-loopback API-key policy as REST routes."""
    monkeypatch.setenv("API_AUTH_KEY", "s3cr3t")
    mod = _reload_api_server(monkeypatch, "1")
    app = FastAPI()
    assert mod._maybe_mount_committee_mcp(app) is True

    with TestClient(app, client=("10.7.7.7", 40000)) as client:
        resp = client.post("/mcp/", headers=_MCP_HEADERS, json=_mcp_initialize_payload())
        assert resp.status_code in (401, 403), resp.text


def test_mcp_allows_non_loopback_client_with_key(monkeypatch):
    monkeypatch.setenv("API_AUTH_KEY", "s3cr3t")
    mod = _reload_api_server(monkeypatch, "1")
    app = FastAPI()
    assert mod._maybe_mount_committee_mcp(app) is True

    headers = dict(_MCP_HEADERS, Authorization="Bearer s3cr3t")
    with TestClient(app, client=("10.7.7.7", 40000)) as client:
        resp = client.post("/mcp/", headers=headers, json=_mcp_initialize_payload())
        assert resp.status_code == 200, resp.text
        assert "protocolVersion" in resp.text


def test_mcp_loopback_client_bypasses_key_requirement(monkeypatch):
    """Loopback clients stay trusted even when API_AUTH_KEY is configured,
    matching require_auth's dev-mode escape hatch for the REST routes."""
    monkeypatch.setenv("API_AUTH_KEY", "s3cr3t")
    mod = _reload_api_server(monkeypatch, "1")
    app = FastAPI()
    assert mod._maybe_mount_committee_mcp(app) is True

    with TestClient(app) as client:
        resp = client.post("/mcp/", headers=_MCP_HEADERS, json=_mcp_initialize_payload())
        assert resp.status_code == 200, resp.text
        assert "protocolVersion" in resp.text
