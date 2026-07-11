# Two-Tier Cadence — Design

**Date:** 2026-07-11
**Branch:** `feat/two-tier-cadence` (base: `f302828`, stacked on `feat/paper-trading-loop`)
**Status:** Approved by user ("2-hourly intraday tick + configurable committee cadence + the event trigger")

## 1. Goal

Give the system intraday responsiveness where it pays (risk management, event
reaction) without multiplying full-committee spend. Three additive pieces:

1. the paper tick runs every 2 hours on intraday (1H) bars,
2. the full committee becomes a first-class scheduled job with env-configurable
   cadence and symbol universe,
3. a deterministic event trigger fires an ad-hoc committee run when the market
   moves materially — with a cooldown so a sustained move triggers once.

**Out of scope:** live execution, changes to committee reasoning or journal
schemas, sub-hourly anything, event triggers beyond price-move and funding.

## 2. Components

### 2.1 Intraday tick (`agent/src/paper/tick.py`, `broker.py`)

- `VIBE_PAPER_TICK_INTERVAL` = `1D` (default, exactly today's behavior) | `1H`.
- In `1H` mode, each tick evaluates ALL confirmed 1H bars since the last
  evaluated bar (watermark `last_bar_ts` per symbol, persisted in a new
  `tick_state.json` in the paper root; first-ever tick starts at the newest
  confirmed bar — no deep backfill), in chronological order, applying the
  existing per-bar rules (stop-beats-TP, gap-at-open). The entry-bar rule
  generalizes: skip bars whose period ended before `opened_at` and the partial
  bar containing `opened_at` — the unprotected window shrinks from ≤1 day to
  ≤1 hour.
- Equity snapshots stay ONE per UTC date (first tick of the day writes it);
  conditional evaluation and retriable-decision retry run every tick.
- Scheduled job `paper-trading-tick` schedule becomes env-configurable:
  `VIBE_PAPER_TICK_SCHEDULE` (default `30 0 * * *`, unchanged). The
  recommended 2-hourly deployment sets `30 */2 * * *` + `1H` interval —
  documented, not defaulted (upstream behavior preserved; registration stays
  non-clobbering, and a changed env does NOT rewrite an existing user-edited
  job).

### 2.2 Scheduled committee runs (`agent/src/api/scheduled_routes.py`)

- New job `committee-run`, registered ONLY when `VIBE_COMMITTEE_SCHEDULE` is
  set (unset = not registered — fully additive), gated on the scheduler env,
  non-clobbering.
- Envs: `VIBE_COMMITTEE_SCHEDULE` (cron, e.g. `0 8 * * *`),
  `VIBE_COMMITTEE_SYMBOLS` (comma list, default `BTC-USDT`),
  `VIBE_COMMITTEE_TIMEFRAME` (default `72h swing`).
- Job prompt: for each configured symbol, call `run_swarm` with preset
  `crypto_committee`, the symbol, and the timeframe — serially, reporting each
  run id and final rating; never fabricate results; if a run fails, report the
  failure and continue with the next symbol.

### 2.3 Event trigger (`agent/src/paper/events.py` + tick integration)

- Deterministic, LLM-free check executed inside `run_tick` (same 2-hourly
  cadence, zero extra jobs): for each watched symbol —
  union of open-position symbols and `VIBE_COMMITTEE_SYMBOLS` — fetch last
  price + current funding via the existing snapshot fetchers and flag:
  - |price − reference| / reference ≥ `VIBE_EVENT_PRICE_MOVE_PCT` (default 5;
    0 = disabled). Reference = last committee decision's execution-time price
    for that symbol when available (ledger fill / journal ref), else the
    previous tick's stored price.
  - |funding rate| ≥ `VIBE_EVENT_FUNDING_ABS` (default 0.001 = 0.1%/8h;
    0 = disabled).
- Cooldown: `VIBE_EVENT_COOLDOWN_H` (default 12) per symbol, persisted in
  `tick_state.json` (`last_event_trigger_ts`); a symbol in cooldown is not
  re-flagged.
- Output: `run_tick` result gains `event_triggers: [{symbol, reason, metric,
  value, threshold}]`; the `paper_tick` tool surfaces them, and the tick job's
  prompt is amended: if `event_triggers` is non-empty, invoke `run_swarm`
  (preset `crypto_committee`) for each flagged symbol with
  `VIBE_COMMITTEE_TIMEFRAME`, then report which runs were started and why.
  The scheduled tick job therefore gets the `run_swarm` tool; ad-hoc committee
  runs journal + execute through the existing loop unchanged.
- Fetch failure for a symbol → no trigger, reason logged in tick errors
  (never invent).

## 3. Config reference (all additive; unset = today's behavior)

```bash
VIBE_PAPER_TICK_INTERVAL=1H       # 1D (default) | 1H
VIBE_PAPER_TICK_SCHEDULE="30 */2 * * *"   # default "30 0 * * *"
VIBE_COMMITTEE_SCHEDULE="0 8 * * *"       # unset = committee job not registered
VIBE_COMMITTEE_SYMBOLS=BTC-USDT,ETH-USDT  # default BTC-USDT
VIBE_COMMITTEE_TIMEFRAME="72h swing"      # default
VIBE_EVENT_PRICE_MOVE_PCT=5       # 0 = off
VIBE_EVENT_FUNDING_ABS=0.001      # 0 = off
VIBE_EVENT_COOLDOWN_H=12
```

## 4. Testing (socket-disabled)

- 1H multi-bar evaluation: chronological ordering; watermark advance;
  entry-partial-bar skip at 1H granularity; stop in bar N beats TP in bar N+1
  only via ordering (each bar independent); no backfill on first tick; 1D
  default byte-identical to current behavior (regression).
- Committee job: registered only when schedule env set; gating; non-clobbering;
  prompt names every configured symbol.
- Events: threshold boundaries (at/above/below); reference resolution order
  (decision price → previous tick price); cooldown (trigger, then silent for
  the window, re-arms after); disabled thresholds; fetch-failure → no trigger
  + error recorded; tick_state.json atomic writes.
- Tick job prompt contract: references paper_tick AND the run_swarm follow-up
  instruction.

## 5. Honest limits

- 1H-bar conditional fills remain approximations (no order book); the ≤1h
  unprotected entry window persists.
- Event reference price is decision-time or last-tick — a fast spike that
  fully reverses within 2h is invisible by design.
- Ad-hoc committee runs consume the same quota as scheduled ones; cooldown is
  the only rate limiter (12h default → worst case ~2 extra runs/symbol/day).
