# MiniMax M3 Migration — Phase 0 Findings

Phase 0 of the MiniMax M3 Token Plan migration is a validation spike: it
empirically resolves the unknowns that pick **Path A** (reuse the existing
OpenAI-compatible `ChatOpenAI` adapter against `/v1/chat/completions`) vs
**Path B** (a new Anthropic-compatible adapter against
`/anthropic/v1/messages`) before any integration code is written. No product
code changes ship in this phase — only the probe script
(`scripts/minimax_probe.py`) and this note.

This environment has no `MINIMAX_API_KEY`, so the live probes in section
"How to run the probes" below have **not** been executed here. Section
"Findings" is a template to fill in once someone runs them with a real key.

## What's already known (from platform docs, before probing)

- **Subscription Key ≠ pay-as-you-go API key.** The MiniMax Token Plan issues
  a separate "Subscription Key" credential; it is not guaranteed to behave
  identically to a standard pay-as-you-go `MINIMAX_API_KEY` against every
  endpoint. Whether the Subscription Key authenticates the OpenAI-compatible
  `/v1/chat/completions` surface at all is **the** open question probe 0.1
  answers, and it decides Path A vs Path B.
- An **Anthropic-compatible endpoint** (`https://api.minimax.io/anthropic/v1/messages`,
  Anthropic wire format: `x-api-key` header, `anthropic-version: 2023-06-01`,
  `max_tokens` required) is documented specifically for the Token Plan. If the
  Subscription Key only works there, the migration needs a second adapter path
  (Path B) rather than reusing `ChatOpenAI`.
- **`/v1` authentication is the central unknown** this phase exists to
  resolve — every later phase's design (adapter reuse vs new adapter,
  capability flags, tests) depends on the answer.
- **Throttle reset is documented at roughly 1 minute.** Backoff/retry
  parameters for Phase 2 should be grounded in an observed reset latency, not
  assumed from this number alone — probe 0.4 measures it directly.
- **M3 reasoning must be replayed on multi-turn tool calls.** If a
  reasoning/thinking field is dropped when the assistant's prior turn is
  replayed into a follow-up request, tool-calling conversations are expected
  to degrade (wrong answers, refusal to continue, or the model re-deriving
  reasoning from scratch). Probe 0.3 is designed to make this degradation
  observable by running turn 2 twice — once with reasoning replayed, once
  with it stripped — and diffing the two responses.
- **Thinking is adaptive and can be disabled.** M3 decides on its own whether
  to emit reasoning content per turn; it is not always present, and the exact
  field name it appears under on each wire format (`reasoning_details` /
  `reasoning_content` / inline `<think>` tags on the OpenAI-compatible
  surface, `thinking` content blocks on the Anthropic-compatible surface) is
  unconfirmed until probe 0.3 runs.
- **Temperature default is 1.0, top_p default is 0.95.** MiniMax requires
  temperature strictly greater than 0. The current adapter
  (`agent/src/providers/llm.py`, around the `_build_openai_llm` /
  `build_llm` provider dispatch near lines 612-642) already clamps
  `temperature <= 0.0` up to `0.01` for the `minimax` provider — a
  workaround for the repo's default `LANGCHAIN_TEMPERATURE=0.0` — but does
  not currently push the documented default of `1.0`, and none of the probes
  or the committee smoke run below rely on that clamp: they all set
  `LANGCHAIN_TEMPERATURE=1.0` / `temperature=1.0` explicitly.

## Current adapter state (read before probing)

- `agent/src/providers/capabilities.py` registers `minimax` with only the
  bare minimum: `api_key_env=MINIMAX_API_KEY`, `base_url_env=MINIMAX_BASE_URL`,
  and every other capability flag left at its default
  (`capture_reasoning=False`, `send_reasoning_content=False`,
  `normalize_assistant_content=False`, no `default_headers`,
  no `native_adapter_package`). Compare this to `deepseek` and `moonshot`,
  which both set `capture_reasoning=True` (and `moonshot` also sets
  `send_reasoning_content=True`) — `minimax` today does **not** capture or
  replay reasoning at all. If probe 0.3 confirms M3 needs reasoning replayed
  across turns, `minimax`'s capability flags will need to change in a later
  phase.
- `agent/src/providers/llm.py` (`build_llm`, around lines 612-642) builds a
  plain `ChatOpenAIWithReasoning` for `minimax` today — there is no MiniMax
  native adapter and no Anthropic-compatible code path. The only
  MiniMax-specific logic in that function is the temperature clamp described
  above. If probe 0.1 shows Path B is required, this function needs a new
  branch analogous to the existing `openai-codex` / `deepseek` special cases.
- `agent/src/providers/llm_providers.json` already lists `minimax` with
  `default_model: MiniMax-M3` and `default_base_url: https://api.minimax.io/v1`,
  and `agent/tests/test_llm_provider_defaults.py` pins that default model —
  this is registry/config only, it does not imply the endpoint has been
  verified to authenticate.

## Known limitations (Phase 1)

- **Path B reasoning round-trip is unimplemented/unverified.** Phase 1's Path B
  (`MINIMAX_BASE_URL` containing `/anthropic`) constructs a stock
  `langchain-anthropic` `ChatAnthropic` client, but the ReAct loop replays
  history as OpenAI-format dicts carrying reasoning in `reasoning_content` —
  nothing translates that into Anthropic `thinking` content blocks, so M3
  reasoning is most likely **not** replayed across tool turns on Path B.
  Implementing the translation is deferred until a live probe 0.3 run confirms
  the exact thinking-block round-trip shape. **Path A (OpenAI-compatible `/v1`
  with `reasoning_split` / `reasoning_details` capture-and-replay) is the
  evidence-backed path** — its replay behavior is pinned by the mocked-stream
  regression tests in `agent/tests/test_minimax_provider_hardening.py`.
- **Path A assumes `reasoning_details` is a STRING.** Capture, transcript
  accumulation, and replay all treat the field as a plain string (the mocked
  regression tests feed string values, and the tolerance for an empty-string /
  missing field is exercised there). If M3 actually returns `reasoning_details`
  as a structured **list** (e.g. an array of reasoning blocks), all three stages
  mishandle it — capture stores the wrong type, accumulation concatenates
  incorrectly, and replay re-emits a malformed field. Live probe 0.3 must verify
  the exact wire shape before Path A is trusted for multi-turn tool calls.

## Known limitations (Phase 2)

- **Layer-deadline math models a single run owning the LLM gate.** With the
  global gate enabled, `compute_layer_deadline` (`agent/src/swarm/runtime.py`)
  bounds a run's assumed parallelism by `min(SWARM_MAX_WORKERS,
  VIBE_LLM_MAX_CONCURRENT)`, so one run never sizes its waves for more
  concurrency than the gate allows. What it does **not** model is *cross-run*
  contention: N simultaneous swarm runs (or a run plus a busy main agent /
  scheduler) share the same `VIBE_LLM_MAX_CONCURRENT` slots, so a layer can
  queue on the gate beyond any single run's deadline and be marked timed out
  even though every worker is healthy. For multi-run deployments, prefer
  single-run-at-a-time scheduling; if concurrent runs are required, raise the
  per-task budget (`timeout_seconds` / retries in the preset, which the layer
  deadline is derived from) to absorb the expected gate queueing.
- **Removed retry-delay knobs.** The old fixed-delay env vars
  `VT_STREAM_RETRY_DELAY_S` / `SWARM_STREAM_RETRY_DELAY_S` are now **ignored** —
  Phase 2 replaced the fixed delay with capped exponential backoff + jitter. Use
  `VT_STREAM_RETRY_BASE_S` (base delay, default 2s) instead.

## How to run the probes

Requires `httpx` (already a repo dependency) and a real `MINIMAX_API_KEY`.
No other setup needed — the script is stdlib + `httpx` only.

```bash
export MINIMAX_API_KEY=sk-...   # Subscription Key or pay-as-you-go key
python scripts/minimax_probe.py all
```

Individual probes can be run standalone:

```bash
python scripts/minimax_probe.py auth                         # 0.1
python scripts/minimax_probe.py models [--endpoint openai|anthropic]   # 0.2
python scripts/minimax_probe.py reasoning                     # 0.3
python scripts/minimax_probe.py concurrency [--endpoint openai|anthropic]  # 0.4
```

`models` and `concurrency` default to the OpenAI-compatible endpoint; pass
`--endpoint anthropic` to target the Anthropic-compatible endpoint instead
(e.g. once `auth` shows Path A fails and Path B works). `reasoning` always
probes both surfaces in one run. The script exits nonzero immediately with a
clear message if `MINIMAX_API_KEY` is unset, and all request bodies use
`temperature=1.0` (MiniMax requires `> 0`).

Redirect output to a file for the findings table below, e.g.:

```bash
python scripts/minimax_probe.py all 2>&1 | tee /tmp/minimax-probe-output.txt
```

## Findings (fill in after running the probes)

| # | Task | Exit criterion | Result | Notes |
|---|---|---|---|---|
| 0.1 | Subscription Key vs `/v1/chat/completions` | Documented yes/no (+ error shape). **Decides Path A vs B** | _not yet run — no MINIMAX_API_KEY in this environment_ | |
| 0.2 | Model coverage of the Token Plan | List of usable models → fixes the tier table | _not yet run_ | |
| 0.3 | Reasoning round-trip shapes | Notes on exact field names for Phase 1 | _not yet run_ | |
| 0.4 | Concurrency probe | Backoff parameters for Phase 2 grounded in reality | _not yet run_ | |

Fill in the "Result" column with a one-line verdict (e.g. "Path A: OK
(200)" / "Path A: FAIL (401, error.type=invalid_api_key)") and "Notes" with
anything a later phase needs (exact field names, model availability caveats,
observed `Retry-After` values, recovery latency).

## Probe 0.5 — baseline committee run

Task 0.5 is a smoke run of the existing `crypto_committee` swarm preset
pointed at MiniMax, run as-is (no adapter changes) to produce a failure
inventory that feeds Phases 1–2. It is not part of `scripts/minimax_probe.py`
— it exercises the real swarm engine, not raw HTTP.

**Before running (historical, pre-Phase 3):** `agent/src/swarm/presets/crypto_committee.yaml`
used to pin `model_name: deepseek-v4-pro` on two manager seats
(`research_manager` and `portfolio_manager`) as a hardcoded per-agent model
override. Per the preset's own "engine contract" comment, `model_name` uses
the same provider as `LANGCHAIN_PROVIDER` — so with `LANGCHAIN_PROVIDER=minimax`
those pins would have asked MiniMax for a model named `deepseek-v4-pro`, which
does not exist on that provider. **As of Phase 3 (see below) this is no
longer a hardcoded vendor name** — both seats now read `model_name:
${VIBE_DEEP_MODEL}`, which resolves to `None` (global model) when unset, so
the smoke run needs no manual edit to the preset anymore.

**Run with:**

```bash
export LANGCHAIN_PROVIDER=minimax
export LANGCHAIN_MODEL_NAME=MiniMax-M3
export LANGCHAIN_TEMPERATURE=1.0
export SWARM_MAX_WORKERS=3
export MINIMAX_API_KEY=sk-...
# then invoke the crypto_committee preset through whatever CLI/entry point
# this repo uses to run a swarm preset end to end (see agent/src/swarm/ and
# the repo's CLI docs for the exact invocation).
```

Capture the full run log (success or instructive failure) — that log is the
failure inventory Phases 1–2 consume, so prefer `tee`-ing it to a file rather
than letting it scroll.

## Phase 3 — Quick/deep model tiering

`agent/src/swarm/presets.py` now resolves `${ENV_VAR}` / `${ENV_VAR:-default}`
placeholders in a preset agent's `model_name` field at preset-build time
(`_resolve_model_name`, applied in both `build_run_from_preset` and
`inspect_preset`). This makes presets provider-agnostic instead of
hardcoding a vendor model string:

- `${VAR}` resolves to `os.environ["VAR"]` when set and non-empty.
- `${VAR:-default}` falls back to `default` when `VAR` is unset/empty.
- Unset with no default (or an empty default) resolves to `None`, which
  makes the agent fall back to the run's global model
  (`LANGCHAIN_MODEL_NAME`) — the same behavior as omitting `model_name`
  entirely. This is model_name-only substitution, not a general templating
  engine.

`agent/src/swarm/presets/crypto_committee.yaml` uses this for two tiers:
`research_manager` / `portfolio_manager` read `model_name: ${VIBE_DEEP_MODEL}`
(deep tier — the debate judge and final decision seats), and every other
seat (`market_analyst`, `onchain_analyst`, `news_analyst`,
`sentiment_analyst`, `reflection_officer`, `bull_researcher`,
`bear_researcher`, `trader`, `risky_analyst`, `safe_analyst`,
`neutral_analyst`) reads `model_name: ${VIBE_QUICK_MODEL}`. With both env
vars unset, every seat resolves to `None` and uses the global model — this
is an intentional behavior change from the pre-Phase-3 preset (which pinned
`deepseek-v4-pro` on the two manager seats unconditionally); it is controlled
purely from `.env` (see `agent/.env.example`'s "Model tiering (Phase 3)"
block).

**Same-provider constraint (unchanged, pre-existing):** `SwarmAgentSpec.model_name`
(`agent/src/swarm/models.py`) only substitutes the model string passed to
`ChatLLM(model_name=...)` (`agent/src/providers/chat.py`), which in turn
calls `build_llm(model_name=...)` (`agent/src/providers/llm.py`). `build_llm`
always constructs its client from the current `LANGCHAIN_PROVIDER` env var
(api key, base URL, adapter selection) — there is no way to select a
different provider per seat via `model_name` alone. Any `VIBE_DEEP_MODEL` /
`VIBE_QUICK_MODEL` / `VIBE_COMPACT_MODEL` value MUST name a model available
under the run's single configured `LANGCHAIN_PROVIDER`.

An optional utility tier applies the same mechanism to context compaction:
`AgentLoop._auto_compact` (`agent/src/agent/loop.py`) routes its
summarization call through `ChatLLM(model_name=os.getenv("VIBE_COMPACT_MODEL"))`
when that env var is set (same same-provider constraint), and through the
loop's main model when unset (unchanged upstream behavior).

## Phase 6 — Reflection loop automation + crypto benchmark fix

**Scheduled resolution.** The committee decision journal
(`agent/src/committee/journal.py`, tool `agent/src/tools/committee_journal_tool.py`)
previously resolved past decisions' 24h/72h/7d outcomes only when the *next*
committee run's reflection officer happened to fire — a day with no committee
run just left due outcomes unresolved and no lesson accrued. With
`VIBE_TRADING_ENABLE_SCHEDULER=1`, server startup now also registers a daily
scheduled-research job (`agent/src/api/scheduled_routes.py::_ensure_decision_journal_job`,
job id `decision-journal-reflection`, schedule `0 0 * * *` UTC) whose prompt
instructs the agent to call `decision_journal action=resolve_due` and then
`action=reflect` for every entry the resolve pass surfaces. Registration is
idempotent — a restart never resets the job's schedule or clobbers a manual
edit — and dispatch reuses the existing scheduled-research executor/session
runtime unchanged (`agent/src/scheduled_research/`); this is new *wiring*,
not a new execution mechanism.

**System-cron alternative (scheduler off).** If you keep
`VIBE_TRADING_ENABLE_SCHEDULER` unset/`0` — the upstream default — nothing
runs this automatically. Reflection text requires judgment (`resolve_due` is
pure arithmetic, but the written lesson is not), so the equivalent is a
system-cron entry that invokes the same one-shot agent prompt via the
existing `vibe-trading run` CLI subcommand, e.g.:

```cron
# Daily at 00:00 UTC — resolve due committee decisions and write reflections.
0 0 * * * cd /path/to/Vibe-Trading-cryptoagent/agent && \
  vibe-trading run -p "Call decision_journal action='resolve_due'. For each \
  entry in reflection_due, write a 2-4 sentence reflection citing the \
  realized raw return and alpha at the primary horizon and whether the \
  rating was directionally right, then call decision_journal \
  action='reflect' (entry_id, reflection)." >> ~/.vibe-trading/logs/reflection-cron.log 2>&1
```

This doc is the chosen home for that alternative (over the
`committee_journal_tool.py` docstring) because it is the file `.env.example`
and the rest of the Phase 0–5 notes already point users to for env-var
behavior — a user deciding whether to leave the scheduler off is reading
migration/config docs, not swarm tool source. The tool's docstring carries
only a one-line pointer back here for readers browsing the code.

**Crypto benchmark routing fix.** `agent/backtest/benchmark.py` maps
`crypto -> BTC-USDT` (`MARKET_BENCHMARKS`), but `_fetch_benchmark` fetched
every ticker via yfinance only — which cannot reliably resolve every
OKX-format symbol, so crypto benchmark comparison in *backtests* was silently
broken (a fetch failure is caught by `resolve_benchmark` and just returns
`None`, no benchmark line). `_fetch_benchmark` now routes crypto-format
tickers (`BTC-USDT`, detected the same way `_infer_market` already does)
through the backtest loader registry (`okx` -> `ccxt`, via the new
`backtest.loaders.registry.fetch_ohlcv_with_fallback` helper) — the same
source chain the decision journal's alpha calc already uses
(`src/tools/committee_journal_tool.py::_loader_fetch_bars`). Every other
market (`us_equity`, `hk_equity`, `a_share`, `futures`) keeps using yfinance,
unchanged. The journal's own alpha path was not touched — it already used
the loader registry and is unaffected by this fix.

**Lessons-to-manager experiment (flagged, default OFF).** TradingAgents
deliberately restricts reflection/memory context to the Portfolio Manager;
`crypto_committee.yaml` follows that on purpose (`task-decision` is the only
task with `past_lessons: task-reflection` in `input_from`).
`VIBE_LESSONS_TO_MANAGER=1` mirrors that same wiring onto the
`research_manager` task too (`agent/src/swarm/presets.py::_inject_lessons_into_manager`),
for A/B testing whether the debate-judging seat benefits from the same
lessons the PM sees. Unset/`0` reproduces the current preset graph exactly.

## Final configuration (Phase 7)

This section maps `cfg.md`'s target-state config block onto what's actually
implemented on this branch, as `agent/.env.example` ships it today (see that
file for the authoritative, commented, copy-pasteable block — it is already
assembled with a "MiniMax M3 Token Plan (active)" block at the top of the
provider section, and a "DeepSeek (rollback path from MiniMax)" block kept
commented immediately below the other alternate providers as the escape
hatch called for below). Every var here is verified present in code as of
this phase.

| Var | Where it's read | Verified default / behavior |
|---|---|---|
| `LANGCHAIN_PROVIDER=minimax` | `agent/src/providers/llm.py:build_llm` | Selects the MiniMax branch; Path A (`ChatOpenAIWithReasoning`) vs Path B (`ChatAnthropic`) chosen by `MINIMAX_BASE_URL`. |
| `LANGCHAIN_MODEL_NAME=MiniMax-M3` | `agent/src/providers/llm_providers.json` | Registry default; pinned by `agent/tests/test_llm_provider_defaults.py`. |
| `MINIMAX_API_KEY` | `agent/src/providers/capabilities.py` (`api_key_env`) | Subscription Key or pay-as-you-go key. |
| `MINIMAX_BASE_URL` | `_minimax_base_url` / `_minimax_uses_anthropic_endpoint` (`llm.py:430-447`) | `/v1` (default) → Path A. Containing `/anthropic` (case-insensitive substring match) → Path B, which requires `pip install "vibe-trading-ai[minimax]"` (`langchain-anthropic`) and raises an explicit ImportError with that install hint if missing. |
| `LANGCHAIN_TEMPERATURE=1.0` | `llm.py` MiniMax temperature clamp (~line 805-812) | **Only self-triggers when temperature is left at the repo's own upstream default of `0.0`** — the clamp promotes `0.0` → `1.0` and *also* sets `top_p=0.95` in that case. Since `.env.example` sets `LANGCHAIN_TEMPERATURE=1.0` explicitly, the clamp branch is not entered for the recommended config: `top_p` is left unset (not forced to `0.95`) and MiniMax's own server-side default (documented as `0.95`) applies. Functionally equivalent for the recommended config, but worth knowing if you ever set `LANGCHAIN_TEMPERATURE` to something other than `0.0` or `1.0` — no clamp applies once temperature is non-zero. |
| `TIMEOUT_SECONDS=180`, `MAX_RETRIES=4` | Generic LLM client construction | Not MiniMax-specific plumbing; just the recommended values for M3's longer thinking turns. |
| `MINIMAX_THINKING` (optional; unset = adaptive) | `_minimax_thinking_mode` (`llm.py:450-457`) | `disabled` turns off M3 thinking (quick tier); any other value/unset keeps adaptive (M3 decides per turn). |
| `MINIMAX_MAX_TOKENS` (optional; default `4096`) | `_build_native_minimax_anthropic` (`llm.py:461-514`) | Path B (Anthropic adapter) only — `max_tokens` is required by that wire format. |
| `VIBE_LLM_MAX_CONCURRENT=3` | `agent/src/providers/chat.py` (`_resolve_gate_limit`) | Process-wide `BoundedSemaphore`; `0` (default) disables the gate entirely (byte-identical to upstream). 3 matches the MiniMax Token Plan Plus tier's observed concurrent-agent ceiling. |
| `SWARM_MAX_WORKERS=3` | `agent/src/tools/swarm_tool.py:758` | Per-run thread-pool hint; code default is `4` — `.env.example` recommends `3` to match the gate cap so a single committee run doesn't routinely queue on `VIBE_LLM_MAX_CONCURRENT`. |
| `VT_STREAM_RETRY_MAX=5`, `VT_STREAM_RETRY_BASE_S=2` | `agent/src/providers/backoff.py` | These are also the code defaults — `.env.example` lists them commented as documentation, not because 5/2 need overriding. |
| `VIBE_RUN_TOKEN_BUDGET_WARN=3000000` (optional; default `0` = off) | `agent/src/core/token_budget.py` | Observability-only warning, no hard cutoff. |
| `VIBE_DEEP_MODEL` / `VIBE_QUICK_MODEL` | `agent/src/swarm/presets.py::_resolve_model_name`, consumed by `crypto_committee.yaml` | Unset ⇒ `None` ⇒ every seat falls back to `LANGCHAIN_MODEL_NAME`. Must name a model on the same `LANGCHAIN_PROVIDER`. |
| `VIBE_COMPACT_MODEL` (optional) | `agent/src/agent/loop.py::AgentLoop._auto_compact` | Not a committee-seat tier — routes context-compaction summarization for *any* agent run, main-agent or swarm. |
| `VIBE_DEBATE_ROUNDS` / `VIBE_RISK_ROUNDS` (optional; default `1` each) | `agent/src/swarm/presets.py::_resolve_debate_rounds`, `crypto_committee.yaml`'s `debates:` block | Capped at 4; rejected above that at preset-build time. `1`/`1` reproduces the pre-Phase-4 single-pass graph exactly. |
| `VIBE_COMMITTEE_BENCHMARK=BTC-USDT` (default) | `agent/src/committee/journal.py` (`DEFAULT_BENCHMARK`), `agent/src/tools/committee_journal_tool.py` (`BENCHMARK_ENV`) | Alpha benchmark for the decision journal; always fetched via the loader registry (okx → ccxt) regardless of whether this is explicitly set. |
| `VIBE_TRADING_ENABLE_SCHEDULER=1` (default off) | `agent/src/api/scheduled_routes.py` | Enables the daily `decision-journal-reflection` job (and any other scheduled-research jobs) at server startup. |
| `VIBE_LESSONS_TO_MANAGER` (optional; default off) | `agent/src/swarm/presets.py::_inject_lessons_into_manager` | A/B experiment knob, not part of the recommended target state — omitted from `cfg.md`'s block and from `.env.example`'s active lines (documented, commented). |

**`.env.example` coherence check (Phase 7).** `agent/.env.example` was
inspected end-to-end for the config landed incrementally across Phases 0-6.
It already assembles a single coherent MiniMax-active block (provider
selection with Path A/B comments, LLM parameters, concurrency governance,
model tiering, debate depth, and the learning loop) in one place, with every
optional var correctly commented out and every default matching the code
verified above — no duplication, no stale phase-numbered TODOs, no
conflicting values found. The DeepSeek block is present and commented,
immediately below the provider alternatives, satisfying the "keep the
DeepSeek block commented in `.env.example` permanently as the escape hatch"
requirement below. No changes were needed.

## Transition protocol (DeepSeek → MiniMax)

The committee's own decision journal (`docs/crypto-committee.md#decision-journal--learning-loop`)
is the instrument for this evaluation — no separate harness is built or
needed. Four steps:

1. **Freeze two `.env` configs.** A DeepSeek-baseline block (provider =
   `deepseek`, model = `deepseek-v4-pro`, `VIBE_DEBATE_ROUNDS`/`VIBE_RISK_ROUNDS`
   unset or matching whatever the baseline period used) and the MiniMax
   config from the table above. Keep both as literal, copy-pasteable blocks
   — `agent/.env.example` already keeps the DeepSeek block commented
   immediately below the MiniMax block for exactly this purpose; do not let
   the two configs drift by editing one in place. When restoring the DeepSeek
   block, restore its frozen LLM parameters together with the provider lines —
   `LANGCHAIN_TEMPERATURE` (0.0 baseline vs MiniMax's 1.0) plus `TIMEOUT_SECONDS`
   / `MAX_RETRIES` (120 / 2 baseline vs MiniMax's 180 / 4) — or DeepSeek silently
   runs at MiniMax's parameters and the A/B comparison is invalid.
2. **Run daily via the Phase 6 scheduler.** Start the server with
   `VIBE_TRADING_ENABLE_SCHEDULER=1` and register one scheduled-research job
   per asset in the fixed universe (e.g. `BTC-USDT`, `ETH-USDT`, `SOL-USDT`),
   each with a daily cron schedule and a prompt that runs the
   `crypto_committee` swarm on that asset with a consistent `timeframe`
   (e.g. `"Run the crypto_committee swarm on BTC-USDT for a 72h swing
   decision."` — the agent calls `run_swarm` itself). Create jobs over REST
   per the README's "Scheduled research" section, e.g.:
   ```bash
   curl -X POST http://localhost:8899/scheduled-runs \
     -H "Content-Type: application/json" \
     -d '{"prompt":"Run the crypto_committee swarm on BTC-USDT for a 72h swing decision.","schedule":"0 1 * * *"}'
   ```
   (add `-H "Authorization: Bearer $API_AUTH_KEY"` if auth is enabled.)
   Repeat for ETH-USDT and SOL-USDT at staggered times so the LLM gate isn't
   fighting itself. Run for **10-14 days** on the MiniMax build; if quota
   allows, alternate-day baseline (DeepSeek) runs on the same universe give
   a same-window comparison rather than a different-week one.
3. **Compare metrics from `journal.jsonl` plus operational telemetry.** Per
   horizon (24h/72h/7d), pull `direction_correct` rate and mean `alpha` for
   each build from the journal (`decision_journal action=list`, or read
   `~/.vibe-trading/committee/journal.jsonl` directly — see the [entry
   format](crypto-committee.md#journal-entry-format-one-json-object-per-line)).
   Alongside decision quality, track operational health per build: run
   failures, retry counts (`agent/src/providers/backoff.py`'s retry
   schedule), 429/`gate_wait_seconds` occurrences
   (`llm_gate_wait` events, `agent/src/swarm/worker.py`), wall time per run,
   and tokens/run against the ~5-hour Token Plan window
   (`VIBE_RUN_TOKEN_BUDGET_WARN` warnings, if any fired).
4. **Cut over when both hold:** the MiniMax build is *operationally clean*
   (zero failed runs in the last 5 days of the window) **and** decision
   quality is statistically indistinguishable-or-better than the DeepSeek
   baseline over the same window. Cutover is deploying the MiniMax `.env`
   block as the new default. **Rollback is a pure `.env` swap** — restore
   the DeepSeek block (still commented in `agent/.env.example`, kept there
   permanently as the escape hatch) and restart; no code changes are
   involved either direction.

**Honest caveat.** 10-14 daily decisions per asset is an **operational smoke
test, not statistical proof of decision quality** — n is tiny (roughly 10-14
per asset, 30-42 across a 3-asset universe) and crypto is noisy; a handful of
lucky or unlucky calls can swing `direction_correct` rate and mean alpha well
outside what a larger sample would show, and neither build's week-two
numbers are a verdict on the underlying model's trading judgment. What this
window *does* establish reliably is operational health (does the MiniMax
integration run cleanly under real daily load — throttling, retries, context
length, reasoning replay across turns) and whether the journal mechanism
itself is functioning (decisions appended, horizons resolving, reflections
written). Treat the journal as what it is: a way to make decision quality
*observable over time*, not a one-shot verdict after two weeks. If a cutover
decision is made on this window, keep collecting journal data afterward and
revisit the comparison once a materially larger sample has accrued.
