# Committee Observatory UI + MCP Interface Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One-command operator UI for observing every committee discussion (seats, debate rounds, decisions, journal outcomes, paper PnL) plus a double-gated MCP surface so external agents can read performance and trigger budget-capped committee runs.

**Architecture:** Extend the existing FastAPI serve process with a read-only REST layer delegating to PaperStore/journal/swarm stores; add two lazy pages to the existing React app (poll-based list, SSE live-follow detail); extend `mcp_server.py` with a flag-gated committee tool group and mount its streamable-HTTP app at `/mcp` in serve; add a `vibe-trading ui` launcher.

**Tech Stack:** FastAPI, existing PaperStore/PaperBroker/journal/swarm stores, FastMCP (existing `mcp_server.py`), React + TypeScript + vite + echarts + react-i18next (existing frontend), pytest + TestClient, vitest (frontend).

**Spec:** `docs/superpowers/specs/2026-07-19-committee-observatory-mcp-design.md` (binding, including its incorporation of `docs/development-guides/A-trading-ops-dashboard.md` §2).

## Global Constraints

- Every REST route is `GET`. No mutation endpoint anywhere in the web surface; `paper reset` stays CLI-only.
- Route code delegates to `PaperStore` / `PaperBroker` / `src.paper.pnl.decision_pnl` / `src.committee.journal.load_entries` / existing swarm run store helpers — never re-parse JSONL, never re-glob run dirs when a store API exists.
- Auth: mirror `register_scheduled_routes` — resolve `require_auth` from host `api_server` via `sys.modules`; every route guarded with `dependencies=[Depends(require_auth)]`.
- Env toggles (all default OFF; unset ⇒ serve and `vibe-trading-mcp` byte-identical to today): `VIBE_MCP_COMMITTEE`, `VIBE_MCP_ALLOW_TRIGGER`, `VIBE_MCP_TRIGGER_BUDGET` (int, default 4). Truthy set `{"1","true","yes","on"}` (mirror `_persist_transcripts_enabled`, `agent/src/swarm/worker.py`).
- MCP tools read stores directly — no HTTP self-calls. Trigger audit log: `~/.vibe-trading/committee/mcp_triggers.jsonl`; budget counts `accepted: true` rows in the current UTC day from that file (no in-memory counter).
- Frontend: all strings via `react-i18next`, keys in ALL five locales (`en`, `zh-CN`, `ja`, `ko`, `ar`); RTL must not break layout. Poll 30–60s for paper/journal data; NO new SSE emitters — live-follow reuses the existing swarm event stream only.
- Never invent a price/number: missing artifacts render `"missing": true` / explicit "not available" states; sentinels and stale flags surface verbatim.
- Repo mechanics: venv `source .venv/bin/activate`; Python tests run from `agent/` with `python -m pytest`; run `git add` from repo root; anything under `docs/` needs `git add -f`; commit messages end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- New env vars must be registered with the conftest hermeticity guard if the existing pattern requires it (check `agent/tests/conftest.py`).

---

## Task R1: Paper read-only REST surface (`paper_routes.py`)

**Files:**
- Create: `agent/src/api/paper_routes.py`
- Test: `agent/tests/test_paper_api.py`
- (Registration in `agent/api_server.py` is finalized in Task R3 alongside committee routes; R1 adds its own line for independent GREEN.)

**Interfaces:**
- Produces: `register_paper_routes(app: FastAPI, require_auth: AuthDep | None = None) -> None`
- Endpoints: `GET /paper/status`, `GET /paper/ledger?limit=N`, `GET /paper/equity`, `GET /paper/pnl/{decision_id}`
- Consumes: `PaperStore(paper_root())` (`src/paper/store.py:27,46`), `PaperBroker(store).equity()` (`src/paper/broker.py:179,546`), `src.paper.pnl.decision_pnl(decision_id, store)` (`src/paper/pnl.py:232`), host `_validate_path_param`/`require_auth` via `sys.modules` (mirror `scheduled_routes.py:505-527`).

- [ ] **Step 1: Write the failing test** — `agent/tests/test_paper_api.py`:

```python
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
    resp = _client().get("/paper/pnl/..%2f..")
    assert resp.status_code in (400, 404)
```

- [ ] **Step 2: Run test to verify it fails** — from `agent/`:

```
source ../.venv/bin/activate && python -m pytest tests/test_paper_api.py -q
```

Expected failure: `404 Not Found` on every request (routes not registered) or `ImportError` for `register_paper_routes` — the module does not exist yet.

- [ ] **Step 3: Write minimal implementation** — `agent/src/api/paper_routes.py`:

```python
"""Read-only paper-trading REST routes.

Mounted by ``agent/api_server.py`` via ``register_paper_routes(app, ...)``.
Every route is GET; there is NO mutation endpoint (A-guide §2.1 — ``paper
reset`` stays CLI-only). Reads delegate to PaperStore / PaperBroker /
src.paper.pnl and never re-parse JSONL (A-guide §2.2). Auth mirrors
``register_scheduled_routes`` (A-guide §2.3): ``require_auth`` and
``_validate_path_param`` are resolved from the host ``api_server`` module via
``sys.modules``.
"""

from __future__ import annotations

import sys as _sys
from typing import Any, Awaitable, Callable

from fastapi import Depends, FastAPI, Query

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
        from src.paper.broker import PaperBroker
        from src.paper.store import PaperStore, paper_root

        return PaperBroker(PaperStore(paper_root())).equity()

    @app.get("/paper/ledger", dependencies=[Depends(require_auth)])
    async def paper_ledger(limit: int = Query(200, ge=1, le=1000)):
        """Fill ledger, newest-last as stored; ``limit`` is a tail slice.

        Includes ``order_type=="noop"`` rows verbatim (they are real rows;
        the UI must not treat them as fills).
        """
        from src.paper.store import PaperStore, paper_root

        rows = list(PaperStore(paper_root()).iter_ledger())
        return rows[-limit:]

    @app.get("/paper/equity", dependencies=[Depends(require_auth)])
    async def paper_equity():
        """All persisted equity snapshots (``store.iter_equity()``)."""
        from src.paper.store import PaperStore, paper_root

        return list(PaperStore(paper_root()).iter_equity())

    @app.get("/paper/pnl/{decision_id}", dependencies=[Depends(require_auth)])
    async def paper_pnl(decision_id: str):
        """Per-decision PnL (``src.paper.pnl.decision_pnl``) verbatim.

        Never 404s for a missing/unexecuted decision — resolves to
        ``executed: false``; only the path-param character class is validated.
        """
        from src.paper import pnl as pnl_mod
        from src.paper.store import PaperStore, paper_root

        _host_validate_path_param(decision_id, "decision_id")
        return pnl_mod.decision_pnl(decision_id, PaperStore(paper_root()))
```

Then add these two lines to `agent/api_server.py` after the scheduled-routes block (`api_server.py:1013`) so R1 is independently GREEN (R3 finalizes the combined block):

```python
from src.api.paper_routes import register_paper_routes  # noqa: E402
register_paper_routes(app)
```

- [ ] **Step 4: Run test to verify it passes** — from `agent/`:

```
source ../.venv/bin/activate && python -m pytest tests/test_paper_api.py -q
```

Expected: all 8 tests pass, zero writes outside tmp.

- [ ] **Step 5: Commit** — run `git add` from repo root:

```
cd /Users/opcw05/rt/vibe001/Vibe-Trading-cryptoagent && git add agent/src/api/paper_routes.py agent/tests/test_paper_api.py agent/api_server.py && git commit -m "feat(api): read-only paper REST surface (status/ledger/equity/pnl)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task R2: Committee runs REST — list + detail (`committee_routes.py` part 1)

**Files:**
- Create: `agent/src/api/committee_routes.py`
- Test: `agent/tests/test_committee_api.py`

**Interfaces:**
- Produces: `register_committee_routes(app: FastAPI, require_auth: AuthDep | None = None) -> None` (R3 extends the same function)
- Endpoints (this task): `GET /committee/runs?limit=N&status=&symbol=`, `GET /committee/runs/{run_id}`
- Consumes: `SwarmStore` + `swarm_runs_root` (`src/swarm/store.py:63,215,175`), `src.committee.journal.load_entries` (`journal.py:70`), `src.paper.pnl.decision_pnl` (`pnl.py:232`), host `_validate_path_param`/`require_auth`. Real shapes: `SwarmRun`(`id, preset_name, status, user_vars{target,timeframe}, tasks, created_at, completed_at, total_input_tokens, total_output_tokens`), `SwarmTask`(`id, agent_id, status, artifacts`); debate round from `-r{n}` task-id suffix (`presets.py:262`, e.g. `task-bull` / `task-bull-r2`); decision at `artifacts/portfolio_manager/decision.portfolio_decision.json`; seat reports at `artifacts/<agent_id>/report.md`.

- [ ] **Step 1: Write the failing test** — `agent/tests/test_committee_api.py`:

```python
"""API tests for the committee observatory REST surface.

Socket-free: swarm runs are seeded under a tmp root (committee_routes.
_swarm_runs_root monkeypatched); the journal env points at a tmp file. Loopback
TestClient bypasses dev-mode auth (see tests/test_alpha_compare_api.py).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api_server
from src.api import committee_routes
from src.swarm.models import RunStatus, SwarmRun, SwarmTask, TaskStatus


def _client() -> TestClient:
    return TestClient(api_server.app, client=("127.0.0.1", 50000))


@pytest.fixture(autouse=True)
def _tmp_swarm_and_journal(tmp_path, monkeypatch):
    runs_root = tmp_path / "swarm-runs"
    runs_root.mkdir()
    monkeypatch.setattr(committee_routes, "_swarm_runs_root", lambda: runs_root)
    monkeypatch.setenv("VIBE_TRADING_COMMITTEE_JOURNAL", str(tmp_path / "journal.jsonl"))
    return runs_root


def _seed_run(runs_root: Path, run_id: str, *, target="BTC-USDT",
              status=RunStatus.completed, with_reports=True, corrupt_decision=False):
    rd = runs_root / run_id
    (rd / "artifacts" / "portfolio_manager").mkdir(parents=True)
    tasks = [
        SwarmTask(id="task-market", agent_id="market_analyst", prompt_template="",
                  status=TaskStatus.completed),
        SwarmTask(id="task-bull", agent_id="bull_researcher", prompt_template="",
                  status=TaskStatus.completed),
        SwarmTask(id="task-bull-r2", agent_id="bull_researcher", prompt_template="",
                  status=TaskStatus.completed),
        SwarmTask(id="task-bear", agent_id="bear_researcher", prompt_template="",
                  status=TaskStatus.completed),
        SwarmTask(id="task-decision", agent_id="portfolio_manager", prompt_template="",
                  status=TaskStatus.completed),
    ]
    run = SwarmRun(
        id=run_id, preset_name="crypto_committee", status=status,
        user_vars={"target": target, "timeframe": "72h swing"}, tasks=tasks,
        created_at="2026-07-18T20:00:58+00:00",
        completed_at="2026-07-18T20:18:29+00:00",
        total_input_tokens=704573, total_output_tokens=104805,
    )
    (rd / "run.json").write_text(run.model_dump_json(indent=2), encoding="utf-8")
    if with_reports:
        for agent in ("market_analyst", "bull_researcher", "bear_researcher",
                      "portfolio_manager"):
            (rd / "artifacts" / agent).mkdir(parents=True, exist_ok=True)
            (rd / "artifacts" / agent / "report.md").write_text(
                f"# {agent} report\n", encoding="utf-8")
    dec_path = rd / "artifacts" / "portfolio_manager" / "decision.portfolio_decision.json"
    if corrupt_decision:
        dec_path.write_text("{ not json", encoding="utf-8")
    else:
        dec_path.write_text(json.dumps({
            "rating": "Hold", "price_target": 65500.0, "stop_loss": 61800.0,
            "take_profit": 65500.0, "position_size_pct": 80.0,
        }), encoding="utf-8")
    return rd


def _seed_journal(run_id: str, symbol="BTC-USDT"):
    from src.committee import journal
    return journal.append_decision(
        symbol=symbol, rating="Hold", time_horizon="72h swing",
        run_id=run_id, decided_at="2026-07-18T20:18:29+00:00",
    )


def test_runs_list_newest_first_with_shape(_tmp_swarm_and_journal):
    _seed_run(_tmp_swarm_and_journal, "swarm-20260718-200058-aaa")
    _seed_run(_tmp_swarm_and_journal, "swarm-20260719-100000-bbb")
    rows = _client().get("/committee/runs").json()
    assert [r["run_id"] for r in rows] == [
        "swarm-20260719-100000-bbb", "swarm-20260718-200058-aaa"]
    r = rows[0]
    assert set(r) >= {"run_id", "created_at", "status", "target",
                      "wall_clock_s", "input_tokens", "output_tokens"}
    assert r["target"] == "BTC-USDT"
    assert r["status"] == "completed"
    assert r["input_tokens"] == 704573
    assert r["wall_clock_s"] == pytest.approx(1050.0, abs=1.0)


def test_runs_list_joins_journal_by_run_id(_tmp_swarm_and_journal):
    _seed_run(_tmp_swarm_and_journal, "swarm-run-x")
    entry = _seed_journal("swarm-run-x")
    row = next(r for r in _client().get("/committee/runs").json()
               if r["run_id"] == "swarm-run-x")
    assert row["decision_id"] == entry["id"]
    assert row["rating"] == "Hold"
    assert row["journal_status"] == "pending"


def test_runs_list_filters_by_symbol_and_status(_tmp_swarm_and_journal):
    _seed_run(_tmp_swarm_and_journal, "run-btc", target="BTC-USDT")
    _seed_run(_tmp_swarm_and_journal, "run-eth", target="ETH-USDT",
              status=RunStatus.failed)
    only_eth = _client().get("/committee/runs", params={"symbol": "ETH-USDT"}).json()
    assert [r["run_id"] for r in only_eth] == ["run-eth"]
    only_failed = _client().get("/committee/runs", params={"status": "failed"}).json()
    assert [r["run_id"] for r in only_failed] == ["run-eth"]


def test_runs_list_ignores_non_committee_presets(_tmp_swarm_and_journal):
    rd = _tmp_swarm_and_journal / "run-other"
    rd.mkdir()
    run = SwarmRun(id="run-other", preset_name="research_team",
                   status=RunStatus.completed, created_at="2026-07-18T20:00:00+00:00")
    (rd / "run.json").write_text(run.model_dump_json(), encoding="utf-8")
    assert _client().get("/committee/runs").json() == []


def test_run_detail_shape(_tmp_swarm_and_journal):
    _seed_run(_tmp_swarm_and_journal, "run-detail")
    entry = _seed_journal("run-detail")
    body = _client().get("/committee/runs/run-detail").json()
    assert body["run"]["run_id"] == "run-detail"
    seats = {(s["agent_id"], s["round"]): s for s in body["seats"]}
    assert seats[("bull_researcher", 2)]["phase"] == "debate"
    assert seats[("market_analyst", 1)]["report_md"].startswith("# market_analyst")
    assert body["debate"]["rounds"] == 2
    assert body["decision"]["rating"] == "Hold"
    assert body["journal"]["reflection"] is None
    assert body["pnl"]["decision_id"] == entry["id"]


def test_run_detail_missing_report_marks_missing(_tmp_swarm_and_journal):
    rd = _seed_run(_tmp_swarm_and_journal, "run-nomd", with_reports=False)
    (rd / "artifacts" / "market_analyst").mkdir(parents=True, exist_ok=True)
    body = _client().get("/committee/runs/run-nomd").json()
    market = next(s for s in body["seats"] if s["agent_id"] == "market_analyst")
    assert market["report_md"] is None
    assert market["missing"] is True


def test_run_detail_corrupt_decision_reports_error_not_500(_tmp_swarm_and_journal):
    _seed_run(_tmp_swarm_and_journal, "run-baddec", corrupt_decision=True)
    resp = _client().get("/committee/runs/run-baddec")
    assert resp.status_code == 200
    assert "error" in resp.json()["decision"]


def test_run_detail_unknown_run_id_404(_tmp_swarm_and_journal):
    assert _client().get("/committee/runs/swarm-does-not-exist").status_code == 404


def test_run_detail_rejects_path_traversal(_tmp_swarm_and_journal):
    assert _client().get("/committee/runs/..%2f..").status_code in (400, 404)
```

- [ ] **Step 2: Run test to verify it fails** — from `agent/`:

```
source ../.venv/bin/activate && python -m pytest tests/test_committee_api.py -q
```

Expected failure: `ImportError`/`AttributeError` on `from src.api import committee_routes` (module absent), collected as errors.

- [ ] **Step 3: Write minimal implementation** — `agent/src/api/committee_routes.py`:

```python
"""Committee observatory REST routes (read-only, all GET).

Mounted by ``agent/api_server.py`` via ``register_committee_routes(app, ...)``.
Auth/host-symbol resolution mirrors ``register_scheduled_routes`` (host
``api_server`` via ``sys.modules``). Run listing REUSES ``SwarmStore``
(no re-globbing of ``.swarm/runs``); the journal join goes through
``src.committee.journal.load_entries``. Task R3 appends /journal/decisions,
/scheduler/health and /mcp/status to this same register function.
"""

from __future__ import annotations

import json
import sys as _sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from fastapi import Depends, FastAPI, HTTPException, Query

AuthDep = Callable[..., Awaitable[Any] | Any]

_COMMITTEE_PRESET = "crypto_committee"

# agent_id -> pipeline phase (analysts -> debate -> research_manager -> trader
# -> risk -> portfolio_manager -> reflection). Unknown seats fall back to "other".
_PHASE_BY_AGENT = {
    "market_analyst": "analysts", "news_analyst": "analysts",
    "onchain_analyst": "analysts", "sentiment_analyst": "analysts",
    "bull_researcher": "debate", "bear_researcher": "debate",
    "research_manager": "research_manager",
    "trader": "trader",
    "risky_analyst": "risk", "neutral_analyst": "risk", "safe_analyst": "risk",
    "portfolio_manager": "portfolio_manager",
    "reflection_officer": "reflection",
}


def _swarm_runs_root() -> Path:
    """Resolve the swarm runs directory (single source of truth).

    Wrapped so tests can monkeypatch it to a tmp dir without touching the real
    ``agent/.swarm/runs``.
    """
    from src.swarm.store import swarm_runs_root

    return swarm_runs_root()


def _swarm_store():
    """Construct a SwarmStore over the runs root (cheap; a Path holder)."""
    from src.swarm.store import SwarmStore

    return SwarmStore(base_dir=_swarm_runs_root())


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _wall_clock_s(created_at: str | None, completed_at: str | None) -> float | None:
    start, end = _parse_iso(created_at), _parse_iso(completed_at)
    if start is None or end is None:
        return None
    return (end - start).total_seconds()


def _debate_round(task_id: str) -> int:
    """Round number from the ``-r{n}`` debate suffix (default 1)."""
    marker = "-r"
    idx = task_id.rfind(marker)
    if idx != -1:
        suffix = task_id[idx + len(marker):]
        if suffix.isdigit():
            return int(suffix)
    return 1


def _journal_by_run_id() -> dict[str, dict]:
    """Map run_id -> latest journal entry for join (last write wins)."""
    from src.committee.journal import load_entries

    out: dict[str, dict] = {}
    for entry in load_entries():
        rid = entry.get("run_id")
        if rid:
            out[rid] = entry
    return out


def _pnl_summary_for(decision_id: str) -> str | None:
    from src.paper import pnl as pnl_mod
    from src.paper.store import PaperStore, paper_root

    try:
        return pnl_mod.decision_pnl(decision_id, PaperStore(paper_root())).get("summary")
    except Exception:  # never let a PnL read fail the list
        return None


def _list_item(run, journal_map: dict[str, dict]) -> dict[str, Any]:
    item: dict[str, Any] = {
        "run_id": run.id,
        "created_at": run.created_at,
        "status": run.status.value,
        "target": run.user_vars.get("target"),
        "wall_clock_s": _wall_clock_s(run.created_at, run.completed_at),
        "input_tokens": run.total_input_tokens,
        "output_tokens": run.total_output_tokens,
    }
    entry = journal_map.get(run.id)
    if entry:
        item["decision_id"] = entry["id"]
        item["rating"] = entry.get("rating")
        item["journal_status"] = entry.get("status")
        item["pnl_summary"] = _pnl_summary_for(entry["id"])
    return item


def _read_report(run_dir: Path, agent_id: str) -> dict[str, Any]:
    p = run_dir / "artifacts" / agent_id / "report.md"
    if not p.exists():
        return {"report_md": None, "missing": True}
    try:
        return {"report_md": p.read_text(encoding="utf-8")}
    except OSError as exc:  # corrupt/unreadable -> per-seat error, not 500
        return {"report_md": None, "error": str(exc)}


def _read_decision(run_dir: Path) -> dict[str, Any]:
    p = run_dir / "artifacts" / "portfolio_manager" / "decision.portfolio_decision.json"
    if not p.exists():
        return {"missing": True}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return {"error": str(exc)}


def register_committee_routes(app: FastAPI, require_auth: AuthDep | None = None) -> None:
    """Mount the committee observatory routes onto ``app``."""
    host = _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")
    if host is None:
        raise RuntimeError(
            "register_committee_routes: api_server module not in sys.modules; "
            "ensure api_server is imported before calling this function"
        )
    if require_auth is None:
        require_auth = host.require_auth

    def _host_validate_path_param(value: str, kind: str) -> None:
        h = _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")
        h._validate_path_param(value, kind)

    @app.get("/committee/runs", dependencies=[Depends(require_auth)])
    async def committee_runs(
        limit: int = Query(20, ge=1, le=100),
        status: Optional[str] = Query(None),
        symbol: Optional[str] = Query(None),
    ):
        """Newest-first crypto_committee runs joined with journal entries.

        A corrupt run.json is skipped by ``SwarmStore.list_runs`` (the list
        never fails because one run is unreadable).
        """
        journal_map = _journal_by_run_id()
        runs = _swarm_store().list_runs(limit=200)
        sym = symbol.upper() if symbol else None
        items: list[dict[str, Any]] = []
        for run in runs:
            if run.preset_name != _COMMITTEE_PRESET:
                continue
            if status is not None and run.status.value != status:
                continue
            if sym is not None and (run.user_vars.get("target") or "").upper() != sym:
                continue
            items.append(_list_item(run, journal_map))
            if len(items) >= limit:
                break
        return items

    @app.get("/committee/runs/{run_id}", dependencies=[Depends(require_auth)])
    async def committee_run_detail(run_id: str):
        """Full discussion for one run: seats, debate structure, decision,
        journal outcome, and paper PnL. Missing artifacts -> explicit markers,
        never fabricated."""
        _host_validate_path_param(run_id, "run_id")
        run = _swarm_store().load_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

        run_dir = _swarm_runs_root() / run_id
        seats: list[dict[str, Any]] = []
        debate_order: list[str] = []
        debate_rounds = 1
        for task in run.tasks:
            phase = _PHASE_BY_AGENT.get(task.agent_id, "other")
            rnd = _debate_round(task.id)
            seat: dict[str, Any] = {
                "agent_id": task.agent_id,
                "phase": phase,
                "round": rnd,
                "status": task.status.value,
            }
            seat.update(_read_report(run_dir, task.agent_id))
            if task.agent_id == "portfolio_manager":
                seat["decision_json"] = _read_decision(run_dir)
            seats.append(seat)
            if phase == "debate":
                debate_order.append(task.id)
                debate_rounds = max(debate_rounds, rnd)

        journal_map = _journal_by_run_id()
        entry = journal_map.get(run_id)
        journal_block = None
        pnl_block = None
        if entry:
            journal_block = {
                "horizons": entry.get("horizons"),
                "reflection": entry.get("reflection"),
                "reflected_at": entry.get("reflected_at"),
            }
            from src.paper import pnl as pnl_mod
            from src.paper.store import PaperStore, paper_root

            try:
                pnl_block = pnl_mod.decision_pnl(entry["id"], PaperStore(paper_root()))
            except Exception:
                pnl_block = None

        return {
            "run": {
                "run_id": run.id,
                "status": run.status.value,
                "target": run.user_vars.get("target"),
                "timeframe": run.user_vars.get("timeframe"),
                "created_at": run.created_at,
                "completed_at": run.completed_at,
                "wall_clock_s": _wall_clock_s(run.created_at, run.completed_at),
                "input_tokens": run.total_input_tokens,
                "output_tokens": run.total_output_tokens,
            },
            "seats": seats,
            "debate": {"rounds": debate_rounds, "order": debate_order},
            "decision": _read_decision(run_dir),
            "journal": journal_block,
            "pnl": pnl_block,
        }
```

Add the registration line to `agent/api_server.py` (finalized in R3), after the paper line:

```python
from src.api.committee_routes import register_committee_routes  # noqa: E402
register_committee_routes(app)
```

- [ ] **Step 4: Run test to verify it passes** — from `agent/`:

```
source ../.venv/bin/activate && python -m pytest tests/test_committee_api.py -q
```

Expected: all 9 tests pass.

- [ ] **Step 5: Commit** — from repo root:

```
git add agent/src/api/committee_routes.py agent/tests/test_committee_api.py agent/api_server.py && git commit -m "feat(api): committee runs list + discussion detail REST

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task R3: Journal / scheduler / mcp-status endpoints + registration (`committee_routes.py` part 2)

**Files:**
- Modify: `agent/src/api/committee_routes.py` (add 3 routes + helpers to the existing `register_committee_routes`)
- Modify: `agent/api_server.py` (finalize the routes-registration block for both modules)
- Modify: `agent/tests/conftest.py` (hermeticity guard for the 3 new env vars)
- Test: append to `agent/tests/test_committee_api.py`

**Interfaces:**
- Endpoints (this task): `GET /journal/decisions?limit=N&symbol=`, `GET /scheduler/health`, `GET /mcp/status`
- Consumes: `src.committee.journal.load_entries` (`journal.py:70`); host `_get_scheduled_research_store()` (re-exported on `api_server`, `api_server.py:1021`) → `store.list_jobs(limit=...)` returning `ScheduledResearchJob(id, prompt, schedule, next_run_at, status, created_at, config)`; run72 heartbeat at `${VIBE_OPS_ROOT:-~/.vibe-trading/ops}/heartbeat.jsonl` (rows `{ts,ok,http,latency_ms}`, `scripts/ops/run72.sh:44,65`); MCP triggers at `~/.vibe-trading/committee/mcp_triggers.jsonl`; env `VIBE_MCP_COMMITTEE`/`VIBE_MCP_ALLOW_TRIGGER`/`VIBE_MCP_TRIGGER_BUDGET`, truthy set `{"1","true","yes","on"}`.

- [ ] **Step 1: Write the failing test** — append to `agent/tests/test_committee_api.py`:

```python
# --- Task R3: journal / scheduler / mcp-status ------------------------------


def test_journal_decisions_newest_first_projection(_tmp_swarm_and_journal):
    from src.committee import journal
    journal.append_decision(symbol="BTC-USDT", rating="Buy", time_horizon="72h swing",
                            run_id="r1", decided_at="2026-07-18T00:00:00+00:00")
    journal.append_decision(symbol="ETH-USDT", rating="Hold", time_horizon="24h",
                            run_id="r2", decided_at="2026-07-19T00:00:00+00:00")
    rows = _client().get("/journal/decisions").json()
    assert [r["symbol"] for r in rows] == ["ETH-USDT", "BTC-USDT"]  # newest first
    r = rows[0]
    assert set(r) >= {"id", "decided_at", "symbol", "rating", "status",
                      "primary_horizon", "horizons", "reflected_at", "run_id"}


def test_journal_decisions_symbol_filter_and_limit(_tmp_swarm_and_journal):
    from src.committee import journal
    journal.append_decision(symbol="BTC-USDT", rating="Buy", time_horizon="72h swing",
                            run_id="r1", decided_at="2026-07-18T00:00:00+00:00")
    journal.append_decision(symbol="ETH-USDT", rating="Hold", time_horizon="24h",
                            run_id="r2", decided_at="2026-07-19T00:00:00+00:00")
    rows = _client().get("/journal/decisions",
                         params={"symbol": "ETH-USDT", "limit": 5}).json()
    assert [r["symbol"] for r in rows] == ["ETH-USDT"]


def test_journal_decisions_empty_when_no_file(_tmp_swarm_and_journal):
    assert _client().get("/journal/decisions").json() == []


def test_scheduler_health_lists_jobs(_tmp_swarm_and_journal, monkeypatch):
    monkeypatch.setenv("VIBE_TRADING_ENABLE_SCHEDULER", "0")
    store = api_server._get_scheduled_research_store()
    from src.scheduled_research.models import ScheduledResearchJob
    store.upsert(ScheduledResearchJob(id="committee-run", prompt="p", schedule="0 0 * * *"))
    body = _client().get("/scheduler/health").json()
    ids = {j["id"] for j in body["jobs"]}
    assert "committee-run" in ids
    job = next(j for j in body["jobs"] if j["id"] == "committee-run")
    assert set(job) >= {"id", "schedule", "status", "next_run_at"}
    assert body["supervisor"] is None  # no heartbeat file seeded


def test_scheduler_health_supervisor_from_heartbeat(_tmp_swarm_and_journal, tmp_path, monkeypatch):
    ops = tmp_path / "ops"
    ops.mkdir()
    (ops / "heartbeat.jsonl").write_text(
        '{"ts":"2026-07-19T00:00:00Z","ok":true,"http":200,"latency_ms":12}\n',
        encoding="utf-8")
    monkeypatch.setenv("VIBE_OPS_ROOT", str(ops))
    body = _client().get("/scheduler/health").json()
    assert body["supervisor"] is not None
    assert body["supervisor"]["last_row"]["ok"] is True


def test_mcp_status_defaults_off(_tmp_swarm_and_journal, monkeypatch):
    for var in ("VIBE_MCP_COMMITTEE", "VIBE_MCP_ALLOW_TRIGGER", "VIBE_MCP_TRIGGER_BUDGET"):
        monkeypatch.delenv(var, raising=False)
    body = _client().get("/mcp/status").json()
    assert body == {
        "committee_tools_enabled": False, "trigger_enabled": False,
        "trigger_budget": 4, "triggers_used_today": 0,
        "http_mount": None, "stdio_command": "vibe-trading-mcp",
    }


def test_mcp_status_gated_on_counts_today_triggers(_tmp_swarm_and_journal, tmp_path, monkeypatch):
    monkeypatch.setenv("VIBE_MCP_COMMITTEE", "1")
    monkeypatch.setenv("VIBE_MCP_ALLOW_TRIGGER", "yes")
    monkeypatch.setenv("VIBE_MCP_TRIGGER_BUDGET", "6")
    trig = tmp_path / "committee"
    trig.mkdir()
    monkeypatch.setattr(committee_routes, "_mcp_triggers_path", lambda: trig / "mcp_triggers.jsonl")
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    (trig / "mcp_triggers.jsonl").write_text(
        f'{{"ts":"{today}","symbol":"BTC-USDT","accepted":true}}\n'
        '{"ts":"2020-01-01T00:00:00Z","symbol":"BTC-USDT","accepted":true}\n',
        encoding="utf-8")
    body = _client().get("/mcp/status").json()
    assert body["committee_tools_enabled"] is True
    assert body["trigger_enabled"] is True
    assert body["trigger_budget"] == 6
    assert body["triggers_used_today"] == 1  # only today's row
    assert body["http_mount"] == "/mcp"
```

Also register the conftest hermeticity guard for the new env vars — append inside `_paper_env_guard` (`agent/tests/conftest.py:116`, before `yield`):

```python
    for _mcp_var in ("VIBE_MCP_COMMITTEE", "VIBE_MCP_ALLOW_TRIGGER", "VIBE_MCP_TRIGGER_BUDGET"):
        monkeypatch.delenv(_mcp_var, raising=False)
```

- [ ] **Step 2: Run test to verify it fails** — from `agent/`:

```
source ../.venv/bin/activate && python -m pytest tests/test_committee_api.py -k "journal_decisions or scheduler_health or mcp_status" -q
```

Expected failure: `404 Not Found` on `/journal/decisions`, `/scheduler/health`, `/mcp/status` (routes not yet added).

- [ ] **Step 3: Write minimal implementation** — add these module-level helpers to `agent/src/api/committee_routes.py` (after `_read_decision`):

```python
_MCP_TRUE_VALUES = {"1", "true", "yes", "on"}


def _truthy(env_name: str) -> bool:
    import os

    return os.environ.get(env_name, "").strip().lower() in _MCP_TRUE_VALUES


def _ops_root() -> Path:
    import os

    override = os.environ.get("VIBE_OPS_ROOT")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".vibe-trading" / "ops"


def _mcp_triggers_path() -> Path:
    """Same file M3's run_committee writes. Honors the VIBE_MCP_TRIGGER_AUDIT
    override (hermetic tests / ops) exactly like mcp_server._mcp_triggers_path
    so REST and MCP always read/write one audit log."""
    import os

    env = os.environ.get("VIBE_MCP_TRIGGER_AUDIT", "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".vibe-trading" / "committee" / "mcp_triggers.jsonl"


def _supervisor_liveness() -> dict[str, Any] | None:
    """Best-effort run72 heartbeat: file mtime + last row. None when absent."""
    p = _ops_root() / "heartbeat.jsonl"
    if not p.exists():
        return None
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return None
    last_row = None
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line:
            try:
                last_row = json.loads(line)
            except json.JSONDecodeError:
                last_row = None
            break
    return {"heartbeat_mtime": p.stat().st_mtime, "last_row": last_row}


def _triggers_used_today() -> int:
    """Count today's (UTC) ACCEPTED rows in the MCP trigger audit log; 0 if
    absent. accepted=false rows (refusals) never consume budget — identical
    semantics to mcp_server._triggers_used_today (M3), which owns writing."""
    p = _mcp_triggers_path()
    if not p.exists():
        return 0
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    count = 0
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("accepted") and str(row.get("ts", ""))[:10] == today:
                count += 1
    except OSError:
        return 0
    return count
```

Then add these three routes inside `register_committee_routes`, after the `committee_run_detail` route (before the function ends):

```python
    @app.get("/journal/decisions", dependencies=[Depends(require_auth)])
    async def journal_decisions(
        limit: int = Query(50, ge=1, le=500),
        symbol: Optional[str] = Query(None),
    ):
        """Newest-first journal projection (load_entries is oldest-first)."""
        from src.committee.journal import load_entries

        entries = list(reversed(load_entries()))
        if symbol:
            sym = symbol.upper()
            entries = [e for e in entries if (e.get("symbol") or "").upper() == sym]
        entries = entries[:limit]
        return [
            {
                "id": e["id"],
                "decided_at": e.get("decided_at"),
                "symbol": e.get("symbol"),
                "rating": e.get("rating"),
                "status": e.get("status"),
                "primary_horizon": e.get("primary_horizon"),
                "horizons": e.get("horizons"),
                "reflected_at": e.get("reflected_at"),
                "run_id": e.get("run_id"),
            }
            for e in entries
        ]

    @app.get("/scheduler/health", dependencies=[Depends(require_auth)])
    async def scheduler_health():
        """Registered scheduled jobs + best-effort run72 supervisor liveness."""
        h = _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")
        jobs = h._get_scheduled_research_store().list_jobs(limit=200)
        return {
            "jobs": [
                {
                    "id": j.id,
                    "schedule": j.schedule,
                    "status": j.status.value,
                    "next_run_at": j.next_run_at,
                }
                for j in jobs
            ],
            "supervisor": _supervisor_liveness(),
        }

    @app.get("/mcp/status", dependencies=[Depends(require_auth)])
    async def mcp_status():
        """MCP interface gate state (read-only reflection of env toggles)."""
        import os

        committee_on = _truthy("VIBE_MCP_COMMITTEE")
        try:
            budget = int(os.environ.get("VIBE_MCP_TRIGGER_BUDGET", "4"))
        except ValueError:
            budget = 4
        return {
            "committee_tools_enabled": committee_on,
            "trigger_enabled": committee_on and _truthy("VIBE_MCP_ALLOW_TRIGGER"),
            "trigger_budget": budget,
            "triggers_used_today": _triggers_used_today(),
            "http_mount": "/mcp" if committee_on else None,
            "stdio_command": "vibe-trading-mcp",
        }
```

Finalize `agent/api_server.py`: ensure the routes-registration block after `register_scheduled_routes(app)` (`api_server.py:1013`) contains both modules exactly once — reconcile the lines R1/R2 added into one block:

```python
from src.api.paper_routes import register_paper_routes  # noqa: E402
register_paper_routes(app)

from src.api.committee_routes import register_committee_routes  # noqa: E402
register_committee_routes(app)
```

- [ ] **Step 4: Run test to verify it passes** — from `agent/`:

```
source ../.venv/bin/activate && python -m pytest tests/test_committee_api.py tests/test_paper_api.py -q
```

Expected: full committee + paper API suites pass (R1/R2/R3 combined), including the new journal/scheduler/mcp cases.

- [ ] **Step 5: Commit** — from repo root:

```
git add agent/src/api/committee_routes.py agent/tests/test_committee_api.py agent/tests/conftest.py agent/api_server.py && git commit -m "feat(api): journal/scheduler/mcp-status observatory endpoints + registration

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

**Implementer caveat (from the planning pass):** `/scheduler/health` returns whatever jobs are persisted in the shared `ScheduledResearchJobStore`; its store path is not tmp-pinned by conftest. If a test sees cross-test job bleed, pin the store dir in the scheduler test (the provided test sets `VIBE_TRADING_ENABLE_SCHEDULER=0` and asserts only that the seeded job is present, which is bleed-tolerant).

---

## Task M1: Gate helpers + committee READ tool group in `mcp_server.py`

**Section note (consistency with Task R3):** `run_committee` (M3) writes/reads `~/.vibe-trading/committee/mcp_triggers.jsonl` with rows `{ts, symbol, note, accepted, reason?, run_id?}`; REST `/mcp/status` (R3) reads the same file for its counter. M3 adds an optional `VIBE_MCP_TRIGGER_AUDIT` path-override env (default = the pinned path) purely for hermetic tests; production default is byte-identical either way.

**Files:**
- Modify: `agent/mcp_server.py` (add gate helpers, aggregation/join/transcript helpers, and a gated `_register_committee_read_tools(mcp)` registrar covering `committee_performance`, `list_decisions`, `get_decision`, `list_committee_runs`, `get_run_transcript`, `paper_account`)
- Modify: `agent/tests/conftest.py` (register `VIBE_MCP_COMMITTEE` in the dotenv leak-guard list)
- Create/Test: `agent/tests/test_mcp_committee_tools.py`

**Interfaces:**
- Consumes: `src.committee.journal.load_entries(path=None) -> list[dict]` (each dict: `id, decided_at, symbol, rating, status, primary_horizon, horizons{"24h"|"72h"|"7d":{raw_return,benchmark_return,alpha,mark_price,direction_correct,resolved_at}}, reflection, reflected_at, run_id`); `src.committee.journal.HORIZONS`, `DEFAULT_BENCHMARK`; `SwarmStore.list_runs(limit)`, `.reconcile_run(run, write=False)`, `.run_dir(run_id) -> Path`, `.load_run(run_id)`; `PaperStore(paper_root())`, `.iter_ledger()`, `.iter_equity()`; existing `_get_swarm_store()`, `_json_ok/_json_error`.
- Produces (FastMCP tools registered only when `VIBE_MCP_COMMITTEE` truthy): `committee_performance(window_hours: int|None=None, symbol: str|None=None)`, `list_decisions(limit: int=20, symbol: str|None=None)`, `get_decision(decision_id: str)`, `list_committee_runs(limit: int=20, status: str|None=None)`, `get_run_transcript(run_id: str, seat: str|None=None)`, `paper_account()`. Each returns a `_json_ok` string envelope; reads stores directly (no HTTP self-calls, no live price fetches).

- [ ] **Step 1 — failing test** — write `agent/tests/test_mcp_committee_tools.py`:

```python
"""Gate + behavior tests for the committee READ tool group in mcp_server.

The tools are registered only when VIBE_MCP_COMMITTEE is truthy. Because
registration is an import-time side effect keyed on the env var, each test
sets the env then reloads the module and introspects / calls the tools via
the FastMCP instance (mcp.list_tools / mcp.call_tool), exactly the surface a
real MCP client drives.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from datetime import datetime, timedelta, timezone

import pytest

COMMITTEE_TOOL_NAMES = {
    "committee_performance",
    "list_decisions",
    "get_decision",
    "list_committee_runs",
    "get_run_transcript",
    "paper_account",
}


def _reload_mcp(monkeypatch, **env):
    for key, val in env.items():
        if val is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, val)
    import mcp_server
    return importlib.reload(mcp_server)


def _tool_names(mod) -> set[str]:
    tools = asyncio.run(mod.mcp.list_tools())
    return {t.name for t in tools}


def _call(mod, name, **args) -> dict:
    result = asyncio.run(mod.mcp.call_tool(name, args))
    return json.loads(result.content[0].text)


def _seed_journal(path, *, run_id, symbol="BTC-USDT", decided_at=None,
                  rating="Buy", status="resolved", horizons=None):
    decided_at = decided_at or datetime.now(timezone.utc).isoformat()
    horizons = horizons if horizons is not None else {
        "24h": {"raw_return": 0.05, "benchmark_return": 0.0, "alpha": 0.05,
                "mark_price": 105.0, "direction_correct": True,
                "resolved_at": decided_at},
    }
    entry = {
        "id": "dec_" + run_id[-8:], "decided_at": decided_at, "symbol": symbol,
        "rating": rating, "time_horizon": "24h", "primary_horizon": "24h",
        "price_target": 110.0, "run_id": run_id, "status": status,
        "ref_price": 100.0, "horizons": horizons,
        "reflection": "Momentum held.", "reflected_at": decided_at,
    }
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
    return entry


def _seed_committee_run(swarm_root, *, run_id, symbol="BTC-USDT", status="completed",
                        report="## Bull\nlong.", decision=None):
    rd = swarm_root / run_id
    (rd / "artifacts" / "bull_researcher").mkdir(parents=True, exist_ok=True)
    (rd / "artifacts" / "portfolio_manager").mkdir(parents=True, exist_ok=True)
    (rd / "artifacts" / "bull_researcher" / "report.md").write_text(report, encoding="utf-8")
    if decision is not None:
        (rd / "artifacts" / "portfolio_manager" / "decision.portfolio_decision.json").write_text(
            json.dumps(decision), encoding="utf-8")
    now = datetime.now(timezone.utc)
    run_json = {
        "id": run_id, "preset_name": "crypto_committee", "status": status,
        "user_vars": {"target": symbol, "timeframe": "24h"},
        "created_at": now.isoformat(),
        "completed_at": (now + timedelta(seconds=90)).isoformat(),
        "total_input_tokens": 1000, "total_output_tokens": 500,
        "tasks": [
            {"id": "task-bull-r1", "agent_id": "bull_researcher", "status": "completed",
             "summary": "bull", "worker_iterations": 1, "error": None,
             "started_at": now.isoformat(), "completed_at": now.isoformat(),
             "depends_on": [], "blocked_by": []},
            {"id": "task-decision", "agent_id": "portfolio_manager", "status": "completed",
             "summary": "pm", "worker_iterations": 1, "error": None,
             "started_at": now.isoformat(), "completed_at": now.isoformat(),
             "depends_on": ["task-bull-r1"], "blocked_by": []},
        ],
    }
    (rd / "run.json").write_text(json.dumps(run_json), encoding="utf-8")
    return run_id


@pytest.fixture
def committee_env(tmp_path, monkeypatch):
    journal = tmp_path / "journal.jsonl"
    swarm_root = tmp_path / "runs"
    paper_root = tmp_path / "paper"
    swarm_root.mkdir()
    paper_root.mkdir()
    monkeypatch.setenv("VIBE_TRADING_COMMITTEE_JOURNAL", str(journal))
    monkeypatch.setenv("VIBE_SWARM_RUNS_DIR", str(swarm_root))
    monkeypatch.setenv("VIBE_PAPER_ROOT", str(paper_root))
    return {"journal": journal, "swarm_root": swarm_root, "paper_root": paper_root}


def test_committee_tools_absent_when_gate_off(monkeypatch):
    mod = _reload_mcp(monkeypatch, VIBE_MCP_COMMITTEE=None, VIBE_MCP_ALLOW_TRIGGER=None)
    names = _tool_names(mod)
    assert not (COMMITTEE_TOOL_NAMES & names), f"committee tools leaked with gate off: {COMMITTEE_TOOL_NAMES & names}"
    assert "run_committee" not in names
    assert {"analyze_options", "get_market_data", "list_skills"} <= names


def test_committee_read_tools_present_with_gate_on(monkeypatch):
    mod = _reload_mcp(monkeypatch, VIBE_MCP_COMMITTEE="1", VIBE_MCP_ALLOW_TRIGGER=None)
    names = _tool_names(mod)
    assert COMMITTEE_TOOL_NAMES <= names
    assert "run_committee" not in names  # gate 2 still off


def test_list_and_get_decision(monkeypatch, committee_env):
    _seed_journal(committee_env["journal"], run_id="swarm-aaa11111", symbol="BTC-USDT")
    _seed_journal(committee_env["journal"], run_id="swarm-bbb22222", symbol="ETH-USDT")
    mod = _reload_mcp(monkeypatch, VIBE_MCP_COMMITTEE="1")

    payload = _call(mod, "list_decisions", limit=10)
    assert payload["status"] == "ok"
    ids = [d["id"] for d in payload["decisions"]]
    assert ids == ["dec_bbb22222", "dec_aaa11111"]  # newest-first

    filtered = _call(mod, "list_decisions", limit=10, symbol="ETH-USDT")
    assert [d["symbol"] for d in filtered["decisions"]] == ["ETH-USDT"]

    got = _call(mod, "get_decision", decision_id="dec_aaa11111")
    assert got["status"] == "ok"
    assert got["decision"]["horizons"]["24h"]["alpha"] == 0.05
    assert got["decision"]["reflection"] == "Momentum held."

    missing = _call(mod, "get_decision", decision_id="dec_nope")
    assert missing["status"] == "error" and missing["error_type"] == "not_found"


def test_list_committee_runs_join_and_status_filter(monkeypatch, committee_env):
    _seed_committee_run(committee_env["swarm_root"], run_id="swarm-aaa11111", status="completed")
    _seed_committee_run(committee_env["swarm_root"], run_id="swarm-ccc33333", status="running")
    _seed_journal(committee_env["journal"], run_id="swarm-aaa11111", rating="Buy")
    mod = _reload_mcp(monkeypatch, VIBE_MCP_COMMITTEE="1")

    payload = _call(mod, "list_committee_runs", limit=10)
    assert payload["status"] == "ok"
    by_id = {r["run_id"]: r for r in payload["runs"]}
    assert by_id["swarm-aaa11111"]["rating"] == "Buy"
    assert by_id["swarm-aaa11111"]["decision_id"] == "dec_aaa11111"
    assert by_id["swarm-ccc33333"]["rating"] is None  # unjournaled
    assert by_id["swarm-aaa11111"]["target"] == "BTC-USDT"

    running = _call(mod, "list_committee_runs", limit=10, status="running")
    assert [r["run_id"] for r in running["runs"]] == ["swarm-ccc33333"]


def test_get_run_transcript(monkeypatch, committee_env):
    _seed_committee_run(
        committee_env["swarm_root"], run_id="swarm-aaa11111",
        report="## Bull case\nGo long.",
        decision={"rating": "Buy", "price_target": 110.0},
    )
    mod = _reload_mcp(monkeypatch, VIBE_MCP_COMMITTEE="1")

    payload = _call(mod, "get_run_transcript", run_id="swarm-aaa11111")
    assert payload["status"] == "ok"
    seats = {s["agent_id"]: s for s in payload["seats"]}
    assert "Bull case" in seats["bull_researcher"]["report_md"]
    assert seats["bull_researcher"]["round"] == 1
    assert payload["decision"]["rating"] == "Buy"
    assert payload["debate"]["rounds"] == 1

    only = _call(mod, "get_run_transcript", run_id="swarm-aaa11111", seat="bull_researcher")
    assert [s["agent_id"] for s in only["seats"]] == ["bull_researcher"]

    missing = _call(mod, "get_run_transcript", run_id="swarm-doesnotexist")
    assert missing["status"] == "error" and missing["error_type"] == "not_found"


def test_get_run_transcript_missing_artifact_marked_not_fabricated(monkeypatch, committee_env):
    rid = "swarm-aaa11111"
    _seed_committee_run(committee_env["swarm_root"], run_id=rid, decision=None)
    (committee_env["swarm_root"] / rid / "artifacts" / "bull_researcher" / "report.md").unlink()
    mod = _reload_mcp(monkeypatch, VIBE_MCP_COMMITTEE="1")

    payload = _call(mod, "get_run_transcript", run_id=rid)
    seats = {s["agent_id"]: s for s in payload["seats"]}
    assert seats["bull_researcher"]["report_md"] is None
    assert seats["bull_researcher"]["missing"] is True
    assert payload["decision"] is None


def test_committee_performance_single_symbol_caveat(monkeypatch, committee_env):
    now = datetime.now(timezone.utc)
    _seed_journal(committee_env["journal"], run_id="swarm-aaa11111", symbol="BTC-USDT",
                  decided_at=now.isoformat(), rating="Buy")
    _seed_committee_run(committee_env["swarm_root"], run_id="swarm-aaa11111")
    mod = _reload_mcp(monkeypatch, VIBE_MCP_COMMITTEE="1")

    payload = _call(mod, "committee_performance")
    assert payload["status"] == "ok"
    assert "definitionally ~0" in payload["alpha_caveat"]
    h = payload["horizons"]["24h"]
    assert h["count"] == 1
    assert h["direction_correct_rate"] == 1.0
    assert h["mean_alpha"] == 0.05
    assert payload["runs"]["count"] == 1
    assert payload["runs"]["avg_input_tokens"] == 1000


def test_committee_performance_window_filter(monkeypatch, committee_env):
    now = datetime.now(timezone.utc)
    _seed_journal(committee_env["journal"], run_id="swarm-old00000", symbol="BTC-USDT",
                  decided_at=(now - timedelta(hours=200)).isoformat())
    _seed_journal(committee_env["journal"], run_id="swarm-new11111", symbol="BTC-USDT",
                  decided_at=(now - timedelta(hours=1)).isoformat())
    mod = _reload_mcp(monkeypatch, VIBE_MCP_COMMITTEE="1")

    payload = _call(mod, "committee_performance", window_hours=24)
    assert payload["horizons"]["24h"]["count"] == 1  # only the recent decision


def test_paper_account(monkeypatch, committee_env):
    from src.paper.store import PaperStore
    store = PaperStore(committee_env["paper_root"])
    store.create_account(100_000.0, {"fee_bps": 10})
    store.append_ledger({"ts": "2026-07-19T00:00:00Z", "symbol": "BTC-USDT", "side": "buy",
                         "qty": 1.0, "fill_price": 100.0, "fee_paid": 0.1,
                         "realized_pnl": None, "order_type": "market"})
    mod = _reload_mcp(monkeypatch, VIBE_MCP_COMMITTEE="1")

    payload = _call(mod, "paper_account")
    assert payload["status"] == "ok"
    assert payload["equity"]["cash"] == 100_000.0
    assert payload["ledger_tail"][-1]["symbol"] == "BTC-USDT"
```

> Implementer note: the seeded journal/run use `VIBE_TRADING_COMMITTEE_JOURNAL` (from `journal.py`) and `VIBE_PAPER_ROOT` (from `paper/store.py`). If `swarm_runs_root()` in `src/swarm/store.py` reads a different env var than `VIBE_SWARM_RUNS_DIR`, adjust the fixture to that var (grep `swarm_runs_root`); everything else is unaffected.

- [ ] **Step 2 — run, expect fail** — from `agent/`:

```
source ../.venv/bin/activate && python -m pytest tests/test_mcp_committee_tools.py -q
```

Expected: import succeeds but gate-on / behavior tests fail (tools absent / KeyError).

- [ ] **Step 3 — implementation** — in `agent/mcp_server.py`, add after `_json_error` (~line 136) the gate + committee constants and helpers:

```python
# ---------------------------------------------------------------------------
# Committee Observatory MCP — double-gated, both default OFF (spec §3.4)
# ---------------------------------------------------------------------------
_MCP_TRUE_VALUES = {"1", "true", "yes", "on"}  # mirrors _persist_transcripts_enabled
_MCP_COMMITTEE_ENV = "VIBE_MCP_COMMITTEE"
_MCP_ALLOW_TRIGGER_ENV = "VIBE_MCP_ALLOW_TRIGGER"
COMMITTEE_PRESET = "crypto_committee"

ALPHA_CAVEAT = (
    "Alpha is measured against BTC-USDT. For a single-symbol universe (or the "
    "benchmark asset itself) alpha vs a same-symbol benchmark is definitionally "
    "~0, so directional correctness is judged on raw return, not alpha."
)

# Coarse pipeline phase per crypto_committee seat (unknown seats -> 'other').
_SEAT_PHASE = {
    "reflection_officer": "reflection",
    "market_analyst": "analysis", "onchain_analyst": "analysis",
    "news_analyst": "analysis", "sentiment_analyst": "analysis",
    "bull_researcher": "debate", "bear_researcher": "debate",
    "research_manager": "research_management",
    "trader": "trading",
    "risky_analyst": "risk", "safe_analyst": "risk", "neutral_analyst": "risk",
    "portfolio_manager": "decision",
}


def _mcp_committee_enabled() -> bool:
    """Gate 1: registers the committee READ tool group (default OFF)."""
    return os.getenv(_MCP_COMMITTEE_ENV, "").strip().lower() in _MCP_TRUE_VALUES


def _mcp_trigger_enabled() -> bool:
    """Gate 2: only meaningful when gate 1 is on (default OFF)."""
    return os.getenv(_MCP_ALLOW_TRIGGER_ENV, "").strip().lower() in _MCP_TRUE_VALUES


def _parse_iso_utc(value: str | None):
    from datetime import datetime, timezone
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _wall_clock_s(created_at: str | None, completed_at: str | None) -> float | None:
    start, end = _parse_iso_utc(created_at), _parse_iso_utc(completed_at)
    if start is None or end is None:
        return None
    return round((end - start).total_seconds(), 1)


def _round_from_task_id(task_id: str) -> int | None:
    """Debate round from the '-r{n}' suffix convention (_expand_debate)."""
    import re
    m = re.search(r"-r(\d+)$", task_id or "")
    return int(m.group(1)) if m else None


def _decision_projection(entry: dict, *, full: bool) -> dict:
    """Journal entry -> public projection. full=True keeps horizons+reflection."""
    proj = {
        "id": entry.get("id"),
        "decided_at": entry.get("decided_at"),
        "symbol": entry.get("symbol"),
        "rating": entry.get("rating"),
        "status": entry.get("status"),
        "primary_horizon": entry.get("primary_horizon"),
        "reflected_at": entry.get("reflected_at"),
        "run_id": entry.get("run_id"),
    }
    if full:
        proj["horizons"] = entry.get("horizons") or {}
        proj["reflection"] = entry.get("reflection")
        proj["price_target"] = entry.get("price_target")
        proj["time_horizon"] = entry.get("time_horizon")
    return proj


def _committee_run_row(run, entry: dict | None) -> dict:
    row = {
        "run_id": run.id,
        "created_at": run.created_at,
        "status": run.status.value,
        "target": (run.user_vars or {}).get("target"),
        "wall_clock_s": _wall_clock_s(run.created_at, run.completed_at),
        "input_tokens": run.total_input_tokens,
        "output_tokens": run.total_output_tokens,
        "decision_id": None,
        "rating": None,
        "journal_status": None,
    }
    if entry:
        row["decision_id"] = entry.get("id")
        row["rating"] = entry.get("rating")
        row["journal_status"] = entry.get("status")
    return row


def _committee_runs_joined(store, *, limit: int, status: str | None):
    """crypto_committee runs (newest-first) joined to journal entries by run_id."""
    from src.committee.journal import load_entries
    entries_by_run = {e.get("run_id"): e for e in load_entries() if e.get("run_id")}
    rows: list[dict] = []
    for run in store.list_runs(limit=200):
        if run.preset_name != COMMITTEE_PRESET:
            continue
        recon = store.reconcile_run(run, write=False)
        if status and recon.status.value != status:
            continue
        rows.append(_committee_run_row(recon, entries_by_run.get(recon.id)))
        if len(rows) >= limit:
            break
    return rows


def _read_seat_report(run_dir, agent_id: str) -> dict:
    """Read artifacts/<agent>/report.md; never fabricate a missing file."""
    report_path = run_dir / "artifacts" / agent_id / "report.md"
    if not report_path.exists():
        return {"report_md": None, "missing": True}
    try:
        return {"report_md": report_path.read_text(encoding="utf-8")}
    except OSError as exc:
        return {"report_md": None, "error": str(exc)}


def _read_pm_decision(run_dir) -> dict | None:
    path = run_dir / "artifacts" / "portfolio_manager" / "decision.portfolio_decision.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _run_transcript(store, run_id: str, seat: str | None):
    """Return the transcript dict or None when the run is unknown."""
    try:
        run = store.load_run(run_id)
    except ValueError:
        return None
    if run is None:
        return None
    recon = store.reconcile_run(run, write=False)
    run_dir = store.run_dir(run_id)

    seats: list[dict] = []
    rounds = 0
    order: list[str] = []
    for task in recon.tasks:
        order.append(task.id)
        rnd = _round_from_task_id(task.id)
        if rnd:
            rounds = max(rounds, rnd)
        if seat is not None and task.agent_id != seat:
            continue
        entry = {
            "agent_id": task.agent_id,
            "task_id": task.id,
            "phase": _SEAT_PHASE.get(task.agent_id, "other"),
            "round": rnd or 1,
            "status": task.status.value if hasattr(task.status, "value") else str(task.status),
        }
        entry.update(_read_seat_report(run_dir, task.agent_id))
        seats.append(entry)

    return {
        "run_id": recon.id,
        "status": recon.status.value,
        "target": (recon.user_vars or {}).get("target"),
        "seats": seats,
        "debate": {"rounds": rounds, "order": order},
        "decision": _read_pm_decision(run_dir),
    }


def _aggregate_performance(window_hours: int | None, symbol: str | None) -> dict:
    """Aggregate resolved journal horizons + paper PnL + run cost. Store-only."""
    from statistics import mean, median
    from datetime import datetime, timedelta, timezone
    from src.committee.journal import load_entries, HORIZONS

    entries = load_entries()
    if symbol:
        entries = [e for e in entries if e.get("symbol") == symbol]
    if window_hours is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
        entries = [e for e in entries if (_parse_iso_utc(e.get("decided_at")) or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff]

    horizons_out: dict[str, dict] = {}
    for key in HORIZONS:
        rows = [e["horizons"][key] for e in entries
                if isinstance(e.get("horizons"), dict) and key in e["horizons"]]
        if not rows:
            horizons_out[key] = {"count": 0}
            continue
        dc = [r["direction_correct"] for r in rows if r.get("direction_correct") is not None]
        nonhold = [
            e["horizons"][key]["direction_correct"]
            for e in entries
            if key in (e.get("horizons") or {}) and e.get("rating") != "Hold"
            and e["horizons"][key].get("direction_correct") is not None
        ]
        alphas = [r["alpha"] for r in rows if r.get("alpha") is not None]
        raws = [r["raw_return"] for r in rows if r.get("raw_return") is not None]
        horizons_out[key] = {
            "count": len(rows),
            "direction_correct_rate": round(sum(dc) / len(dc), 4) if dc else None,
            "direction_correct_rate_non_hold": round(sum(nonhold) / len(nonhold), 4) if nonhold else None,
            "mean_alpha": round(mean(alphas), 6) if alphas else None,
            "median_alpha": round(median(alphas), 6) if alphas else None,
            "mean_raw_return": round(mean(raws), 6) if raws else None,
            "median_raw_return": round(median(raws), 6) if raws else None,
        }

    paper = _paper_pnl_summary(symbol)
    runs = _committee_run_cost_summary(window_hours, symbol)

    distinct = {e.get("symbol") for e in entries}
    out = {
        "window_hours": window_hours,
        "symbol": symbol,
        "decisions_considered": len(entries),
        "horizons": horizons_out,
        "paper": paper,
        "runs": runs,
    }
    if len(distinct) <= 1:
        out["alpha_caveat"] = ALPHA_CAVEAT
    return out


def _paper_pnl_summary(symbol: str | None) -> dict:
    """Realized PnL/fees from the ledger + unrealized from the last equity snap.

    Store-only: no live price fetch (mirrors the read-only invariant)."""
    from src.paper.store import PaperStore, paper_root
    store = PaperStore(paper_root())
    realized, fees = 0.0, 0.0
    for row in store.iter_ledger():
        if symbol and row.get("symbol") != symbol:
            continue
        if row.get("realized_pnl") is not None:
            realized += float(row["realized_pnl"])
        fees += float(row.get("fee_paid") or 0.0)
    snaps = list(store.iter_equity())
    unrealized, equity = None, None
    if snaps:
        last = snaps[-1]
        equity = last.get("equity")
        rows = last.get("positions") or []
        if symbol:
            rows = [p for p in rows if p.get("symbol") == symbol]
        unrealized = sum(float(p.get("unrealized") or 0.0) for p in rows) if rows else 0.0
    return {"realized_pnl": round(realized, 6), "fees_paid": round(fees, 6),
            "unrealized_pnl": unrealized, "equity": equity}


def _committee_run_cost_summary(window_hours: int | None, symbol: str | None) -> dict:
    from datetime import datetime, timedelta, timezone
    store = _get_swarm_store()
    cutoff = None
    if window_hours is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    ins, outs, walls, n = [], [], [], 0
    for run in store.list_runs(limit=500):
        if run.preset_name != COMMITTEE_PRESET:
            continue
        if symbol and (run.user_vars or {}).get("target") != symbol:
            continue
        created = _parse_iso_utc(run.created_at)
        if cutoff is not None and (created is None or created < cutoff):
            continue
        n += 1
        ins.append(run.total_input_tokens)
        outs.append(run.total_output_tokens)
        w = _wall_clock_s(run.created_at, run.completed_at)
        if w is not None:
            walls.append(w)
    avg = lambda xs: round(sum(xs) / len(xs), 1) if xs else None
    return {"count": n, "avg_input_tokens": avg(ins), "avg_output_tokens": avg(outs),
            "avg_wall_clock_s": avg(walls)}
```

Then add the gated registrar near the bottom of the module, immediately before `def main()` (~line 1900):

```python
# ---------------------------------------------------------------------------
# Committee Observatory READ tools (gate 1) — registered only when enabled so
# that with VIBE_MCP_COMMITTEE unset the tool catalogue is byte-identical to
# today. Module-level if-guard around the decorated defs (the run_committee
# trigger, gate 2, is registered by Task M3's addition inside this registrar).
# ---------------------------------------------------------------------------
def _register_committee_read_tools(mcp: "FastMCP") -> None:
    @mcp.tool
    def committee_performance(window_hours: int | None = None, symbol: str | None = None) -> str:
        """Aggregate committee performance over resolved journal horizons.

        Reports per-horizon (24h/72h/7d) resolved counts, direction-correct
        rate (overall and excluding Hold), mean/median alpha and raw return,
        paper realized/unrealized PnL, and average token/wall-clock cost per
        run. Includes the standing alpha caveat for single-symbol universes.

        Args:
            window_hours: Only decisions decided within this trailing window.
            symbol: Restrict to one instrument (e.g. "BTC-USDT").
        """
        try:
            return _json_ok(**_aggregate_performance(window_hours, symbol))
        except Exception as exc:  # store read failure -> clean envelope
            return _json_error(f"performance aggregation failed: {exc}")

    @mcp.tool
    def list_decisions(limit: int = 20, symbol: str | None = None) -> str:
        """List journaled committee decisions, newest first.

        Args:
            limit: Maximum decisions to return.
            symbol: Optional instrument filter.
        """
        from src.committee.journal import load_entries
        entries = load_entries()
        if symbol:
            entries = [e for e in entries if e.get("symbol") == symbol]
        entries = list(reversed(entries))[:max(0, limit)]
        return _json_ok(decisions=[_decision_projection(e, full=False) for e in entries])

    @mcp.tool
    def get_decision(decision_id: str) -> str:
        """Return one journaled decision incl. full horizons + reflection.

        Args:
            decision_id: Journal id, e.g. "dec_ab12cd34ef56".
        """
        from src.committee.journal import load_entries
        for e in load_entries():
            if e.get("id") == decision_id:
                return _json_ok(decision=_decision_projection(e, full=True))
        return _json_error(f"decision {decision_id} not found", error_type="not_found")

    @mcp.tool
    def list_committee_runs(limit: int = 20, status: str | None = None) -> str:
        """List crypto_committee runs joined to their journal entries.

        Args:
            limit: Maximum runs to return (newest first).
            status: Optional run-status filter (running/completed/failed/cancelled).
        """
        store = _get_swarm_store()
        return _json_ok(runs=_committee_runs_joined(store, limit=max(0, limit), status=status))

    @mcp.tool
    def get_run_transcript(run_id: str, seat: str | None = None) -> str:
        """Return a committee run's per-seat reports, debate structure, decision.

        Args:
            run_id: Swarm run id (from list_committee_runs).
            seat: Optional agent_id filter (e.g. "portfolio_manager").
        """
        store = _get_swarm_store()
        transcript = _run_transcript(store, run_id, seat)
        if transcript is None:
            return _json_error(f"run {run_id} not found", error_type="not_found")
        return _json_ok(**transcript)

    @mcp.tool
    def paper_account() -> str:
        """Return the paper broker's marked equity plus a recent ledger tail.

        Store-only: the equity snapshot comes from the last persisted
        mark-to-market row, not a live price fetch.
        """
        from src.paper.store import PaperStore, paper_root
        store = PaperStore(paper_root())
        snaps = list(store.iter_equity())
        equity = snaps[-1] if snaps else {"cash": (store.load_account() or {}).get("cash")}
        ledger_tail = list(store.iter_ledger())[-20:]
        return _json_ok(equity=equity, ledger_tail=ledger_tail)


if _mcp_committee_enabled():
    _register_committee_read_tools(mcp)
```

Register the gate var in `agent/tests/conftest.py` `_LEAK_PRONE_ENV_VARS` tuple:

```python
    "VIBE_MCP_COMMITTEE",
```

- [ ] **Step 4 — run, expect pass** — from `agent/`:

```
source ../.venv/bin/activate && python -m pytest tests/test_mcp_committee_tools.py tests/test_mcp_server_smoke.py -q
```

- [ ] **Step 5 — commit** — from repo root:

```
git add agent/mcp_server.py agent/tests/test_mcp_committee_tools.py agent/tests/conftest.py && git commit -m "feat(mcp): committee READ tool group behind VIBE_MCP_COMMITTEE gate

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task M2: Streamable-HTTP `/mcp` mount inside serve, gated by `VIBE_MCP_COMMITTEE`

**Files:**
- Modify: `agent/api_server.py` (add `_committee_mcp_enabled()` + `_maybe_mount_committee_mcp(app)`; call it from `serve_main` before the SPA mount)
- Create/Test: `agent/tests/test_mcp_committee_mount.py`

**Interfaces:**
- Consumes: `mcp_server.mcp.http_app(path="/mcp", transport="streamable-http")` (fastmcp 3.4.4 → returns a Starlette `StarletteWithLifespan` whose `.router.lifespan_context` must be entered for the streamable-HTTP session manager to run — verified against the installed fastmcp).
- Produces: `_maybe_mount_committee_mcp(app: FastAPI) -> bool` — mounts the FastMCP app at `/mcp` and wires its lifespan into `app`'s startup/shutdown when `VIBE_MCP_COMMITTEE` is truthy; a no-op returning `False` when the gate is off. With the gate off, `serve_main`'s route set is byte-identical to today.

- [ ] **Step 1 — failing test** — write `agent/tests/test_mcp_committee_mount.py`:

```python
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
```

- [ ] **Step 2 — run, expect fail** (`_maybe_mount_committee_mcp` undefined) — from `agent/`:

```
source ../.venv/bin/activate && python -m pytest tests/test_mcp_committee_mount.py -q
```

Expected: `AttributeError: module 'api_server' has no attribute '_maybe_mount_committee_mcp'`.

- [ ] **Step 3 — implementation** — in `agent/api_server.py`, add near the other env helpers (after `_parse_extra_loopback_hosts`, ~line 210):

```python
_MCP_COMMITTEE_ENV = "VIBE_MCP_COMMITTEE"
_MCP_TRUE_VALUES = {"1", "true", "yes", "on"}


def _committee_mcp_enabled() -> bool:
    """Gate 1 (VIBE_MCP_COMMITTEE): mount the committee MCP app at /mcp."""
    return os.getenv(_MCP_COMMITTEE_ENV, "").strip().lower() in _MCP_TRUE_VALUES


def _maybe_mount_committee_mcp(app: FastAPI) -> bool:
    """Mount the FastMCP streamable-HTTP app at /mcp when gate 1 is on.

    No-op (returns False) when VIBE_MCP_COMMITTEE is unset/falsy, so the
    served route set is byte-identical to today. The FastMCP http_app carries
    its own lifespan (the streamable-HTTP session manager); Starlette does NOT
    run a mounted sub-app's lifespan automatically, so we enter/exit it from
    this app's startup/shutdown. Idempotent: a second call is a no-op.
    """
    if not _committee_mcp_enabled():
        return False
    if any(getattr(route, "path", "") == "/mcp" for route in app.routes):
        return True

    import mcp_server  # import-time registration keyed on the same env var

    mcp_app = mcp_server.mcp.http_app(path="/mcp", transport="streamable-http")
    app.mount("/mcp", mcp_app)

    @app.on_event("startup")
    async def _committee_mcp_startup() -> None:
        cm = mcp_app.router.lifespan_context(mcp_app)
        app.state._committee_mcp_cm = cm
        await cm.__aenter__()

    @app.on_event("shutdown")
    async def _committee_mcp_shutdown() -> None:
        cm = getattr(app.state, "_committee_mcp_cm", None)
        if cm is not None:
            await cm.__aexit__(None, None, None)
            app.state._committee_mcp_cm = None

    return True
```

Then in `serve_main`, mount `/mcp` **before** the SPA catch-all mount (the `"/"` mount shadows everything after it). Insert immediately before the `frontend_dist = ...` line (~line 1065):

```python
    # Committee Observatory MCP (spec §3.4): mount /mcp before the SPA
    # catch-all "/" mount below, and only when VIBE_MCP_COMMITTEE is set.
    if _maybe_mount_committee_mcp(app):
        print("[mcp] Committee MCP mounted at /mcp (VIBE_MCP_COMMITTEE=on)")
```

- [ ] **Step 4 — run, expect pass** — from `agent/`:

```
source ../.venv/bin/activate && python -m pytest tests/test_mcp_committee_mount.py -q
```

If the `initialize` handshake assertion is flaky under some fastmcp session-manager configs, keep only `resp.status_code == 200` and drop the body-substring check — the mount + lifespan wiring is what this task verifies; deep protocol coverage stays in `test_mcp_server_smoke.py`.

- [ ] **Step 5 — commit** — from repo root:

```
git add agent/api_server.py agent/tests/test_mcp_committee_mount.py && git commit -m "feat(mcp): mount committee MCP streamable-HTTP app at /mcp under VIBE_MCP_COMMITTEE

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task M3: `run_committee` trigger tool with file-backed daily budget, audit log, grounding validation

**Files:**
- Modify: `agent/mcp_server.py` (audit/budget helpers, grounding-validated dispatch, and the `run_committee` tool registered inside `_register_committee_read_tools` under gate 2)
- Modify: `agent/tests/conftest.py` (register `VIBE_MCP_ALLOW_TRIGGER`, `VIBE_MCP_TRIGGER_BUDGET`, `VIBE_MCP_TRIGGER_AUDIT` in the leak-guard list)
- Create/Test: `agent/tests/test_mcp_run_committee.py`

**Interfaces:**
- Consumes: `src.swarm.grounding.resolve_identity_symbol(raw) -> str|None`, `.fetch_grounding_data([symbol]) -> dict[str,list]`, `.InstrumentResolutionError`; `SwarmRuntime(store, agent_config).start_run(preset, variables, include_shell_tools=...) -> SwarmRun` (returns immediately, background execution) via `src.config.load_swarm_agent_config`; existing `_get_swarm_store()`.
- Produces: `run_committee(symbol: str, note: str|None=None)` (registered only when both gates on) returning `{run_id}` on accept, or a structured refusal (`error_type` in `{"validation","budget_exhausted"}`, budget refusal carries `resets_at`). Every attempt appends one row `{ts, symbol, note, accepted, reason?, run_id?}` to `~/.vibe-trading/committee/mcp_triggers.jsonl` (identical path + row shape to REST R3; `VIBE_MCP_TRIGGER_AUDIT` overrides the path for tests). Budget = rows with `accepted=true` and `ts` in the current UTC day, capped at `VIBE_MCP_TRIGGER_BUDGET` (default 4).

**Design note:** `start_run` catches `InstrumentResolutionError` internally and marks the run failed in the background rather than raising to the caller — so to guarantee "no ungrounded run can be triggered," `run_committee` validates the symbol BEFORE dispatch (shape-resolve, then a real grounding fetch), replicating the same rules the scheduled path applies at run start.

- [ ] **Step 1 — failing test** — write `agent/tests/test_mcp_run_committee.py`:

```python
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
    calls = []
    def _fake_dispatch(symbol, timeframe):
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
```

- [ ] **Step 2 — run, expect fail** — from `agent/`:

```
source ../.venv/bin/activate && python -m pytest tests/test_mcp_run_committee.py -q
```

Expected: `run_committee` absent / helper attributes (`_grounding_resolve`, `_dispatch_committee_run`) undefined.

- [ ] **Step 3 — implementation** — in `agent/mcp_server.py`, add the trigger constants + helpers alongside the M1 committee helpers (after `_committee_run_cost_summary`):

```python
# --- Trigger tool (gate 2): file-backed daily budget + audit -----------------
_MCP_TRIGGER_BUDGET_ENV = "VIBE_MCP_TRIGGER_BUDGET"
_MCP_TRIGGER_AUDIT_ENV = "VIBE_MCP_TRIGGER_AUDIT"  # test/ops override for the audit path
DEFAULT_TRIGGER_BUDGET = 4


def _trigger_budget() -> int:
    raw = os.getenv(_MCP_TRIGGER_BUDGET_ENV, "").strip()
    if not raw:
        return DEFAULT_TRIGGER_BUDGET
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_TRIGGER_BUDGET


def _mcp_triggers_path():
    """Audit-log path. Default is the pinned ~/.vibe-trading/committee location;
    VIBE_MCP_TRIGGER_AUDIT overrides it (hermetic tests / ops), mirroring the
    journal's JOURNAL_PATH_ENV precedent. Keep this identical to the REST
    layer's _mcp_triggers_path (committee_routes.py) — same file, same rows."""
    env = os.getenv(_MCP_TRIGGER_AUDIT_ENV, "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".vibe-trading" / "committee" / "mcp_triggers.jsonl"


def _load_trigger_audit() -> list[dict]:
    p = _mcp_triggers_path()
    if not p.exists():
        return []
    rows: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _append_trigger_audit(row: dict) -> None:
    p = _mcp_triggers_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _triggers_used_today(rows: list[dict], *, now=None) -> int:
    from datetime import datetime, timezone
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(timezone.utc).date()
    used = 0
    for r in rows:
        if not r.get("accepted"):
            continue
        dt = _parse_iso_utc(r.get("ts"))
        if dt is not None and dt.astimezone(timezone.utc).date() == today:
            used += 1
    return used


def _utc_day_reset(now) -> str:
    from datetime import datetime, time, timedelta, timezone
    tomorrow = now.astimezone(timezone.utc).date() + timedelta(days=1)
    return datetime.combine(tomorrow, time.min, tzinfo=timezone.utc).isoformat()


# Thin seams so tests can stub grounding + dispatch without touching the network
# or launching a real 13-seat swarm (spec §3.5 permits a faked dispatch).
def _grounding_resolve(symbol: str):
    from src.swarm import grounding
    return grounding.resolve_identity_symbol(symbol)


def _grounding_fetch(symbol: str) -> dict:
    from src.swarm import grounding
    return grounding.fetch_grounding_data([symbol])


def _dispatch_committee_run(symbol: str, timeframe: str) -> str:
    """Start a crypto_committee run via the same runtime the scheduled job's
    run_swarm path uses (structured variables), returning the run_id
    immediately (execution is backgrounded)."""
    from src.config import load_swarm_agent_config
    from src.swarm.runtime import SwarmRuntime
    store = _get_swarm_store()
    runtime = SwarmRuntime(store=store, agent_config=load_swarm_agent_config())
    run = runtime.start_run(
        COMMITTEE_PRESET,
        {"target": symbol, "timeframe": timeframe},
        include_shell_tools=_include_shell_tools,
    )
    return run.id


def _committee_timeframe() -> str:
    return os.getenv("VIBE_COMMITTEE_TIMEFRAME", "").strip() or "72h swing"
```

Then register `run_committee` inside `_register_committee_read_tools`, at the END of that function (guarded by gate 2 so it appears only when both gates are on):

```python
    if not _mcp_trigger_enabled():
        return

    @mcp.tool
    def run_committee(symbol: str, note: str | None = None) -> str:
        """Trigger a crypto_committee run for ``symbol`` (double-gated).

        Fail-fast: the symbol is validated against the same instrument
        resolution + grounding rules as scheduled runs (no ungrounded run can
        be triggered). Capped at VIBE_MCP_TRIGGER_BUDGET runs per UTC day
        (file-backed audit is the single source of truth — over budget returns
        a structured refusal and never queues). Returns {run_id}; poll
        list_committee_runs / get_run_transcript for completion.

        Args:
            symbol: Instrument to analyze, e.g. "BTC-USDT".
            note: Optional free-text note recorded in the audit log.
        """
        from datetime import datetime, timezone

        def _audit(accepted: bool, *, reason=None, run_id=None):
            _append_trigger_audit({
                "ts": datetime.now(timezone.utc).isoformat(),
                "symbol": symbol, "note": note, "accepted": accepted,
                **({"reason": reason} if reason else {}),
                **({"run_id": run_id} if run_id else {}),
            })

        # 1. Symbol shape resolution (cheap, deterministic).
        resolved = _grounding_resolve(symbol)
        if resolved is None:
            _audit(False, reason="could not resolve a tradable symbol from input")
            return _json_error(
                f"could not resolve a tradable instrument from {symbol!r}",
                error_type="validation")

        # 2. Budget (file-backed; counts accepted rows in the current UTC day).
        now = datetime.now(timezone.utc)
        used = _triggers_used_today(_load_trigger_audit(), now=now)
        budget = _trigger_budget()
        if used >= budget:
            _audit(False, reason="budget_exhausted")
            return json.dumps({
                "status": "error", "error_type": "budget_exhausted",
                "error": f"daily committee-trigger budget ({budget}) exhausted",
                "resets_at": _utc_day_reset(now),
            }, ensure_ascii=False, indent=2)

        # 3. Deep grounding validation (real market data must exist).
        try:
            data = _grounding_fetch(resolved)
        except Exception as exc:
            _audit(False, reason=f"grounding fetch failed: {exc}")
            return _json_error(f"grounding fetch failed for {resolved}: {exc}",
                               error_type="validation")
        if resolved not in data or not data.get(resolved):
            _audit(False, reason="no market data for symbol (ungrounded)")
            return _json_error(
                f"no market data resolved for {resolved}; refusing ungrounded run",
                error_type="validation")

        # 4. Dispatch + record the accepted run.
        try:
            run_id = _dispatch_committee_run(resolved, _committee_timeframe())
        except Exception as exc:
            _audit(False, reason=f"dispatch failed: {exc}")
            return _json_error(f"committee dispatch failed: {exc}")
        _audit(True, run_id=run_id)
        return _json_ok(run_id=run_id, symbol=resolved, note=note)
```

Ordering rationale (documented so reviewers don't "fix" it): shape-resolve → budget → deep grounding fetch → dispatch. Shape-resolve is a cheap deterministic gate; the budget check precedes the network grounding fetch to avoid wasting a fetch when over budget; a validation refusal (accepted=false) never consumes budget since only accepted=true rows are counted.

Register the three new vars in `agent/tests/conftest.py` `_LEAK_PRONE_ENV_VARS`:

```python
    "VIBE_MCP_ALLOW_TRIGGER",
    "VIBE_MCP_TRIGGER_BUDGET",
    "VIBE_MCP_TRIGGER_AUDIT",
```

- [ ] **Step 4 — run, expect pass** — from `agent/`:

```
source ../.venv/bin/activate && python -m pytest tests/test_mcp_run_committee.py tests/test_mcp_committee_tools.py -q
```

- [ ] **Step 5 — commit** — from repo root:

```
git add agent/mcp_server.py agent/tests/test_mcp_run_committee.py agent/tests/conftest.py && git commit -m "feat(mcp): run_committee trigger with file-backed daily budget, audit log, grounding validation

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task F1: API client, committee store, routes + nav + i18n scaffolding

**Files:**
- Modify `frontend/src/lib/api.ts` (new typed functions + interfaces)
- Create `frontend/src/stores/committee.ts` (zustand store)
- Modify `frontend/src/router.tsx` (two lazy routes)
- Modify `frontend/src/components/layout/Layout.tsx` (nav entry)
- Modify `frontend/src/i18n/locales/{en,zh-CN,ja,ko,ar}.json` (`committee` namespace)
- Create `frontend/src/pages/Committee.tsx` and `frontend/src/pages/CommitteeRunDetail.tsx` as **stubs** (real pages land in F2/F3 so routes/lazy imports resolve and `tsc` passes)
- Test `frontend/src/lib/__tests__/committeeApi.test.ts`

**Interfaces:**
- Consumes REST §3.1 shapes exactly (endpoints pinned in Global Constraints).
- Produces `api.getCommitteeRuns/getCommitteeRun/getPaperStatus/getPaperEquity/getPaperPnl/getJournalDecisions/getSchedulerHealth/getMcpStatus` + their TS types, and `useCommitteeStore`.

- [ ] **Add TS interfaces to `api.ts`** (append near the other `// --- ... types ---` blocks). Nested-but-unpinned objects typed loosely rather than invented:

```ts
// --- Committee observatory types (spec §3.1) ---

export interface CommitteePnlSummary {
  realized_pnl?: number | null;
  unrealized_pnl?: number | null;
  executed?: boolean;
  [key: string]: unknown;
}

export interface CommitteeRunItem {
  run_id: string;
  created_at: string;
  status: string;
  target: string;
  wall_clock_s?: number | null;
  input_tokens?: number | null;
  output_tokens?: number | null;
  decision_id?: string | null;
  rating?: string | null;
  journal_status?: string | null;
  pnl_summary?: CommitteePnlSummary | null;
}

export interface CommitteeSeat {
  agent_id: string;
  phase: string;
  round?: number | null;
  status: string;
  /** null + missing:true when artifacts/<agent>/report.md is absent — never fabricated. */
  report_md: string | null;
  decision_json?: Record<string, unknown> | null;
  missing?: boolean;
  error?: string;
}

export interface CommitteeDebate {
  rounds: number;
  order: string[];
}

/** decision.portfolio_decision.json projection; keys optional per §3 A-guide journal shape. */
export interface CommitteeDecision {
  rating?: string | null;
  price_target?: number | null;
  stop_loss?: number | null;
  take_profit?: number | null;
  position_size_pct?: number | null;
  time_horizon?: string | null;
  primary_horizon?: string | null;
  [key: string]: unknown;
}

export interface JournalHorizon {
  raw_return?: number | null;
  benchmark_return?: number | null;
  alpha?: number | null;
  mark_price?: number | null;
  direction_correct?: boolean | null;
  resolved_at?: string | null;
}

export interface CommitteeJournal {
  horizons: Record<string, JournalHorizon>;
  reflection?: string | null;
  reflected_at?: string | null;
}

/** src.paper.pnl.decision_pnl output (A-guide Task 1). */
export interface DecisionPnl {
  decision_id: string;
  executed: boolean;
  realized_pnl: number | null;
  fees_paid?: number | null;
  unrealized_pnl?: number | null;
  position_open?: boolean;
  exit_kind?: string | null;
  max_drawdown_pct?: number | null;
  summary?: string | null;
}

export interface CommitteeRunDetail {
  run: Record<string, unknown>;
  seats: CommitteeSeat[];
  debate: CommitteeDebate;
  decision: CommitteeDecision | null;
  journal: CommitteeJournal | null;
  pnl: DecisionPnl | null;
}

export interface PaperPositionRow {
  symbol: string;
  qty: number;
  avg_entry: number;
  mark: number;
  value: number;
  unrealized: number;
  stale?: boolean;
}

export interface PaperStatus {
  ts?: string | number;
  cash: number;
  positions_value: number;
  equity: number;
  positions: PaperPositionRow[];
  /** Count in broker equity; some snapshots may carry a list — accept both. */
  stale_positions?: number | string[];
}

/** One persisted equity snapshot from GET /paper/equity (no drawdown field — F2 derives it). */
export interface PaperEquityRow {
  ts: string | number;
  cash?: number;
  positions_value?: number;
  equity: number;
  /** Count in broker equity; some snapshots may carry a list — accept both. */
  stale_positions?: number | string[];
}

export interface JournalDecision {
  id: string;
  decided_at: string;
  symbol: string;
  rating?: string | null;
  status?: string | null;
  primary_horizon?: string | null;
  horizons?: Record<string, JournalHorizon>;
  reflected_at?: string | null;
  run_id?: string | null;
}

export interface SchedulerJob {
  id: string;
  schedule?: string | null;
  next_run_at?: string | null;
  status?: string | null;
  last_state?: string | null;
  [key: string]: unknown;
}

export interface SchedulerSupervisor {
  alive: boolean;
  last_tick?: number | string | null;
  last_tick_age_seconds?: number | null;
}

export interface SchedulerHealth {
  jobs: SchedulerJob[];
  supervisor: SchedulerSupervisor | null;
}

export interface McpStatus {
  committee_tools_enabled: boolean;
  trigger_enabled: boolean;
  trigger_budget: number;
  triggers_used_today: number;
  http_mount: string | null;
  stdio_command: string;
}

export interface CommitteeRunsParams {
  limit?: number;
  status?: string;
  symbol?: string;
}

export interface JournalDecisionsParams {
  limit?: number;
  symbol?: string;
}
```

- [ ] **Add the fetchers to the `api` object** (insert a `// Committee observatory API` block before the closing `};` of `export const api = {`), reusing the private `request<T>` idiom exactly as existing entries do:

```ts
  // Committee observatory API (read-only, spec §3.1)
  getCommitteeRuns: (params: CommitteeRunsParams = {}) => {
    const q = new URLSearchParams();
    if (params.limit !== undefined) q.set("limit", String(params.limit));
    if (params.status) q.set("status", params.status);
    if (params.symbol) q.set("symbol", params.symbol);
    const qs = q.toString();
    return request<CommitteeRunItem[]>(`/committee/runs${qs ? `?${qs}` : ""}`);
  },
  getCommitteeRun: (runId: string) =>
    request<CommitteeRunDetail>(`/committee/runs/${encodeURIComponent(runId)}`),
  getPaperStatus: () => request<PaperStatus>("/paper/status"),
  getPaperEquity: () => request<PaperEquityRow[]>("/paper/equity"),
  getPaperPnl: (decisionId: string) =>
    request<DecisionPnl>(`/paper/pnl/${encodeURIComponent(decisionId)}`),
  getJournalDecisions: (params: JournalDecisionsParams = {}) => {
    const q = new URLSearchParams();
    if (params.limit !== undefined) q.set("limit", String(params.limit));
    if (params.symbol) q.set("symbol", params.symbol);
    const qs = q.toString();
    return request<JournalDecision[]>(`/journal/decisions${qs ? `?${qs}` : ""}`);
  },
  getSchedulerHealth: () => request<SchedulerHealth>("/scheduler/health"),
  getMcpStatus: () => request<McpStatus>("/mcp/status"),
```

- [ ] **Write the failing test first** `frontend/src/lib/__tests__/committeeApi.test.ts` (vitest, mock `fetch`, assert URL construction) — mirror the `vi.hoisted`/`vi.fn` conventions from `pages/__tests__/Runtime.test.tsx`:

```ts
import { api } from "@/lib/api";

describe("committee api client", () => {
  const fetchMock = vi.fn();
  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    fetchMock.mockResolvedValue(
      new Response("[]", { status: 200, headers: { "content-type": "application/json" } }),
    );
  });
  afterEach(() => vi.unstubAllGlobals());

  it("builds the committee runs query with limit+status+symbol", async () => {
    await api.getCommitteeRuns({ limit: 20, status: "completed", symbol: "BTC-USDT" });
    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toBe("/committee/runs?limit=20&status=completed&symbol=BTC-USDT");
  });

  it("percent-encodes the run id in the detail path", async () => {
    await api.getCommitteeRun("run/abc 1");
    expect(fetchMock.mock.calls[0][0]).toBe("/committee/runs/run%2Fabc%201");
  });

  it("hits the fixed paper/scheduler/mcp paths", async () => {
    await api.getPaperStatus();
    await api.getSchedulerHealth();
    await api.getMcpStatus();
    const urls = fetchMock.mock.calls.map((c) => c[0]);
    expect(urls).toEqual(["/paper/status", "/scheduler/health", "/mcp/status"]);
  });
});
```

Run: `npm --prefix frontend run test:run -- committeeApi` → RED until the two edits above are in, then GREEN.

- [ ] **Create the committee store** `frontend/src/stores/committee.ts` (zustand `create` idiom identical to `stores/agent.ts`; holds fetched data + async loaders that delegate to `api`, so both pages share one data-access surface per guide §2.5):

```ts
import { create } from "zustand";
import {
  api,
  type CommitteeRunItem,
  type CommitteeRunDetail,
  type PaperStatus,
  type PaperEquityRow,
  type SchedulerHealth,
  type McpStatus,
  type JournalDecision,
  type CommitteeRunsParams,
} from "@/lib/api";

interface CommitteeState {
  runs: CommitteeRunItem[];
  paperStatus: PaperStatus | null;
  paperEquity: PaperEquityRow[];
  schedulerHealth: SchedulerHealth | null;
  mcpStatus: McpStatus | null;
  journalDecisions: JournalDecision[];
  runDetail: CommitteeRunDetail | null;
  error: string | null;

  loadDashboard: (params?: CommitteeRunsParams) => Promise<void>;
  loadRuns: (params?: CommitteeRunsParams) => Promise<void>;
  loadRunDetail: (runId: string) => Promise<CommitteeRunDetail | null>;
  reset: () => void;
}

export const useCommitteeStore = create<CommitteeState>((set) => ({
  runs: [],
  paperStatus: null,
  paperEquity: [],
  schedulerHealth: null,
  mcpStatus: null,
  journalDecisions: [],
  runDetail: null,
  error: null,

  loadDashboard: async (params) => {
    try {
      const [runs, paperStatus, paperEquity, schedulerHealth, mcpStatus] = await Promise.all([
        api.getCommitteeRuns(params ?? { limit: 50 }),
        api.getPaperStatus().catch(() => null),
        api.getPaperEquity().catch(() => []),
        api.getSchedulerHealth().catch(() => null),
        api.getMcpStatus().catch(() => null),
      ]);
      set({ runs, paperStatus, paperEquity, schedulerHealth, mcpStatus, error: null });
    } catch (err) {
      set({ error: err instanceof Error ? err.message : "load failed" });
    }
  },

  loadRuns: async (params) => {
    try {
      set({ runs: await api.getCommitteeRuns(params ?? { limit: 50 }), error: null });
    } catch (err) {
      set({ error: err instanceof Error ? err.message : "load failed" });
    }
  },

  loadRunDetail: async (runId) => {
    try {
      const runDetail = await api.getCommitteeRun(runId);
      set({ runDetail, error: null });
      return runDetail;
    } catch (err) {
      set({ error: err instanceof Error ? err.message : "load failed", runDetail: null });
      return null;
    }
  },

  reset: () =>
    set({
      runs: [], paperStatus: null, paperEquity: [], schedulerHealth: null,
      mcpStatus: null, journalDecisions: [], runDetail: null, error: null,
    }),
}));
```

- [ ] **Create stub pages** so the router type-checks (real bodies land F2/F3):

```tsx
// frontend/src/pages/Committee.tsx
export function Committee() {
  return null;
}
```

```tsx
// frontend/src/pages/CommitteeRunDetail.tsx
export function CommitteeRunDetail() {
  return null;
}
```

- [ ] **Register lazy routes** in `frontend/src/router.tsx`. Add after the `AlphaZoo` lazy const (line 25-27):

```tsx
const Committee = lazy(() =>
  import("@/pages/Committee").then((m) => ({ default: m.Committee })),
);
const CommitteeRunDetail = lazy(() =>
  import("@/pages/CommitteeRunDetail").then((m) => ({ default: m.CommitteeRunDetail })),
);
```

Add inside the `children` array (after the `/runs/:runId` entry, line 54):

```tsx
      { path: "/committee", element: wrap(Committee) },
      { path: "/committee/runs/:runId", element: wrap(CommitteeRunDetail) },
```

- [ ] **Add the nav entry** in `frontend/src/components/layout/Layout.tsx`. Add `Users` to the lucide import on line 4, then insert into the `NAV` array (after the `/runtime` entry, line 21):

```tsx
  { to: "/committee", icon: Users, label: t('layout.committee') },
```

(The existing `pathname.startsWith(to)` active-check already handles `/committee/runs/...` highlighting.)

- [ ] **Add the `committee` i18n namespace to all five locales.** Insert this block into `frontend/src/i18n/locales/en.json` (top level, alongside `runtime`), and add `"committee": "Committee"` under the existing `layout` object:

```json
"committee": {
  "title": "Committee Observatory",
  "subtitle": "Read-only view of AI committee runs, paper account, and system health.",
  "refresh": "Refresh",
  "loadError": "Could not load committee data",
  "loadErrorHint": "Check the API key in Settings if accessing remotely, or run the backend on localhost.",
  "paperAccount": "Paper Account",
  "equity": "Equity",
  "cash": "Cash",
  "positionsValue": "Positions Value",
  "equityCurve": "Equity Curve",
  "noEquity": "No equity snapshots yet",
  "stale": "stale",
  "scheduler": "Scheduler Health",
  "supervisor": "Supervisor",
  "alive": "alive",
  "stopped": "stopped",
  "lastFired": "Last state",
  "noJobs": "No scheduled jobs registered",
  "mcp": "MCP Interface",
  "mcpEnabled": "Read tools enabled",
  "mcpDisabled": "Disabled",
  "mcpTrigger": "Trigger",
  "mcpBudget": "Budget",
  "mcpUsedToday": "used today",
  "mcpStdio": "stdio command",
  "mcpHttp": "HTTP mount",
  "mcpConnectHint": "External agents connect via the stdio command or HTTP mount above.",
  "runs": "Committee Runs",
  "noRuns": "No committee runs in the current window",
  "colTime": "Time",
  "colSymbol": "Symbol",
  "colRating": "Rating",
  "colStatus": "Status",
  "colWallClock": "Wall-clock",
  "colTokens": "Tokens",
  "colPnl": "PnL",
  "viewPnl": "View PnL",
  "backToCommittee": "Back to Committee",
  "runNotFound": "Committee run not found",
  "discussion": "Discussion",
  "phaseAnalysts": "Analysts",
  "phaseDebate": "Bull / Bear Debate",
  "phaseResearchManager": "Research Manager",
  "phaseTrader": "Trader",
  "phaseRisk": "Risk Rotation",
  "phasePortfolioManager": "Portfolio Manager",
  "phaseOther": "Other Seats",
  "round": "Round {{n}}",
  "reportUnavailable": "Report not available",
  "reportUnavailableHint": "This seat produced no report.md artifact for this run.",
  "seatError": "Seat could not be read",
  "expand": "Expand",
  "collapse": "Collapse",
  "decision": "Decision",
  "priceTarget": "Price target",
  "stopLoss": "Stop loss",
  "takeProfit": "Take profit",
  "positionSize": "Position size",
  "timeHorizon": "Time horizon",
  "noDecision": "No portfolio decision recorded for this run",
  "journal": "Journal Outcome",
  "reflection": "Reflection",
  "notReflected": "Not yet reflected",
  "horizon": "Horizon",
  "rawReturn": "Raw return",
  "alpha": "Alpha",
  "directionCorrect": "Direction",
  "pending": "pending",
  "correct": "correct",
  "incorrect": "incorrect",
  "noJournal": "No journal entry for this decision yet",
  "pnl": "Decision PnL",
  "realizedPnl": "Realized",
  "unrealizedPnl": "Unrealized",
  "feesPaid": "Fees",
  "notExecuted": "Not executed",
  "noPnl": "No PnL available for this decision",
  "liveFollowing": "Live-following run",
  "liveFallbackPolling": "Live stream unavailable — polling"
}
```

Then add the **same keys with translated values** to `zh-CN.json`, `ja.json`, `ko.json`, and `ar.json` (same structure; translate strings; keep `{{n}}` interpolation tokens intact; Arabic values are plain strings — RTL is handled by the `dir` attribute set in `i18n/index.ts`, no layout tokens needed). Add the matching `layout.committee` value in each ("委员会" / "委員会" / "위원회" / "اللجنة").

- [ ] **Verify wiring:** `npm --prefix frontend run test:run -- committeeApi` (GREEN) and `npm --prefix frontend run build` (tsc + vite pass — proves stub pages, routes, nav import, and every locale JSON parse cleanly). Also `node -e "for (const l of ['en','zh-CN','ja','ko','ar']) JSON.parse(require('fs').readFileSync('frontend/src/i18n/locales/'+l+'.json'))"` → no output = all five locales valid JSON with the added block.

- [ ] **Commit:**

```bash
git add -A frontend/src/lib/api.ts frontend/src/stores/committee.ts frontend/src/router.tsx \
  frontend/src/components/layout/Layout.tsx frontend/src/pages/Committee.tsx \
  frontend/src/pages/CommitteeRunDetail.tsx frontend/src/i18n/locales \
  frontend/src/lib/__tests__/committeeApi.test.ts
git commit -m "feat(committee-ui): api client, store, routes, nav, i18n scaffolding

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task F2: `/committee` dashboard page

**Files:**
- Modify `frontend/src/pages/Committee.tsx` (real page, replacing the F1 stub)
- Create `frontend/src/pages/committee/RunsTable.tsx` (extracted subcomponent — keeps the page under ~250 lines)
- Test `frontend/src/pages/__tests__/Committee.test.tsx`

**Interfaces:**
- Consumes `useCommitteeStore` (`runs`, `paperStatus`, `paperEquity`, `schedulerHealth`, `mcpStatus`) and `api` types from F1; reuses `EquityChart` (`EquityPoint[]`).
- Produces the operator dashboard; row click navigates to `/committee/runs/:runId`.

- [ ] **Write the failing component test first** `frontend/src/pages/__tests__/Committee.test.tsx` — mirror `Runtime.test.tsx` (`vi.hoisted` + `vi.mock("@/lib/api")`), wrap in `MemoryRouter` because the runs table renders `Link`s/navigates, and mock `EquityChart` (echarts needs no DOM in jsdom tests):

```tsx
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { Committee } from "../Committee";

const apiMock = vi.hoisted(() => ({
  getCommitteeRuns: vi.fn(),
  getPaperStatus: vi.fn(),
  getPaperEquity: vi.fn(),
  getSchedulerHealth: vi.fn(),
  getMcpStatus: vi.fn(),
}));
vi.mock("@/lib/api", () => ({ api: apiMock }));
vi.mock("@/components/charts/EquityChart", () => ({ EquityChart: () => <div data-testid="equity-chart" /> }));

describe("Committee dashboard", () => {
  beforeEach(() => {
    Object.values(apiMock).forEach((m) => m.mockReset());
    apiMock.getPaperStatus.mockResolvedValue({ cash: 9000, positions_value: 1000, equity: 10000, positions: [], stale_positions: [] });
    apiMock.getPaperEquity.mockResolvedValue([{ ts: "2026-07-18T00:00:00Z", equity: 10000 }]);
    apiMock.getSchedulerHealth.mockResolvedValue({ jobs: [{ id: "committee-run", schedule: "0 */8 * * *", last_state: "ok" }], supervisor: { alive: true, last_tick_age_seconds: 30 } });
    apiMock.getMcpStatus.mockResolvedValue({ committee_tools_enabled: true, trigger_enabled: false, trigger_budget: 4, triggers_used_today: 0, http_mount: "/mcp", stdio_command: "vibe-trading-mcp" });
  });

  it("renders paper account, scheduler, mcp cards and the runs table", async () => {
    apiMock.getCommitteeRuns.mockResolvedValue([
      { run_id: "r1", created_at: "2026-07-18T10:00:00Z", status: "completed", target: "BTC-USDT", wall_clock_s: 42, input_tokens: 1000, output_tokens: 500, decision_id: "d1", rating: "Buy", journal_status: "pending", pnl_summary: { realized_pnl: 12.5 } },
    ]);
    render(<MemoryRouter><Committee /></MemoryRouter>);

    expect(await screen.findByText("BTC-USDT")).toBeInTheDocument();
    expect(screen.getByText("Buy")).toBeInTheDocument();
    expect(screen.getByTestId("equity-chart")).toBeInTheDocument();
    expect(screen.getByText("committee-run")).toBeInTheDocument();
    expect(screen.getByText("vibe-trading-mcp")).toBeInTheDocument();
  });

  it("shows an empty state when there are no runs", async () => {
    apiMock.getCommitteeRuns.mockResolvedValue([]);
    render(<MemoryRouter><Committee /></MemoryRouter>);
    expect(await screen.findByText("No committee runs in the current window")).toBeInTheDocument();
  });
});
```

Run: `npm --prefix frontend run test:run -- Committee` → RED.

- [ ] **Implement `RunsTable.tsx`** (extracted; navigable rows, rating badge, formatted tokens/wall-clock, PnL link):

```tsx
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { cn } from "@/lib/utils";
import type { CommitteeRunItem } from "@/lib/api";

function fmtTime(iso: string): string {
  const d = new Date(iso);
  return Number.isFinite(d.getTime()) ? d.toLocaleString() : iso;
}
function fmtPnl(item: CommitteeRunItem): string {
  const v = item.pnl_summary?.realized_pnl;
  if (typeof v !== "number") return "-";
  return `${v >= 0 ? "+" : ""}${v.toFixed(2)}`;
}
function ratingTone(rating?: string | null): string {
  const r = (rating || "").toLowerCase();
  if (r.includes("buy") || r.includes("long")) return "bg-success/10 text-success";
  if (r.includes("sell") || r.includes("short")) return "bg-danger/10 text-danger";
  return "bg-muted text-muted-foreground";
}

export function RunsTable({ runs }: { runs: CommitteeRunItem[] }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  if (runs.length === 0) {
    return <p className="rounded-md border border-dashed p-8 text-center text-sm text-muted-foreground">{t("committee.noRuns")}</p>;
  }
  return (
    <div className="overflow-x-auto rounded-md border">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b text-start text-muted-foreground">
            <th className="p-2 text-start">{t("committee.colTime")}</th>
            <th className="p-2 text-start">{t("committee.colSymbol")}</th>
            <th className="p-2 text-start">{t("committee.colRating")}</th>
            <th className="p-2 text-start">{t("committee.colStatus")}</th>
            <th className="p-2 text-end">{t("committee.colWallClock")}</th>
            <th className="p-2 text-end">{t("committee.colTokens")}</th>
            <th className="p-2 text-end">{t("committee.colPnl")}</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => (
            <tr
              key={run.run_id}
              onClick={() => navigate(`/committee/runs/${encodeURIComponent(run.run_id)}`)}
              className="cursor-pointer border-b last:border-0 hover:bg-muted/30"
            >
              <td className="p-2 font-mono text-xs">{fmtTime(run.created_at)}</td>
              <td className="p-2">{run.target}</td>
              <td className="p-2">
                {run.rating ? <span className={cn("rounded px-2 py-0.5 text-xs font-medium", ratingTone(run.rating))}>{run.rating}</span> : "-"}
              </td>
              <td className="p-2 text-muted-foreground">{run.status}</td>
              <td className="p-2 text-end tabular-nums">{typeof run.wall_clock_s === "number" ? `${run.wall_clock_s.toFixed(1)}s` : "-"}</td>
              <td className="p-2 text-end tabular-nums text-muted-foreground">
                {(run.input_tokens ?? 0) + (run.output_tokens ?? 0) || "-"}
              </td>
              <td className="p-2 text-end tabular-nums">
                {run.decision_id ? <span className="text-primary hover:underline">{fmtPnl(run)}</span> : fmtPnl(run)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
```

- [ ] **Implement `Committee.tsx`** (polling via `useEffect` interval calling the store's `loadDashboard`, 45s per spec §3.2; equity rows mapped to `EquityPoint[]` with a **derived** running-drawdown since `/paper/equity` carries no drawdown field — never fabricated, computed from the running peak):

```tsx
import { useEffect, useMemo } from "react";
import { useTranslation } from "react-i18next";
import { Activity, ServerCog, Wallet, Plug } from "lucide-react";
import { useCommitteeStore } from "@/stores/committee";
import { EquityChart } from "@/components/charts/EquityChart";
import type { EquityPoint, PaperEquityRow } from "@/lib/api";
import { RunsTable } from "@/pages/committee/RunsTable";

const COMMITTEE_POLL_MS = 45_000;

function toEquityPoints(rows: PaperEquityRow[]): EquityPoint[] {
  let peak = -Infinity;
  return rows.map((row) => {
    const equity = Number(row.equity);
    peak = Math.max(peak, equity);
    const drawdown = peak > 0 ? equity / peak - 1 : 0;
    return { time: String(row.ts), equity, drawdown };
  });
}

export function Committee() {
  const { t } = useTranslation();
  const { runs, paperStatus, paperEquity, schedulerHealth, mcpStatus, error, loadDashboard } =
    useCommitteeStore();

  useEffect(() => {
    loadDashboard({ limit: 50 });
    const id = window.setInterval(() => loadDashboard({ limit: 50 }), COMMITTEE_POLL_MS);
    return () => window.clearInterval(id);
  }, [loadDashboard]);

  const equityPoints = useMemo(() => toEquityPoints(paperEquity), [paperEquity]);

  return (
    <div className="min-h-screen p-6 lg:p-8">
      <div className="mx-auto flex w-full max-w-6xl flex-col gap-6">
        <header className="border-b pb-4">
          <h1 className="text-3xl font-bold tracking-tight">{t("committee.title")}</h1>
          <p className="mt-2 text-sm text-muted-foreground">{t("committee.subtitle")}</p>
        </header>

        {error ? (
          <section className="rounded-md border border-amber-500/30 bg-amber-500/5 p-4">
            <p className="text-sm font-medium text-amber-700 dark:text-amber-300">{t("committee.loadError")}</p>
            <p className="mt-1 text-xs text-muted-foreground">{error}</p>
            <p className="mt-1 text-xs text-muted-foreground">{t("committee.loadErrorHint")}</p>
          </section>
        ) : null}

        <div className="grid gap-4 lg:grid-cols-3">
          <section className="rounded-md border p-4">
            <div className="mb-3 flex items-center gap-2 text-sm font-medium"><Wallet className="h-4 w-4 text-muted-foreground" />{t("committee.paperAccount")}</div>
            {paperStatus ? (
              <dl className="space-y-1 text-sm">
                <div className="flex justify-between"><dt className="text-muted-foreground">{t("committee.equity")}</dt><dd className="tabular-nums font-medium">{paperStatus.equity.toLocaleString()}</dd></div>
                <div className="flex justify-between"><dt className="text-muted-foreground">{t("committee.cash")}</dt><dd className="tabular-nums">{paperStatus.cash.toLocaleString()}</dd></div>
                <div className="flex justify-between"><dt className="text-muted-foreground">{t("committee.positionsValue")}</dt><dd className="tabular-nums">{paperStatus.positions_value.toLocaleString()}</dd></div>
              </dl>
            ) : <p className="text-sm text-muted-foreground">-</p>}
          </section>

          <section className="rounded-md border p-4">
            <div className="mb-3 flex items-center gap-2 text-sm font-medium"><ServerCog className="h-4 w-4 text-muted-foreground" />{t("committee.scheduler")}</div>
            {schedulerHealth && schedulerHealth.jobs.length > 0 ? (
              <ul className="space-y-1.5 text-sm">
                {schedulerHealth.jobs.map((job) => (
                  <li key={job.id} className="flex items-center justify-between gap-2">
                    <span className="font-mono text-xs">{job.id}</span>
                    <span className="text-xs text-muted-foreground">{job.last_state || job.status || "-"}</span>
                  </li>
                ))}
              </ul>
            ) : <p className="text-sm text-muted-foreground">{t("committee.noJobs")}</p>}
            {schedulerHealth?.supervisor ? (
              <p className="mt-3 border-t pt-2 text-xs text-muted-foreground">
                {t("committee.supervisor")}: {schedulerHealth.supervisor.alive ? t("committee.alive") : t("committee.stopped")}
              </p>
            ) : null}
          </section>

          <section className="rounded-md border p-4">
            <div className="mb-3 flex items-center gap-2 text-sm font-medium"><Plug className="h-4 w-4 text-muted-foreground" />{t("committee.mcp")}</div>
            {mcpStatus ? (
              <dl className="space-y-1 text-sm">
                <div className="flex justify-between"><dt className="text-muted-foreground">{t("committee.mcpEnabled")}</dt><dd>{mcpStatus.committee_tools_enabled ? "✓" : t("committee.mcpDisabled")}</dd></div>
                <div className="flex justify-between"><dt className="text-muted-foreground">{t("committee.mcpTrigger")}</dt><dd>{mcpStatus.trigger_enabled ? `${mcpStatus.triggers_used_today}/${mcpStatus.trigger_budget}` : t("committee.mcpDisabled")}</dd></div>
                <div className="flex justify-between"><dt className="text-muted-foreground">{t("committee.mcpStdio")}</dt><dd className="font-mono text-xs">{mcpStatus.stdio_command}</dd></div>
                {mcpStatus.http_mount ? <div className="flex justify-between"><dt className="text-muted-foreground">{t("committee.mcpHttp")}</dt><dd className="font-mono text-xs">{mcpStatus.http_mount}</dd></div> : null}
                {mcpStatus.committee_tools_enabled ? <p className="pt-2 text-xs text-muted-foreground">{t("committee.mcpConnectHint")}</p> : null}
              </dl>
            ) : <p className="text-sm text-muted-foreground">-</p>}
          </section>
        </div>

        <section className="rounded-md border p-4">
          <div className="mb-3 flex items-center gap-2 text-sm font-medium"><Activity className="h-4 w-4 text-muted-foreground" />{t("committee.equityCurve")}</div>
          {equityPoints.length > 0 ? <EquityChart data={equityPoints} height={260} /> : <p className="text-sm text-muted-foreground">{t("committee.noEquity")}</p>}
        </section>

        <section>
          <h2 className="mb-3 text-lg font-semibold">{t("committee.runs")}</h2>
          <RunsTable runs={runs} />
        </section>
      </div>
    </div>
  );
}
```

- [ ] **Verify:** `npm --prefix frontend run test:run -- Committee` (GREEN) and `npm --prefix frontend run build` (tsc + vite pass).

- [ ] **Commit:**

```bash
git add -A frontend/src/pages/Committee.tsx frontend/src/pages/committee/RunsTable.tsx frontend/src/pages/__tests__/Committee.test.tsx
git commit -m "feat(committee-ui): /committee dashboard (paper, scheduler, mcp, equity, runs table)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task F3: `/committee/runs/:runId` discussion view with live-follow

**Files:**
- Modify `frontend/src/pages/CommitteeRunDetail.tsx` (real page, replacing the F1 stub)
- Create `frontend/src/pages/committee/SeatSection.tsx` (extracted expandable seat)
- Test `frontend/src/pages/__tests__/CommitteeRunDetail.test.tsx`

**Interfaces:**
- Consumes `useCommitteeStore.loadRunDetail`, `CommitteeRunDetail`/`CommitteeSeat`/`CommitteeJournal`/`DecisionPnl`/`CommitteeDecision` types (F1), the existing `useSSE` hook, and `api.swarmSseUrl`.
- Live-follow contract (reused, not new): subscribe to the swarm stream via `useSSE`; treat `swarm.event` completion-type events as a "refetch trigger" and re-`loadRunDetail` for authoritative seat `report_md` (SSE payloads carry no artifact markdown — honoring "never fabricate"). Poll every 30s as fallback while `status === "running"`. No new SSE emitters (guide §2.4 / spec §3.2). Event names/shape match `Agent.tsx`'s consumption (`swarm.event` → `d.event.type`).

- [ ] **Write the failing test first** `frontend/src/pages/__tests__/CommitteeRunDetail.test.tsx` — `MemoryRouter` with `initialEntries` for the `:runId` param, mock `@/lib/api`, and mock `@/hooks/useSSE` so no real EventSource opens:

```tsx
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { CommitteeRunDetail } from "../CommitteeRunDetail";
import type { CommitteeRunDetail as Detail } from "@/lib/api";

const apiMock = vi.hoisted(() => ({ getCommitteeRun: vi.fn() }));
vi.mock("@/lib/api", () => ({ api: apiMock }));
vi.mock("@/hooks/useSSE", () => ({
  useSSE: () => ({ connect: vi.fn(), disconnect: vi.fn(), getStatus: () => "disconnected", onStatusChange: vi.fn() }),
}));

function renderAt(runId: string) {
  return render(
    <MemoryRouter initialEntries={[`/committee/runs/${runId}`]}>
      <Routes><Route path="/committee/runs/:runId" element={<CommitteeRunDetail />} /></Routes>
    </MemoryRouter>,
  );
}
function makeDetail(over: Partial<Detail> = {}): Detail {
  return {
    run: { run_id: "r1", status: "completed" },
    seats: [
      { agent_id: "market_analyst", phase: "analysts", round: null, status: "done", report_md: "# Market view\nBullish." },
      { agent_id: "bull_researcher", phase: "debate", round: 1, status: "done", report_md: "Bull case." },
      { agent_id: "risk_manager", phase: "risk", round: null, status: "done", report_md: null, missing: true },
    ],
    debate: { rounds: 1, order: ["bull-r1", "bear-r1"] },
    decision: { rating: "Buy", price_target: 70000, position_size_pct: 5 },
    journal: { horizons: { "24h": { raw_return: 0.01, alpha: 0.0, direction_correct: true, resolved_at: "2026-07-18T00:00:00Z" }, "7d": {} }, reflection: "Held as planned.", reflected_at: "2026-07-18T12:00:00Z" },
    pnl: { decision_id: "d1", executed: true, realized_pnl: 12.5, unrealized_pnl: 3.0, fees_paid: 0.4 },
    ...over,
  };
}

describe("CommitteeRunDetail", () => {
  beforeEach(() => apiMock.getCommitteeRun.mockReset());

  it("renders seats, rendered markdown, decision, journal and pnl", async () => {
    apiMock.getCommitteeRun.mockResolvedValue(makeDetail());
    renderAt("r1");
    expect(await screen.findByText("Buy")).toBeInTheDocument();
    expect(screen.getByText("market_analyst")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Market view" })).toBeInTheDocument(); // markdown rendered
    expect(screen.getByText("Held as planned.")).toBeInTheDocument();
  });

  it("shows an explicit not-available state for a missing report, never blank", async () => {
    apiMock.getCommitteeRun.mockResolvedValue(makeDetail());
    renderAt("r1");
    expect(await screen.findByText("Report not available")).toBeInTheDocument();
  });

  it("renders a not-found state when the run is absent", async () => {
    apiMock.getCommitteeRun.mockResolvedValue(null);
    renderAt("missing");
    expect(await screen.findByText("Committee run not found")).toBeInTheDocument();
  });
});
```

(Seats default-expanded so markdown asserts render — see impl.) Run: `npm --prefix frontend run test:run -- CommitteeRunDetail` → RED.

- [ ] **Implement `SeatSection.tsx`** (expandable; rendered markdown via the same `react-markdown` + `remark-gfm` + `rehype-highlight` + `prose` pattern as `components/chat/MessageBubble.tsx`; explicit missing/error states — never blank):

```tsx
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { ChevronDown, ChevronRight } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import { cn } from "@/lib/utils";
import type { CommitteeSeat } from "@/lib/api";

const remarkPlugins = [remarkGfm];
const rehypePlugins = [rehypeHighlight];

export function SeatSection({ seat, defaultOpen = true }: { seat: CommitteeSeat; defaultOpen?: boolean }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(defaultOpen);
  return (
    <article className="rounded-md border">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 p-3 text-start hover:bg-muted/30"
      >
        {open ? <ChevronDown className="h-4 w-4 shrink-0" /> : <ChevronRight className="h-4 w-4 shrink-0" />}
        <span className="font-mono text-sm font-medium">{seat.agent_id}</span>
        {seat.round ? <span className="rounded bg-muted px-1.5 py-0.5 text-xs text-muted-foreground">{t("committee.round", { n: seat.round })}</span> : null}
        <span className={cn("ms-auto text-xs", seat.status === "done" ? "text-success" : "text-muted-foreground")}>{seat.status}</span>
      </button>
      {open ? (
        <div className="border-t p-3">
          {seat.error ? (
            <p className="text-sm text-danger">{t("committee.seatError")}: {seat.error}</p>
          ) : seat.report_md ? (
            <div className="prose prose-sm dark:prose-invert max-w-none leading-relaxed prose-hr:hidden">
              <ReactMarkdown remarkPlugins={remarkPlugins} rehypePlugins={rehypePlugins}>{seat.report_md}</ReactMarkdown>
            </div>
          ) : (
            <div className="rounded-md border border-dashed p-4 text-center">
              <p className="text-sm font-medium text-muted-foreground">{t("committee.reportUnavailable")}</p>
              <p className="mt-1 text-xs text-muted-foreground">{t("committee.reportUnavailableHint")}</p>
            </div>
          )}
        </div>
      ) : null}
    </article>
  );
}
```

- [ ] **Implement `CommitteeRunDetail.tsx`** (pipeline-ordered phase grouping; debate grouped by round; decision/journal/pnl cards; live-follow reusing `useSSE` + `api.swarmSseUrl`, with a 30s polling fallback while running):

```tsx
import { useEffect, useMemo, useRef, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { ArrowLeft, Radio } from "lucide-react";
import { useCommitteeStore } from "@/stores/committee";
import { useSSE } from "@/hooks/useSSE";
import { api, type CommitteeSeat, type CommitteeJournal, type DecisionPnl, type CommitteeDecision } from "@/lib/api";
import { SeatSection } from "@/pages/committee/SeatSection";

const PHASE_ORDER = ["analysts", "debate", "research_manager", "trader", "risk", "portfolio_manager"] as const;
const PHASE_LABEL_KEY: Record<string, string> = {
  analysts: "committee.phaseAnalysts",
  debate: "committee.phaseDebate",
  research_manager: "committee.phaseResearchManager",
  trader: "committee.phaseTrader",
  risk: "committee.phaseRisk",
  portfolio_manager: "committee.phasePortfolioManager",
};
const RUNNING_POLL_MS = 30_000;
// Swarm event types that mean seat artifacts may have changed → refetch authoritative detail.
const REFETCH_EVENTS = new Set(["task_completed", "worker_completed", "run_completed", "run_error", "task_failed"]);

export function CommitteeRunDetail() {
  const { runId } = useParams<{ runId: string }>();
  const navigate = useNavigate();
  const { t } = useTranslation();
  const { runDetail, error, loadRunDetail } = useCommitteeStore();
  const [loading, setLoading] = useState(true);
  const [following, setFollowing] = useState(false);
  const { connect, disconnect } = useSSE();

  const status = String((runDetail?.run as { status?: string } | undefined)?.status ?? "");
  const isRunning = status === "running";

  // Initial + param-change load.
  useEffect(() => {
    if (!runId) return;
    setLoading(true);
    loadRunDetail(runId).finally(() => setLoading(false));
  }, [runId, loadRunDetail]);

  // Live-follow: reuse the swarm SSE stream (no new emitters). On completion-type
  // events, refetch the detail endpoint (SSE payloads carry no report.md). Poll as fallback.
  useEffect(() => {
    if (!runId || !isRunning) return;
    let active = true;
    const refetch = () => { if (active) loadRunDetail(runId); };
    connect(api.swarmSseUrl(runId), {
      "swarm.event": (d) => {
        const event = (d.event ?? {}) as { type?: string };
        if (event.type && REFETCH_EVENTS.has(event.type)) refetch();
      },
      "swarm.started": refetch,
      reconnect: () => setFollowing(false),
      message: refetch,
    });
    setFollowing(true);
    const poll = window.setInterval(refetch, RUNNING_POLL_MS); // fallback if the stream is quiet/unavailable
    return () => { active = false; window.clearInterval(poll); disconnect(); setFollowing(false); };
  }, [runId, isRunning, connect, disconnect, loadRunDetail]);

  const grouped = useMemo(() => groupSeats(runDetail?.seats ?? []), [runDetail]);

  if (loading) return <div className="p-8 text-sm text-muted-foreground">…</div>;
  if (!runDetail) {
    return (
      <div className="p-8 space-y-2">
        <p className="font-medium text-danger">{t("committee.runNotFound")}</p>
        {error ? <p className="text-sm text-muted-foreground">{error}</p> : null}
        <button onClick={() => navigate("/committee")} className="inline-flex items-center gap-1.5 text-sm text-primary hover:underline">
          <ArrowLeft className="h-3.5 w-3.5" />{t("committee.backToCommittee")}
        </button>
      </div>
    );
  }

  return (
    <div className="min-h-screen p-6 lg:p-8">
      <div className="mx-auto flex w-full max-w-5xl flex-col gap-6">
        <header className="flex items-center gap-3 border-b pb-4">
          <button onClick={() => navigate("/committee")} className="rounded-md p-1 text-muted-foreground hover:bg-muted" title={t("committee.backToCommittee")}>
            <ArrowLeft className="h-4 w-4" />
          </button>
          <h1 className="font-mono text-sm font-medium">{runId}</h1>
          <span className="text-xs text-muted-foreground">{status}</span>
          {isRunning && following ? (
            <span className="inline-flex items-center gap-1 text-xs text-success"><Radio className="h-3.5 w-3.5 animate-pulse" />{t("committee.liveFollowing")}</span>
          ) : null}
        </header>

        <section className="space-y-4">
          <h2 className="text-lg font-semibold">{t("committee.discussion")}</h2>
          {PHASE_ORDER.map((phase) => {
            const seats = grouped.get(phase);
            if (!seats || seats.length === 0) return null;
            return (
              <div key={phase} className="space-y-2">
                <h3 className="text-sm font-medium text-muted-foreground">{t(PHASE_LABEL_KEY[phase])}</h3>
                {phase === "debate"
                  ? renderDebateByRound(seats).map(([round, roundSeats]) => (
                      <div key={round} className="space-y-2 border-s-2 border-muted ps-3">
                        <div className="text-xs font-medium text-muted-foreground">{t("committee.round", { n: round })}</div>
                        {roundSeats.map((s) => <SeatSection key={s.agent_id + s.phase + (s.round ?? "")} seat={s} />)}
                      </div>
                    ))
                  : seats.map((s) => <SeatSection key={s.agent_id + s.phase} seat={s} />)}
              </div>
            );
          })}
          {grouped.get("__other__")?.length ? (
            <div className="space-y-2">
              <h3 className="text-sm font-medium text-muted-foreground">{t("committee.phaseOther")}</h3>
              {grouped.get("__other__")!.map((s) => <SeatSection key={s.agent_id + s.phase} seat={s} />)}
            </div>
          ) : null}
        </section>

        <DecisionCard decision={runDetail.decision} />
        <JournalCard journal={runDetail.journal} />
        <PnlCard pnl={runDetail.pnl} />
      </div>
    </div>
  );
}

function groupSeats(seats: CommitteeSeat[]): Map<string, CommitteeSeat[]> {
  const map = new Map<string, CommitteeSeat[]>();
  for (const seat of seats) {
    const key = (PHASE_ORDER as readonly string[]).includes(seat.phase) ? seat.phase : "__other__";
    const list = map.get(key) ?? [];
    list.push(seat);
    map.set(key, list);
  }
  return map;
}

function renderDebateByRound(seats: CommitteeSeat[]): Array<[number, CommitteeSeat[]]> {
  const byRound = new Map<number, CommitteeSeat[]>();
  for (const seat of seats) {
    const r = seat.round ?? 1;
    const list = byRound.get(r) ?? [];
    list.push(seat);
    byRound.set(r, list);
  }
  return [...byRound.entries()].sort((a, b) => a[0] - b[0]);
}

function Row({ label, value }: { label: string; value: string }) {
  return <div className="flex justify-between gap-4"><span className="text-muted-foreground">{label}</span><span className="tabular-nums">{value}</span></div>;
}

function DecisionCard({ decision }: { decision: CommitteeDecision | null }) {
  const { t } = useTranslation();
  if (!decision) return <section className="rounded-md border p-4 text-sm text-muted-foreground">{t("committee.noDecision")}</section>;
  return (
    <section className="rounded-md border p-4">
      <h2 className="mb-3 text-lg font-semibold">{t("committee.decision")}</h2>
      <div className="space-y-1 text-sm">
        {decision.rating != null ? <Row label={t("committee.colRating")} value={String(decision.rating)} /> : null}
        {decision.price_target != null ? <Row label={t("committee.priceTarget")} value={String(decision.price_target)} /> : null}
        {decision.stop_loss != null ? <Row label={t("committee.stopLoss")} value={String(decision.stop_loss)} /> : null}
        {decision.take_profit != null ? <Row label={t("committee.takeProfit")} value={String(decision.take_profit)} /> : null}
        {decision.position_size_pct != null ? <Row label={t("committee.positionSize")} value={`${decision.position_size_pct}%`} /> : null}
        {decision.time_horizon != null ? <Row label={t("committee.timeHorizon")} value={String(decision.time_horizon)} /> : null}
      </div>
    </section>
  );
}

function JournalCard({ journal }: { journal: CommitteeJournal | null }) {
  const { t } = useTranslation();
  if (!journal) return <section className="rounded-md border p-4 text-sm text-muted-foreground">{t("committee.noJournal")}</section>;
  const horizons = Object.entries(journal.horizons ?? {});
  return (
    <section className="rounded-md border p-4">
      <h2 className="mb-3 text-lg font-semibold">{t("committee.journal")}</h2>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead><tr className="border-b text-start text-muted-foreground">
            <th className="p-2 text-start">{t("committee.horizon")}</th>
            <th className="p-2 text-end">{t("committee.rawReturn")}</th>
            <th className="p-2 text-end">{t("committee.alpha")}</th>
            <th className="p-2 text-end">{t("committee.directionCorrect")}</th>
          </tr></thead>
          <tbody>
            {horizons.map(([h, v]) => {
              const resolved = !!v.resolved_at;
              return (
                <tr key={h} className="border-b last:border-0">
                  <td className="p-2 font-mono text-xs">{h}</td>
                  <td className="p-2 text-end tabular-nums">{resolved && v.raw_return != null ? `${(v.raw_return * 100).toFixed(2)}%` : t("committee.pending")}</td>
                  <td className="p-2 text-end tabular-nums">{resolved && v.alpha != null ? `${(v.alpha * 100).toFixed(2)}%` : t("committee.pending")}</td>
                  <td className="p-2 text-end">{resolved && v.direction_correct != null ? (v.direction_correct ? t("committee.correct") : t("committee.incorrect")) : t("committee.pending")}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <div className="mt-3 border-t pt-2 text-sm">
        <span className="text-muted-foreground">{t("committee.reflection")}: </span>
        {journal.reflection ? journal.reflection : <span className="text-muted-foreground">{t("committee.notReflected")}</span>}
      </div>
    </section>
  );
}

function PnlCard({ pnl }: { pnl: DecisionPnl | null }) {
  const { t } = useTranslation();
  if (!pnl) return <section className="rounded-md border p-4 text-sm text-muted-foreground">{t("committee.noPnl")}</section>;
  return (
    <section className="rounded-md border p-4">
      <h2 className="mb-3 text-lg font-semibold">{t("committee.pnl")}</h2>
      {pnl.executed ? (
        <div className="space-y-1 text-sm">
          <Row label={t("committee.realizedPnl")} value={pnl.realized_pnl != null ? pnl.realized_pnl.toFixed(2) : "-"} />
          {pnl.unrealized_pnl != null ? <Row label={t("committee.unrealizedPnl")} value={pnl.unrealized_pnl.toFixed(2)} /> : null}
          {pnl.fees_paid != null ? <Row label={t("committee.feesPaid")} value={pnl.fees_paid.toFixed(2)} /> : null}
          {pnl.summary ? <p className="mt-2 text-xs text-muted-foreground">{pnl.summary}</p> : null}
        </div>
      ) : <p className="text-sm text-muted-foreground">{t("committee.notExecuted")}</p>}
    </section>
  );
}
```

- [ ] **Verify:** `npm --prefix frontend run test:run -- CommitteeRunDetail` (GREEN) and `npm --prefix frontend run build` (tsc + vite pass).

- [ ] **Commit:**

```bash
git add -A frontend/src/pages/CommitteeRunDetail.tsx frontend/src/pages/committee/SeatSection.tsx frontend/src/pages/__tests__/CommitteeRunDetail.test.tsx
git commit -m "feat(committee-ui): /committee/runs/:runId discussion view with live-follow

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task C1: `vibe-trading ui` CLI subcommand

**Files:**
- Modify: `agent/cli/_legacy.py` (imports, new helpers `_probe_health`/`_wait_for_health`/`cmd_ui`, `ui` subparser, dispatch in `main()`)
- Create: `agent/tests/test_cli_ui.py`

**Interfaces:**
- Consumes: `GET /health` (`agent/src/api/system_routes.py:81` `health_check`, unauthenticated, returns `{"status":"healthy",...}`); the existing frontend-build helpers `_resolve_node_and_npm()`, `_build_frontend_cmd(frontend_dir)`, `_run_step(description, cmd, cwd)` (`agent/cli/_legacy.py:5294-5370`); the same backend-launch shape `cmd_dev` already uses (`[sys.executable, "-m", "cli._legacy", "serve", ...]`, `cwd=AGENT_DIR`, `agent/cli/_legacy.py:5497,5518`).
- Produces: `vibe-trading ui [--host H] [--port P]` (defaults `127.0.0.1` / `8000`, matching `serve_main`'s real defaults in `agent/api_server.py:1049-1051`). Side effect: opens `http://<host>:<port>/committee` in the system browser; prints the URL unconditionally. Never invokes `scripts/ops/run72.sh`.

- [ ] **Step 1 — add the `ui` subparser.** In `_build_parser()` (`agent/cli/_legacy.py:4216`), immediately after the `serve_parser` block (ends `agent/cli/_legacy.py:4257`), add:

```python
ui_parser = subparsers.add_parser(
    "ui", help="Build the frontend if needed, start/attach the API server, and open the Committee Observatory"
)
ui_parser.add_argument("--host", default="127.0.0.1", help="Server host (default: 127.0.0.1)")
ui_parser.add_argument("--port", type=int, default=8000, help="Server port (default: 8000)")
```

Run: `source .venv/bin/activate && cd agent && python -m pytest tests/test_cli_setup_dev.py -q` — expect `13 passed` (confirms the file still imports cleanly before adding new logic).

- [ ] **Step 2 — add `import webbrowser`.** In the stdlib import block (`agent/cli/_legacy.py:17-28`), insert `import webbrowser` alphabetically after `import uuid` (line 28) and before `from datetime import datetime` (line 29):

```python
import uuid
import webbrowser
from datetime import datetime
```

Top-level (not function-local) so tests can `patch.object(cli._legacy.webbrowser, "open", ...)`.

- [ ] **Step 3 — write the failing tests first** (`agent/tests/test_cli_ui.py`), mirroring `agent/tests/test_cli_setup_dev.py`'s fixture/mocking style (fake `frontend/` dirs under `tmp_path`, `patch.object` on the CLI module, no real network/subprocess/browser):

```python
"""Unit tests for `vibe-trading ui` (Task C1).

Three branches from docs/superpowers/specs/2026-07-19-committee-observatory-mcp-design.md
§3.3: no dist / serve down / serve up. Network (`_probe_health`), browser
(`webbrowser.open`), and subprocess (`subprocess.Popen`, `subprocess.run` via
`_run_step`) are all faked — no real server, no real browser, no real npm.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import cli


def _frontend_with_dist(tmp_path: Path) -> Path:
    frontend_dir = tmp_path / "frontend"
    dist = frontend_dir / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html></html>")
    return frontend_dir


def _frontend_without_dist(tmp_path: Path) -> Path:
    frontend_dir = tmp_path / "frontend"
    frontend_dir.mkdir()
    return frontend_dir


class TestNoDist:
    """Branch 1: frontend/dist/index.html missing."""

    def test_builds_when_npm_present(self, tmp_path: Path) -> None:
        frontend_dir = _frontend_without_dist(tmp_path)

        def _fake_run(cmd, **kwargs):
            # Simulate the build actually producing dist/index.html.
            if cmd[:2] == ["npm", "run"] or cmd[:2] == ["npm", "install"]:
                (frontend_dir / "dist").mkdir(exist_ok=True)
                (frontend_dir / "dist" / "index.html").write_text("<html></html>")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch.object(cli._legacy, "_resolve_node_and_npm", return_value=("/usr/bin/node", "/usr/bin/npm")):
            with patch.object(cli._legacy, "_is_windows", return_value=False):
                with patch("cli._legacy.subprocess.run", side_effect=_fake_run) as mock_run:
                    with patch.object(cli._legacy, "_wait_for_health", return_value=True):
                        with patch("cli._legacy.subprocess.Popen") as mock_popen:
                            with patch.object(cli._legacy.webbrowser, "open") as mock_open:
                                rc = cli._legacy.cmd_ui(frontend_dir=frontend_dir)

        assert rc == cli._legacy.EXIT_SUCCESS
        assert mock_run.call_count == 2  # npm install + npm run build
        mock_popen.assert_called_once()  # serve was down -> started
        mock_open.assert_called_once_with("http://127.0.0.1:8000/committee")

    def test_refuses_when_npm_missing(self, tmp_path: Path, capsys) -> None:
        frontend_dir = _frontend_without_dist(tmp_path)
        with patch.object(cli._legacy, "_resolve_node_and_npm", return_value=(None, None)):
            with patch("cli._legacy.subprocess.Popen") as mock_popen:
                with patch.object(cli._legacy.webbrowser, "open") as mock_open:
                    rc = cli._legacy.cmd_ui(frontend_dir=frontend_dir)
        out = capsys.readouterr().out
        assert rc == cli._legacy.EXIT_USAGE_ERROR
        assert "npm --prefix frontend run build" in out
        mock_popen.assert_not_called()
        mock_open.assert_not_called()

    def test_run_failed_when_build_step_fails(self, tmp_path: Path) -> None:
        frontend_dir = _frontend_without_dist(tmp_path)
        with patch.object(cli._legacy, "_resolve_node_and_npm", return_value=("/usr/bin/node", "/usr/bin/npm")):
            with patch.object(cli._legacy, "_is_windows", return_value=False):
                with patch("cli._legacy.subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="boom")
                    with patch("cli._legacy.subprocess.Popen") as mock_popen:
                        rc = cli._legacy.cmd_ui(frontend_dir=frontend_dir)
        assert rc == cli._legacy.EXIT_RUN_FAILED
        mock_popen.assert_not_called()


class TestServeDown:
    """Branch 2: dist present, /health not answering -> start serve, wait, open browser."""

    def test_starts_serve_and_opens_browser(self, tmp_path: Path) -> None:
        frontend_dir = _frontend_with_dist(tmp_path)
        with patch.object(cli._legacy, "_wait_for_health", return_value=True) as mock_wait:
            with patch.object(cli._legacy, "_probe_health", return_value=False):
                with patch("cli._legacy.subprocess.Popen") as mock_popen:
                    with patch.object(cli._legacy.webbrowser, "open") as mock_open:
                        rc = cli._legacy.cmd_ui(host="127.0.0.1", port=8123, frontend_dir=frontend_dir)

        assert rc == cli._legacy.EXIT_SUCCESS
        mock_popen.assert_called_once()
        popen_cmd = mock_popen.call_args.args[0]
        assert popen_cmd[:4] == [cli._legacy.sys.executable, "-m", "cli._legacy", "serve"]
        assert "--host" in popen_cmd and "--port" in popen_cmd
        assert popen_cmd[popen_cmd.index("--port") + 1] == "8123"
        mock_wait.assert_called_once()
        mock_open.assert_called_once_with("http://127.0.0.1:8123/committee")

    def test_run_failed_when_never_healthy(self, tmp_path: Path, capsys) -> None:
        frontend_dir = _frontend_with_dist(tmp_path)
        with patch.object(cli._legacy, "_probe_health", return_value=False):
            with patch.object(cli._legacy, "_wait_for_health", return_value=False):
                with patch("cli._legacy.subprocess.Popen") as mock_popen:
                    with patch.object(cli._legacy.webbrowser, "open") as mock_open:
                        rc = cli._legacy.cmd_ui(frontend_dir=frontend_dir)
        assert rc == cli._legacy.EXIT_RUN_FAILED
        mock_popen.assert_called_once()
        mock_open.assert_not_called()


class TestServeUp:
    """Branch 3: /health already answering -> attach, never double-start."""

    def test_attaches_without_starting_serve(self, tmp_path: Path) -> None:
        frontend_dir = _frontend_with_dist(tmp_path)
        with patch.object(cli._legacy, "_probe_health", return_value=True):
            with patch("cli._legacy.subprocess.Popen") as mock_popen:
                with patch.object(cli._legacy.webbrowser, "open") as mock_open:
                    rc = cli._legacy.cmd_ui(frontend_dir=frontend_dir)

        assert rc == cli._legacy.EXIT_SUCCESS
        mock_popen.assert_not_called()
        mock_open.assert_called_once_with("http://127.0.0.1:8000/committee")


class TestWaitForHealth:
    """`_wait_for_health` polls `_probe_health` until healthy or timeout, sleeping between polls."""

    def test_returns_true_as_soon_as_healthy(self) -> None:
        calls = {"n": 0}

        def _fake_probe(url, **kwargs):
            calls["n"] += 1
            return calls["n"] >= 3

        with patch.object(cli._legacy, "_probe_health", side_effect=_fake_probe):
            with patch.object(cli._legacy.time, "sleep") as mock_sleep:
                ok = cli._legacy._wait_for_health("http://x/health", timeout_s=10.0, poll_interval=0.1)
        assert ok is True
        assert calls["n"] == 3
        assert mock_sleep.call_count == 2  # slept between polls 1->2 and 2->3, not after success

    def test_returns_false_on_timeout(self) -> None:
        # time.monotonic() sequence: start, then jump straight past the deadline.
        with patch.object(cli._legacy, "_probe_health", return_value=False):
            with patch.object(cli._legacy.time, "sleep"):
                with patch.object(cli._legacy.time, "monotonic", side_effect=[0.0, 100.0]):
                    ok = cli._legacy._wait_for_health("http://x/health", timeout_s=10.0, poll_interval=0.1)
        assert ok is False
```

Run: `source .venv/bin/activate && cd agent && python -m pytest tests/test_cli_ui.py -q` — expect failures (`AttributeError: module 'cli._legacy' has no attribute 'cmd_ui'`), confirming the tests actually exercise not-yet-written code.

- [ ] **Step 4 — implement `_probe_health`, `_wait_for_health`, `cmd_ui`.** Add after `cmd_dev` (`agent/cli/_legacy.py:5566`, right before `def main(argv...`):

```python
def _probe_health(url: str, *, timeout: float = 2.0) -> bool:
    """Return True if a GET against `url` returns HTTP 200.

    Local import (matches the existing httpx-on-demand pattern at
    `_commit_mandate`, `agent/cli/main.py:1013`) so `httpx` is not a hard
    import-time dependency of the whole CLI module. Any failure (connection
    refused, timeout, DNS) means "not healthy" — never raises.
    """
    import httpx

    try:
        response = httpx.get(url, timeout=timeout)
        return response.status_code == 200
    except Exception:  # noqa: BLE001 -- unreachable server is a normal, expected state here
        return False


def _wait_for_health(url: str, *, timeout_s: float = 30.0, poll_interval: float = 0.5) -> bool:
    """Poll `_probe_health(url)` until healthy or `timeout_s` elapses.

    Returns True the moment a probe succeeds (no extra sleep after success);
    returns False once the deadline passes with no healthy probe.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        if _probe_health(url):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_interval)


def cmd_ui(
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    frontend_dir: Optional[Path] = None,
) -> int:
    """Build the frontend if needed, start/attach the API server, and open
    the Committee Observatory (`docs/superpowers/specs/2026-07-19-committee-observatory-mcp-design.md` §3.3).

    Three branches, in order:

    1. `frontend/dist/index.html` missing -> build it via
       `npm --prefix frontend run build` when npm is on PATH (reusing the
       same `_build_frontend_cmd`/`_run_step` steps as `cmd_setup`);
       otherwise refuse with the exact manual build command.
    2. `GET /health` not answering on `host:port` -> start `vibe-trading
       serve` backgrounded (same invocation `cmd_dev` uses) and wait for it
       to become healthy; if it is already answering, attach instead of
       double-starting.
    3. Open the system browser at `http://<host>:<port>/committee` via
       `webbrowser.open`, printing the URL either way.

    Never wraps `scripts/ops/run72.sh` — supervised evidence runs are a
    deliberately separate workflow; this always starts a plain,
    unsupervised serve for interactive use.
    """
    frontend_dir = frontend_dir or (AGENT_DIR.parent / "frontend")
    dist_index = frontend_dir / "dist" / "index.html"

    if not dist_index.exists():
        node, npm = _resolve_node_and_npm()
        if not npm:
            console.print(
                f"[red]No frontend build found at {dist_index}, and npm is not on PATH.[/red]\n"
                "[dim]Install Node.js (>= 18) from https://nodejs.org, then run:\n"
                "  npm --prefix frontend run build\n"
                "or  vibe-trading setup[/dim]"
            )
            return EXIT_USAGE_ERROR
        console.print(f"[dim]No frontend build found at {dist_index}; building…[/dim]")
        for step in _build_frontend_cmd(frontend_dir):
            description = " ".join(step[:3])
            if not _run_step(description, step, frontend_dir):
                console.print("[red]Frontend build failed.[/red] See the error above.")
                return EXIT_RUN_FAILED
        if not dist_index.exists():
            console.print(f"[red]Build completed but {dist_index} is still missing.[/red]")
            return EXIT_RUN_FAILED

    base_url = f"http://{host}:{port}"
    health_url = f"{base_url}/health"
    committee_url = f"{base_url}/committee"

    if _probe_health(health_url):
        console.print(f"[dim]Server already running at {base_url} — attaching.[/dim]")
    else:
        console.print(f"[dim]Starting server at {base_url} …[/dim]")
        subprocess.Popen(
            [sys.executable, "-m", "cli._legacy", "serve", "--host", host, "--port", str(port)],
            cwd=str(AGENT_DIR),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if not _wait_for_health(health_url):
            console.print(
                f"[red]Server did not become healthy within 30s at {health_url}.[/red]"
            )
            return EXIT_RUN_FAILED

    console.print(f"[green]Committee Observatory:[/green] {committee_url}")
    try:
        webbrowser.open(committee_url)
    except Exception as exc:  # noqa: BLE001 -- headless/no-display is not a command failure
        console.print(f"[dim]Could not auto-open a browser ({exc}); open the URL above manually.[/dim]")
    return EXIT_SUCCESS
```

Run: `source .venv/bin/activate && cd agent && python -m pytest tests/test_cli_ui.py -q` — expect `9 passed`.

- [ ] **Step 5 — wire dispatch in `main()`.** In `agent/cli/_legacy.py:5569`, right after the `serve` branch (`agent/cli/_legacy.py:5596-5597`):

```python
if args.command == "serve":
    return serve_main(raw_argv[1:])
if args.command == "ui":
    return _coerce_exit_code(cmd_ui(host=args.host, port=args.port))
```

Note: the top-level parser also defines global `-p/--prompt` etc., but `args.host`/`args.port` here resolve from the `ui` subparser's own arguments (argparse subparser namespaces merge into the same `args` object, same as `args.paper_limit`/`args.ops_window` do for their subcommands).

- [ ] **Step 6 — full regression pass + manual smoke.**

```bash
source .venv/bin/activate && cd agent
python -m pytest tests/test_cli_ui.py tests/test_cli_setup_dev.py tests/test_paper_cli.py -q
# expect: all passed, 0 failed
python -c "from cli._legacy import _build_parser; p = _build_parser(); ns = p.parse_args(['ui', '--port', '9000']); print(ns.command, ns.host, ns.port)"
# expect: ui 127.0.0.1 9000
```

- [ ] **Step 7 — commit.**

```bash
cd /Users/opcw05/rt/vibe001/Vibe-Trading-cryptoagent
git add agent/cli/_legacy.py agent/tests/test_cli_ui.py
git commit -m "$(cat <<'EOF'
Add `vibe-trading ui` launch command (build/start/attach + open /committee)

One command to observe the crypto committee: builds the frontend if
missing, starts or attaches to the API server, and opens the browser on
/committee. Never touches run72.sh -- supervised evidence runs stay a
separate workflow.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task C2: Docs + finalization (env, README/crypto-committee.md, CHANGELOG, full suite, live verification)

**Files:**
- Modify: `agent/.env.example`, `docs/crypto-committee.md`, `README.md`, `CHANGELOG.md`

**Interfaces:**
- Consumes: `VIBE_MCP_COMMITTEE` / `VIBE_MCP_ALLOW_TRIGGER` / `VIBE_MCP_TRIGGER_BUDGET` (gate semantics owned by Tasks M1-M3, spec §3.4); `GET /mcp/status` (Task R3, spec §3.1); `vibe-trading-mcp` stdio entry point (`agent/pyproject.toml:71`, `mcp_server:main`); the HTTP mount path `/mcp` (spec §3.4 gate 1); `vibe-trading ui` (Task C1).
- Produces: operator-facing documentation only — no runtime code. This task depends on milestones 1-6 (REST, MCP read/trigger tools, UI pages, CLI command) already landing; it is the final milestone (spec §3.5, milestone 7).

- [ ] **Step 1 — `.env.example` entries.** `docs/` is gitignored (`.gitignore:54`) but `agent/.env.example` is not — plain `git add`. Append after the last line of `agent/.env.example` (currently `CONTENT_FILTER_WARNING_THRESHOLD=0.05`), mirroring the `VIBE_SWARM_PERSIST_TRANSCRIPTS` doc-comment style (`agent/.env.example:381-388`):

```bash

# ============================================================================
# Committee Observatory MCP interface (optional, default OFF)
# ============================================================================
# Read-only MCP tool group over committee performance/decisions/transcripts +
# paper account (committee_performance, list_decisions, get_decision,
# list_committee_runs, get_run_transcript, paper_account). Mounts the MCP
# streamable-HTTP app at /mcp inside `vibe-trading serve` in addition to the
# existing `vibe-trading-mcp` stdio entry point. With this unset, serve and
# vibe-trading-mcp behave byte-identically to before this feature.
# Truthy: 1/true/yes/on (same idiom as VIBE_SWARM_PERSIST_TRANSCRIPTS).
# VIBE_MCP_COMMITTEE=1

# Only meaningful when VIBE_MCP_COMMITTEE is also set. Additionally registers
# `run_committee(symbol, note=None)`, letting an external agent (e.g. an
# operator's own "Hermes" agent) trigger a real committee run through MCP.
# Budget-capped (see VIBE_MCP_TRIGGER_BUDGET) and audit-logged
# (~/.vibe-trading/committee/mcp_triggers.jsonl); every attempt, accepted or
# refused, is recorded. No config/strategy mutation is ever exposed via MCP.
# VIBE_MCP_ALLOW_TRIGGER=1

# Max `run_committee` triggers accepted per UTC day (file-backed count, reads
# mcp_triggers.jsonl -- no in-memory counter that resets on restart). Over
# budget refuses with a structured {error_type: "budget_exhausted", resets_at}
# -- it never queues.
# VIBE_MCP_TRIGGER_BUDGET=4
```

Run: `source .venv/bin/activate && cd agent && python -m pytest tests/test_paper_env_guard.py -q` — expect pass (hermeticity sanity check after editing `.env.example`).

- [ ] **Step 2 — `docs/crypto-committee.md` new sections.** Append two new `##` sections at the end of the file (after `## Transition protocol (DeepSeek → MiniMax)`, `docs/crypto-committee.md:911-918`), matching the doc's existing register (concrete file/function pointers, env var tables, honest-limits framing). Section 1, "Observing the committee": document `vibe-trading ui` (build-if-missing → start/attach serve via /health → open browser on /committee, with `--port` example), the poll-based dashboard contents (paper account + equity curve, scheduler health, MCP status card, runs table), the `/committee/runs/:runId` discussion view (pipeline-ordered seats, debate by round, decision, journal outcome, PnL, SSE live-follow with poll fallback), and the statement that the view is 100% read-only with the MCP toggle being env-level, not a UI button. Section 2, "Connecting an external agent (MCP)": the two-gate env table (`VIBE_MCP_COMMITTEE` off → read tool group + /mcp mount; `VIBE_MCP_ALLOW_TRIGGER` off → `run_committee`; `VIBE_MCP_TRIGGER_BUDGET` default 4), the six read-tool signatures, the trigger tool semantics (grounding validation, same dispatch path as scheduled runs, returns `{run_id}` immediately, audit log path), the explicit never-exposed list (config/env mutation, paper reset, scheduler management, strategy parameters), stdio connection JSON (`{"mcpServers": {"vibe-trading": {"command": "vibe-trading-mcp"}}}` + set the env gates in that process's environment), HTTP connection URL (`http://127.0.0.1:8000/mcp` when serve runs with gate 1), and a pointer to `GET /mcp/status` as the live gate-state check.

Run: `python3 -c "import pathlib; t = pathlib.Path('docs/crypto-committee.md').read_text(); assert '## Observing the committee' in t and '## Connecting an external agent (MCP)' in t; print('sections present')"` — expect `sections present`.

- [ ] **Step 3 — README pointers.** Two small, additive edits (do not restructure existing sections):
  - In the CLI Reference bash block (`README.md:669-676`), add one line after `vibe-trading serve         # API server`:

```
vibe-trading ui            # build/start/attach + open the Committee Observatory (/committee)
```

  - At the end of the `## 🔌 MCP Plugin` section (`README.md:947-1046`, after the OpenSpace `</details>` closing the section, before the next `##`), add:

```markdown

**Committee-specific tools (opt-in, default OFF):** `committee_performance`,
`list_decisions`, `get_decision`, `list_committee_runs`, `get_run_transcript`,
`paper_account`, and (double-gated) `run_committee` — see
[`docs/crypto-committee.md`, "Connecting an external agent (MCP)"](docs/crypto-committee.md#connecting-an-external-agent-mcp)
for the env gates and connection instructions.
```

Run: `grep -n "vibe-trading ui" README.md` — expect one match; `grep -n "Connecting an external agent" README.md` — expect one match.

- [ ] **Step 4 — CHANGELOG entry.** Add a new bullet under `## [Unreleased]` → `### Added` (`CHANGELOG.md:6-8`), following the existing numbering convention (use the next PR number at commit time — check `gh pr list --state all --limit 1` for the highest existing number):

```markdown
- **Committee Observatory UI + MCP interface.** A `/committee` operator
  dashboard (paper account + equity, scheduler health, MCP status, runs
  table) and a `/committee/runs/:runId` discussion view (every seat's report,
  the debate, the decision, journal outcome, paper PnL; live-follows a
  running committee via the existing swarm SSE stream). New read-only REST
  endpoints (`/committee/runs`, `/journal/decisions`, `/scheduler/health`,
  `/mcp/status`) plus paper endpoints per the ops-dashboard spec. A
  double-gated MCP tool group (`VIBE_MCP_COMMITTEE` / `VIBE_MCP_ALLOW_TRIGGER`,
  both default OFF) exposes committee performance/decisions/transcripts and
  an optional, budget-capped `run_committee` trigger to external agents (no
  strategy-knob mutation, ever). New `vibe-trading ui` command builds the
  frontend if needed, starts/attaches `serve`, and opens the browser on
  `/committee`.
```

Run: `grep -n "Committee Observatory UI + MCP interface" CHANGELOG.md` — expect one match.

- [ ] **Step 5 — full-suite run.** Only run this once milestones 1-6 (REST, MCP tools, UI pages, `vibe-trading ui`) are actually in the working tree — this is the final gate before live verification:

```bash
source .venv/bin/activate && cd agent
python -m pytest -q
```

Expect `0 failed`. Per spec §5 risk note ("add a frontend build job ONLY if it does not slow the existing test job, else defer, noted in plan") — record whichever was chosen here (e.g. "Frontend CI build job deferred — `npm run build` not yet wired into CI; `vibe-trading ui` builds on demand."). Also run:

```bash
cd frontend && npm run build
```

Expect success (tsc + vite) — the one live signal that `frontend/dist/index.html` (which `vibe-trading ui` depends on) actually builds clean.

- [ ] **Step 6 — live verification (binding, per spec §3.5).** Perform against the REAL stores/server, not mocks:
  1. **Real launch:** `vibe-trading ui` — confirm it prints `Committee Observatory: http://127.0.0.1:8000/committee`, the browser opens, and the dashboard renders the real 24h-window runs (not fixture data).
  2. **Real MCP stdio client call** — with `VIBE_MCP_COMMITTEE=1` set in the shell running `vibe-trading-mcp`, call two read tools through a real stdio client (`fastmcp` is already vendored — `agent/mcp_server.py:56` `from fastmcp import Context, FastMCP`):

```python
import asyncio
from fastmcp import Client

async def main():
    config = {"mcpServers": {"vibe-trading": {"command": "vibe-trading-mcp"}}}
    async with Client(config) as client:
        perf = await client.call_tool("committee_performance", {})
        print("committee_performance:", perf)
        runs = await client.call_tool("list_committee_runs", {"limit": 1})
        print("list_committee_runs:", runs)
        run_id = runs.data[0]["run_id"] if getattr(runs, "data", None) else None
        if run_id:
            transcript = await client.call_tool("get_run_transcript", {"run_id": run_id})
            print("get_run_transcript:", transcript)

asyncio.run(main())
```

Confirm real data comes back (not an empty/gated response) and no exception is raised.
  3. **Trigger path — exercise at most ONCE:** with `VIBE_MCP_ALLOW_TRIGGER=1` additionally set, either (a) call `run_committee(symbol="BTC-USDT")` for real through the same client and confirm it returns `{run_id}`, counts against `VIBE_MCP_TRIGGER_BUDGET`, and appends a row to `~/.vibe-trading/committee/mcp_triggers.jsonl` — this spends real LLM tokens (13-seat committee run), so confirm with the operator before running it; **or**, if the operator vetoes the token spend, fake the swarm dispatch (monkeypatch `run_swarm` at the trigger tool's call site) and verify the audit-log row + budget decrement happen correctly with no real committee run. Record which of the two was done and why.
  4. Note the result of each of the three checks above (pass/fail + any anomaly) in the PR description or a handover note — this is the evidence the spec's binding live-verification requirement is satisfied, not just claimed.

- [ ] **Step 7 — commit.** `docs/` is gitignored (`.gitignore:54`), so `docs/crypto-committee.md` needs `git add -f`; `.env.example`, `README.md`, `CHANGELOG.md` are tracked normally:

```bash
cd /Users/opcw05/rt/vibe001/Vibe-Trading-cryptoagent
git add agent/.env.example README.md CHANGELOG.md
git add -f docs/crypto-committee.md
git commit -m "$(cat <<'EOF'
Document the Committee Observatory UI + MCP interface

.env.example entries for the double MCP gate, an "Observing the
committee" + "Connecting an external agent (MCP)" pair of sections in
crypto-committee.md, matching README pointers, and a CHANGELOG entry.
Live-verified: real vibe-trading ui launch, real MCP stdio calls, trigger
path exercised per spec's binding live-verification checklist.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
git status
```
