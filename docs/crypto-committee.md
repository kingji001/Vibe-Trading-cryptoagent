# Crypto Investment Committee

Operator reference for the `crypto_committee` swarm preset — a 13-seat,
TradingAgents-style decision pipeline for crypto assets, adapted from
[TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents)
(Apache-2.0). This document describes the preset as implemented on this
branch (`agent/src/swarm/presets/crypto_committee.yaml`). For the MiniMax M3
provider migration this committee runs on, see
[`docs/minimax-migration-notes.md`](minimax-migration-notes.md) — that doc
also carries the [10–14-day transition protocol](minimax-migration-notes.md#transition-protocol-deepseek--minimax)
for cutting a build over from a DeepSeek baseline.

## Architecture

The committee is a single-pass (or multi-round, see [Debate rounds](#debate-rounds))
DAG of 13 agent seats, executed by the existing swarm engine
(`agent/src/swarm/runtime.py`) — no new execution mechanism, just a preset:

```
reflection_officer ──────────────────────────────────────────┐
                                                               │
market_analyst ──┐                                            │
onchain_analyst ─┼─→ [bull_researcher ⇄ bear_researcher] ─→ research_manager ─→ trader ─→ [risky ⇄ safe ⇄ neutral] ─→ portfolio_manager
news_analyst ────┤        (research_debate)                  (judge)              (risk_rotation)                  (final decision, journaled)
sentiment_analyst┘
```

1. **Reflection officer** resolves past decisions' realized outcomes and
   distills lessons — runs independently, feeds only the final decision task.
2. **Four analysts** (market/technical, on-chain/flow, news/macro, sentiment)
   run in parallel with no dependencies on each other.
3. **Bull/bear debate** (`research_debate`): the two researchers argue from
   the four analyst reports; the **research manager** judges and issues a
   binding research plan.
4. **Trader** turns the research plan into one executable Buy/Hold/Sell
   proposal (sizing is the PM's call).
5. **Risk rotation** (`risk_rotation`): aggressive/conservative/neutral risk
   debators review the trader's proposal.
6. **Portfolio manager** issues the final, binding 5-tier rating and appends
   it to the decision journal.

### Seat map (13)

| Seat | Role | Model tier | Key tools |
|---|---|---|---|
| `reflection_officer` | Reflection Officer (learning loop) | quick | `decision_journal`, `write_file` |
| `market_analyst` | Market & Technical Analyst | quick | `get_market_data`, `get_verified_crypto_snapshot`, `bash` |
| `onchain_analyst` | On-Chain & Flow Analyst | quick | `read_url`, `load_skill`, `bash` |
| `news_analyst` | News & Macro Analyst | quick | `web_search`, `read_url` |
| `sentiment_analyst` | Sentiment Analyst | quick | `get_crypto_sentiment_data`, `submit_decision` |
| `bull_researcher` | Bull Researcher | quick | `write_file`, `read_file` |
| `bear_researcher` | Bear Researcher | quick | `write_file`, `read_file` |
| `research_manager` | Research Manager (debate judge) | **deep** | `submit_decision`, `get_verified_crypto_snapshot` |
| `trader` | Trader | quick | `get_market_data`, `get_verified_crypto_snapshot`, `submit_decision` |
| `risky_analyst` | Aggressive Risk Debator | quick | `get_verified_crypto_snapshot` |
| `safe_analyst` | Conservative Risk Debator | quick | `get_verified_crypto_snapshot` |
| `neutral_analyst` | Neutral Risk Debator | quick | `get_verified_crypto_snapshot` |
| `portfolio_manager` | Portfolio Manager (final decision) | **deep** | `submit_decision`, `decision_journal`, `get_verified_crypto_snapshot` |

"deep"/"quick" resolve `${VIBE_DEEP_MODEL}` / `${VIBE_QUICK_MODEL}` at
preset-build time (see [Model tiering](#model-tiering) below); with both
unset every seat falls back to the run's global `LANGCHAIN_MODEL_NAME`.

## Running the committee

Variables: `target` (loader-format symbol, e.g. `BTC-USDT`) and `timeframe`
(decision horizon, e.g. `72h swing`). Example (`agent/src/tools/swarm_tool.py`
ships this exact pair as the preset's example vars):

```bash
# CLI (interactive)
/swarm run crypto_committee {"target": "BTC-USDT", "timeframe": "72h swing"}

# CLI (one-shot prompt through the main agent, which calls run_swarm)
vibe-trading run -p "Run the crypto_committee swarm on BTC-USDT for a 72h swing decision."
```

`target` doubles as the preset's **identity anchor** (see below) — it is not
free-text framing, it is the one instrument all 13 seats analyze and vote on.

### Identity anchor (anti-hallucination, fail-fast)

`crypto_committee` is registered in `IDENTITY_ANCHOR_VARS`
(`agent/src/swarm/grounding.py`) as the only preset where a symbol that fails
to resolve **fails the run at start** (`InstrumentResolutionError`) instead
of silently dropping out of the grounding block, which is the default
behavior for every other preset. Rationale: an ungrounded `{target}` here
means 13 agents debate and the PM issues a binding, journaled decision for
an asset nobody actually looked up — the TradingAgents "hallucinated the
wrong company from chart shape" failure mode, but worse because the
committee commits to a rating. `_prefetch_grounding_data`
(`agent/src/swarm/runtime.py`) resolves `target` to a real symbol, fetches
OHLCV for it, and renders a one-line "last price @ timestamp" anchor
(`format_identity_anchor`) that is prepended to every worker's grounding
block. A resolution failure (bad symbol, network failure, delisted pair)
raises before any of the 13 workers run.

## Anti-hallucination tool surfaces

Two deterministic, LLM-free tools give the committee a shared source of
truth instead of letting workers narrate numbers from training data or
free-text pages:

- **`get_verified_crypto_snapshot(symbol)`** (`agent/src/tools/crypto_snapshot_tool.py`)
  — fetches six fields directly from OKX's public REST API (ccxt fallback on
  a fetch failure): `last_price`, `stats_24h`, `funding_rate`,
  `open_interest`, `mark_price`, `index_price`. Each field independently
  resolves to a real value or an instructive
  `NO_DATA_AVAILABLE: <reason> — do not estimate this value` sentinel — one
  field's failure never blocks another's fetch. Every seat that cites an
  exact current number (market/technical analyst, research manager, trader,
  all three risk debators, portfolio manager) is instructed to treat this
  tool as the source of truth and report — never reconcile — a discrepancy
  against any other source.
- **`get_crypto_sentiment_data(symbol)`** (`agent/src/tools/crypto_sentiment_tool.py`)
  — pre-fetches all three sentiment sources in code before the sentiment
  analyst ever reasons about them: the Crypto Fear & Greed Index (14-day
  series, alternative.me), r/CryptoCurrency top-week posts, and the
  StockTwits stream for the asset's `<BASE>.X` symbol. Each source is
  independent; a failed source returns `<unavailable>` rather than a gap the
  worker might fill from memory. `sources_available` (0-3) tells the
  sentiment analyst how far to down-rate its confidence.

Both tools replace ad-hoc `read_url`/bash scraping that previously let
workers reconcile or invent numbers when a fetch failed mid-reasoning.

## Typed decisions

Workers have no native structured-output mode, so validation happens in the
`submit_decision` tool (`agent/src/tools/committee_decision_tool.py`) against
Pydantic schemas in `agent/src/committee/schemas.py`
(ported/adapted from TradingAgents):

- **`sentiment_report`** → `SentimentReport`: `sentiment` (6-tier:
  very_bearish…very_bullish), `score_0_10`, `confidence` (low/medium/high),
  `narrative` (≥ 50 chars).
- **`research_plan`** → `ResearchPlan`: `recommendation` (5-tier rating),
  `rationale` (≥ 50 chars), `strategic_actions` (list, ≥ 1 item).
- **`trader_proposal`** → `TraderProposal`: `action` (Buy/Hold/Sell only —
  sizing granularity is the PM's job), `reasoning` (≥ 50 chars),
  `entry_price`/`stop_loss`/`take_profit` (nullable floats with nullish-string
  coercion — models emitting `"n/a"`/`"tbd"`/`"-"` get `None`, not a
  validation error), `position_sizing` (optional note).
- **`portfolio_decision`** → `PortfolioDecision`: `rating` (5-tier: Buy /
  Overweight / Hold / Underweight / Sell), `executive_summary` (≥ 50 chars),
  `investment_thesis` (≥ 100 chars), `price_target` (nullable),
  `time_horizon`.

A worker whose `submit_decision` call fails schema validation gets an
actionable error back and can retry; `render_markdown` in the same module
emits a deterministic `**Rating**:` / `**Action**:` header line so
downstream consumers (the journal, other seats) parse decisions without an
LLM call. `parse_rating` extracts the rating from that header via regex — no
LLM in the loop for the deterministic parts of the pipeline.

## Decision journal & learning loop

`agent/src/committee/journal.py` is the committee's persistence layer —
append-only JSONL with atomic rewrites (temp file + `os.replace`), no LLM
and no live-trading dependency. Default path:
`~/.vibe-trading/committee/journal.jsonl` (override with
`VIBE_TRADING_COMMITTEE_JOURNAL`).

### Journal entry format (one JSON object per line)

```json
{
  "id": "dec_<12-char sha256 prefix>",
  "decided_at": "2026-07-10T00:00:00+00:00",
  "symbol": "BTC-USDT",
  "rating": "Overweight",
  "time_horizon": "72h swing",
  "primary_horizon": "72h",
  "price_target": 68000.0,
  "run_id": "<swarm run id, for append-idempotency>",
  "status": "pending",
  "ref_price": 65000.0,
  "horizons": {
    "24h": {
      "raw_return": 0.012, "benchmark_return": 0.008, "alpha": 0.004,
      "mark_price": 65780.0, "direction_correct": true,
      "resolved_at": "2026-07-11T00:05:00+00:00"
    }
  },
  "reflection": "Overweight call on BTC ... [2-4 sentence lesson]",
  "reflected_at": "2026-07-13T00:05:00+00:00"
}
```

`primary_horizon` is derived from `time_horizon` text (`"24h"`/`"1 day"` →
`24h`; `"7d"`/`"week"`/`"month"`/`"position"` → `7d`; else `72h`). Outcomes
resolve at three horizons — 24h / 72h / 7d — via 1H bars from the backtest
loader registry (`okx` → `ccxt`); the reference price is the OPEN of the
first bar at/after the decision, horizon prices are the CLOSE of the last
bar at/before the deadline (lookahead-safe). `alpha = raw_return -
benchmark_return`, benchmark default `BTC-USDT` (crypto's SPY;
`VIBE_COMMITTEE_BENCHMARK` overrides). `direction_correct`: Buy/Overweight
correct if `score_move > 0`; Sell/Underweight correct if `score_move < 0`
(`score_move` is alpha, except for the benchmark asset itself where alpha is
definitionally 0 and raw return is used instead); Hold correct if
`|raw_return| <= 0.02` (the `HOLD_BAND`).

### Tool surface

`decision_journal` (`agent/src/tools/committee_journal_tool.py`), one tool
with five actions, so both swarm workers and the main agent can drive the
loop: `append` (PM records a fresh decision), `resolve_due` (deterministic
outcome math, no LLM), `reflect` (attach a 2-4 sentence lesson to a resolved
entry), `lessons` (render the prompt-injection block for a symbol), `list`
(raw entries).

### Reflection loop automation (Phase 6)

Resolution/reflection normally run inline via the `reflection_officer` seat
at the start of the next committee run. With
`VIBE_TRADING_ENABLE_SCHEDULER=1`, server startup also registers a daily
scheduled-research job (job id `decision-journal-reflection`, schedule
`0 0 * * *` UTC, `agent/src/api/scheduled_routes.py::_ensure_decision_journal_job`)
that calls `resolve_due` then `reflect` for every due entry — so outcomes
resolve even on days with no committee run. Registration is idempotent (a
restart never resets the schedule or clobbers a manual edit). If you keep
the scheduler off, run the equivalent from system cron — see
[minimax-migration-notes.md, Phase 6](minimax-migration-notes.md#phase-6--reflection-loop-automation--crypto-benchmark-fix)
for the exact `vibe-trading run` command.

`VIBE_LESSONS_TO_MANAGER=1` (default off) additionally injects the
reflection officer's `past_lessons` block into the `research_manager` seat,
not just the portfolio manager — TradingAgents deliberately restricts
memory context to the PM, so this is an opt-in A/B experiment, not the
default wiring.

## Paper-trading loop

Closes the loop from committee decision to executed (paper) trade to
money-graded reflection: "direction was right but the stop was too tight"
becomes a learnable lesson, not just a directional grade. Everything below is
additive to the decision journal — the journal's own schema, resolution
logic, and idempotency key are untouched; a new package,
`agent/src/paper/`, holds a deterministic, LLM-free portfolio engine
(no credentials, no real orders — the live-execution stack in
`agent/src/live/` and `policy.py` is completely separate and untouched).

### End-to-end flow

```
committee (portfolio_manager decision)
  -> decision_journal action=append               (agent/src/tools/committee_journal_tool.py)
  -> maybe_execute_paper(entry)                    (agent/src/paper/hook.py, called from the
                                                     append success path — never fails the
                                                     committee run, any exception is caught
                                                     and returned as {"error": ...})
       -> execute_decision(entry, broker)          (agent/src/paper/translator.py)
            -> PaperBroker.market_buy / market_sell (agent/src/paper/broker.py)
            -> PaperStore.append_ledger(...)         (agent/src/paper/store.py, ledger.jsonl)

scheduled "paper-trading-tick" job, 00:30 UTC        (agent/src/api/scheduled_routes.py,
  (double-gated: VIBE_TRADING_ENABLE_SCHEDULER=1      registered idempotently at server
   AND paper trading itself enabled)                  startup, after the 00:00 UTC
  -> paper_tick tool (no params)                      decision-journal reflection job)
       -> run_tick()                                (agent/src/paper/tick.py)
            -> PaperBroker.evaluate_conditionals(...)  (stop/take-profit checks against
                                                         the latest confirmed daily bar)
            -> PaperBroker.equity(...)                 (mark-to-market)
            -> PaperStore.append_equity(...)           (equity.jsonl, one row/UTC day)

reflection_officer seat (next committee run, or the daily reflection job)
  -> decision_journal action=pnl (decision_id or symbol)
       -> decision_pnl(...)                         (agent/src/paper/pnl.py)
       -> compact summary block quoted into the reflection prompt
```

Also runnable by hand: `vibe-trading paper tick` (see CLI section below) runs
exactly the same `run_tick()` the scheduled job calls.

### Decision -> order translation

`agent/src/paper/translator.py::execute_decision` reads ONLY typed fields on
the journaled entry — `stop_loss`, `take_profit`, `position_size_pct`
(optional, additive fields on `PortfolioDecision`; absent on legacy entries) —
never free prose. Rating -> action (spot long-only; the real 5-tier enum is
`Buy | Overweight | Hold | Underweight | Sell` per
`committee/schemas.py::parse_rating`):

| Rating | No position | Existing long position |
|---|---|---|
| Buy | open long, sized `position_size_pct`% of current equity (default `VIBE_PAPER_DEFAULT_SIZE_PCT`, 10%) | add, same sizing rule, capped by the symbol-exposure mandate |
| Overweight | open at HALF the Buy sizing | add at half sizing, same cap |
| Hold | no entry | apply any provided typed `stop_loss`/`take_profit`; `price_target` is NOT used as a TP fallback for Hold — a Hold that only carries `price_target` is a pure no-op |
| Underweight | ledger noop (`"sell signal with no position"`) — no shorting | reduce the position by half at market |
| Sell | ledger noop (`"sell signal with no position"`) — no shorting | close the full position at market |

Stop/TP defaults when the typed fields are absent: stop ← fill price ×
`(1 - VIBE_PAPER_DEFAULT_STOP_PCT/100)`; take-profit ← `price_target` (single
TP, fraction 1.0).

**Idempotency:** a decision is "already executed" — and a repeat call (e.g. a
duplicate journal append) is skipped — if the ledger has ANY row with that
`decision_id`, with one exception: rows recording "price unavailable — not
executed" are retriable (a price-fetch failure never fills, so it must not
permanently block the decision). Retriable decisions are actively re-driven by
the daily tick: `run_tick` re-runs any decision whose only ledger rows are
retriable noops, for up to **7 days** after `decided_at` (older ones are left
alone), reporting the outcomes under `retried_decisions`. Every other outcome
is final and never retried, including **mandate-rejected decisions** (max
positions / symbol exposure cap) and **sell-with-no-position noops** — a
decision rejected by a mandate does NOT get retried automatically later when a
position slot frees up; a fresh decision is needed.

### Fill math (binding)

```
buy fill  = price * (1 + VIBE_PAPER_SLIPPAGE_BPS/10000)
sell fill = price * (1 - VIBE_PAPER_SLIPPAGE_BPS/10000)
fee       = fill_notional * VIBE_PAPER_FEE_BPS/10000        (deducted from cash on both sides)
```

Conditional orders (stop / take-profit), evaluated once per UTC day against
the latest confirmed daily OHLC bar:

- **entry-day bars are NOT evaluated.** Conditional evaluation begins on the
  first FULL daily bar *after* the position was opened. The partially
  overlapping entry-day bar (whose period contains `opened_at`) is skipped —
  otherwise pre-entry price action within the entry day could fire a
  fictitious stop/take-profit — and the daily tick records a note
  (`entry-day bar skipped for SYMBOL …`) so the skip is visible;
- no slippage on conditional fills (bar prices are already conservative) —
  fee still applies;
- a bar that gaps THROUGH a stop fills at the bar's OPEN, not the stop price
  (worse for the trader, never invents a better fill than what actually
  happened);
- a stop AND a take-profit inside the same bar → the stop wins (the
  conservative, worse outcome) — the take-profit is skipped entirely and the
  position closes in full at the stop fill;
- each take-profit's `fraction` applies to the position's REMAINING qty at
  the time it triggers, not the original entry qty — so a TP ladder (e.g.
  50% then 50%) sells half of whatever is still held at each step, not half
  of the original size twice.

### State files

Under `~/.vibe-trading/paper/` (override: `VIBE_PAPER_ROOT`), atomic writes
(tmp + `os.replace`, matching `src/swarm/task_store.py`'s pattern):

- `account.json` — cash, created_at, a config snapshot taken at account
  creation (fees/slippage settings don't retroactively change an existing
  account — only `paper reset` picks up new values).
- `positions.json` — open positions: symbol, qty, avg_entry, stop,
  take_profits (list of `{price, fraction}`), opened_at, decision_id (the id
  of whichever decision most recently opened the position from flat).
- `ledger.jsonl` — append-only fills AND noop rows: ts, symbol, side, qty,
  fill_price, slippage_paid, fee_paid, order_type
  (market/stop/take_profit/noop), decision_id, realized_pnl, trade_id, note.
- `equity.jsonl` — one row per UTC day: ts, cash, positions_value, equity,
  per-position marks/unrealized, `stale_positions` count.

### Env knobs

| Var | Default | Meaning |
|---|---|---|
| `VIBE_PAPER_ENABLED` | `1` (unset = enabled) | Kill switch for the whole executor. The falsy set is **exactly** `{"0", "false", ""}` (case-insensitive, whitespace-trimmed) — anything else enables, so e.g. `"off"` / `"no"` / `"disabled"` still ENABLE. Gates the hook/translator and the daily tick (`run_tick` / `paper_tick` no-op when disabled). |
| `VIBE_PAPER_START_CASH` | `100000` | Paper USDT at account creation only. |
| `VIBE_PAPER_SLIPPAGE_BPS` | `5` | Market-fill slippage against the trader (basis points). |
| `VIBE_PAPER_FEE_BPS` | `10` | Taker fee on notional, both sides. |
| `VIBE_PAPER_MAX_POSITIONS` | `3` | Mandate: max concurrent open positions. |
| `VIBE_PAPER_MAX_SYMBOL_PCT` | `25` | Mandate: max % of equity a single symbol may hold; buys are clamped (not rejected) to the remaining headroom, except when headroom is already zero. |
| `VIBE_PAPER_DEFAULT_SIZE_PCT` | `10` | Entry size (% of equity) when a decision omits `position_size_pct`. |
| `VIBE_PAPER_DEFAULT_STOP_PCT` | `8` | Stop distance (% below fill) when a decision omits `stop_loss`. |
| `VIBE_PAPER_ROOT` | `~/.vibe-trading/paper` | State-dir override (used by tests). |

Note that disabling `VIBE_PAPER_ENABLED` **freezes** existing positions:
stops/take-profits are not evaluated while the switch is off (no conditional
fills, no mark-to-market), not just new trades blocked.

One further mandate is always on (no env knob): a **cash floor** — buys are
clamped to available cash net of fee (ledger note `"clamped to available
cash"`), and a buy with zero/negative cash is rejected outright, so cash can
never go negative.

### PnL-aware reflection

`decision_journal action=pnl` (`decision_id` or `symbol`) replays the ledger
into per-symbol open/close "lineages" (`agent/src/paper/pnl.py`) and returns
realized PnL, fees paid, current unrealized PnL, max drawdown while held, and
how the position ended (`stopped` / `took_profit` / `closed_by_sell` /
`open` / `not_executed`). A few non-obvious behaviors, worth knowing before
trusting the numbers:

- **PnL is position-lifecycle-wide, not per-decision.** If a later add or a
  separate Sell/Underweight decision touched the same physical position, its
  fills are folded into the same lineage and the summary flags this: `note:
  PnL is position-lifecycle-wide (includes trades under N other decision(s))`.
  Don't read the reported number as this one decision's marginal quality when
  that note is present.
- **`realized_pnl` is net of EXIT fees only** (`(sell_fill - avg_entry) *
  qty_sold - sell_fee`); the entry-side fee already reduced cash at open and
  shows up separately in `fees_paid`, not subtracted a second time from
  `realized_pnl`.
- A decision whose only ledger rows are noops (mandate-rejected, no-position
  sell, disabled kill switch) reports `executed: false` /
  `exit_kind: "not_executed"` — the noop note(s) are folded into the summary
  text.

### Reading `vibe-trading paper status`

`status` never auto-creates an account (it's read-only) — a missing account
just prints a hint. When an account exists it shows cash, equity, and mandate
headroom as `positions used/max` plus the per-symbol exposure cap, then one
block per open position: qty @ avg entry, mark, unrealized PnL, exposure %
vs the cap, stop, and take-profit(s). **Stale marks are always shown, never
hidden**: a position whose live price/latest bar mark isn't available is
valued at `avg_entry` (zero unrealized) and flagged `[STALE]` inline plus a
"no live price available" note on the Mark line —
`PaperBroker.equity()`'s `stale` flag is surfaced verbatim, not silently
absorbed into the equity total. `paper ledger [--limit N] [--symbol S]`
lists recent fills (date/symbol/side/qty/fill/fee/PnL/type); rows carrying a
note (mandate rejections, no-position noops, retriable price-unavailable
noops) are additionally listed underneath so a rejected decision is visible,
not just silently absent. `paper tick` runs the daily conditional-order/
mark-to-market pass immediately (the same `run_tick()` the scheduled job
calls) and reports fills/equity/stale count/bar-fetch errors. `paper reset`
refuses without `--confirm` (nonzero exit) — with `--confirm` it archives
account/positions/ledger/equity into a timestamped `archive-<UTC-stamp>/`
subdirectory and a fresh account is created on the next trade or tick.

### Honest limits

Read this before treating paper PnL as a strategy backtest:

- **Synthetic fills.** No order book, no partial fills; slippage is a flat
  basis-point model applied uniformly regardless of size or liquidity. Good
  enough to grade committee decisions on realistic-ish executed money, not to
  certify a strategy — the broker interface is deliberately connector-shaped
  so a real OKX-demo backend can replace the fill simulator later.
- **Daily-bar approximation for conditional orders.** Stop/take-profit
  evaluation uses one confirmed daily OHLC bar per UTC day: an intraday touch
  that reverses by close IS caught (via the bar's low/high), but fill prices
  are approximations (bar open on a gap, the stop/TP price otherwise) — not
  the exact intraday price at the moment of the touch.
- **Long-only v1.** Sell/Underweight signals with no held position are
  no-shorting no-ops, recorded in the ledger's notes (not silently dropped)
  so reflection can still see the signal went unused.
- **Reflection prompt size.** PnL-aware reflection adds one compact block per
  resolved decision to the reflection officer's prompt — a modest but
  nonzero token-budget cost.

## Debate rounds

`agent/src/swarm/presets.py::_expand_debate` unrolls the `debates:` YAML
block into chained DAG tasks at preset-build time — the engine stays
single-pass and acyclic; deeper debates only add wall-time (a serial chain),
never concurrency pressure on the LLM gate. Two debates are declared:

- **`research_debate`**: `bull_researcher` ⇄ `bear_researcher`, rounds from
  `VIBE_DEBATE_ROUNDS` (`${VIBE_DEBATE_ROUNDS:-1}`).
- **`risk_rotation`**: `risky_analyst` → `safe_analyst` → `neutral_analyst`,
  rounds from `VIBE_RISK_ROUNDS` (`${VIBE_RISK_ROUNDS:-1}`).

Both env vars unset ⇒ 1 round each, which reproduces the historical
single-pass graph exactly (bull → bear → research manager; risky → safe →
neutral → PM). Rounds ≥ 2 append `-r{n}` task ids; each round's task
receives every prior round's summary, and the rebuttal prompt instructs the
seat to answer the latest opposing argument rather than restate its opener
("debate, don't list"). Rounds are capped at 4 (`_DEBATE_ROUNDS_CAP` in
`presets.py`) and rejected at build time above that, before spending tokens.

## Model tiering

`agent/src/swarm/presets.py::_resolve_model_name` resolves `${ENV_VAR}` /
`${ENV_VAR:-default}` placeholders in a seat's `model_name` at preset-build
time. `research_manager` and `portfolio_manager` (the debate judge and final
decision seats) read `${VIBE_DEEP_MODEL}`; every other seat reads
`${VIBE_QUICK_MODEL}`. Both unset ⇒ every seat resolves to `None` ⇒ falls
back to the run's global `LANGCHAIN_MODEL_NAME`. Any tier value must name a
model available under the run's single configured `LANGCHAIN_PROVIDER` —
`model_name` only substitutes the model string, it cannot select a different
provider per seat (`agent/src/providers/llm.py::build_llm` always builds its
client from `LANGCHAIN_PROVIDER`).

A third, unrelated tier — `VIBE_COMPACT_MODEL` — routes `AgentLoop._auto_compact`
(context-window summarization, `agent/src/agent/loop.py`) through a cheaper
model; it is not a committee seat tier and applies to any agent run, not
just `crypto_committee`.

## Concurrency governance

Every LLM call in the process (swarm workers, the main agent, context
compaction) funnels through `ChatLLM` (`agent/src/providers/chat.py`), which
holds a single process-wide `BoundedSemaphore` sized by
`VIBE_LLM_MAX_CONCURRENT` (0 = disabled, upstream behavior). With the gate
enabled, queue-wait time is measured before the HTTP request timer starts
and surfaced on `LLMResponse.gate_wait_seconds` — swarm workers log a
`llm_gate_wait` event when `gate_wait_seconds > 0`
(`agent/src/swarm/worker.py`), and the main loop folds it into run totals
(`agent/src/agent/loop.py`). `compute_layer_deadline`
(`agent/src/swarm/runtime.py`) bounds a run's assumed wave parallelism by
`min(SWARM_MAX_WORKERS, VIBE_LLM_MAX_CONCURRENT)` when the gate is enabled,
so a run never sizes its layer-deadline math for more concurrency than the
gate actually allows — see the cross-run contention caveat in
[minimax-migration-notes.md, Known limitations (Phase 2)](minimax-migration-notes.md#known-limitations-phase-2).

Throttle-aware retry (`agent/src/providers/backoff.py`) wraps retryable
408/429/5xx/transport stream errors with capped exponential backoff + equal
jitter (base `VT_STREAM_RETRY_BASE_S`, default 2s; factor 2; cap 90s; max
attempts `VT_STREAM_RETRY_MAX`, default 5), honoring a provider's
`Retry-After` header when present. `VIBE_RUN_TOKEN_BUDGET_WARN` is
observability-only — it logs a warning when a run exceeds the configured
token count, with no hard cutoff (a throttled wait beats a mid-pipeline
abort).

## Transition protocol (DeepSeek → MiniMax)

See [`docs/minimax-migration-notes.md`](minimax-migration-notes.md#transition-protocol-deepseek--minimax)
for the 4-step operational cutover protocol (freeze configs, run the daily
scheduler on a fixed universe for 10–14 days, compare journal metrics,
cutover criteria + rollback) and the honest caveat on what that evaluation
window can and cannot prove.
