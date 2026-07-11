# Post-Deployment Optimization Roadmap

Compiled at the start of the live 72-hour test run (branch `main`; all three
feature PRs merged — MiniMax M3 migration, paper-trading loop, two-tier cadence).
The run: scheduled `crypto_committee` every 2h on BTC-USDT, a 2-hourly 1H-bar
paper tick, deterministic event triggers, decisions journaled and paper-traded
automatically, reflections resolving at 24h/72h/7d horizons.

This roadmap is **evidence-gated**: most directions wait on the metrics the
72-hour run will actually produce. Section 1 is the checklist that reads those
metrics; Section 2 is the prioritized candidate list (each item cites the ledger
or doc it came from — nothing here is invented); Section 3 is what we are
deliberately not building yet and why.

Every ledger/doc citation below points to one of:
`.superpowers/sdd/progress.md` (phase 1, MiniMax migration),
`.superpowers/sdd2/progress.md` (phase 2, paper trading),
`.superpowers/sdd3/progress.md` (phase 3, two-tier cadence),
`docs/minimax-migration-notes.md`, `docs/crypto-committee.md`, or the two specs
under `docs/superpowers/specs/`.

---

## 1. After the 72-hour run: what to evaluate

Read the artifacts, then let the thresholds below route which direction (A–D or
an engine-hygiene batch) is worth doing first. Artifacts:

- `~/.vibe-trading/committee/journal.jsonl` — one JSON object per decision
  (schema in `docs/crypto-committee.md#journal-entry-format`).
- `~/.vibe-trading/paper/{ledger.jsonl, equity.jsonl, positions.json, account.json}`.
- Operational telemetry: `llm_gate_wait` events (`agent/src/swarm/worker.py`),
  backoff retry logs (`agent/src/providers/backoff.py`),
  `VIBE_RUN_TOKEN_BUDGET_WARN` warnings (`agent/src/core/token_budget.py`),
  scheduled-job dispatch logs.

### 1a. Decision quality (journal.jsonl)

- [ ] **Per-horizon `direction_correct` rate and mean `alpha`** at 24h / 72h / 7d,
  read only from *resolved* entries (`horizons.<h>.resolved_at` present). Over a
  72h run only the 24h horizon (and possibly 72h) will have resolved; 7d stays
  `pending` — do **not** grade on unresolved horizons.
  - *Gates:* whether tiering (D) risks quality. If M3-only 24h `direction_correct`
    is already weak/noisy, do **not** introduce a cheaper quick-tier model until
    there is a baseline to compare against (D depends on a journal A/B, per
    `docs/minimax-migration-notes.md` transition protocol).
  - *Honest caveat (from the migration notes):* ~12 runs/day for 3 days on one
    symbol is an **operational smoke test, not statistical proof** — a few lucky
    calls swing the rate. Treat quality signals as directional only.

### 1b. Executed money (paper ledger + equity)

- [ ] **Realized PnL net of fees**, from `ledger.jsonl` `realized_pnl` + `fee_paid`
  (recall `realized_pnl` is net of *exit* fees only; entry fee is in `fees_paid`
  separately — `docs/crypto-committee.md#pnl-aware-reflection`).
- [ ] **Fee/slippage drag vs gross alpha:** sum `fee_paid`+`slippage_paid` across
  the ledger against gross realized PnL.
  - *Gates:* cadence/turnover work. **If fees eat >30% of gross alpha**, the
    committee is over-trading relative to edge — do cadence/turnover tuning
    (coarser committee schedule, higher event thresholds, `VIBE_EVENT_COOLDOWN_H`
    up) **before** any dashboard (A) or backend (B) work. Turnover is the lever
    that directly changes this ratio; a dashboard only visualizes it.
- [ ] **Stop/TP hit quality:** in `ledger.jsonl`, count `order_type` in
  {`stop`,`take_profit`} and inspect whether stops fired on the entry-day-skipped
  bar boundary or gapped through (fill-at-open cases, per fill-math rules). Check
  the `entry-day bar skipped` notes are appearing (confirms the ≤2h unprotected
  window is the only gap, per `sdd3` T-cadence honest limit).
- [ ] **Event-trigger usefulness:** cross-reference tick-result `event_triggers`
  against journal entries — **did a trigger precede a decision that turned out
  directionally correct?** Count spurious triggers (re-entry false positives, the
  known `last_price` staleness case in `sdd3` T3).
  - *Gates:* whether the event path stays on. If triggers fired but never preceded
    a better-than-scheduled decision (or fired spuriously), raise
    `VIBE_EVENT_PRICE_MOVE_PCT` / `VIBE_EVENT_FUNDING_ABS` or lengthen cooldown —
    cheaper than any feature work, and it directly cuts the "~2 extra runs/symbol/
    day" quota cost (`docs/crypto-committee.md` event honest-limit).

### 1c. Operational health (telemetry)

- [ ] **429 / throttle count** and **`gate_wait_seconds` distribution** across the
  run. At 12 runs/day, ≤3 runs land in any clock-aligned 5-hour window
  (~2–2.4M tokens, ≈20% of even Plus's per-window budget —
  `docs/minimax-migration-notes.md` "Token Plan quota mechanics").
  - *Gates:* tiering (D) priority. **Zero 429s and near-zero gate-wait at 12×/day
    → tiering is optional** (a cost/wall-clock nicety, not a reliability need).
    **Throttled (429s, or gate-wait accumulating into layer-deadline timeouts) →
    tiering becomes a priority** (M2.7-highspeed on the 11 quick seats cuts tokens
    and concurrency pressure). Also note the cross-run contention caveat: N
    simultaneous runs share `VIBE_LLM_MAX_CONCURRENT` (Phase 2 known limitation).
- [ ] **Tokens/run vs the quota bar:** compare per-run token totals against the
  live console usage bar and any `VIBE_RUN_TOKEN_BUDGET_WARN` fires. Watch the 2×
  billing cliff — keep committee context under 512K tokens
  (`docs/minimax-migration-notes.md`).
- [ ] **Failed runs / failed tasks / retry counts:** any run that errored, any
  task that exhausted `VT_STREAM_RETRY_MAX`. Window-exhaustion 429s exhaust the
  90s retry cap by design and fail the task — expected, but count them.
  - *Gates:* cutover confidence. The migration-notes cutover bar is "zero failed
    runs in the last 5 days" — a 72h run with failures means the integration
    hygiene batch (below) outranks feature work.
- [ ] **Reflection quality:** read the `reflection` text on resolved entries. Are
  lessons **specific** (cite realized return/alpha at the primary horizon, name
  the execution failure) rather than generic? Is the **execution-vs-thesis
  separation** working (a "direction right, stop too tight" lesson looks different
  from a "thesis wrong" lesson — `agent/src/paper/pnl.py` +
  `reflection_officer` prompt, `sdd2` T6)?
  - *Gates:* the `VIBE_LESSONS_TO_MANAGER` A/B and whether PnL-aware reflection is
    earning its prompt-size cost. If lessons are vague, tune the prompt before
    adding surface area.

---

## 2. Candidate directions

### Priority table

| # | Direction | Effort | Gated on | Value |
|---|---|---|---|---|
| A | Trading Ops web dashboard | **L** | user still wants it after seeing raw artifacts; needs a paper REST surface (none exists) | High — largest UX gap; makes 1a–1c legible without CLI/JSONL spelunking |
| C | Live-probe completions (0.2–0.4) + probe URL fix + Path B replay | **S**–**M** | live key in hand (user has it); D depends on probe 0.2 | High-leverage, low effort; unblocks D and de-risks Path A/B assumptions |
| D | Model-tiering activation (M2.7 on 11 quick seats) | **S** (config) + **M** (validation) | probe 0.2 (does the key cover M2.7?) + §1c throttling evidence + §1a quality baseline | High if throttled (§1c) — cuts cost + wall-clock; neutral if never throttled |
| E | Engine-hygiene batch (real ledger minors) | **S** each | mostly none (fix-later backlog); a few gated on live evidence | Medium — correctness/robustness debt paydown |
| B | OKX demo-trading backend | **L** | internal-broker journal showing a track record **and** a deliberate `policy.py` relaxation decision | High long-term, but earned — not now |

Ordering rationale: **C before D** (C's probe 0.2 is D's precondition and C is
tiny). **A gated on the user's reaction to raw artifacts** — if the CLI
(`vibe-trading paper status/ledger`, `decision_journal action=list`) is enough,
the L-effort dashboard waits. **B last** — it is the designed endgame but
explicitly evidence-gated.

### A. Trading Ops web dashboard (largest gap, user-requested)

The React frontend (`frontend/src/pages/` — `Agent.tsx`, `RunDetail.tsx`,
`Runtime.tsx`, `Home.tsx`, `Reports.tsx`, etc.) has chat, swarm status cards, and
run detail, but **no panels** for: (1) the **paper account** — equity curve from
`equity.jsonl`, open positions with stops/TPs from `positions.json`, the fill
`ledger.jsonl`, per-decision PnL from `decision_pnl`; (2) the **decision journal**
— decisions, realized alpha per horizon, reflections/lessons from `journal.jsonl`;
(3) **scheduler health** — registered jobs (`committee-run`,
`paper-trading-tick`, `decision-journal-reflection`), last-fired times, recent
event triggers and their cooldowns. This requires a **small REST surface for the
paper engine, which deliberately does not exist today** (the engine is CLI/tool-
only; confirmed — `agent/src/api/` has only `scheduled_routes.py`, no paper
routes), plus one dashboard page reusing the existing store/SSE patterns. Effort
**L**: the read-only REST endpoints are straightforward over the existing
deterministic engine (`agent/src/paper/{store,pnl}.py`, `committee/journal.py`),
the frontend page is the bulk. Gated on the user still wanting it after seeing the
raw artifacts from this run — if the CLI views suffice, defer.

### B. OKX demo-trading backend

The paper broker interface is **deliberately connector-shaped** so the existing
`okx-paper-trade` profile (a real simulated matching engine) can replace the
internal fill simulator — the designed upgrade path
(`docs/superpowers/specs/2026-07-11-paper-trading-loop-design.md` §1 out-of-scope
and §6; `docs/crypto-committee.md#honest-limits`). This is the endgame for
realistic fills (real order book, partial fills) but is **gated on evidence** (the
internal-broker journal must first show a track record worth upgrading) **and on a
deliberate `policy.py` relaxation** — the live-execution stack (`agent/src/live/`,
`policy.py`, `order_guard.py`, `sdk_order_gate.py`) is currently completely
separate and untouched, by design. Not a 72h-window decision. Effort **L**.

### C. Live-probe completions + Path B (`docs/minimax-migration-notes.md`)

Only **probe 0.1 / Path A** is verified live (against `api.minimaxi.com/v1`,
MiniMax-M3, reasoning_split shape pinned by regression tests). Still open:

- **Probes 0.2–0.4 never run with the live key** — 0.2 (model coverage of the
  Token Plan key), 0.3 (reasoning round-trip shapes on the Anthropic surface),
  0.4 (concurrency/429 behavior; grounds the backoff params in reality). The
  Findings table in the migration notes is still a template.
- **Probe-script URL bug:** `scripts/minimax_probe.py` hardcodes
  `api.minimax.io` (lines 33–34) while the user's account lives on
  `api.minimaxi.com` — running the probes as-shipped hits the wrong host. Fix
  before running (**S**).
- **Path B reasoning replay is unimplemented/unverified** — the `/anthropic`
  path builds a stock `ChatAnthropic` but nothing translates OpenAI-format
  `reasoning_content` history into Anthropic `thinking` content blocks, so M3
  reasoning is most likely **not** replayed across tool turns on Path B
  (migration notes, Phase 1 known limitations). Only matters if Path B ever wins.
- **Path A `reasoning_details` type assumption:** capture/accumulate/replay treat
  it as a string; the *resolved* live finding says M3 returns a typed **list** on
  `api.minimaxi.com` — the adapter already wraps/replays correctly per the pinned
  tests, but probe 0.3 on the live key is the final confirmation
  (migration notes, Phase 1). Also: **truncated-output edge** — the T0 probe
  regex misses an unclosed `<think>` tag (`.superpowers/sdd/progress.md` T0).

Effort **S–M**, high leverage: probe 0.2 is D's precondition, and 0.3/0.4 retire
the last unverified assumptions in the provider layer. Gated only on the live key
(in hand).

### D. Model-tiering activation

`VIBE_DEEP_MODEL` / `VIBE_QUICK_MODEL` exist and are wired into
`crypto_committee.yaml` (2 deep seats: `research_manager`, `portfolio_manager`; 11
quick seats), but the user runs **everything on MiniMax-M3** (both unset → global
model). Activating **M2.7-highspeed on the 11 quick seats** would cut per-run cost
and wall-clock substantially. Gated on: **probe 0.2** (does the Token Plan key
cover M2.7 at all — same-provider constraint means the model must exist under
`LANGCHAIN_PROVIDER=minimax`, `docs/minimax-migration-notes.md` Phase 3) **plus a
quality comparison via the journal** (does swapping quick seats to a faster model
degrade `direction_correct`/alpha vs the M3-only baseline this run establishes).
Config change is **S**; the journal A/B validation is **M**. Priority set by §1c:
optional if never throttled, priority if throttled.

### E. Engine-hygiene batch (real deferred findings from the ledgers)

Grouped from the actual ledgers/docs — each line cites its source. None invented.

**E1. Provider / concurrency layer** (`.superpowers/sdd/progress.md`,
`docs/minimax-migration-notes.md`):
- **Gate-wait telemetry under-reports** — reports only the final attempt's wait;
  failed-attempt waits are dropped, so cumulative queue time is undercounted
  (`sdd` T2). Matters for reading §1c gate-wait numbers accurately. **S**, gated
  on §1c showing gate-wait is non-trivial.
- **Retry-After has no jitter** — the `Retry-After` path returns the bare value;
  a shared `Retry-After: 30` causes thundering-herd re-entry across concurrent
  workers (`sdd` T2). **S**, gated on §1c observing shared Retry-After 429s.
- **`compute_layer_deadline` hidden env dependency** — reads
  `VIBE_LLM_MAX_CONCURRENT` directly; could take the cap as a param for
  testability (`sdd` T2 minor). **S**.
- **Test hermeticity** — deadline tests read `VIBE_LLM_MAX_CONCURRENT` without
  pinning env (not hermetic if a dev shell exports it); the post-merge backlog
  calls for autouse `delenv` fixtures (`sdd` T2 minor + "Post-merge hygiene
  backlog"). **S**.
- **Path A `top_p` non-fire** — `.env.example` sets `LANGCHAIN_TEMPERATURE=1.0`
  explicitly, so the clamp that would set `top_p=0.95` never fires; server-side
  default applies (functionally equivalent, documented) (`sdd` T1;
  migration-notes Phase 7 table). Doc-only, no action unless temperature is
  retuned.

**E2. Preset / debate / tiering** (`.superpowers/sdd/progress.md`):
- **`inspect_preset` error contract for debate misconfig** — post-merge hygiene
  backlog item (`sdd` "Post-merge hygiene backlog"; `sdd` T3 minors: no tests for
  `${VAR:-}` empty-default / malformed `${VAR` / `${}` / nested forms). **S**.
- **Malformed participant dict raises raw `KeyError` not `ValueError`** in
  `_expand_debate` (`sdd` T4). **S**.
- **`entry_inputs` grounding dropped for rounds≥2** — risk rotation r2+ lacks
  `research_plan` in `input_from` (documented) (`sdd` T4). **S–M**, only bites if
  `VIBE_RISK_ROUNDS≥2` is used.
- **`_resolve_model_name` strip inconsistency** — unreachable via YAML today
  (`sdd` T3 minor). Trivial.

**E3. Grounding / anti-hallucination tools** (`.superpowers/sdd/progress.md`):
- **`grounding.py` private `_detect_market` import** — post-merge hygiene backlog
  (`sdd` backlog; also `sdd` T6: non-crypto market label always `"us_equity"`,
  unread today). **S**.
- **`stats_24h` sentinel loses detailed reason** on a shared-ticker failure
  (`sdd` T5). **S**.
- **Index price uses BTC-USD index vs USDT-margined mark** — labeled, a semantic
  nicety (`sdd` T5). Doc-only.
- **Prompt-contract test is whole-file substring, not per-seat** (`sdd` T5). Test
  quality. **S**.

**E4. Journal / benchmark** (`.superpowers/sdd/progress.md`):
- **`_loader_fetch_bars` → `fetch_ohlcv_with_fallback` dedup** — the benchmark
  fallback loop duplicates the journal's loader routing (journal path
  deliberately untouched) (`sdd` T6 + backlog). **S**.
- **Idempotency test drives `journal.resolve_due` directly** rather than real
  dispatch paths (`sdd` T6 minor). Test fidelity.

**E5. Paper engine** (`.superpowers/sdd2/progress.md`,
`docs/crypto-committee.md`):
- **`price_target` not coerced at the tool layer** — may journal literal strings
  like `"n/a"`; the TP fallback tolerates it, but coercing at append would be
  cleaner (`sdd2` T1). **S**.
- **JSONL append is read-rewrite O(n)** — fine at paper volume, single-process
  lock only; revisit if volume grows or multi-process access is ever needed
  (`sdd2` T2). **M**, gated on volume evidence (unlikely to bind this run).
- **`default_bars_fn` / `default_stop` edge cases** — `default_bars_fn`
  duplicates loader routing and its live path is untested (`sdd2` T3); add-path
  default stop overwrites a pre-existing stop from an incremental fill, and a
  double price fetch on add (sizing vs fill) (`sdd2` T4). **S–M**; the live-path
  test gap is worth closing given this run exercises it.
- **Duplicated `VIBE_PAPER_ENABLED` truthiness helper in 3 files** — drift risk
  (`sdd2` T5). **S**. Plus `_paper_enabled()` check sits outside the hook's try
  (theoretical) and `BrokerConfig.enabled` is a dead field with narrower
  truthiness than the enforced rule (`sdd2` T5, T7). **S**.
- **Double `load_positions` in the sell path** (`sdd2` T3). Trivial.
- **Missing tests:** no `Overweight`+explicit-pct test (`sdd2` T4); no explicit
  anti-truncation test (substring asserts protect indirectly) (`sdd2` T7). **S**.

**E6. Cadence / event trigger** (`.superpowers/sdd3/progress.md`,
`docs/crypto-committee.md`):
- **`last_price` staleness across watched-set gaps** — a symbol that leaves and
  rejoins the watched set can fire one spurious trigger vs a stale price;
  undocumented staleness pruning (`sdd3` T3; documented as a known reference-price
  semantic in `crypto-committee.md`). **S–M**, gated on §1b seeing spurious
  triggers. A `last_price` staleness / pruning knob is the fix.
- **`last_price` writer asymmetry in 1H mode** — bar close vs live price (`sdd3`
  T3). **S**.
- **Scheduled-job deletion re-registration tombstone** — a deleted `_ensure_*`
  job re-registers on restart (the documented "delete once, restart" upgrade
  dance for `paper-trading-tick`); a tombstone would make deletion stick
  (`sdd` "Post-merge hygiene backlog"; upgrade note in
  `docs/crypto-committee.md#event-trigger`). **M**.
- **Funding-regime / event-threshold cooldown knobs** — `VIBE_EVENT_COOLDOWN_H`,
  `VIBE_EVENT_PRICE_MOVE_PCT`, `VIBE_EVENT_FUNDING_ABS` are the turnover levers
  §1b routes to; no code change needed, just tuning (`crypto-committee.md`
  event-trigger section). **S** (config).
- **Prose-prompt target parsing** — incidental hyphenated tokens after "on" can
  parse as targets; mitigated by identity-anchor fail-fast + structured-channel
  precedence (`sdd3` T2). Low risk given mitigations.

---

## 3. Deliberately not doing (yet)

- **Live real-money execution.** The `policy.py` / `agent/src/live/` guard stack
  stays untouched until the internal-broker journal *earns* it (a real track
  record) **and** a deliberate risk-profile + policy-relaxation decision is made.
  Committee produces journaled typed decisions and paper fills only — live order
  execution was out of scope in all three phases
  (`.superpowers/sdd/progress.md` global constraints;
  `docs/superpowers/specs/2026-07-11-paper-trading-loop-design.md` §1;
  `docs/superpowers/specs/2026-07-11-two-tier-cadence-design.md` §out-of-scope).
- **Sub-hourly cadence.** The tick runs on confirmed 1H bars every 2h and the
  committee on a coarse 1–2×/day (plus events); going sub-hourly buys no signal
  (no order book, no sub-tick sampling — a fast spike that reverses within the
  interval is invisible by design) and multiplies LLM quota spend against the
  5-hour window gate (`docs/crypto-committee.md#cadence` + event honest-limit;
  two-tier spec §5).
- **Multi-account.** Single paper account, single symbol universe. Multi-account
  was explicitly out of scope for the paper-trading phase and adds state/mandate
  complexity with no evaluation payoff yet
  (`docs/superpowers/specs/2026-07-11-paper-trading-loop-design.md` §1).
