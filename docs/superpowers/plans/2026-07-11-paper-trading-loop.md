# Paper-Trading Executor with PnL-Aware Reflection — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically execute journaled committee decisions as paper trades against live OKX prices, account for PnL net of fees/slippage, and feed executed outcomes back into the reflection loop.

**Architecture:** A new `agent/src/paper/` package holds a deterministic, LLM-free portfolio engine (store + broker + translator). An in-process hook after journal append executes decisions; a scheduled daily tick manages conditional orders and mark-to-market. The `decision_journal` tool gains a `pnl` action the reflection officer calls. The decision journal's resolution logic and idempotency are NOT modified; `PortfolioDecision` gains optional typed execution fields passed through to journal entries.

**Tech Stack:** Python 3.12, pydantic (existing), the repo's OKX/ccxt loader plumbing (reuse — no new HTTP clients), pytest (socket-disabled).

**Spec:** `docs/superpowers/specs/2026-07-11-paper-trading-loop-design.md` — binding for all values.

## Global Constraints

- All changes additive; live-execution stack (`agent/src/live/`, `policy.py`, connectors) untouched
- Spot long-only v1: Sell/Underweight with no position = ledger-noted no-op; no shorting
- Never invent a price: fetch failure → no fill, logged, retried next tick
- Env knobs and defaults exactly as spec §5: `VIBE_PAPER_ENABLED=1`, `VIBE_PAPER_START_CASH=100000`, `VIBE_PAPER_SLIPPAGE_BPS=5`, `VIBE_PAPER_FEE_BPS=10`, `VIBE_PAPER_MAX_POSITIONS=3`, `VIBE_PAPER_MAX_SYMBOL_PCT=25`, `VIBE_PAPER_DEFAULT_SIZE_PCT=10`, `VIBE_PAPER_DEFAULT_STOP_PCT=8`, `VIBE_PAPER_ROOT` override
- State dir `~/.vibe-trading/paper/` (or `VIBE_PAPER_ROOT`); atomic writes (tmp+rename, same pattern as swarm store)
- Fill math: buy fill = price × (1 + slippage_bps/10000); sell fill = price × (1 − slippage_bps/10000); fee = fill_notional × fee_bps/10000, deducted from cash on both sides
- Conditional-order gap rule: bar gaps through stop → fill at bar OPEN; stop and TP inside one bar → stop wins (worse outcome)
- Executor idempotent per decision id; execution failures never fail the committee run
- Tests socket-disabled; regression tests cite the driving requirement; run with `.venv/bin/python -m pytest`
- Commit after each task (implementer dispatch authorizes commits)

---

### Task 1: Typed execution fields on PortfolioDecision + journal passthrough

**Files:**
- Modify: `agent/src/committee/schemas.py` (PortfolioDecision, line ~136)
- Modify: `agent/src/committee/journal.py` (`append_decision`, line ~103)
- Modify: `agent/src/swarm/presets/crypto_committee.yaml` (portfolio_manager prompt — minimal amendment)
- Test: `agent/tests/test_committee_schemas.py` (extend), `agent/tests/test_committee_journal.py` (extend)

**Interfaces:**
- Produces: `PortfolioDecision.stop_loss: float | None`, `PortfolioDecision.take_profit: float | None`, `PortfolioDecision.position_size_pct: float | None` (validated 0–100 when present, nullish-coerced like `price_target`); journal entries gain optional keys `stop_loss`, `take_profit`, `position_size_pct` (absent when None — do NOT write nulls, keeps old-entry shape identical)
- Consumed by: Task 4 translator reads these keys off journal entry dicts

**Steps:**
- [ ] Write failing tests: (a) `PortfolioDecision(**old_payload)` without new fields still validates (regression — old submissions must not break); (b) new fields validate: `position_size_pct=125` → ValidationError, `stop_loss="<unavailable>"` → coerced None (mirror existing `_nullish` tests); (c) `append_decision` with a decision carrying the fields writes them into the JSONL entry, and without them writes an entry with NO such keys (byte-shape regression vs existing fixture entries)
- [ ] Run: `.venv/bin/python -m pytest agent/tests/test_committee_schemas.py agent/tests/test_committee_journal.py -q` — new tests FAIL (unknown field under `extra="forbid"`? NO — fields must be declared; expect AttributeError/KeyError shape)
- [ ] Implement: add the three optional fields to `PortfolioDecision` with the `_nullish` validator extended to cover them; `position_size_pct: float | None = Field(default=None, ge=0, le=100)`; thread through `append_decision` (only write keys when value is not None)
- [ ] Amend the PM prompt in `crypto_committee.yaml`: one short paragraph instructing that Buy/Overweight decisions SHOULD include `stop_loss`, `take_profit`, `position_size_pct` in the submitted decision, grounded in the trader's proposal and verified snapshot prices; Hold decisions MAY include stop/TP adjustments; never invent — omit when not determinable
- [ ] Run full files: same pytest command → all pass. Also `.venv/bin/python -m pytest agent/tests/test_crypto_committee_preset.py -q` (prompt edit regression)
- [ ] Commit: `feat(paper): typed execution fields on PortfolioDecision + journal passthrough`

### Task 2: Paper store + broker core (account, market fills, mandates)

**Files:**
- Create: `agent/src/paper/__init__.py`, `agent/src/paper/store.py`, `agent/src/paper/broker.py`
- Test: `agent/tests/test_paper_broker.py`

**Interfaces:**
- Produces (store.py): `PaperStore(root: Path)` with `load_account() -> dict | None`, `create_account(start_cash: float, config: dict) -> dict`, `load_positions() -> list[dict]`, `save_positions(list[dict])`, `append_ledger(entry: dict)`, `iter_ledger() -> Iterator[dict]`, `append_equity(entry: dict)`, `archive_and_reset() -> Path`. Root resolution: `paper_root() -> Path` honoring `VIBE_PAPER_ROOT` else `~/.vibe-trading/paper`. All writes atomic.
- Produces (broker.py): `PaperBroker(store: PaperStore, price_fn: Callable[[str], PriceQuote] | None = None)` where `PriceQuote = {"price": float, "ts": str}`; methods:
  - `market_buy(symbol: str, notional_usdt: float, *, decision_id: str, stop: float | None, take_profit: float | None) -> dict` (returns ledger entry; raises `MandateViolation` / `PriceUnavailable`)
  - `market_sell(symbol: str, fraction: float, *, decision_id: str, reason: str) -> dict | None` (None when no position)
  - `set_risk(symbol: str, *, stop: float | None, take_profit: float | None) -> bool`
  - `equity(mark_prices: dict[str, float] | None = None) -> dict` (cash, positions_value, equity, per-position unrealized)
  - Config dataclass `BrokerConfig.from_env()` reading the spec §5 envs
- Default `price_fn` wraps the existing snapshot fetch path (`agent/src/tools/crypto_snapshot_tool.py`'s last-price fetcher — import the module-level fetch function, do not duplicate HTTP code)
- Position dict shape (binding for Tasks 3–7): `{"symbol", "qty", "avg_entry", "stop", "take_profits": [{"price", "fraction"}], "opened_at", "decision_id"}`
- Ledger entry shape (binding): `{"ts", "trade_id", "symbol", "side": "buy"|"sell", "qty", "fill_price", "slippage_paid", "fee_paid", "order_type": "market"|"stop"|"take_profit"|"noop", "decision_id", "realized_pnl": float|None, "note": str|None}`
- Mandates enforced inside `market_buy`: reject when open-position count ≥ MAX_POSITIONS and symbol not already held (`MandateViolation`); clamp notional so symbol exposure ≤ MAX_SYMBOL_PCT% of current equity (clamp, don't reject; record clamped notional in note)

**Steps:**
- [ ] Write failing tests with a fixture `price_fn` and `tmp_path` root (`monkeypatch.setenv("VIBE_PAPER_ROOT", ...)`): exact fill math (buy 10_000 USDT at price 100, slip 5bps, fee 10bps → fill 100.05, qty 99.9500…, fee 10.0005 — assert to 8 decimals); sell realizes pnl net of both-side costs; 4th symbol rejected; oversize clamped to 25% equity; `PriceUnavailable` when price_fn raises → NO ledger entry, positions unchanged; account auto-create with START_CASH; atomicity (write interrupted via monkeypatched os.replace → old state intact); realized_pnl formula: (sell_fill − avg_entry) × qty_sold − sell_fee (buy fee already reduced cash at entry)
- [ ] Run: `.venv/bin/python -m pytest agent/tests/test_paper_broker.py -q` → FAIL (module not found)
- [ ] Implement store.py then broker.py to the interfaces above; follow the swarm store's tmp+rename pattern for atomic writes
- [ ] Run → all pass; also full-file rerun plus `agent/tests/test_crypto_snapshot_tool.py -q` (import-reuse regression)
- [ ] Commit: `feat(paper): paper store + broker core with mandates and exact fill math`

### Task 3: Conditional orders + daily tick

**Files:**
- Create: `agent/src/paper/tick.py`
- Modify: `agent/src/paper/broker.py` (add `evaluate_conditionals`)
- Test: `agent/tests/test_paper_tick.py`

**Interfaces:**
- Consumes: `PaperBroker`, position/ledger shapes from Task 2; daily OHLC via the journal's loader path (`agent/src/tools/committee_journal_tool.py` `_loader_fetch_bars` — import and reuse; it already routes okx→ccxt)
- Produces: `run_tick(store: PaperStore | None = None, *, bars_fn=None, price_fn=None, now=None) -> dict` returning `{"conditional_fills": [...], "equity_snapshot": {...}, "errors": [...]}`; `PaperBroker.evaluate_conditionals(symbol: str, bar: dict) -> list[dict]` where bar = `{"open","high","low","close","ts"}`
- Rules (binding, from spec): stop triggers when `bar["low"] <= stop`; fill at `min(bar["open"], stop)` if `bar["open"] <= stop` (gap-through → open) else at stop. TP fraction triggers when `bar["high"] >= tp_price`; fill at tp_price (or bar open if gapped above). Both stop and any TP inside one bar → stop executes, TPs skipped. Slippage NOT applied to conditional fills (bar prices already conservative); fee applies.
- Tick is same-day idempotent: `equity.jsonl` keyed by date; a second `run_tick` same UTC date re-evaluates nothing already filled (conditional fills are recorded per position and stops/TPs removed once executed) and overwrites nothing (skip equity append if today's snapshot exists)

**Steps:**
- [ ] Write failing tests with fixture bars: stop hit exactly; gap-through stop fills at open; TP partial fill (fraction 0.5 halves qty, remaining TP list preserved); stop+TP same bar → stop wins, position fully closed, no TP fill; no-trigger bar → no fills; bars_fn failure → error recorded, position untouched; same-day double tick → single equity snapshot, no duplicate fills
- [ ] Run: `.venv/bin/python -m pytest agent/tests/test_paper_tick.py -q` → FAIL
- [ ] Implement `evaluate_conditionals` + `run_tick`
- [ ] Run → pass; rerun `agent/tests/test_paper_broker.py -q`
- [ ] Commit: `feat(paper): conditional stop/TP evaluation + daily mark-to-market tick`

### Task 4: Decision→order translator with idempotency

**Files:**
- Create: `agent/src/paper/translator.py`
- Test: `agent/tests/test_paper_translator.py`

**Interfaces:**
- Consumes: journal entry dicts (Task 1 keys incl. optional `stop_loss`/`take_profit`/`position_size_pct`; legacy entries without them — use the REAL 2026-07-10 entry shape as a fixture: `{"id": "dec_...", "symbol": "BTC-USDT", "rating": "Hold", "price_target": 65000, ...}`); `PaperBroker` from Task 2
- Produces: `execute_decision(entry: dict, broker: PaperBroker) -> dict` returning `{"decision_id", "actions": [ledger entries or noop records], "skipped": str | None}`
- Mapping (binding; the REAL 5-tier enum is `Buy | Overweight | Hold | Underweight | Sell` — schemas.py `parse_rating`): Buy no-position → `market_buy` sized `position_size_pct` (default env DEFAULT_SIZE_PCT) percent of current equity; Overweight no-position → same but at HALF that size; stop = `stop_loss` or entry×(1−DEFAULT_STOP_PCT/100); tp = `take_profit` or `price_target` (single TP, fraction 1.0). Buy/Overweight with existing position → add up to symbol cap (Overweight adds at half sizing). Hold → `set_risk` with any provided stop/TP; nothing else. Underweight → `market_sell` fraction 0.5; Sell → fraction 1.0; either with no position → ledger noop entry (`order_type="noop"`, note="sell signal with no position"). Rating read case-insensitively (journal stores "Hold").
- Idempotency: before acting, scan ledger for `decision_id` == entry id → if found, return `{"skipped": "already executed"}` with no actions. Noop entries count as executed.
- Kill switch: `VIBE_PAPER_ENABLED` falsy → `{"skipped": "paper trading disabled"}`

**Steps:**
- [ ] Write failing table-driven tests: full rating×position matrix (10 cells: 5 ratings × {no position, existing long}); defaults applied when fields absent (assert stop = entry×0.92 to 8 decimals); real 2026-07-10 HOLD fixture → `set_risk` not called (no typed stop/TP present) and result has empty actions; same entry twice → second call skipped, ledger length unchanged; disabled env → skipped, no account auto-created
- [ ] Run → FAIL; implement; run → pass; rerun broker tests
- [ ] Commit: `feat(paper): decision-to-order translator with per-decision idempotency`

### Task 5: Execution hook + scheduled tick job

**Files:**
- Modify: `agent/src/tools/committee_journal_tool.py` (post-append hook seam — where `action="append"` succeeds)
- Modify: `agent/src/api/scheduled_routes.py` (register `paper-trading-tick` job next to `decision-journal-reflection`, same gating/non-clobbering pattern)
- Test: `agent/tests/test_paper_hook.py`
- Modify: `agent/tests/test_scheduled_research_registration.py` or the Phase 6 test file for job registration (extend, do not weaken)

**Interfaces:**
- Consumes: `execute_decision` (Task 4), `run_tick` (Task 3)
- Produces: after a successful journal append, call `maybe_execute_paper(entry: dict) -> dict | None` (new function in `agent/src/paper/hook.py` — create it here): returns None fast when `VIBE_PAPER_ENABLED` falsy; otherwise translate+execute, catching ALL exceptions and returning `{"error": str}` — the journal tool's response gains an optional `paper_execution` key with the result; the append result itself is unchanged on executor failure (failure isolation)
- Scheduled job: id `paper-trading-tick`, schedule `30 0 * * *` (after the 00:00 reflection job), prompt instructing exactly one `bash`-free tool path — give the scheduled agent a direct instruction to run the CLI? NO — mirror the reflection job's pattern: the job prompt tells the agent to call the `decision_journal` tool? Paper tick is not an agent tool; instead register the job with a prompt that runs `paper tick` via the existing scheduled-research executor's prompt→agent path using the `bash` tool: `.venv/bin/vibe-trading paper tick` is NOT acceptable (env-dependent path). Resolution (binding): expose tick as a lightweight agent tool `paper_tick` (`agent/src/tools/paper_tick_tool.py`, wraps `run_tick`, no params) so the scheduled job prompt is "call the paper_tick tool once and report its summary" — same shape as the reflection job calling `decision_journal`. Create that tool file in this task.
- Non-clobbering registration gated on `VIBE_TRADING_ENABLE_SCHEDULER` AND `VIBE_PAPER_ENABLED` (both required)

**Steps:**
- [ ] Write failing tests: journal append with executor monkeypatched to raise → append still succeeds and response carries `paper_execution: {"error": ...}` (isolation); append with paper disabled → no `paper_execution` key... (decide: key absent when disabled — assert that); `paper_tick` tool discovered by registry (name match, mirrors Task 5 of phase one's discovery test); job registered only when both envs set; user-edited schedule preserved on re-registration
- [ ] Run → FAIL; implement `hook.py`, `paper_tick_tool.py`, journal-tool seam (minimal diff), scheduled registration
- [ ] Run → pass; rerun `agent/tests/test_committee_journal.py -q` (journal behavior unchanged)
- [ ] Commit: `feat(paper): post-journal execution hook + scheduled paper-trading-tick job`

### Task 6: PnL action + reflection integration

**Files:**
- Modify: `agent/src/tools/committee_journal_tool.py` (add `action="pnl"`)
- Create: `agent/src/paper/pnl.py`
- Modify: `agent/src/swarm/presets/crypto_committee.yaml` (reflection_officer prompt — minimal amendment)
- Test: `agent/tests/test_paper_pnl.py`

**Interfaces:**
- Produces (pnl.py): `decision_pnl(decision_id: str, store: PaperStore | None = None, mark_price_fn=None) -> dict`: `{"decision_id", "executed": bool, "realized_pnl", "fees_paid", "unrealized_pnl", "position_open": bool, "exit_kind": "stopped"|"took_profit"|"closed_by_sell"|"open"|"not_executed", "max_drawdown_pct": float | None, "summary": str}` — `summary` is a ≤5-line human block the reflection officer quotes; `max_drawdown_pct` from equity snapshots while the position was open (None when unavailable — never invented)
- `decision_journal` tool `action="pnl"` params: `decision_id` (or `symbol` → most recent executed decision for it); returns the summary block; when paper account absent or decision unexecuted → explicit `"not executed — no paper-trading data"` (instructive, not an error)
- Reflection prompt amendment: when reflecting on a resolved decision, additionally call `decision_journal action=pnl decision_id=<id>`; weigh executed outcome vs directional outcome; name execution lessons (stop placement, sizing) separately from thesis lessons; if pnl reports not-executed, reflect on direction only
- Regression: existing `resolve_due`/`reflect`/`lessons` behavior byte-identical when paper account doesn't exist

**Steps:**
- [ ] Write failing tests: fixture ledger → exact realized/fees/unrealized numbers; exit_kind for each termination path (stop fill, tp fill(s) exhausting qty, sell close, still open, never executed); tool action `pnl` by id and by symbol; unexecuted decision → instructive message; journal regression with no paper root (monkeypatch VIBE_PAPER_ROOT to empty tmp dir)
- [ ] Run → FAIL; implement; run → pass; rerun `agent/tests/test_committee_journal.py agent/tests/test_crypto_committee_preset.py -q`
- [ ] Commit: `feat(paper): decision-level PnL action + PnL-aware reflection prompt`

### Task 7: CLI + config + docs

**Files:**
- Modify: `agent/cli/_legacy.py` (add `paper` subcommand next to existing subcommand wiring; follow the `provider`/`memory` subcommand pattern)
- Modify: `agent/.env.example` (spec §5 block, commented where optional)
- Modify: `docs/crypto-committee.md` (new "Paper-trading loop" section), `docs/minimax-migration-notes.md` (one-line pointer)
- Test: `agent/tests/test_paper_cli.py`

**Interfaces:**
- Consumes: store/broker/tick/pnl from Tasks 2–6
- Produces: `vibe-trading paper status` (equity, cash, positions w/ unrealized + stops/TPs, mandate headroom), `paper ledger [--limit N] [--symbol S]`, `paper tick`, `paper reset --confirm` (archives to `archive-<ts>/` via `store.archive_and_reset()`; refuses without `--confirm`)
- Docs section covers: how the loop runs end-to-end (committee → hook → ledger → tick → pnl → reflection), all env knobs with defaults, the honest-limits paragraph from spec §6 (synthetic fills; daily-bar approximation; long-only), and how to read `paper status`

**Steps:**
- [ ] Write failing CLI tests (invoke the command functions directly with a fixture account, capture output): status shows equity and mandate headroom; ledger --limit truncates; reset without --confirm refuses and exits nonzero; tick prints the run_tick summary
- [ ] Run → FAIL; implement CLI; write docs + .env.example block
- [ ] Run → pass; full sweep: `.venv/bin/python -m pytest agent/tests/test_paper_broker.py agent/tests/test_paper_tick.py agent/tests/test_paper_translator.py agent/tests/test_paper_hook.py agent/tests/test_paper_pnl.py agent/tests/test_paper_cli.py agent/tests/test_committee_journal.py agent/tests/test_committee_schemas.py agent/tests/test_crypto_committee_preset.py -q`
- [ ] Commit: `feat(paper): paper CLI, config reference, and docs`

---

## Self-review notes (resolved inline)

- Spec §3.3 "hook at the journal-append seam" → bound to `committee_journal_tool` append path (Task 5) with the tool response carrying the result; committee-run failure isolation tested.
- Scheduled tick needed an agent-callable surface → `paper_tick` tool (Task 5), mirroring how the reflection job calls `decision_journal`.
- Type consistency: position/ledger dict shapes defined once in Task 2 and referenced by Tasks 3–7; `PriceQuote` only used inside broker.
- Spec coverage: §3.1→Task 2+3, §3.2→Task 1+4, §3.3→Task 5, §3.4→Task 6, §3.5→Task 7, §4 tests distributed per task, §5→Task 7.
