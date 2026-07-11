# Feature A — Trading Ops Web Dashboard: Development Guidance Plan

Process contract: this plan is executed under `docs/development-guides/README.md`
(the loop-rules playbook). Everything there is binding — the per-task loop (fresh
implementer, evidenced TDD RED→GREEN, independent reviewer, fix→re-review before
proceeding), the ledger at `.superpowers/sdd4/progress.md`, the final whole-branch
review, and LIVE end-to-end verification. This file only adds the feature-specific
decisions so a future assistant does not re-derive them. Roadmap source of truth:
`docs/optimization-roadmap.md` §2.A.

---

## 1. Goal & evidence gate

**Goal.** Give the operator a read-only web view of the paper-trading engine and
decision journal that the 72h run already produces on disk, so §1a–§1c of the
roadmap become legible without CLI/JSONL spelunking: (1) paper account — equity
curve, open positions with stops/TPs, fill ledger, per-decision PnL; (2) decision
journal — decisions, per-horizon realized alpha, reflections; (3) scheduler health
— registered jobs, last-fired, recent event triggers.

**Evidence gate (do NOT start until both hold).** Per roadmap §2.A and the priority
table:
- **User still wants it after seeing the raw artifacts** from this run. The CLI
  (`vibe-trading paper status/ledger`, `decision_journal action=list`) and the raw
  files under `~/.vibe-trading/{paper,committee}` may already suffice; if so, this
  L-effort feature defers.
- **Not before turnover tuning if fees are eating the edge.** Roadmap §1b: if
  `fee_paid`+`slippage_paid` sum to **>30% of gross realized PnL**, do
  cadence/turnover tuning (coarser committee schedule, higher event thresholds,
  `VIBE_EVENT_COOLDOWN_H` up) FIRST — a dashboard only visualizes turnover, it does
  not change the ratio. Confirm this gate is clear before spending L effort here.

This plan is what you execute **once the gate opens**, not a reason to open it.

---

## 2. Binding design constraints

1. **Read-only REST surface for paper data. No mutation endpoints in v1 — none.**
   Every new route is `GET`. `paper reset` (`PaperStore.archive_and_reset`,
   `agent/src/paper/store.py:163`) stays **CLI-only** and is never exposed. Buys,
   sells, ticks, and resets remain the committee/scheduler/CLI's job; the dashboard
   observes, it never acts.
2. **Reuse `PaperStore` + `pnl`/`broker` functions — never re-parse JSONL in route
   code.** Read via `PaperStore.load_account/load_positions/iter_ledger/iter_equity/
   load_tick_state` (`agent/src/paper/store.py:79,98,110,117,121`),
   `PaperBroker.equity()` (`agent/src/paper/broker.py:546`), and
   `src.paper.pnl.decision_pnl` (`agent/src/paper/pnl.py:232`). Routes construct a
   `PaperStore(paper_root())` and delegate; no route re-implements ledger replay,
   lineage attribution, or mark-to-market. Journal reads go through
   `src.committee.journal.load_entries` (`agent/src/committee/journal.py:70`), never
   a raw open() of `journal.jsonl`.
3. **Auth consistent with existing routes.** Follow `register_scheduled_routes`
   exactly (`agent/src/api/scheduled_routes.py:505`): a `register_paper_routes(app,
   require_auth=None)` that resolves `require_auth` from the host `api_server` module
   via `sys.modules` and guards every route with `dependencies=[Depends(require_auth)]`.
   Registered from `api_server.py` beside the others (`agent/api_server.py:1011`).
   Loopback stays dev-trusted; the key gates only non-local access — no new auth
   logic.
4. **SSE only if it is cheap; otherwise poll.** The existing SSE pattern
   (`frontend/src/hooks/useSSE.ts`) is bound to *session/swarm/alpha* event streams
   fed by a live executor; there is **no event emitter for paper/journal state**.
   Standing one up is out of scope. **Poll** these read-only endpoints on an interval
   (e.g. 30–60s) via `api.ts` + a store, matching `Runtime.tsx`'s `getLiveStatus`
   polling. Do NOT add an EventSource for paper data in v1.
5. **UI follows existing store/component conventions.** New page is a lazy route in
   `frontend/src/router.tsx` (mirror the `Runtime`/`Reports` entries), data access
   through `frontend/src/lib/api.ts`'s `request<T>` + typed interfaces, charts via
   the existing echarts wrappers (`frontend/src/lib/echarts.ts`,
   `chart-theme.ts`), formatting via `frontend/src/lib/formatters.ts`.
6. **i18n like existing pages.** All user-facing strings go through `react-i18next`
   with keys added to every locale in `frontend/src/i18n/locales/`
   (`en`, `zh-CN`, `ja`, `ko`, `ar` — see `frontend/src/i18n/index.ts`). No hardcoded
   copy; RTL (`ar`) must not break layout.

**Repo invariants still apply** (README §"Repo invariants"): additive and flag-gated
(server behaves byte-identically with no new env set); never invent a price/number
(surface sentinels/stale flags, never fabricate); `docs/` is gitignored so this file
and any doc edits need `git add -f`.

---

## 3. Task decomposition (SDD tasks, strictly sequential)

Four tasks. Each new env var with filesystem/network side effects (none expected
here — reads honor the existing `VIBE_PAPER_ROOT`) would need the conftest guard;
call it out if that changes.

### Task 1 — Paper REST surface (read-only)

**Create:** `agent/src/api/paper_routes.py` (new `register_paper_routes(app,
require_auth=None)`), `agent/tests/test_paper_api.py`.
**Modify:** `agent/api_server.py` — add `from src.api.paper_routes import
register_paper_routes` + `register_paper_routes(app)` in the routes section
(`agent/api_server.py:1011` block).

**Endpoints & response shapes (derived from the real dicts — do not invent fields):**
- `GET /paper/status` → `PaperBroker(store).equity()` verbatim
  (`agent/src/paper/broker.py:546-585`):
  `{ts, cash, positions_value, equity, positions:[{symbol, qty, avg_entry, mark,
  value, unrealized, stale}], stale_positions}`. When no account exists yet, return
  the same shape with `equity=cash=start_cash` (broker `_ensure_account` handles it)
  — but note `.equity()` fetches live marks; see pitfalls.
- `GET /paper/ledger?limit=N` → `list(store.iter_ledger())`
  (`agent/src/paper/store.py:110`), each row
  `{ts, trade_id, symbol, side, qty, fill_price, slippage_paid, fee_paid,
  order_type, decision_id, realized_pnl, note}` (`agent/src/paper/broker.py:20-22,
  299-312`). Return newest-last as stored; apply `limit` as a tail slice.
- `GET /paper/equity` → `list(store.iter_equity())`
  (`agent/src/paper/store.py:117`); each row is a persisted `.equity()` snapshot
  `{ts, cash, positions_value, equity, positions:[...], stale_positions}`
  (`agent/src/paper/tick.py:597-603` writes `persist_row`, which is the broker
  equity dict minus the transient `date`/`already_recorded` keys).
- `GET /paper/pnl/{decision_id}` → `src.paper.pnl.decision_pnl(decision_id, store)`
  verbatim (`agent/src/paper/pnl.py:232-335`):
  `{decision_id, executed, realized_pnl, fees_paid, unrealized_pnl, position_open,
  exit_kind, max_drawdown_pct, summary}`. `decision_pnl` never raises for a missing
  account/decision (resolves to `executed:false`), so no 404 branch is needed;
  validate `decision_id` via the host `_validate_path_param`
  (`agent/api_server.py:772`) as `scheduled_routes` does (`:525`).

**Binding interface to land in the ledger for Task 3's dispatch:** the exact
`register_paper_routes` signature and the four response shapes above (copy them into
`.superpowers/sdd4/progress.md`).

**Tests (socket-free, `TestClient` + `VIBE_PAPER_ROOT` tmp fixture):** mirror
`agent/tests/test_alpha_compare_api.py` — `TestClient(api_server.app,
client=("127.0.0.1", 50000))` for loopback dev-auth bypass. The conftest autouse
guard already pins `VIBE_PAPER_ROOT` to tmp + `VIBE_PAPER_ENABLED=0`
(`agent/tests/conftest.py`); seed a `PaperStore` under that tmp root (append_ledger/
save_positions/append_equity) and assert the endpoints echo the store. **Inject a
`price_fn`** for `/paper/status` so `.equity()` never hits the network in tests
(broker takes `price_fn`; expose a way to pass a fake, or seed positions with a mark
so `_mark_for` short-circuits) — a route test must not open a socket.

**Pitfalls (cite the ledger/code):**
- **Stale flags must be RENDERED, not hidden** — `.equity()` marks unfetchable
  positions at `avg_entry` and sets `stale:true` + `stale_positions` on purpose
  (`agent/src/paper/broker.py:551,595-609`, review Important 1). Pass them through
  untouched; do not coalesce a stale mark into a clean-looking number.
- **`/paper/status` fetches live prices** (`_mark_for` → `price_fn`). A dashboard
  poll that triggers OKX fetches every 30s is a real cost. Acceptable in v1
  (deterministic, no new HTTP path), but note it; do not add caching that could
  serve an invented price.
- **noop ledger rows are real rows** — `order_type=="noop"`, `realized_pnl=None`,
  `qty=0.0`, `fill_price=None` (`agent/src/paper/pnl.py:11-16`). The ledger endpoint
  returns them; the UI (Task 3) must not treat them as fills.

### Task 2 — Journal REST surface (read-only)

**Create/Modify:** either extend `agent/src/api/paper_routes.py` or a sibling
`journal_routes.py` (prefer extending Task 1's module to keep one paper/committee
read surface); `agent/tests/test_journal_api.py`.

**Endpoints & shapes (from `agent/src/committee/journal.py`):**
- `GET /journal/decisions?symbol=&limit=` → `load_entries()`
  (`:70`, oldest-first) filtered/tailed. Each entry:
  `{id, decided_at, symbol, rating, time_horizon, primary_horizon, price_target,
  run_id, status, ref_price, horizons, reflection, reflected_at}` plus the optional
  `stop_loss`/`take_profit`/`position_size_pct` keys that are **only present when
  supplied** (`:151-156`) — the UI must treat them as optional.
  `horizons[h]` = `{raw_return, benchmark_return, alpha, mark_price,
  direction_correct, resolved_at}` (`:261-268`).
- `GET /journal/decisions/{id}` → the single matching entry (404 if absent — do the
  lookup over `load_entries()`; validate `id` with `_validate_path_param`).
- Reflections are the `reflection`/`reflected_at` fields already on each entry — no
  separate endpoint needed; the list carries them.

**Binding interface:** the two routes + entry shape → ledger.

**Tests:** socket-free; point `VIBE_TRADING_COMMITTEE_JOURNAL`
(`agent/src/committee/journal.py:35`) at a tmp file, write entries via
`append_decision`/`resolve_due`/`write_reflection`, assert the endpoints echo them.
Follow `agent/tests/test_committee_journal.py` for fixture style.

**Pitfalls:**
- **Grade only resolved horizons.** Roadmap §1a: `direction_correct`/`alpha` are only
  meaningful where `horizons[h].resolved_at` is present; unresolved horizons must not
  be rendered as a score. Pass `status` and per-horizon presence through so the UI
  shows "pending", never a fabricated 0.
- **Alpha is definitionally 0 for the benchmark asset itself** (`:34,260`); for
  BTC-USDT decisions the UI should lean on raw return. Do not present benchmark-asset
  alpha as edge.

### Task 3 — Dashboard page

**Create:** `frontend/src/pages/TradingOps.tsx`, `frontend/src/pages/__tests__/
TradingOps.test.tsx`, i18n keys in every `frontend/src/i18n/locales/*.json`.
**Modify:** `frontend/src/router.tsx` (lazy route, mirror `Runtime` at
`frontend/src/router.tsx:16,52`), `frontend/src/lib/api.ts` (add `getPaperStatus`,
`getPaperLedger`, `getPaperEquity`, `getPaperPnl`, `getJournalDecisions` +
their TS interfaces mirroring the Task 1/2 shapes), plus a nav entry in the Layout
switcher wherever `Runtime`/`Reports` are listed.

**Panels:**
- **Equity curve** from `GET /paper/equity` (`ts`,`equity`) via the echarts wrapper.
- **Positions table** from `GET /paper/status.positions`: symbol, qty, avg_entry,
  mark, value, unrealized, **stop + take_profits**. Note: `/paper/status` does NOT
  carry `stop`/`take_profits` (those live in `positions.json`, shape
  `{symbol,qty,avg_entry,stop,take_profits:[{price,fraction}],opened_at,
  decision_id}`, `agent/src/paper/broker.py:17-18,638-649`). **Decision:** extend
  Task 1's `/paper/status` positions rows to include `stop`/`take_profits` by merging
  `store.load_positions()` (keyed by symbol) into the equity rows in the route — a
  small, verified join, still no JSONL re-parse. Land this addendum in the ledger so
  Task 3 consumes a known shape. Render `stale:true` rows with a visible badge.
- **Recent decisions with outcomes** from `GET /journal/decisions`: rating, horizon,
  resolved alpha/raw where present, reflection text; "pending" otherwise.
- **Scheduler / event-trigger sidebar** from `GET /scheduled-runs`
  (existing — `agent/src/api/scheduled_routes.py:568`, returns
  `{id,prompt,schedule,next_run_at,status,created_at,config}`) for the registered
  jobs (`committee-run`, `paper-trading-tick`, `decision-journal-reflection`), plus
  the tick/event state from `GET /paper/status`'s companion — expose
  `store.load_tick_state()` (`{last_bar_ts,last_event_trigger_ts,last_price}`,
  `agent/src/paper/store.py:121-141`) via a small `GET /paper/tick-state` added in
  Task 1 for the "recent event triggers + cooldowns" view.

**Tests:** follow `frontend/src/pages/__tests__/Runtime.test.tsx` —
`vi.mock("@/lib/api")`, render, assert panels render seeded shapes and that a
`stale` position shows its badge and a `pending` horizon shows "pending".

**Pitfalls:** stale badges rendered not hidden (carry through from Task 1); noop rows
excluded from the fills view; a lineage-blended PnL `summary` from `decision_pnl`
contains a `note: PnL is position-lifecycle-wide ...` line
(`agent/src/paper/pnl.py:217-225`) — render that caveat line, do not strip it.

### Task 4 — Docs + e2e smoke

**Modify:** `docs/crypto-committee.md` (a "Trading Ops dashboard" subsection: the four
paper endpoints, the journal endpoints, read-only + poll-not-SSE rationale, the
live-price-on-status honest limit), `agent/.env.example` if any knob is added
(none expected), and this file's status. Remember `git add -f` (docs gitignored,
README §Quirks).

**Tests:** an e2e smoke that boots `TestClient`, seeds a tmp paper store + journal,
and hits all endpoints asserting 200 + shape; plus the frontend page test from
Task 3. Include an honest-limits paragraph for the live-mark cost and the
grade-only-resolved rule.

---

## 4. Loop rules application

**Model tiers (README §Model-selection).**
- Task 1: **top-tier** implementer + reviewer. It surfaces money-shaped numbers
  (realized/unrealized PnL, fees, stale marks) and the `decision_pnl` lineage
  attribution; a wrong field mapping mis-states money. Hand-recompute one PnL row.
- Task 2: **mid-tier** — mechanical read/serialize of journal entries; reviewer
  matches diff risk.
- Task 3: **mid-tier** for the React page; **top-tier reviewer pass on the
  REST-shape → UI-consumption seam** (the cross-task seam that bites: a field the UI
  reads that the route never emits).
- Task 4: **mid-tier**; final review is always most-capable.

**What the final (whole-branch) review must trace.** The end-to-end **REST-shape vs
UI-consumption seam**: for every field the page renders, confirm a route actually
emits it with that name/type (positions `stop`/`take_profits` join from Task 3;
`horizons[h]` optionality; stale/noop/lifecycle-wide caveats). Confirm **no mutation
endpoint** slipped in and `archive_and_reset` stays unexposed. Confirm additivity:
with the frontend unbuilt and no new env, server behavior is byte-identical (the
routes are inert until called). Reproduce at least one shape assertion with a live
`TestClient`, not by reading.

**Live-verification recipe (README §Live verification).** After "Ready to merge:
Yes": run a real committee tick or two against a real (or seeded-real) paper account
so `ledger.jsonl`/`equity.jsonl`/`positions.json` and `journal.jsonl` have genuine
rows; `python -m agent.api_server` (or `serve_main`) on loopback; open the dashboard
and **eyeball each panel against the raw files** — equity curve matches
`equity.jsonl` tail, a stale position shows its badge, a pending horizon shows
"pending", the scheduler sidebar lists the three jobs from `GET /scheduled-runs`, a
`/paper/pnl/<real decision_id>` matches the CLI `decision_pnl`. Inspect artifacts,
not just HTTP 200s. Any live-found bug goes through the same fix→re-review loop.

---

## 5. Acceptance criteria

1. Four read-only `GET` paper endpoints + two journal endpoints (+ `/paper/tick-state`),
   each returning the verified shapes above; **zero mutation endpoints**; `paper
   reset` remains CLI-only.
2. No route re-parses JSONL — all reads delegate to `PaperStore`/`pnl`/`broker`/
   `journal.load_entries`.
3. Auth mirrors `register_scheduled_routes`; loopback dev-trust preserved; non-local
   requires the key.
4. Dashboard page renders equity curve, positions (with stops/TPs + **stale badges**),
   recent decisions with outcomes (**pending where unresolved**), and the scheduler/
   event-trigger sidebar; polls (no new SSE); all strings i18n'd across all 5 locales
   incl. RTL.
5. Route tests are socket-free (`TestClient` + tmp `VIBE_PAPER_ROOT`/journal env, live
   marks injected); frontend tests follow the `__tests__` mock-`api` pattern.
6. README §"Definition of done" satisfied: all tasks reviewed + ledgered; final
   review "Ready to merge: Yes" with a reproduced shape check; full suite green with
   zero writes outside tmp; live e2e performed and artifacts eyeballed;
   `docs/crypto-committee.md` updated (with `git add -f`) incl. honest-limits
   (live-mark cost on `/paper/status`, grade-only-resolved horizons).

## 6. Effort estimate

**L**, consistent with roadmap §2.A. Backend Tasks 1–2 are **S each** (thin
serialization over deterministic, already-tested engine functions — the hard logic
exists). Task 3 (page + api.ts + router + i18n×5 + tests) is the **bulk, M**. Task 4
is **S**. Rough split: T1 ~0.5d, T2 ~0.5d, T3 ~2–3d, T4 ~0.5d, plus the loop overhead
(per-task reviews, final review, live verification) the README mandates.
