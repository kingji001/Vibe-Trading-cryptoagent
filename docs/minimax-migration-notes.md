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

**Before running:** `agent/src/swarm/presets/crypto_committee.yaml` pins
`model_name: deepseek-v4-pro` on two manager seats (`research_manager` and
`portfolio_manager`, currently around lines 250 and 375) as an explicit
per-agent model override. Per the preset's own "engine contract" comment,
`model_name` uses the same provider as `LANGCHAIN_PROVIDER` — so with
`LANGCHAIN_PROVIDER=minimax` these pins would ask MiniMax for a model named
`deepseek-v4-pro`, which does not exist on that provider. For the smoke run,
either comment out / delete both `model_name: deepseek-v4-pro` lines (so
every agent falls back to the global `LANGCHAIN_MODEL_NAME`), or otherwise
ensure they're ignored, before invoking the committee.

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
