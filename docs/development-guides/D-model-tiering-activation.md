# Feature D: Model-Tiering Activation

Companion guide to `docs/development-guides/README.md` (binding process
contract — read that first) and `docs/optimization-roadmap.md` §2 direction D.
This feature is mostly **config + measurement**, not code: `VIBE_DEEP_MODEL` /
`VIBE_QUICK_MODEL` already exist and are wired into
`agent/src/swarm/presets/crypto_committee.yaml` (deep tier on 2 seats —
`research_manager`, `portfolio_manager`; quick tier on the other 11 seats).
Today both env vars are unset in the deployed `.env`, so every seat runs on
MiniMax-M3 (the global model). Activating the quick tier means setting
`VIBE_QUICK_MODEL=MiniMax-M2.7-highspeed` (or a thinking-disabled M3 fallback,
see below) and validating it does not degrade decision quality.

## 1. Goal & evidence gates

Goal: cut per-run token cost and wall-clock on the 11 quick seats without a
statistically-worse decision quality outcome, gated by real evidence rather
than assumption. Three preconditions from the roadmap, all must clear before
flipping the env var in the deployed `.env`:

1. **Probe 0.2 (Feature C prerequisite) — model coverage.** The Token Plan
   key must be confirmed to serve `MiniMax-M2.7-highspeed` under
   `LANGCHAIN_PROVIDER=minimax`. `SwarmAgentSpec.model_name` overrides are
   constrained to the run's single configured provider (documented in the
   preset's own "engine contract" comment and in
   `docs/minimax-migration-notes.md` Phase 3) — there is no per-seat provider
   switch, only a per-seat model-string switch. If probe 0.2
   (`scripts/minimax_probe.py models`) shows the key does **not** cover
   M2.7-highspeed, D falls back to the M3+thinking-disabled tier (§3 covers
   whether that fallback needs a code change).
2. **A journal quality baseline must exist.** Per roadmap §1a: if M3-only
   24h `direction_correct` is already weak/noisy on an M3-only baseline
   window, do not introduce a cheaper quick-tier model until that baseline
   exists to compare against — there is nothing to A/B against otherwise.
3. **Throttling evidence decides urgency, not eligibility.** Per roadmap
   §1c: zero 429s and near-zero `gate_wait_seconds` at the 12-runs/day
   cadence make tiering optional (a cost/wall-clock nicety); observed
   429s or gate-wait accumulating into layer-deadline timeouts make tiering
   a priority. Read this from the same telemetry the baseline window already
   produces — no separate instrumentation needed.

## 2. The A/B protocol

Adapted directly from the migration notes' "Transition protocol
(DeepSeek → MiniMax)" (`docs/minimax-migration-notes.md`), reusing the same
instrument (the decision journal) rather than building a separate harness.

1. **Freeze the M3-only baseline stats first.** Before touching
   `VIBE_QUICK_MODEL`, snapshot the baseline window's journal and telemetry:
   per-horizon `direction_correct` rate and mean `alpha` from
   `~/.vibe-trading/committee/journal.jsonl` (only *resolved* entries —
   `horizons.<h>.resolved_at` present, per roadmap §1a), realized PnL net of
   fees and fee/slippage drag from `~/.vibe-trading/paper/{ledger.jsonl,
   equity.jsonl}` (fields: `realized_pnl`, `fee_paid`, `fees_paid`,
   `slippage_paid` — roadmap §1b), and ops metrics (429 count, `gate_wait_seconds`
   distribution, tokens/run, wall-clock per run, failed runs/retries — roadmap
   §1c). Keep this as a literal copy, the same discipline the migration notes
   use for the DeepSeek-baseline `.env` block — do not let it drift.
2. **Flip the tier.** Set `VIBE_QUICK_MODEL=MiniMax-M2.7-highspeed` in `.env`
   if probe 0.2 confirmed coverage. If it did not, use the fallback tier
   instead: keep `VIBE_QUICK_MODEL` unset (quick seats stay on the global
   M3) and set `MINIMAX_THINKING=disabled` — this is process-global today
   (see §3), so it disables thinking for **every** seat including the two
   deep seats, which is an honest limitation of the fallback, not a silent
   compromise (call this out explicitly in the run notes if used).
3. **Run the same cadence for a comparable window.** Same asset universe,
   same 2-hourly committee cadence, same wall-clock length as the baseline
   window (the roadmap's 72h run, or longer if the baseline was longer) —
   comparing a 3-day tiered window against a 14-day baseline window is not a
   fair A/B.
4. **Compare, per horizon, three categories of metric:**
   - **Decision quality:** `direction_correct` rate and mean `alpha` at
     24h/72h/7d, resolved entries only (same journal fields as step 1).
   - **Executed money:** `realized_pnl` (net of exit fees per
     `docs/crypto-committee.md#pnl-aware-reflection`), `fee_paid` +
     `fees_paid` + `slippage_paid` drag vs gross alpha, from `ledger.jsonl`.
   - **Ops metrics:** tokens/run (vs the ~650k/run reference in the
     migration notes' quota-mechanics section), wall-clock per run, 429
     count, `gate_wait_seconds` distribution (`llm_gate_wait` events,
     `agent/src/swarm/worker.py`).
5. **Rollback is a single env unset.** Remove `VIBE_QUICK_MODEL` (or
   `MINIMAX_THINKING`) from `.env` and restart — no code changes either
   direction, matching the migration notes' "pure `.env` swap" rollback
   pattern.
6. **Small-n honesty rule (verbatim from the migration notes, applies
   identically here):** a run at 12 runs/day over a few days is an
   **operational smoke test, not statistical proof** of decision-quality
   parity — n is small and crypto is noisy; a few lucky or unlucky calls can
   swing `direction_correct` and mean alpha outside what a larger sample
   would show. Treat any A/B verdict from this window as directional. What
   the window *does* establish reliably is operational health (does the
   quick tier run cleanly, without new failures) and the ops-metric deltas
   (tokens/wall-clock/429s), which are measured directly rather than
   inferred from a small sample. If a cutover decision is made on this
   window, keep collecting journal data afterward and revisit once a larger
   sample has accrued.

## 3. Possible code tasks (only if gaps emerge)

None of these are known-required — each is a hypothesis to verify against
the actual A/B run before committing to a code task.

- **Per-seat model override vs. reasoning-capture capability — verified
  fine.** `worker.py:338` builds `ChatLLM(model_name=agent_spec.model_name)`
  fresh per task invocation (no cross-task caching); `build_llm`
  (`agent/src/providers/llm.py:770-789`) resolves `name = model_name or
  LANGCHAIN_MODEL_NAME` (the per-seat override wins when set) and calls
  `get_provider_capabilities(provider, name)`. However,
  `get_provider_capabilities` (`agent/src/providers/capabilities.py:149-170`)
  keys its lookup on the **provider string**, not the model string, for any
  non-OpenAI provider (lines 165-166) — so `MiniMax-M3` and
  `MiniMax-M2.7-highspeed` both resolve to the same `minimax` capability
  record (`reasoning_split_extra_body=True`, capabilities.py:121). Net
  effect: no stale-cache bug, and no code task needed for correctness — but
  also no per-model differentiation within a provider. If M2.7-highspeed
  turns out to need different reasoning-capture handling than M3 (unverified
  — probe 0.3 territory), that would become a real gap; nothing in this A/B
  currently indicates it does.
- **`MINIMAX_THINKING` is process-global, not per-seat — confirmed, and a
  real gap if the fallback tier is used.** `_minimax_thinking_mode()`
  (`llm.py:461-469`) reads `os.getenv("MINIMAX_THINKING", "")` bare on every
  call, from both `build_llm` (llm.py:853-855) and
  `_build_native_minimax_anthropic` (llm.py:525). `SwarmAgentSpec`
  (`agent/src/swarm/models.py:60-84`) has no `thinking` field — there is no
  threading path from a preset's per-seat config down to a per-call
  `thinking` kwarg today. **If probe 0.2 says M2.7-highspeed is not covered**
  and the fallback (M3 + `MINIMAX_THINKING=disabled`) is adopted as the
  standing quick tier (not just a one-off A/B), this becomes a small code
  task: add an optional `thinking_mode` field to `SwarmAgentSpec`, resolve it
  in `presets.py` the same way `_resolve_model_name` resolves `model_name`
  (a `${VIBE_QUICK_THINKING:-adaptive}`-style placeholder), thread it through
  `ChatLLM` → `build_llm(..., thinking_mode=...)`, and have `build_llm` prefer
  the per-call value over `_minimax_thinking_mode()`'s global env read.
  Defer this until the fallback is actually needed — do not build it
  speculatively.
- **Token telemetry does not carry the model string per task — confirmed,
  a real gap for post-hoc per-tier cost attribution.** `_estimate_tokens`
  (`worker.py:113-162`) returns `(input_tokens, output_tokens)`;
  `WorkerResult` (`agent/src/swarm/models.py:204-224`) stores only those two
  counts, no `model_name` field; the `task_completed`/`task_failed` events
  (`agent/src/swarm/runtime.py:396-408`, `419-425`) likewise carry
  `{"input_tokens", "output_tokens", ...}` keyed by `task_id` only. Per-tier
  cost attribution is still **derivable**, just not free: join the run's
  `task_id → agent_id` mapping (visible via `inspect_preset`'s `tasks`
  output, `agent/src/swarm/presets.py:481-489`, or a run's own task list)
  against the per-task token counts, then bucket by whether that
  `agent_id`'s resolved seat was deep or quick. This join is a one-off
  analysis script, not a code change to the engine — flag it as a possible
  task only if the manual join proves too fragile to repeat across the A/B
  window.

## 4. Loop rules application, acceptance criteria, effort

**Loop rules.** If any code task from §3 is actually undertaken (the
`thinking_mode` threading is the only one likely to qualify), it follows the
full per-task loop in `docs/development-guides/README.md` verbatim: fresh
implementer, TDD with RED before GREEN (a failing test asserting a per-seat
`thinking` kwarg reaches `build_llm` before the wiring exists), independent
reviewer with a diff package, fix round for Critical/Important, ledger entry.
Config-only steps (setting `VIBE_QUICK_MODEL`, `MINIMAX_THINKING`) skip
implementation review but still get **live verification**: run one real
`crypto_committee` swarm end to end after flipping the env var, then inspect
the run's trace to confirm the right model actually served each seat —
concretely, read the `task_heartbeat` events in that run's `events.jsonl`
(`agent/src/swarm/store.py:243-270`), which embed
`tool_name=f"llm:{agent_spec.model_name or 'default'}"` per streaming call
(`worker.py:469-476`, `493`) — an analyst-seat heartbeat should read
`llm:MiniMax-M2.7-highspeed` (or `llm:default` if the quick tier is unset)
and a judge-seat heartbeat should read `llm:default` when `VIBE_DEEP_MODEL`
is left unset in the A/B (deep seats inherit the global model), or `llm:MiniMax-M3` (or whatever
`VIBE_DEEP_MODEL` names). Note `SwarmRun.provider`/`.model` are explicitly
run-level-only fields (`agent/src/swarm/models.py:167-169`) that do **not**
reflect per-agent overrides — do not use them as the verification source;
the per-task heartbeat is the only trace surface that names the resolved
per-seat model.

**Acceptance criteria.**
- Decision quality statistically indistinguishable-or-better than the M3-only
  baseline (per-horizon `direction_correct` and mean `alpha`, honest small-n
  caveat applied — §2 step 6), **and**
- A measurable token reduction on the quick tier — target **≥30%** tokens/run
  reduction (directional target; adjust once the baseline's actual tokens/run
  is known against the ~650k/run reference figure in the migration notes'
  quota-mechanics section — do not treat 30% as load-bearing until the real
  baseline number is in hand), **and**
- Zero new failed runs / new 429s attributable to the tier flip (ops
  regression check, not just an improvement check).

**Effort.** **S** for the config change itself (one or two env vars, no code
diff, additive and flag-gated per the repo invariants — unset ⇒ byte-identical
upstream behavior). **M** for the validation: freezing the baseline, running
a comparable A/B window, and reading three categories of metrics out of the
journal/ledger/telemetry by hand. Any `thinking_mode` threading task from §3,
if it becomes necessary, is a small (**S**) code task on top of that.
