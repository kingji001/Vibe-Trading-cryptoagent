# Committee Observatory UI + MCP Interface — Design Spec

Date: 2026-07-19
Status: approved by operator (approach + design sections approved in session)
Supersedes/extends: `docs/development-guides/A-trading-ops-dashboard.md` (Direction A).
That guide's binding constraints §2 (read-only REST, store-delegation, auth pattern,
poll-don't-SSE for paper data, frontend conventions, i18n, repo invariants) apply to
this spec verbatim unless explicitly widened here. The evidence gate in that guide
is satisfied: the operator explicitly ordered this feature on 2026-07-19, and the
fee/turnover gate is moot (zero trades executed to date, so fees cannot be eating
the edge).

## 1. Goal

Two deliverables in one coherent surface:

1. **Operator UI** — launch the project with one command and *observe every AI
   committee discussion*: per-seat reports, multi-round bull/bear debate, risk
   rotation, the final decision, its journal outcome (per-horizon alpha,
   direction_correct, reflection), paper-trading PnL, and scheduler/system health.
2. **MCP interface (toggleable)** — external agents (e.g. the operator's "Hermes"
   agent) can read the system's full performance record and (separately gated,
   budget-capped) trigger committee runs, enabling external comparison, strategy
   recommendation, and evolutionary analysis loops. External agents CANNOT change
   this project's configuration through MCP.

## 2. Decisions already made (do not relitigate)

| Decision | Choice |
|---|---|
| Approach | Extend existing surfaces (React frontend + `mcp_server.py` + FastAPI serve), one process |
| MCP capability scope | Read + trigger runs; NO strategy-knob mutations |
| UI experience | Runs list (poll) + full-discussion detail view; live-follow via existing swarm SSE when a run is in progress |
| Trigger safety | Separate env gate + per-UTC-day budget + audit log; default OFF |
| Web UI mutations | None. 100% read-only, per Direction A guide |
| Model tiering | Out of scope; M3-only per operator decision 2026-07-19 |

## 3. Components

### 3.1 REST data layer (read-only, all GET)

New modules `agent/src/api/paper_routes.py` and `agent/src/api/committee_routes.py`,
registered from `agent/api_server.py` beside the existing `register_*_routes` calls,
each following the `register_scheduled_routes` auth-resolution pattern
(`require_auth` from host module via `sys.modules`, `dependencies=[Depends(require_auth)]`
on every route).

Paper endpoints — shapes and store delegation EXACTLY as specified in
`A-trading-ops-dashboard.md` §3 Task 1 (that section is the authoritative endpoint
spec; do not re-derive):

- `GET /paper/status` → `PaperBroker(store).equity()` verbatim.
- `GET /paper/ledger?limit=N` → tail of `store.iter_ledger()`.
- `GET /paper/equity` → `list(store.iter_equity())`.
- `GET /paper/pnl/{decision_id}` → `src.paper.pnl.decision_pnl(...)` verbatim.

Committee endpoints (new in this spec):

- `GET /committee/runs?limit=N&status=&symbol=` → newest-first list of
  crypto_committee swarm runs joined with journal entries by `run_id`:
  `{run_id, created_at, status, target, wall_clock_s, input_tokens, output_tokens,
  decision_id?, rating?, journal_status?, pnl_summary?}`. Source: the swarm run
  store used by the existing `runs_routes.py`/`swarm_routes.py` (reuse its
  listing/loading helpers — do NOT re-glob `agent/.swarm/runs` if a store API
  exists; if listing helpers are private, extract/reuse rather than copy).
  Journal join via `src.committee.journal.load_entries` keyed by `run_id`.
- `GET /committee/runs/{run_id}` → the full discussion:
  `{run: <run.json summary>, seats: [{agent_id, phase, round, status, report_md,
  decision_json?}], debate: {rounds: N, order: [task ids]}, decision: {...},
  journal: {horizons, reflection, reflected_at} | null, pnl: {...} | null}`.
  Seat reports read from `artifacts/<agent>/report.md`; decision from
  `artifacts/portfolio_manager/decision.portfolio_decision.json`; phase/round
  grouping derived from task ids (`-r{n}` suffix convention from
  `_expand_debate`). Missing artifacts → explicit `"report_md": null` with
  `"missing": true`; never fabricated.
- `GET /journal/decisions?limit=N&symbol=` → `load_entries` newest-first
  projection (id, decided_at, symbol, rating, status, primary_horizon, horizons,
  reflected_at, run_id).
- `GET /scheduler/health` → registered scheduled jobs (id, schedule, last state)
  from the scheduled-research store used by `scheduled_routes.py`, plus
  supervisor liveness (run72 heartbeat file mtime/last row, best-effort,
  `null` when absent).
- `GET /mcp/status` → `{committee_tools_enabled, trigger_enabled, trigger_budget,
  triggers_used_today, http_mount: "/mcp"|null, stdio_command: "vibe-trading-mcp"}`.

Error behavior: absent stores/dirs → empty lists or explicit nulls with
`"missing": true` markers; malformed artifact files → per-item `"error"` field,
HTTP 200 (the list must not fail because one run is corrupt); unknown
`run_id`/`decision_id` → 404 via the host's `_validate_path_param` convention.

### 3.2 UI (React app, new lazy routes)

- `/committee` — operator dashboard page:
  - Paper account card + equity curve (existing echarts wrapper + chart theme).
  - Scheduler health card (jobs, last-fired, supervisor liveness).
  - MCP status card (reads `/mcp/status`; shows connect instructions when
    enabled; pure display, no toggle control in UI — the toggle is env-level).
  - Committee runs table, newest-first, poll 30–60s (`api.ts` `request<T>` +
    store convention like `Runtime.tsx`): time, symbol, rating badge, status,
    wall-clock, tokens, PnL link. Row click → detail page.
- `/committee/runs/:runId` — discussion view:
  - Pipeline-ordered seat sections (analysts → bull/bear debate grouped by round
    → research manager → trader → risk rotation → portfolio manager), each
    expandable to full rendered markdown report.
  - Decision card (rating, price target, stop/TP, position size), journal
    outcome (per-horizon raw/alpha/direction_correct, reflection text), paper
    PnL for the decision.
  - Live-follow: when `status == "running"`, subscribe via the existing
    `useSSE` swarm event stream hook (same as `RunDetail.tsx`) and update
    seat statuses/reports as tasks complete; fall back to polling if the
    stream is unavailable. No new SSE emitters are built (guide §2.4).
- i18n: every string through `react-i18next`, keys added to all five locales
  (`en`, `zh-CN`, `ja`, `ko`, `ar`); RTL must not break layout.
- Navigation: sidebar/nav entry for "Committee" following the existing nav
  component conventions.

### 3.3 Launch command

`vibe-trading ui` (new CLI subcommand, registered like existing subcommands):

1. If `frontend/dist/index.html` missing: attempt `npm --prefix frontend run build`
   when npm is on PATH; otherwise exit with the exact build instructions.
2. If serve is not answering on the configured host/port (`GET /health`): start
   it (same code path as `vibe-trading serve`, backgrounded) and wait for
   healthy; if already answering: attach (do not double-start).
3. Open the system browser at `http://<configured host>:<configured port>/committee`
   (default `http://127.0.0.1:8000/committee`) via `webbrowser.open`, printing
   the URL either way.

The command never wraps run72.sh (supervised evidence runs remain a deliberate
separate workflow); it starts a plain serve for interactive use.

### 3.4 MCP interface (extends `agent/mcp_server.py`)

Double-gated, both default OFF; with neither set, serve and `vibe-trading-mcp`
behave byte-identically to today.

Gate 1 — `VIBE_MCP_COMMITTEE=1` (truthy set `{"1","true","yes","on"}`, same idiom
as `VIBE_SWARM_PERSIST_TRANSCRIPTS`): registers the committee READ tool group in
`mcp_server.py` and mounts the MCP streamable-HTTP app at `/mcp` inside serve
(`api_server.py`, flag-checked at startup). Tools (each returns the `_json_ok`
envelope like existing tools, and reads via the SAME store functions the REST
layer uses — never HTTP self-calls):

- `committee_performance(window_hours=None, symbol=None)` — aggregate over
  resolved journal horizons: counts, direction_correct rate (overall and
  non-Hold), mean/median alpha and raw return per horizon, paper realized/
  unrealized PnL, tokens and wall-clock per run averages. Includes the standing
  caveat string that alpha vs a same-symbol benchmark is definitionally ~0 for
  single-symbol universes.
- `list_decisions(limit=20, symbol=None)` / `get_decision(decision_id)` —
  journal projections; `get_decision` includes full horizons + reflection.
- `list_committee_runs(limit=20, status=None)` — same join as REST.
- `get_run_transcript(run_id, seat=None)` — per-seat report markdown (all seats
  or one), debate round structure, decision JSON.
- `paper_account()` — broker equity + recent ledger tail.

Gate 2 — `VIBE_MCP_ALLOW_TRIGGER=1` (only meaningful when gate 1 is on):
additionally registers `run_committee(symbol, note=None)`:

- Validates symbol against the instrument-resolution path (fail-fast, same
  grounding rules as scheduled runs — no ungrounded run can be triggered).
- Budget: at most `VIBE_MCP_TRIGGER_BUDGET` (int, default 4) triggers per UTC
  day. Over budget → structured refusal `{error_type: "budget_exhausted",
  resets_at}` — never queues.
- Audit: every attempt (accepted or refused) appended to
  `~/.vibe-trading/committee/mcp_triggers.jsonl`:
  `{ts, symbol, note, accepted, reason?, run_id?}`. Budget counting reads this
  file (single source of truth; no in-memory counter that resets on restart).
- Execution: dispatches the same code path the scheduled committee job uses
  (structured `variables`, run_swarm), returning `{run_id}` immediately; Hermes
  polls `list_committee_runs`/`get_run_transcript` for completion.

Explicitly NOT exposed via MCP: any config/env mutation, paper reset, scheduler
job management, strategy parameters. Strategy evolution happens OUTSIDE this
system: Hermes reads performance, reasons externally, and its recommendations
reach this repo only through the human operator.

### 3.5 Testing & verification

- REST: `agent/tests/test_paper_api.py` + `test_committee_api.py` — TestClient
  against temp stores (fixture-seeded run dirs + journal files), including
  corrupt-artifact, absent-store, and auth cases.
- MCP: extend the `test_mcp_server_smoke.py` pattern — gates off → tools absent;
  gate 1 on → read tools registered and functional against seeded stores; gate 2
  budget honored across simulated restarts (file-backed count), audit rows
  written, refusal shape; symbol-validation failure path.
- CLI: `vibe-trading ui` unit tests for the three branches (no dist / serve
  down / serve up) with the network and browser calls faked.
- Frontend: component tests per existing `frontend/src/**/__tests__`
  conventions for the two new pages (rendering from fixture JSON, empty states,
  missing-artifact display); `npm run build` must pass (tsc + vite).
- Env guards: new env vars are read-only toggles without filesystem side
  effects at import time; the conftest hermeticity guard list gains
  `VIBE_MCP_COMMITTEE`, `VIBE_MCP_ALLOW_TRIGGER`, `VIBE_MCP_TRIGGER_BUDGET` if
  the existing guard pattern requires explicit registration.
- Live verification (binding, per repo playbook): real `vibe-trading ui` launch,
  browser on `/committee` showing the real 24h-window runs; real MCP client
  (stdio) calling `committee_performance` and `get_run_transcript` against the
  real stores; trigger path exercised at most ONCE live against a real symbol
  (counts against the day's budget) or with the swarm dispatch faked if the
  operator vetoes the token spend.

## 4. Non-goals / out of scope

- No web-UI mutations of any kind (guide §2.1 stands; `paper reset` stays CLI).
- No MCP strategy-knob mutations (operator decision).
- No new SSE emitters for paper/journal data (poll only; guide §2.4).
- No dashboard for the IRR-AGL governance stack or alpha-zoo (existing pages).
- No changes to run72/ops evidence workflow.
- No model-tiering surface (deferred per operator decision 2026-07-19).

## 5. Risks & mitigations

- **Frontend build rot** (dist absent, node toolchain drift): `vibe-trading ui`
  detects and builds/instructs; CI does not build the frontend today — add a
  frontend build job ONLY if it does not slow the existing test job (else defer,
  noted in plan).
- **Large run dirs** (transcripts/artifacts): detail endpoint reads single run
  dirs only; list endpoint reads run.json summaries only; no endpoint walks all
  artifacts of all runs.
- **MCP trigger abuse**: double gate + file-backed daily budget + audit log +
  grounding validation; budget default (4) is ~1/3 of the scheduled daily load.
- **SSE coupling**: live-follow reuses the existing swarm stream contract; if
  the stream shape differs for committee runs, fall back to polling (the detail
  page must render correctly with polling alone).

## 6. Milestone order (for the implementation plan)

1. REST data layer + tests (paper, committee, journal, scheduler, mcp/status).
2. MCP read tool group + HTTP mount + tests (gate 1).
3. MCP trigger tool + budget/audit + tests (gate 2).
4. UI dashboard page (`/committee`) + tests.
5. UI discussion view (`/committee/runs/:runId`) incl. live-follow + tests.
6. `vibe-trading ui` command + tests.
7. Docs (`.env.example`, README section, crypto-committee.md pointer,
   Hermes connection how-to) + full-suite + live verification.
