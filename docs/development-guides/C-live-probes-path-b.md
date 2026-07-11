# Feature C: Live-Probe Completion + Path B Reasoning Replay

This plan follows the process contract in `docs/development-guides/README.md`
(loop rules, model-selection guidance, definition of done). It is the guidance
plan for Direction C in `docs/optimization-roadmap.md` §2.

## 1. Goal & evidence gate

**Ungated.** Unlike Direction A/B/D, this feature needs no 72h-run evidence —
the live MiniMax key is already in hand and the work is the cheapest,
highest-leverage item on the roadmap. It has two purposes:

1. Retire the last unverified assumptions in the MiniMax provider layer by
   actually running probes 0.2–0.4 against the real API (only probe 0.1 /
   Path A has ever been run live, per `docs/minimax-migration-notes.md`
   "Known limitations (Phase 1)" — Path A's reasoning wire shape is RESOLVED,
   everything else is still a template).
2. Unblock **Direction D (model-tiering activation)**: D's precondition is
   probe 0.2 confirming the Token Plan key actually authorizes
   `MiniMax-M2.7-highspeed` under `LANGCHAIN_PROVIDER=minimax` (same-provider
   constraint, migration notes Phase 3). Nothing else in D can start without
   this.

Effort **S–M** (roadmap priority table). C is explicitly ordered before D.

## 2. Task decomposition

### Task 1 — Probe script fixes, then RUN probes 0.2 / 0.3 / 0.4

- **Fix the hardcoded host.** `scripts/minimax_probe.py:33-34` hardcodes
  `OPENAI_CHAT_URL = "https://api.minimax.io/v1/chat/completions"` and
  `ANTHROPIC_MESSAGES_URL = "https://api.minimax.io/anthropic/v1/messages"`.
  The user's account lives on `api.minimaxi.com`, not `api.minimax.io` — as
  shipped, every probe hits the wrong host. Replace both constants with a
  `MINIMAX_BASE_URL`-derived base (env var, default to the current
  `api.minimax.io` values only as a fallback so the script still runs without
  the var set), consistent with how `agent/src/providers/llm.py:441-447`
  (`_minimax_base_url`) already resolves the base URL for the real adapter —
  don't invent a second resolution convention. Keep the `/anthropic` substring
  check (`_minimax_uses_anthropic_endpoint`, `llm.py:450-458`) as the mental
  model for how the script's `--endpoint anthropic` flag should map onto a
  real base URL when Task 2 needs to live-verify against it.
- **Run the probes, minimally.** Each probe subcommand consumes real quota
  (§3 below caps runs at 2× each). Order:
  - `python scripts/minimax_probe.py models` (0.2) — model coverage of
    `MiniMax-M2.7-highspeed` and `MiniMax-M2.5-highspeed` under the
    subscription key (the model list probed is
    `MODELS_TO_PROBE` at `minimax_probe.py:41`, already includes both).
  - `python scripts/minimax_probe.py reasoning` (0.3) — the OpenAI surface
    (`_reasoning_openai`, `minimax_probe.py:244-296`) is **already
    live-verified** (Phase 1 "RESOLVED" finding in the migration notes); this
    run's job is the **Anthropic surface** (`_reasoning_anthropic`,
    `minimax_probe.py:298-321`) — capture the exact `thinking` content-block
    shape and whether the two-turn replay-vs-stripped comparison (mirroring
    what `_reasoning_openai` already does) shows a behavioral difference.
    Note the OpenAI-surface half of this probe still runs every time (it's
    not split in the script) — that's expected, not wasted quota, since it's
    the cheap half.
  - `python scripts/minimax_probe.py concurrency` (0.4) — 429/backoff/
    Retry-After empirics to ground `agent/src/providers/backoff.py`'s
    constants in reality (currently assumed, not measured).
- **Fill the Findings table** in `docs/minimax-migration-notes.md` (currently
  a template at the "Findings" heading) with one-line verdicts + notes per the
  table's own instructions — do not leave 0.2–0.4 as "_not yet run_".
- TDD note: this task is data-gathering, not logic — no RED/GREEN cycle
  applies to the probe runs themselves, but the base-URL fix (`OPENAI_CHAT_URL`
  / `ANTHROPIC_MESSAGES_URL` construction) is code and needs a unit test
  (e.g. monkeypatch `MINIMAX_BASE_URL` and assert the constructed URL) before
  the probes are trusted to hit the right host.

### Task 2 — Path B reasoning replay (conditional on Task 1's 0.3 result)

**Decision rule: don't build what the account can't use.** Branch on what
probe 0.3's Anthropic-surface run showed:

- **If the mainland/subscription-key account has no working
  `/anthropic/v1/messages` route at all** (0.1 already showed Path A works;
  if 0.3 additionally shows the Anthropic surface 400s/401s regardless of
  reasoning content) — do not implement the translation. Instead, update
  `docs/minimax-migration-notes.md` "Known limitations (Phase 1)" to state
  Path B is **permanently unsupported for this account** (not "deferred"),
  and add a one-line comment pointer in `_build_native_minimax_anthropic`'s
  docstring (`agent/src/providers/llm.py:472-497`) so a future reader doesn't
  re-open this question without re-probing.
- **If the Anthropic surface authenticates and returns `thinking` blocks**
  (justifying the build): implement the translation.

Implementation, only if justified:

- **Where the translation is missing today** (confirmed by reading, not
  assumed): the ReAct loop assembles the outbound history entirely in
  OpenAI-dict shape — `agent/src/agent/loop.py:953-959` builds
  `assistant_message` via `context.format_assistant_tool_calls(...,
  reasoning_content=...)`, which (`agent/src/agent/context.py:278-320`) puts
  the reasoning string into the top-level `message["reasoning_content"]` key
  (context.py:323); LangChain folds it into `additional_kwargs`, which is where
  `_get_request_payload` reads it.
  `ChatOpenAIWithReasoning._get_request_payload`
  (`agent/src/providers/llm.py:262-305`) reads that back out and
  re-serializes it into `reasoning_details`/`reasoning_content` for the
  OpenAI-compatible wire (Path A). `_build_native_minimax_anthropic`
  (`llm.py:472-527`) constructs a stock `ChatAnthropic` with **zero**
  translation step — confirmed by search, no `{"type": "thinking", ...}`
  block-builder exists anywhere in `agent/src/providers/`. The stock
  LangChain Anthropic client will drop or mishandle the OpenAI-shaped
  `additional_kwargs["reasoning_content"]` it's handed.
- **Where to add it:** a translation function analogous to
  `ChatOpenAIWithReasoning._get_request_payload`'s reasoning re-serialization,
  but emitting Anthropic `thinking` content blocks instead — either a request
  hook on the `ChatAnthropic` instance built in
  `_build_native_minimax_anthropic`, or (if `langchain-anthropic` doesn't
  expose an equivalent hook point) a message-preprocessing step in the loop
  path that only activates when Path B is selected
  (`_minimax_uses_anthropic_endpoint()`, `llm.py:450-458`). Follow whatever
  exact block shape probe 0.3 captured (field names, whether a `signature`/
  redaction field is present — do not guess the shape from Anthropic's own
  API docs, since this is MiniMax's implementation of that wire format, not
  Anthropic's).
- **Mocked tests mirroring the Path A replay tests**: add a
  `test_minimax_reasoning_replayed_on_turn_2_as_thinking_block`-shaped test
  (structural sibling of `test_minimax_reasoning_replayed_on_turn_2_as_typed_list`,
  `agent/tests/test_minimax_provider_hardening.py:176-198`) plus siblings for
  the "no reasoning this turn" and "list vs string" cases already covered on
  the Path A side (`test_minimax_no_reasoning_turn_omits_reasoning_details`,
  `:218-227`; `test_minimax_list_reasoning_details_replayed_verbatim`,
  `:201-215`). TDD: write these RED first (assert the translated thinking
  block appears in the outbound Anthropic request) against the current stub,
  confirm they fail for the documented reason (no translation exists), then
  implement GREEN.
- **Live-verify**: point `MINIMAX_BASE_URL` at the anthropic endpoint and run
  a real multi-turn tool-calling exchange (not just the probe script) through
  the actual agent loop, confirming reasoning survives turn 2 exactly as the
  mocked tests assert.

### Task 3 — Docs

- `docs/minimax-migration-notes.md` Findings table: all four rows (0.1–0.4)
  filled, no "_not yet run_" remaining.
- "Known limitations (Phase 1)" section updated to reflect Task 2's outcome
  (either "Path B implemented, reasoning replay verified against wire shape
  X" or "Path B permanently unsupported for this account — see probe 0.3
  Anthropic-surface result").
- A short **tiering readiness note** for Direction D: state plainly whether
  probe 0.2 showed `MiniMax-M2.7-highspeed` / `MiniMax-M2.5-highspeed` usable
  under the subscription key (yes/no + status code), since that is D's
  literal precondition per the roadmap.

## 3. Binding constraints

- **Probe runs are the ONLY live-API spend this plan authorizes.** No
  committee/swarm runs, no ad hoc live calls beyond what Tasks 1 and 2's
  live-verification step require. This is not a license to re-run probe 0.1
  or re-run `probe 0.5` (baseline committee smoke run) — those are out of
  scope here.
- **Each probe subcommand (`auth`, `models`, `reasoning`, `concurrency`) runs
  at most twice.** If a run errors on something unrelated to the account
  (e.g. a transient transport error), a second attempt is allowed; a third
  attempt requires stopping and re-reading the error rather than retrying
  blind — quota is real money/allowance, not a free resource (Token Plan
  quota mechanics, migration notes).
- **Never commit captured API responses containing the key.** Redirect probe
  output to a scratch file outside the repo (or a gitignored path), strip any
  `Authorization`/`x-api-key` header value before pasting excerpts into the
  Findings table or a report. The Findings table gets one-line verdicts and
  short notes, not raw response dumps.

## 4. Loop rules application

Per `docs/development-guides/README.md`:

- **Model tier**: mid-tier implementer suffices for both tasks — this is
  config/URL plumbing (Task 1) and a bounded wire-format translation with a
  clear mocked-test template to mirror (Task 2), not money math or novel
  concurrency design. Reviewer: also mid-tier is fine, but the reviewer's
  distinguishing job here is unusual — **it must check the probe's output
  parsing against the actual live JSON the implementer captured**, not just
  the diff. That means the implementer's report must attach the raw (key-
  redacted) probe output alongside the diff, and the reviewer re-derives the
  Findings-table verdict from that output independently rather than trusting
  the implementer's summary of it. This is the same "reproduce, don't trust
  the report" rule as the README's per-task loop, applied to probe output
  instead of test output.
- **TDD**: Task 1's URL-fix is code and needs a RED/GREEN unit test (base-URL
  resolution). Task 2 (if built) follows the README's mandatory TDD with the
  mocked-test siblings named above, RED against the current no-op stub before
  GREEN.
- **Fresh implementer per task**, strictly sequential (Task 2 depends on
  Task 1's probe 0.3 result to even know whether it should build anything).
- **Ledger**: record probe 0.2/0.3/0.4 verdicts and the Task 2 build-or-defer
  decision in the ledger so Direction D's dispatch can copy the model-coverage
  answer directly instead of re-reading this whole plan.

## 5. Live-verification recipe

1. Export the real key: `export MINIMAX_API_KEY=sk-...` (subscription key).
2. Run the fixed probe script against `api.minimaxi.com` (via
   `MINIMAX_BASE_URL` or the script's corrected default) for `models`,
   `reasoning`, `concurrency` — each at most twice, output `tee`'d to a
   scratch file (never the repo).
3. If Task 2 is built: set `MINIMAX_BASE_URL` to the `/anthropic` variant,
   `LANGCHAIN_PROVIDER=minimax`, run one real multi-turn tool-calling agent
   session end-to-end (not the probe script — the actual `AgentLoop` /
   `ChatLLM` path per `agent/src/agent/loop.py`), and inspect that reasoning
   from turn 1 actually reaches turn 2's outbound Anthropic request (log or
   capture the request payload, don't just trust a non-error response — a
   silently dropped `thinking` block would still return 200).
4. Any live-found bug (wrong field name, unexpected 4xx, a shape the mocked
   tests didn't anticipate) goes through the same fix → re-review loop as any
   other finding, per the README's live-verification rules.

## 6. Acceptance criteria

- `scripts/minimax_probe.py` targets `api.minimaxi.com` (or whatever
  `MINIMAX_BASE_URL` resolves to) with no hardcoded `api.minimax.io` left as
  the only option; covered by a unit test.
- `docs/minimax-migration-notes.md` Findings table has real verdicts for
  0.2, 0.3, 0.4 (0.1 already resolved); "Known limitations (Phase 1)" no
  longer describes Path B as merely "deferred" — it states the actual
  resolved status (built-and-verified, or permanently unsupported).
- If Task 2 was built: mocked tests mirroring the Path A replay suite pass,
  live multi-turn reasoning replay on Path B is verified end-to-end, and
  `_build_native_minimax_anthropic`'s docstring no longer claims the
  translation is "UNVERIFIED."
- If Task 2 was declined: the docstring and migration notes both say why
  (cite the exact probe 0.3 result), so no future contributor re-opens the
  question without new evidence.
- A one-line tiering-readiness note exists for Direction D citing probe 0.2's
  model-coverage result.
- Full suite green (`.venv/bin/python -m pytest agent/tests -q`), zero new
  failures, zero writes outside tmp paths.
- No probe response bodies or key material committed.

## 7. Effort

**S–M**, per the roadmap's own estimate. Task 1 is S (URL fix + minimal
probe runs + table fill). Task 3 is S (doc updates). Task 2 is the swing
factor: S if the decision is "permanently unsupported" (just docs), M if the
translation must be built and live-verified.
