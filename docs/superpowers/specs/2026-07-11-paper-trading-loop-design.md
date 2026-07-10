# Paper-Trading Executor with PnL-Aware Reflection — Design

**Date:** 2026-07-11
**Branch:** `feat/paper-trading-loop` (base: `fc77139`, on top of `feat/minimax-m3-migration`)
**Status:** Approved by user (internal paper broker; fully automatic execution)

## 1. Goal

Close the loop from committee decision to executed (paper) trade to money-graded
reflection. Today the committee journals typed decisions and the reflection loop
grades them on direction/alpha vs benchmark. This phase adds: automatic paper
execution of each journaled decision, realized/unrealized PnL accounting net of
fees and slippage, and reflection that judges decisions on executed money —
"direction was right but the stop was too tight" becomes a learnable lesson.

**Explicitly out of scope:** real-money execution (the `policy.py` guard and the
live connector stack stay untouched), the OKX demo backend (the broker interface
is shaped so it can be added later), multi-account support, derivatives (spot
long-only in v1; Sell on no position is a no-op, no shorting).

## 2. User decisions (binding)

| Decision | Choice |
|---|---|
| Paper venue | **Internal paper broker** — fill simulator against live OKX public prices; zero credentials; connector-shaped interface for a future OKX-demo backend |
| Autonomy | **Fully automatic** — committee decision → paper orders immediately; daily tick manages stops/TPs; `VIBE_PAPER_ENABLED` kill switch |
| Ledger vs journal | **Separate trade ledger** linked by `decision_id`; the decision journal's schema, lookahead-safe resolution, and idempotency key are not modified |

## 3. Components

### 3.1 `agent/src/paper/broker.py` — portfolio engine

Deterministic, LLM-free. State under `~/.vibe-trading/paper/` (override:
`VIBE_PAPER_ROOT` for tests):

- `account.json` — cash, created_at, config snapshot (fees/slippage at creation)
- `positions.json` — open positions: symbol, qty, avg_entry, stop, take_profits
  (list of {price, fraction}), opened_at, decision_id
- `ledger.jsonl` — append-only fills: ts, symbol, side, qty, fill_price,
  slippage_paid, fee_paid, order_type (market/stop/take_profit), decision_id,
  realized_pnl (on closes), trade_id
- `equity.jsonl` — daily mark-to-market snapshots: ts, cash, positions_value,
  equity, per-position unrealized

Rules:
- Starting cash `VIBE_PAPER_START_CASH` (default 100_000 USDT), applied only at
  account creation; `paper reset` recreates.
- Market fills: live OKX last price via the existing snapshot fetch path
  (`crypto_snapshot_tool`'s underlying fetchers — reuse, don't duplicate), price
  adjusted by slippage `VIBE_PAPER_SLIPPAGE_BPS` (default 5) against the trader,
  fee `VIBE_PAPER_FEE_BPS` (default 10) on notional.
- Conditional orders (stop / take-profit) are evaluated on each daily tick
  against the day's OHLC bar (same loader path as the journal's alpha math).
  Conservative gap rule: if the bar gaps through a stop, fill at the bar OPEN,
  not at the stop price. If both stop and TP are inside one bar, assume the
  WORSE outcome (stop first).
- If the live price fetch fails, the order is NOT filled; the attempt is logged
  with the failure reason and retried on the next tick. Never invent a price
  (same anti-hallucination discipline as Phase 5).
- All writes atomic (tmp+rename, matching the swarm store pattern).

Mandates enforced in the broker (hard, code-level):
- max open positions: `VIBE_PAPER_MAX_POSITIONS` (default 3)
- max capital per symbol: `VIBE_PAPER_MAX_SYMBOL_PCT` (default 25, percent)
- kill switch: `VIBE_PAPER_ENABLED` (default **1** on this branch; the executor
  hook additionally no-ops when the account doesn't exist and
  `VIBE_PAPER_AUTOCREATE=0`)

### 3.2 `agent/src/paper/translator.py` — decision → orders

Input: a journaled decision entry (the typed `PortfolioDecision` fields captured
in the journal entry plus its `decision_id`/`id`). Mapping (spot long-only):

| Rating | No position | Existing long |
|---|---|---|
| Buy | open long sized `size_pct` of equity (decision's `position_size_pct`, capped by mandates; missing → `VIBE_PAPER_DEFAULT_SIZE_PCT`, default 10) | add up to the symbol cap |
| Overweight | open at HALF the Buy sizing | add at half sizing up to the cap |
| Hold | no entry | apply stop/TP adjustments from typed fields; absent → unchanged |
| Underweight | no-op (no shorting) | reduce position by half at market |
| Sell | no-op (no shorting) | close full position at market |

(Real 5-tier enum per `schemas.py`: Buy | Overweight | Hold | Underweight | Sell.)

- **Schema reality (surveyed 2026-07-11):** journaled entries carry only
  `rating`, `price_target`, `time_horizon` — no stop or sizing. Therefore
  `PortfolioDecision` gains OPTIONAL typed execution fields (additive; old
  payloads stay valid): `stop_loss: float|None`, `take_profit: float|None`,
  `position_size_pct: float|None (0–100)`. `append_decision` passes them
  through as optional entry keys (JSONL tolerates extra keys; resolution
  logic and idempotency are untouched). The PM prompt is amended minimally to
  request them on Buy/Sell decisions.
- The translator reads ONLY these typed fields; it never parses free prose.
  Defaults when absent: TP ← `price_target`; stop ← entry ×
  (1 − `VIBE_PAPER_DEFAULT_STOP_PCT`/100) (default 8); size ←
  `VIBE_PAPER_DEFAULT_SIZE_PCT` (default 10).
- Idempotency: `(decision_id)` is unique in the ledger — the executor refuses
  to act twice on the same decision (a re-run of the hook is a no-op).

### 3.3 Execution hook + daily tick

- **Hook:** after the committee journals the PM decision, the swarm-side hook
  (same seam where the journal append happens — `committee_journal_tool` /
  run-completion path) invokes the executor in-process: translate → place
  orders → append ledger. Failures are logged and never fail the committee run
  (execution is downstream of research).
- **Daily tick:** a scheduled-research job (`paper-trading-tick`, same
  registration pattern as Phase 6's `decision-journal-reflection`, gated on the
  same `VIBE_TRADING_ENABLE_SCHEDULER`): evaluates conditional orders against
  the latest daily bar, marks to market, appends `equity.jsonl`. Also runnable
  manually: `vibe-trading paper tick`.

### 3.4 PnL-aware reflection

- `decision_journal` tool gains a `pnl` action: given a decision id (or
  symbol), returns realized PnL (net), fees paid, current unrealized, max
  drawdown while held, and how the position ended (stopped / took profit /
  closed by Sell / still open) — computed from the ledger, formatted as a
  compact block.
- The reflection officer's prompt (crypto_committee.yaml) is amended minimally:
  when reflecting on a resolved decision, ALSO call `decision_journal
  action=pnl` for that decision and weigh executed outcome vs directional
  outcome; the reflection text should name execution lessons (stop placement,
  sizing) separately from thesis lessons.
- The journal schema itself is unchanged; reflections remain stored exactly as
  today.

### 3.5 CLI

`vibe-trading paper <status|ledger|tick|reset>`:
- `status` — equity, cash, open positions with unrealized PnL and active
  stops/TPs, mandate headroom
- `ledger [--limit N] [--symbol S]` — recent fills with per-trade PnL
- `tick` — run the conditional-order/mark-to-market pass now
- `reset --confirm` — archive current state to a timestamped subdir, recreate

## 4. Testing (socket-disabled throughout)

- **Broker:** fixture-priced fills (slippage/fee math exact); gap-through stop
  fills at open; stop+TP same bar → stop wins; mandate caps (4th position
  rejected, oversize clamped); fetch-failure → no fill + logged retry;
  atomicity (partial-write simulation).
- **Translator:** table-driven rating×position matrix; typed-field extraction
  from REAL journaled fixtures (including the 2026-07-10 HOLD entry's shape);
  absent-field defaults; decision idempotency (same decision twice → one set of
  fills).
- **Hook/tick:** hook failure isolation (executor raising doesn't fail the
  committee run); scheduled-job registration (non-clobbering, gated) mirroring
  Phase 6 tests; tick idempotency within a day.
- **Reflection:** `pnl` action contract test (keys/format); prompt-contract
  test that the reflection officer's prompt references the action; regression
  that journal resolution/reflection behavior is unchanged when the paper
  account doesn't exist.
- **CLI:** smoke tests for the four subcommands against a fixture account.

## 5. Config reference (new, all additive)

```bash
VIBE_PAPER_ENABLED=1              # kill switch for the whole executor
VIBE_PAPER_START_CASH=100000      # paper USDT at account creation
VIBE_PAPER_SLIPPAGE_BPS=5         # market-fill slippage against the trader
VIBE_PAPER_FEE_BPS=10             # taker fee on notional
VIBE_PAPER_MAX_POSITIONS=3        # mandate: max concurrent positions
VIBE_PAPER_MAX_SYMBOL_PCT=25      # mandate: max % of equity per symbol
VIBE_PAPER_DEFAULT_SIZE_PCT=10    # entry size when decision omits sizing
# VIBE_PAPER_ROOT=~/.vibe-trading/paper   # state dir override (tests)
```

## 6. Risks / honest limits

- Synthetic fills: no order book, no partial fills; slippage is a flat model.
  Good enough to grade decisions, not to certify a strategy. The broker
  interface is deliberately connector-shaped so OKX demo can replace it.
- Daily-bar conditional evaluation means intraday stop touches that reverse
  by close ARE caught (bar low/high), but fill prices are approximations
  (open-on-gap, stop price otherwise).
- Long-only v1 mismatches Sell/StrongSell signals on no position (recorded as
  no-ops in the ledger notes so reflection can still see the signal was unused).
- PnL-aware reflection increases reflection-officer prompt size modestly
  (one compact block per resolved decision).
