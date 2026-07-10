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
