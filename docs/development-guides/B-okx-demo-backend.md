# Feature B — OKX demo-trading backend (development guidance)

Process contract: **`docs/development-guides/README.md` is binding** — read it first.
Every rule there (per-task loop, TDD-with-RED-evidence, independent reviewer,
ledger, final reproduction review, live verification, repo invariants) applies
here in full. This file adds only what is specific to Feature B.

Roadmap source: `docs/optimization-roadmap.md` §2 direction **B** and §3
("Deliberately not doing"). B is rated effort **L** and is the *designed endgame*
of the paper-trading loop — but it is the **last** direction and the
**highest-risk** one, because it is the first time this system places real orders
against a real matching engine.

> This is a GUIDANCE plan, not an implementation plan. The assistant that picks
> it up must first pass the evidence gate and the user-decision gate below, then
> produce a spec → plan → task briefs under `docs/superpowers/` per the playbook.

---

## 1. Goal & evidence gate

**Goal.** Add an *alternative broker backend* so the committee's journaled
decisions can be executed against an **OKX demo (paper) account** — OKX's real
simulated-trading matching engine (`x-simulated-trading` header, SDK `flag="1"`)
— instead of the internal deterministic fill simulator, selected by one env var,
with the internal broker remaining the default and the ledger/journal/PnL
remaining the system of record. Realistic fills (real book, real partials) are
the payoff.

**This feature is DOUBLE-GATED. Do not start implementation until BOTH gates
pass. If either is unmet, STOP and report back — never assume, never infer
consent from silence.**

### Gate 1 — evidence (hard, from roadmap §1)

The internal-broker journal must first show a **track record worth upgrading**.
Read the real artifacts (`~/.vibe-trading/committee/journal.jsonl`,
`~/.vibe-trading/paper/{ledger,equity}.jsonl`) and confirm, per roadmap §1a–§1b:

- **§1a decision quality** — per-horizon `direction_correct` rate and mean
  `alpha` at 24h / 72h / 7d, computed **only from resolved entries**
  (`horizons.<h>.resolved_at` present). Honest caveat (migration notes): a few
  days on one symbol is an operational smoke test, not statistical proof —
  directional only.
- **§1b executed money** — **realized PnL net of fees** (`ledger.jsonl`
  `realized_pnl` + `fee_paid`) and **fee/slippage drag vs gross alpha**
  (`fee_paid`+`slippage_paid` summed vs gross realized PnL). Roadmap §1b routes
  turnover work ahead of B **if fees eat >30% of gross alpha** — a
  high-turnover-negative-edge record is a reason to tune cadence, NOT to add a
  realistic backend.
- **§1b stop/TP hit quality** — the conditional-fill rules the backend must
  preserve are actually being exercised (stops/TPs in the ledger, entry-bar-skip
  notes present).

If there is no meaningful resolved track record, or edge is negative/fee-dominated,
**STOP**: B is not earned yet.

### Gate 2 — explicit user decision to relax the policy guard

Repo invariant (README): *"`policy.py` and the live-execution stack stay
untouched unless the user explicitly decides to relax them (feature B documents
that decision point)."* This is that document.

`agent/src/goal/policy.py::reject_live_execution_objective` (regexes at
`policy.py:7-14`) hard-rejects any *goal objective/criterion text* that looks
like order execution ("place/submit/execute … order/trade", "buy/sell … now",
Chinese `下单/市价单/…`). It is invoked from `src/goal/store.py:292, 299, 417`.
The whole system is architected so the committee produces *typed journaled
decisions and paper fills only* — never order placement. A demo backend places
**real orders on OKX's servers** (a real, if simulated-balance, account); that
crosses the exact line this guard and the untouched live stack
(`agent/src/live/`) were built to hold.

**The future assistant MUST:**

1. **STOP and obtain an explicit, recorded user decision** to allow demo-account
   order execution and to relax `policy.py` accordingly. Present the risk plainly
   (real orders leave this process for the first time; the demo/live discriminator
   is only a config flag — see §2). Do **not** treat "the roadmap lists B" or this
   plan's existence as that consent. Only the user, in this session, grants it.
2. Capture the decision's **scope** verbatim: demo-only? which symbols? and the
   exact narrowing of `policy.py` (e.g. relax only for demo-backend paths, keep
   live-execution objectives rejected). Put it in the spec's "authorization"
   section and the ledger.
3. If the user declines or is unavailable — **STOP**. Build nothing.

---

## 2. Binding design constraints

**BC-1 — implement PaperBroker's operation surface exactly.** The backend is a
drop-in for `agent/src/paper/broker.py::PaperBroker`. The conformance surface its
callers (`translator.py`, `hook.py`, `tick.py`) actually use, verified on main:

```
market_buy(symbol: str, notional_usdt: float, *, decision_id: str,
           stop: float | None, take_profit: float | None) -> dict
        # returns a ledger-entry dict; raises MandateViolation / PriceUnavailable
market_sell(symbol: str, fraction: float, *, decision_id: str,
            reason: str) -> dict | None          # None when no position
evaluate_conditionals(symbol: str, bar: dict, *,
                      bar_period: timedelta = _BAR_PERIOD) -> list[dict]
set_risk(symbol: str, *, stop: float | None, take_profit: float | None) -> bool
equity(mark_prices: dict[str, float] | None = None) -> dict
```

Plus attributes callers read directly: **`.store`** (a `PaperStore`;
`translator._buy_or_add` calls `broker.store.load_positions()` /
`broker.store.append_ledger(...)`) and **`.config`** (`broker.config.default_size_pct`,
`broker.config.default_stop_pct`). Plus the two binding dict shapes from the
`broker.py` module docstring (`position`, `ledger`) — the backend must emit
ledger rows of the **same shape** (`ts, trade_id, symbol, side, qty, fill_price,
slippage_paid, fee_paid, order_type, decision_id, realized_pnl, note`) and
maintain positions of the **same shape** (`symbol, qty, avg_entry, stop,
take_profits:[{price,fraction}], opened_at, decision_id`). `market_buy`'s return
must carry `fill_price` (translator reads `result["fill_price"]` for the default
stop). Exceptions `MandateViolation` / `PriceUnavailable` are part of the contract.

**BC-2 — what maps to real OKX demo orders vs stays simulated.**

- `market_buy` / `market_sell` → **real OKX demo orders** via
  `agent/src/trading/connectors/okx/sdk.py::place_order` (import as `src.trading.connectors.okx.sdk`; abbreviated `okx/sdk.py` below). Buy sized by USDT: `place_order(cfg, symbol=…,
  side="buy", notional=<usdt>, order_type="market")` (the SDK sends
  `sz=<notional>, tgtCcy="quote_ccy"`). Sell a fraction: price the held qty →
  `side="sell", quantity=<base_qty*fraction>`. The **fill** (price, qty, fee)
  comes back from OKX, not from the local slippage/fee formulas.
- **Conditional stops / take-profits → tick-evaluated (v1), NOT OKX algo
  orders.** Tradeoff:
  - *OKX algo orders* (native `order-algo` stop/OCO) would let OKX manage the
    stop server-side (fires between ticks, no ≤bar-period unprotected window).
    But it **abandons fill-rule parity**: the internal broker's binding rules
    (`evaluate_conditionals`) are stop-beats-TP-in-a-bar, gap-fill-at-open,
    entry-partial-bar skip, and **zero slippage on conditional fills** — OKX's
    trigger/fill semantics differ, so the two backends would diverge and the
    journal's realized-PnL comparability would break.
  - *Tick-evaluated* keeps `set_risk` as **local position metadata** and keeps
    `evaluate_conditionals` running the **identical deterministic bar logic** for
    both backends; only the resulting sell is routed to OKX demo (a market sell
    at trigger time). **Pick tick-evaluated for v1** — fill-rule parity and
    journal comparability outweigh the sub-bar-latency win. Document the retained
    "≤bar-period unprotected window" honest-limit (already a known `sdd3` limit).
    Native algo orders are an explicit non-goal (§5), revisitable later.

**BC-3 — backend selection env.** `VIBE_PAPER_BACKEND` ∈ {`internal`, `okx-demo`}.
**Default and unset ⇒ `internal`** (byte-identical upstream behavior, pinned by a
regression test per the additive-and-flag-gated invariant). A backend factory
(new, e.g. `src/paper/backend.py`) is the single construction point;
`hook.maybe_execute_paper`, `tick.run_tick`, and any CLI that builds a
`PaperBroker` go through it. `internal` returns today's `PaperBroker` unchanged.

**BC-4 — ledger/journal/PnL stay the system of record.** The OKX backend still
writes the **same** `PaperStore` files (account/positions/ledger/equity) with the
same shapes; downstream (reflection, PnL, event triggers, `journal_ref_fn`) is
untouched. **Reconciliation** of local ledger rows against OKX demo fills
(`get_open_orders(include_executions=True)` → `get_fills`) is its own task
(Task 2) — the backend must record the **broker's** fill price/qty/fee, and a
reconciliation pass flags drift; it must never *invent* a fill (repo invariant:
never invent a price/number → sentinel + recorded error).

**BC-5 — demo keys, and NEVER live.** OKX keys are NOT env vars — they live in
`~/.vibe-trading/okx.json` loaded by `okx/sdk.py::OKXConfig`/`load_config`
(fields `api_key, api_secret, passphrase, expected_uid, host`). The backend MUST
construct config with **`profile="paper"`** (→ `environment="paper"` →
`flag="1"`, `is_demo=True`) and MUST assert `cfg.is_demo` / `cfg.flag == "1"`
before any `place_order`, failing closed otherwise. **NEVER** use, read, or
reference the `live-readonly` / `live` profiles or `okx-live-trade` — the
order-placing profile is `okx-paper-trade` (`profiles.py:42`) ONLY. Set
`expected_uid` to the demo account UID and pin it (`check_status` UID guard) so a
mis-provisioned key can't silently reach a wrong account. **`agent/src/live/`,
`policy.py` (beyond the scoped Gate-2 relaxation), `order_guard.py`,
`sdk_order_gate.py` are OUT OF SCOPE and untouched.** The OKX demo backend does
**not** route through `execute_live_order` (that gate is for LIVE money).

**BC-6 — kill switch preserved.** `VIBE_PAPER_ENABLED` (unset ⇒ enabled;
`0`/`false`/`""` ⇒ disabled) still short-circuits **before any backend is built
or any order placed**, at every entry point (`hook._paper_enabled`,
`translator._paper_enabled`, `tick.run_tick`). It gates the backend factory too:
disabled ⇒ no OKX client, no network.

---

## 3. Task decomposition (~5 tasks)

Sequential per the playbook. Record `BASE` before each implementer; copy landed
interfaces into the ledger for the next dispatch. All tests **socket-disabled**:
the codebase's hermeticity pattern is **dependency injection** (broker takes
`price_fn`; tick takes `bars_fn`; the OKX adapter must likewise take an injected
SDK/transport) plus **recorded fixtures** of real OKX JSON envelopes — never a
live socket in CI.

### T1 — Backend interface + conformance suite (both backends must pass)

- **Files:** new `src/paper/backend.py` (a `PaperBackend` Protocol/ABC capturing
  the BC-1 surface + `.store`/`.config` + the ledger/position dict shapes, and a
  `build_backend()` factory reading `VIBE_PAPER_BACKEND`); new
  `tests/test_paper_backend_conformance.py`.
- **Interface:** extract the BC-1 surface *from the code, not this doc* — verify
  each signature against `broker.py` on main before writing the Protocol.
- **Tests:** a **shared conformance suite** parametrized over backend fixtures.
  It **runs against the internal broker in CI** (deterministic, injected
  `price_fn`) and asserts the invariants: buy persists cash-first / sell persists
  positions-first (money-state write order), decision idempotency
  (`(run_id, symbol)` journal key + executor `decision_id` retriable-noop rule),
  ledger/position dict shapes, `MandateViolation`/`PriceUnavailable` behavior,
  `market_sell`→None on no position. The **okx-demo** parametrization runs
  **only in an opt-in live mode** (env-gated marker, skipped in CI).
- **Pitfalls:** the internal broker must keep passing byte-for-byte —
  `VIBE_PAPER_BACKEND` unset ⇒ internal is the regression pin.

### T2 — OKX demo adapter (placement + fill polling / reconciliation)

- **Files:** new `src/paper/okx_backend.py`; recorded-envelope fixtures under
  `tests/fixtures/okx/`.
- **Interface:** implements the T1 Protocol. `market_buy`/`market_sell` call an
  **injected** `place_order`-shaped callable (default `okx.sdk.place_order` bound
  to a `profile="paper"` config); parse the OKX result
  (`{"status":"ok","order_id",…}` or `{"status":"error","error"}`), then poll
  fills via `get_open_orders(include_executions=True)`/`get_fills`
  (`fillPx,fillSz,fee,ordId,tradeId,ts`) to build the ledger row from the
  **broker's** numbers. `equity()` maps `get_account_snapshot` `total_equity` +
  `get_positions`. A **reconciliation** helper diffs local ledger rows vs OKX
  fills and emits a recorded discrepancy note.
- **Tests:** all against recorded fixtures — success fill, partial fill, OKX
  `sCode!=0` rejection, `get_fills` empty/lagging (retriable), demo-guard refusal
  when `flag!="1"`.
- **Pitfalls:** **side-dependent write order must survive the backend swap** —
  the buy-cash-first / sell-positions-first invariant is *money-state* discipline,
  independent of where the fill came from; assert it in the conformance suite for
  okx-demo too. Fill lag (order accepted, fill not yet reported) is a
  **retriable** state, never an invented fill.

### T3 — Translator / tick integration behind the env

- **Files:** `src/paper/hook.py`, `src/paper/tick.py` (route broker construction
  through `build_backend()`); `src/paper/translator.py` unchanged if it truly
  only touches the Protocol surface — verify.
- **Interface:** `evaluate_conditionals` stays the **deterministic** bar logic;
  when it decides to sell, the sell routes through the active backend's
  `market_sell`/`_execute_sell` path (BC-2 tick-evaluated). `set_risk` stays local
  metadata under both backends.
- **Tests:** with `VIBE_PAPER_BACKEND=okx-demo` + injected SDK, a journaled
  decision produces an OKX-demo order and a same-shape ledger row; with it unset,
  the internal path is byte-identical (regression pin).
- **Pitfalls:** **decision idempotency must survive the backend swap** — the
  `_already_executed` ledger scan and the retriable-noop exception key on
  `decision_id`/`order_type`/`note`, which the OKX backend must reproduce exactly
  (same `RETRIABLE_NOTE`, same noop rows), or a swap re-executes or dedupes wrong.

### T4 — Failure-mode handling (demo API down, retriable semantics)

- **Files:** `src/paper/okx_backend.py` (+ tests).
- **Behavior:** OKX unreachable / 5xx / timeout / `sCode!=0` → **same
  never-invent discipline** as `PriceUnavailable`: no fill, a **retriable** noop
  ledger row (reuse the translator's `RETRIABLE_NOTE` contract so the tick's
  `_drive_retries` re-drives it, bounded to 7 days), recorded error. Distinguish
  **retriable** (network/lag) from **terminal** (rejection: insufficient demo
  balance, bad instrument) — terminal writes a permanent noop that marks the
  decision executed.
- **Tests:** each branch against recorded fixtures; assert retriable rows do NOT
  block a later retry and terminal rows DO.
- **Pitfalls:** an order that OKX *accepted* but whose fill we can't yet read is
  **not** retriable-as-unplaced (double-order risk) — reconcile by `clOrdId`
  before re-placing; use a deterministic client-order-id keyed on `decision_id`.

### T5 — Docs + paper-CLI backend awareness + hermeticity guard

- **Files:** the paper CLI (`vibe-trading paper …`) surfaces the active backend;
  `docs/crypto-committee.md` honest-limits; `.env.example` documents
  `VIBE_PAPER_BACKEND`; **`tests/conftest.py`** extends the autouse hermeticity
  guard.
- **Tests:** CLI shows backend; docs honest-limit paragraph present.
- **Pitfalls — HERMETICITY GUARD EXTENSION IS MANDATORY IN THIS SAME TASK.**
  `tests/conftest.py` (lines 40-55) pins `VIBE_PAPER_ROOT`/`VIBE_PAPER_ENABLED`
  because a missing guard once leaked ~55 phantom trades into the real account.
  `VIBE_PAPER_BACKEND` now has **network side effects** — the guard MUST also pin
  `VIBE_PAPER_BACKEND=internal` (and never allow an okx-demo default) so no test
  can place a demo order. This is the invariant: *any new env var with
  filesystem/network side effects is added to the guard in the task that
  introduces it.*

---

## 4. Loop-rules application (Feature B specifics)

This is the **highest-risk feature in the roadmap** — first real order placement.
Above the baseline README loop:

- **Top-tier models for BOTH implementer AND reviewer on EVERY task** (not just
  the money/concurrency ones). Money math, provider wire format, idempotency
  across a backend swap, and fill semantics are all present in every task here —
  the README already prescribes top-tier for each; B makes it unconditional.
- **Final review MUST reproduce the failure paths** (README: criticals come from
  reproduction). Specifically reproduce, with recorded fixtures: OKX rejection,
  fill lag, API-down retriable, terminal rejection, and the decision-idempotency
  behavior under a mid-sequence backend swap. Reading the diff is not enough.
- **Live verification REQUIRES user-supplied demo keys.** README's live-verify bar
  ("run the real entry point end-to-end, then inspect the artifacts") requires a
  real OKX demo account (`~/.vibe-trading/okx.json`, `profile="paper"`,
  `expected_uid` pinned). **Plan the graceful stop:** if the user has not
  supplied demo keys, the feature is **NOT done** — do not fake it, do not merge
  on green tests alone (tests-are-not-enough is proven twice). Report "code +
  reviews complete; live verification blocked pending user demo keys" and STOP.
  When keys arrive: place one real demo order end-to-end, then inspect the OKX
  demo account, the local ledger, and the reconciliation output before claiming
  done.

---

## 5. Acceptance criteria / non-goals / effort

### Acceptance criteria

1. Both Gate-1 (evidence) and Gate-2 (recorded user policy-relaxation decision)
   documented as passed before any code.
2. `VIBE_PAPER_BACKEND` unset/`internal` ⇒ byte-identical upstream behavior, pinned
   by a regression test; `okx-demo` routes buys/sells to a real OKX **demo**
   account (`flag="1"`, `is_demo` asserted) and never touches a live profile.
3. Shared conformance suite passes for the internal backend in CI and for okx-demo
   in opt-in live mode; both satisfy write-order, idempotency, and dict-shape
   invariants.
4. Conditional stops/TPs remain tick-evaluated with unchanged fill rules; only the
   triggered sell routes to OKX demo.
5. Ledger/journal/PnL unchanged as system of record; reconciliation against OKX
   fills implemented and its discrepancies recorded (never invented).
6. Kill switch and the extended hermeticity guard both proven by test.
7. README Definition-of-Done met: final "Ready to merge: Yes" with reproduced
   failure paths, full suite green with zero writes outside tmp, live end-to-end on
   real demo keys with artifacts inspected, docs+honest-limits updated, stacked PR.

### Explicit non-goals

- **No live trading.** `agent/src/live/`, `sdk_order_gate.py`, `order_guard.py`,
  the live OKX profiles, and `execute_live_order` stay untouched. `policy.py` is
  relaxed ONLY within the exact scope the user granted (demo execution), live
  execution objectives still rejected.
- **No order types beyond market + tick-managed conditionals.** No native OKX algo
  / stop / OCO / limit / TWAP orders (BC-2 tradeoff). Limit-order support is not
  in scope even though `place_order` accepts it.
- No multi-account, no sub-hourly cadence, no new symbols (roadmap §3 stands).

### Effort

**L** (roadmap). Rough split: T1 conformance harness **M**; T2 OKX adapter +
reconciliation **M–L** (wire-format + fixtures are the bulk); T3 integration
**S–M**; T4 failure modes **M** (idempotency-across-swap is the subtle part); T5
docs/CLI/guard **S**. Plus the two gates and live verification, which are
calendar-time, not code-time. Do not start T1 until both gates clear.

> Note: `docs/` is gitignored — this file needs `git add -f` when the branch is
> eventually cut. Per instructions this plan is written only, not committed.
