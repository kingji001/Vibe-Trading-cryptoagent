# Development Playbook — Loop Rules for AI Programming Assistants

This repo's last three feature phases (MiniMax migration, paper-trading loop,
two-tier cadence — PRs #1–#3) were built by AI agent teams under the process
below. It caught three reproduced critical defects, one live wire-format bug no
mock could see, and one test-suite leak into a production account. Follow it for
every subsequent feature in `docs/optimization-roadmap.md`. Each feature has its
own guidance plan in this directory; this file is the process contract they all
share.

## The loop

```
spec (user-approved design, docs/superpowers/specs/)
  → implementation plan (binding values, docs/superpowers/plans/)
  → task briefs split from the plan (one file per task + globals)
  → PER-TASK LOOP (below), tasks strictly sequential
  → final whole-branch review (most capable model)
  → fix wave for Critical/Important → re-review until "Ready to merge: Yes"
  → LIVE end-to-end verification on the real system
  → stacked PR; merge only in stack order
```

### Per-task loop rules

1. **Fresh implementer per task.** It receives: its brief file, the globals
   file, pointers to the exact interfaces earlier tasks landed (copy them from
   the ledger — never make it read the whole plan), and a report-file path.
   Implementers never run in parallel (workspace conflicts).
2. **TDD is mandatory and evidenced.** The report must show RED (command +
   failing output + why expected) before GREEN. A fix that lands without a
   failing-first test is returned.
3. **Independent reviewer per task**, dispatched with the same brief + the
   implementer's report + a diff package (`git diff -U10 BASE..HEAD` to a file;
   record BASE before dispatching the implementer — never `HEAD~1`).
   Reviewer rules: verify claims against the diff, never trust the report;
   file:line evidence for every finding; hand-recompute any money math;
   ⚠️-flag what the diff can't prove. Verdicts: spec compliance AND task
   quality.
4. **Critical/Important findings → fix round → re-review.** The fix dispatch
   names the covering tests; the re-review reads the incremental diff. Never
   proceed with open Critical/Important. Minor findings go to the ledger for
   final-review triage — never silently dropped.
5. **Ledger** (`.superpowers/sdd<N>/progress.md`): task status, landed
   interfaces (exact names/shapes — the next task's dispatch copies from
   here), deferred minors, contract addenda from fix rounds. The ledger is the
   recovery map after context loss; trust it plus `git log` over memory.

### Final review rules

- Most capable model available. Its mandate is CROSS-TASK SEAMS, not
  re-litigating task reviews: trace the end-to-end data flow by hand, probe
  idempotency composition, hunt runaway loops, check stacking hygiene.
- Findings must be REPRODUCED where possible (executable evidence, not
  reading). All three phases' critical finds came from reproduction.
- It triages every deferred minor: must-fix / fix-later (→ roadmap) / accept.
- Its "Ready to merge" verdict gates the PR. Fix waves go to ONE fixer with
  the complete findings list, then back to the same reviewer.

### Live verification rules (tests are not enough — proven twice)

After "Ready to merge", exercise the real system with the real `.env`:
the live key exposed a reasoning wire-format 400 that 5,400 green tests
missed, and a live committee run exposed test-suite writes into the real
paper account. Minimum bar: run the feature's real entry point end-to-end,
then inspect the artifacts it wrote (journal, ledger, run store) — not just
its exit code. Any live-found bug goes through the same fix→re-review loop.

## Repo invariants (binding for every feature)

- **Additive and flag-gated.** Every new env unset ⇒ byte-identical upstream
  behavior, pinned by a regression test. Diffs confined to committee/, paper/,
  tools/, presets/, scheduled_routes, and flag-gated provider/runtime edits.
- **Never invent a price/number.** Fetch failure ⇒ sentinel / no-fill / no
  trigger + recorded error. Sentinels are instructive
  (`NO_DATA_AVAILABLE: <reason> — do not estimate this value`, `<unavailable>`).
- **Money-state writes: "never persist a state richer than reality."**
  Buys persist cash-first; sells persist positions-first (broker.py comments).
  Ledger rows are append-only; idempotency keys: journal `(run_id, symbol)`,
  executor `decision_id` (retriable-noop exception).
- **Test hermeticity.** Tests are socket-disabled by convention.
  `agent/tests/conftest.py` has an autouse guard pinning `VIBE_PAPER_ROOT` to
  tmp and `VIBE_PAPER_ENABLED=0` — ANY new env var with filesystem/network
  side effects MUST be added to that guard in the same task that introduces
  it. (A missing guard leaked ~55 phantom trades into the real account.)
- **`policy.py` and the live-execution stack stay untouched** unless the user
  explicitly decides to relax them (feature B documents that decision point).
- **The scheduler's cron is simplified**: each field accepts only `*`, `*/n`,
  or a single number; a bare positive integer is an interval in
  MILLISECONDS. Validate user-facing schedule strings accordingly.
- **Quirks:** `docs/` is gitignored — new doc files need `git add -f`;
  `.env` resolution order is `~/.vibe-trading/.env` → `agent/.env` → CWD;
  install with `grep -v smartmoneyconcepts agent/requirements.txt` then
  `-e . --no-deps` (broken transitive dep), plus `defusedxml prompt_toolkit
  bottleneck`; run tests via `.venv/bin/python -m pytest`.
- **Scheduled-job registration is non-clobbering**: env changes never rewrite
  an existing job — prompt/schedule changes require the operator to delete
  the job once (document this for every job you add or modify).
- **run_swarm's structured `variables` param** is the only reliable way to
  target `crypto_committee`; prompt extraction is a fallback and a missing
  target is a run-free error by design.

## Model-selection guidance (from three phases of evidence)

- Implementers: mid-tier for well-specified tasks; top-tier for money math,
  concurrency, provider wire formats, and anything touching fill semantics.
- Task reviewers: match the diff's risk (top-tier for money/concurrency).
- Final review: always the most capable model; give it permission to run
  focused tests and require reproduction of criticals.

## Definition of done (every feature)

1. All plan tasks approved by review; ledger complete.
2. Final review verdict "Ready to merge: Yes" with reproductions re-verified.
3. Full suite green (`.venv/bin/python -m pytest agent/tests -q`) with zero
   new failures AND zero writes outside tmp paths during the run.
4. Live end-to-end verification performed and its artifacts inspected.
5. Docs updated in the same branch (crypto-committee.md / migration notes /
   .env.example), including honest-limits paragraphs for every approximation.
6. Stacked PR with the review evidence summarized in the body.
