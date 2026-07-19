"""Read-only paper-trading REST routes.

Mounted by ``agent/api_server.py`` via ``register_paper_routes(app, ...)``.
Every route is GET; there is NO mutation endpoint (``paper reset`` stays
CLI-only). Reads delegate to PaperStore / PaperBroker / src.paper.pnl and
never re-parse JSONL. Auth mirrors ``register_scheduled_routes``:
``require_auth`` and ``_validate_path_param`` are resolved from the host
``api_server`` module via ``sys.modules``.
"""

from __future__ import annotations

import sys as _sys
from typing import Any, Awaitable, Callable

from fastapi import Depends, FastAPI, Query

from src.paper import pnl as pnl_mod
from src.paper.broker import PaperBroker
from src.paper.store import PaperStore, paper_root

AuthDep = Callable[..., Awaitable[Any] | Any]


def register_paper_routes(app: FastAPI, require_auth: AuthDep | None = None) -> None:
    """Mount the read-only paper routes onto ``app``."""
    host = _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")
    if host is None:
        raise RuntimeError(
            "register_paper_routes: api_server module not in sys.modules; "
            "ensure api_server is imported before calling this function"
        )
    if require_auth is None:
        require_auth = host.require_auth

    def _host_validate_path_param(value: str, kind: str) -> None:
        h = _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")
        h._validate_path_param(value, kind)

    @app.get("/paper/status", dependencies=[Depends(require_auth)])
    async def paper_status():
        """Live mark-to-market equity snapshot (``PaperBroker.equity()``).

        Fetches live marks via ``price_fn`` for open positions; unfetchable
        positions are valued at ``avg_entry`` and flagged ``stale`` (never a
        fabricated price). Returns broker equity dict verbatim.
        """
        return PaperBroker(PaperStore(paper_root())).equity()

    @app.get("/paper/ledger", dependencies=[Depends(require_auth)])
    async def paper_ledger(limit: int = Query(200, ge=1, le=1000)):
        """Fill ledger, newest-last as stored; ``limit`` is a tail slice.

        Includes ``order_type=="noop"`` rows verbatim (they are real rows;
        the UI must not treat them as fills).
        """
        rows = list(PaperStore(paper_root()).iter_ledger())
        return rows[-limit:]

    @app.get("/paper/equity", dependencies=[Depends(require_auth)])
    async def paper_equity():
        """All persisted equity snapshots (``store.iter_equity()``)."""
        return list(PaperStore(paper_root()).iter_equity())

    @app.get("/paper/pnl/{decision_id}", dependencies=[Depends(require_auth)])
    async def paper_pnl(decision_id: str):
        """Per-decision PnL (``src.paper.pnl.decision_pnl``) verbatim.

        Never 404s for a missing/unexecuted decision — resolves to
        ``executed: false``; only the path-param character class is validated.
        """
        _host_validate_path_param(decision_id, "decision_id")
        return pnl_mod.decision_pnl(decision_id, PaperStore(paper_root()))
