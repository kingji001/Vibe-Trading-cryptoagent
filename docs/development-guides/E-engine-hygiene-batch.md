# Feature E: Engine-Hygiene Batch — Development Guidance Plan

Follows `docs/development-guides/README.md` (binding loop rules). Items below
are sourced only from the three ledgers (`.superpowers/sdd{,2,3}/progress.md`)
and `docs/optimization-roadmap.md` §2 Direction E, and each was re-verified
against `main` (file:line evidence given). Nothing invented; nothing already
resolved is carried forward as a fix.

## 1. Goal

Pay down the Phase 1–3 fix-later backlog in one reviewed batch with **zero
behavior change for a correctly-configured deployment**. Every fix is either:
telemetry/error-reporting accuracy, an unhandled-crash → typed-error
contract, a private-API dedup, or a test-coverage gap. No fix may change a
decision, fill, journal entry, or schedule unless an operator opts in via a
new, default-off flag.

## 2. Item list (4 SDD tasks by subsystem)

### Task E-1 — Providers / concurrency

**E-1a. Gate-wait telemetry drops failed-attempt waits.**
`agent/src/providers/chat.py:326-337,367-408` stamps `LLMResponse
.gate_wait_seconds` (`chat.py:155-157`) only on the attempt it returns.
`agent/src/providers/backoff.py:219-231` (`run_with_stream_retry`) retries by
re-calling `stream_fn()`; a failed attempt raises before any `LLMResponse`
exists, so its gate-wait is discarded. `agent/src/swarm/worker.py:558-565`
logs `llm_gate_wait` from that single surviving value — under contention the
number is the *last* attempt's wait, not the cumulative queue time, which is
exactly what roadmap §1c needs read accurately.
- Fix: use `backoff.py`'s existing `on_retry` hook (`:206-207`) to accumulate
  each failed attempt's gate-wait and add it to the final response's
  `gate_wait_seconds`.
- Test: extend `test_concurrency_governance.py` with a stream that fails
  twice then succeeds; assert `gate_wait_seconds` sums all three attempts.
- Severity: real — fixes the exact metric §1c gates decisions on.

**E-1b. `Retry-After` has no jitter.**
`backoff.py:154-177` (`next_delay`) returns `min(retry_after, cap_s)` verbatim
(line 174-176) instead of the equal-jitter formula `compute_backoff_delay`
uses (lines 66-93). A shared `Retry-After: 30` causes synchronized re-entry
across concurrent workers.
- Fix: apply the same equal-jitter transform to the `Retry-After` value,
  keeping the `cap_s` ceiling.
- Test: `next_delay` with a fixed header + pinned `rand`; assert the result
  is no longer always exactly the header value.
- Severity: real, low-probability (only under genuine multi-worker 429
  storms); cheap one-line-scale fix, keep.

**E-1c. `compute_layer_deadline` hidden env dependency.**
`agent/src/swarm/runtime.py:53-94` line 90 reads `llm_gate_limit()`
(`VIBE_LLM_MAX_CONCURRENT`) directly inside an otherwise pure function.
- Fix: add keyword-only `gate_cap: int | None = None`; `None` preserves
  today's internal lookup exactly.
- Test: call with `gate_cap=3` and no env var set; assert the divisor path
  is exercised without touching `os.environ`.
- Severity: cosmetic testability nicety, not a bug. First item to drop if
  the batch needs to shrink.

**E-1d. Deadline tests aren't hermetic against ambient env.**
`agent/tests/test_concurrency_governance.py:222-224`
(`test_layer_deadline_handles_zero_workers`) calls `compute_layer_deadline`
with no `monkeypatch` at all — a dev shell exporting
`VIBE_LLM_MAX_CONCURRENT` can flip its result. `conftest.py:26-53` already
autouse-pins `VIBE_PAPER_ROOT`/`VIBE_PAPER_ENABLED` but nothing for this var.
- Fix: add a third autouse fixture in `conftest.py`:
  `delenv("VIBE_LLM_MAX_CONCURRENT", raising=False)` before each test.
- Test: none new — verify by exporting `VIBE_LLM_MAX_CONCURRENT=99` in the
  shell running pytest and confirming no test flips.
- Severity: real, low-blast-radius — same class of gap as the ~55-phantom-
  trade leak the README's hermeticity invariant exists because of.

*Dropped:* "Path A `top_p` non-fire" (`sdd` T1) is documented-as-intended in
the migration notes; no code change unless temperature is retuned.

### Task E-2 — Swarm presets / debate expansion

**E-2a. `inspect_preset` has no error contract for debate misconfig.**
`agent/src/swarm/presets.py:366-367` calls `build_run_from_preset(name, {})`
*before* the `try/except ValueError` that only guards `validate_dag`/
`topological_layers` (lines 391-396). `_expand_debate` (`:198-284`) raises
raw `ValueError` for "no participants" (224) / "sink not a defined task"
(229) — these propagate straight out of `inspect_preset` instead of landing
in its `errors` list, defeating its stated job.
- Fix: wrap the `build_run_from_preset` call in
  `try/except (ValueError, KeyError)` and return the message as an `errors`
  entry, matching the existing return shape.
- Test: `test_swarm_preset_inspect.py` — preset with an empty-participants
  or bad-sink debate; assert `inspect_preset` returns an error, not a raise.
- Severity: real — named directly in `sdd` "Post-merge hygiene backlog."

**E-2b. Malformed participant dict raises `KeyError`, not `ValueError`.**
`presets.py:244-246` (`participant["seat"]`, `["summary_key"]`,
`["task_id"]`) subscripts directly with no validation, unlike every other
failure in the same function (`ValueError`, lines 224/229).
- Fix: validate required keys explicitly; raise `ValueError` naming the
  debate id and missing key.
- Test: participant dict missing `summary_key`; assert `ValueError` not
  `KeyError`.
- Severity: real, small — error-type correction only, no behavior change
  for well-formed presets (all that ship today). Named in `sdd` T4 minors.

**E-2c. No tests for `${VAR:-}` edge forms.**
`presets.py:140-174` (`_resolve_debate_rounds`) handles empty-default
`${VAR:-}`, unterminated `${VAR`, empty `${}`, and nested forms — traced
safe by the ledger but untested; `test_swarm_debate_expansion.py:277-313`
only covers over-cap/below-1/non-integer.
- Fix: no source change. Add parametrized cases for the three string forms.
- Severity: pure test-fidelity gap, no defect. Keep only because it's
  test-only (no regression risk); second candidate to drop under time
  pressure.

*Dropped:* `entry_inputs` grounding drop for rounds≥2 (`sdd` T4) is
documented/intentional and feature-shaped, not hygiene; `_resolve_model_name`
strip inconsistency is unreachable via any shipped YAML.

### Task E-3 — Grounding (anti-hallucination toolchain)

**E-3. `grounding.py` imports a private cross-module function.**
`agent/src/swarm/grounding.py:236,307-308` both do
`from backtest.runner import _detect_market` (module-private by convention)
inside `format_identity_anchor` and `fetch_grounding_data`.
- Fix: promote to a public `detect_market` (note: `_detect_market` is
  re-exported into `runner.py:42` from its defining module, not defined there —
  promote at the definition site), keep
  `_detect_market` as an alias for any other private caller, repoint both
  grounding.py call sites.
- Test: existing grounding suite passes unchanged (regression only).
- Severity: real but low-urgency — maintainability/coupling, not a live bug
  (both sites work today). Do not gold-plate: the related "market label
  always `us_equity`" and "whole-file substring prompt test" ledger notes
  are unread-today / test-only — out of scope, no defect to fix.

### Task E-4 — Paper engine / journal / cadence

**E-4a. `_loader_fetch_bars` duplicates `fetch_ohlcv_with_fallback`.**
`agent/src/tools/committee_journal_tool.py:73-94` hand-rolls an
`for source in ("okx", "ccxt")` loop identical in shape to
`agent/backtest/loaders/registry.py:229-260`, whose docstring (241-246)
names this exact function as the pattern it generalizes. Left untouched
deliberately when the general helper landed (`sdd` T6).
- Fix: reimplement `_loader_fetch_bars` as a thin wrapper over
  `fetch_ohlcv_with_fallback(["okx","ccxt"], symbol, start, end,
  interval="1H")`, reusing `_frame_to_bars` (lines 97-118) for shape
  conversion.
- Test: existing journal/benchmark tests stay green (regression); add one
  asserting `_loader_fetch_bars` delegates to the registry helper (mock it).
- Severity: real, moderate value — dedup reduces future-drift risk; no
  behavior change since both paths implement the same okx→ccxt order today.

**E-4b. Paper-store JSONL append is read-rewrite O(n).**
`agent/src/paper/store.py:148-151` (`_append_jsonl`) reads the whole file
and rewrites it on every append. Ledger (`sdd2` T2) already triaged this as
accepted at current volume.
- **Decision: note, do not fix.** A true-append rewrite would drop the
  current atomic temp-file+`os.replace` durability guarantee for a
  performance win this deployment doesn't need. Add an inline comment
  pointing at this ledger note; no code change, no test.
- Severity: explicitly cosmetic-to-fix-now. Recommend dropping the code
  change entirely — an append-mode rewrite changes the durability contract,
  which the binding constraints below forbid without a flag.

**E-4c. `last_price` staleness across watched-set gaps.**
`agent/src/paper/events.py:178-203` persists `last_price` per symbol; the
docstring (24-32) already documents that a symbol leaving and rejoining the
watched set can fire one spurious, cooldown-bounded trigger off a stale
price. No pruning exists today.
- Fix: gate behind new flag `VIBE_EVENT_PRUNE_STALE_PRICES` (default off).
  When set, `check_events` drops `last_price[symbol]` for symbols outside
  the current watched set before evaluating triggers.
- Test: watched-set-gap scenario; flag unset → today's stale trigger still
  fires (regression pin); flag set → no spurious trigger on re-join.
- Severity: real but explicitly gated on §1b evidence (spurious triggers
  actually observed). Include the flag only if the operator wants the
  escape hatch; do not enable by default.

**E-4d. Funding-regime / event-threshold cooldown "knob."**
`agent/src/paper/events.py:108-132` already implements
`VIBE_EVENT_COOLDOWN_H`/`VIBE_EVENT_PRICE_MOVE_PCT`/`VIBE_EVENT_FUNDING_ABS`
as live, working, parse-or-warn env knobs.
- **Already resolved — drop from batch.** This ledger line is an
  operational-tuning note ("no code change needed, just tuning" per the
  roadmap itself), not a defect. Verified all three knobs exist end-to-end.

**E-4e. Scheduled-job deletion has no tombstone.**
`agent/src/api/scheduled_routes.py:77-97` (`_ensure_decision_journal_job`,
mirrored at `~186-230` and `~335+`) does
`if store.get(JOB_ID) is not None: return` then upserts — cannot
distinguish "never created" from "operator deleted it," so a restart after
deliberate deletion silently re-creates the job (the documented "delete
once, restart" dance in `docs/crypto-committee.md#event-trigger`).
- Fix: write a tombstone (marker file or `deleted_job_ids` set in the
  store) on delete; `_ensure_*_job` checks it before re-creating.
- Test: simulate delete-then-restart for each of the three jobs; job stays
  absent with tombstone, present without it (regression-pinned).
- Severity: real, and **M not S** — touches persisted job-store shape
  across three call sites, the largest item in this batch. If scope must
  shrink, split this into its own follow-up PR rather than drop it — it's a
  genuine correctness gap operators already work around by convention.

## 3. Binding constraints

- **No behavior change without a flag.** E-1, E-2, E-3, E-4a/e are
  byte-identical for any deployment changing no env vars. E-4c is the only
  new env var (default off). E-4b/E-4d make no code change at all.
- **Every fix is regression-tested** (RED before GREEN, per README TDD
  rule). Where a fix changes an error type (E-2a/E-2b), show the RED test
  asserting the *old* crash, then update it to assert the new typed error.
- **One branch, per-subsystem commits**: `fix/engine-hygiene-batch`, one
  commit per task (E-1..E-4, or 5 if E-4e splits out per above), each
  independently revertable — never squash across subsystems.
- **`policy.py` / `agent/src/live/` stay untouched.**

## 4. Loop rules application

- **Per-task loop**: mid-tier implementer suffices for all four tasks (no
  money math, no new concurrency primitives). Mid-tier reviewer for
  E-2/E-3/E-4a/E-4d-note; a stronger reviewer for E-1a/E-1b (concurrency
  telemetry) and E-4c/E-4e (new flag + persisted-state shape) — those are
  where a "hygiene" fix could accidentally become a behavior change.
- **Final review's mandate for this batch**: for every item marked fixed,
  explicitly re-derive and answer "does this change behavior for a
  deployment with unset new env vars?" — more important than re-litigating
  individual diffs. Specifically confirm: (1) E-1a's accumulator is a no-op
  for the non-retry case; (2) E-1b's jitter doesn't change the `Retry-After`
  cap ceiling; (3) E-2a's new `except` doesn't swallow unrelated errors that
  should still propagate; (4) E-4a's rewrite produces byte-identical bars
  for the existing okx/ccxt/1H path (diff a captured fixture before/after);
  (5) E-4c's flag is a true no-op when unset.
- **Live verification**: exercise E-4a (journal bar-fetch, real loader call)
  and E-4e (scheduled-job persistence, real store) once each before merge
  and inspect the artifacts, not just exit codes — the README's live-
  verification rule scoped to the two items that touch real I/O paths.

## Acceptance criteria

- Full suite green: `.venv/bin/python -m pytest agent/tests -q` — zero new
  failures, zero writes outside tmp paths during the run.
- No new env vars beyond `VIBE_EVENT_PRUNE_STALE_PRICES` (E-4c, and only if
  kept after checking §1b evidence); every other item is bugfix-with-
  identical-defaults or comment/test-only.
- Final review verdict "Ready to merge: Yes," with the behavior-change
  question above answered per item, not just pass/fail per task.

## Effort

Each item is **S** except **E-4e (tombstone), which is M** — it touches
persisted job-store shape across three call sites and carries the batch's
highest regression risk. Total batch effort: **M**, matching the roadmap's
own estimate for Direction E.
