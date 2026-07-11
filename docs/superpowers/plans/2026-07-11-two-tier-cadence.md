# Two-Tier Cadence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** 2-hourly intraday paper tick (1H bars), env-configurable scheduled committee runs, and a deterministic event trigger that fires ad-hoc committee runs with cooldown.

**Architecture:** Three additive tasks on `feat/two-tier-cadence`. The tick gains an interval mode + per-symbol bar watermark; the committee becomes a scheduled job registered only when its schedule env is set; the event check is pure code inside `run_tick` whose findings the tick job's agent acts on via `run_swarm`.

**Spec:** `docs/superpowers/specs/2026-07-11-two-tier-cadence-design.md` — binding for every env name, default, rule, and shape.

## Global Constraints

- All changes additive; every new env unset ⇒ byte-identical current behavior (`VIBE_PAPER_TICK_INTERVAL` defaults `1D`; `VIBE_COMMITTEE_SCHEDULE` unset ⇒ no job)
- Never invent a price: fetch failures → no fill / no trigger, error recorded
- `tick_state.json` lives in the paper root, atomic writes (existing store pattern)
- Tests socket-disabled; the conftest paper-env guard (f302828) already isolates VIBE_PAPER_ROOT/ENABLED — event/committee envs must be pinned per-test the same way
- Run tests with `.venv/bin/python -m pytest`; commit per task (dispatch authorizes)

---

### Task 1: Intraday (1H) tick mode with bar watermark

**Files:**
- Modify: `agent/src/paper/tick.py` (interval mode, multi-bar loop, watermark), `agent/src/paper/store.py` (tick_state load/save), `agent/src/paper/broker.py` (only if bar-status helper needs interval awareness)
- Modify: `agent/src/api/scheduled_routes.py` (`VIBE_PAPER_TICK_SCHEDULE` for the job's initial registration)
- Test: `agent/tests/test_paper_tick.py` (extend), new cases per spec §4

**Interfaces:**
- Consumes: existing `run_tick`/`evaluate_conditionals`/`bar_status_vs_entry`; PaperStore atomic write pattern
- Produces: `PaperStore.load_tick_state() -> dict` / `save_tick_state(dict)` (shape: `{"last_bar_ts": {symbol: iso}, "last_event_trigger_ts": {symbol: iso}, "last_price": {symbol: float}}` — event keys used by Task 3); `run_tick` honors `VIBE_PAPER_TICK_INTERVAL` (`1D` default | `1H`); in 1H mode `bars_fn(symbol)` returns the list of confirmed 1H bars AFTER the watermark (default impl fetches with interval="1H", filters confirm), evaluated chronologically, watermark = last evaluated bar ts; first-ever tick (no watermark) evaluates ONLY the newest confirmed bar
- Rules: per-bar evaluation identical to today (stop-beats-TP within one bar; gap-at-open); entry-partial-bar skip via existing `bar_status_vs_entry` at 1H granularity; equity snapshot stays one per UTC date; retriable-decision retry unchanged (runs each tick)

**Steps:** failing tests first (spec §4 bullet 1: ordering, watermark advance+persist, first-tick no-backfill, 1H entry-bar skip, 1D regression byte-identical incl. tick_state absent in 1D mode... decide: watermark tracked only in 1H mode — state file simply absent in 1D; pin that); implement; full paper-suite rerun; commit `feat(paper): intraday 1H tick mode with bar watermark`.

### Task 2: Scheduled committee runs

**Files:**
- Modify: `agent/src/api/scheduled_routes.py` (new `_ensure_committee_run_job` mirroring the existing ensure-pattern)
- Modify: `agent/.env.example` (spec §3 block), `docs/crypto-committee.md` (cadence section)
- Test: `agent/tests/test_scheduled_reflection_job.py` (extend or sibling file)

**Interfaces:**
- Produces: job id `committee-run`; registered only when `VIBE_COMMITTEE_SCHEDULE` set AND scheduler enabled; non-clobbering; prompt iterates `VIBE_COMMITTEE_SYMBOLS` (comma list, default `BTC-USDT`), calling `run_swarm` preset `crypto_committee` with `{"target": <sym>, "timeframe": VIBE_COMMITTEE_TIMEFRAME (default "72h swing")}` serially; report run id + final rating per symbol; on failure report and continue
- Envs resolved at REGISTRATION time into the prompt (document: changing symbols env requires deleting the job or editing its prompt — non-clobbering preserved)

**Steps:** failing tests (registration only-when-set; gating; non-clobbering; prompt names every symbol + preset + timeframe); implement; rerun scheduled-routes tests; commit `feat(committee): env-configurable scheduled committee runs`.

### Task 3: Event trigger

**Files:**
- Create: `agent/src/paper/events.py`
- Modify: `agent/src/paper/tick.py` (call events check, extend result), `agent/src/tools/paper_tick_tool.py` (surface triggers), `agent/src/api/scheduled_routes.py` (tick job prompt gains run_swarm follow-up instruction + tool), `agent/.env.example`, `docs/crypto-committee.md`
- Test: `agent/tests/test_paper_events.py` (new)

**Interfaces:**
- Produces (events.py): `check_events(symbols, state, *, price_fn, funding_fn, journal_ref_fn, now, config) -> (triggers, new_state)` pure function; `EventConfig.from_env()` (`VIBE_EVENT_PRICE_MOVE_PCT` default 5, 0=off; `VIBE_EVENT_FUNDING_ABS` default 0.001, 0=off; `VIBE_EVENT_COOLDOWN_H` default 12); trigger dict `{"symbol","reason","metric","value","threshold"}`
- Reference price resolution: last committee decision's execution price for the symbol (ledger fill for that decision, else journal entry ref) → else state's previous-tick `last_price` → else no price trigger this tick (store price for next); funding via the snapshot module's funding fetcher (reuse)
- Watched symbols: open positions ∪ `VIBE_COMMITTEE_SYMBOLS`; cooldown per symbol via `last_event_trigger_ts` in tick_state (Task 1 shape); fetch failure → no trigger + error
- `run_tick` result gains `event_triggers`; paper_tick tool output includes them; tick JOB prompt amended: on non-empty triggers call `run_swarm` per flagged symbol (`crypto_committee`, configured timeframe), never fabricate; job's tool list gains run_swarm
- Prompt-contract test: tick job prompt references paper_tick AND the run_swarm follow-up

**Steps:** failing tests (spec §4 bullet 3 full list: boundaries, reference order, cooldown lifecycle, disabled, fetch failure, atomicity; prompt contract); implement; full paper suite + scheduled tests; commit `feat(paper): deterministic event trigger with cooldown wired to ad-hoc committee runs`.

---

## Self-review notes
- Spec §2.1→T1, §2.2→T2, §2.3→T3; §3 split across T1(.env tick vars? no — T1 touches schedule env in scheduled_routes; .env.example tick block lands with T1... assign: T1 adds VIBE_PAPER_TICK_* to .env.example, T2 adds committee vars, T3 adds event vars).
- tick_state shape defined once (T1) and shared with T3 (event keys) — T1 creates the file schema incl. empty event maps.
- No task touches the journal, translator mapping, or broker fill math.
