# Ops Evidence Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Loop rules per docs/development-guides/README.md (binding).

**Goal:** Provable 72-hour uninterrupted-operation evidence: supervised runner + heartbeat, cross-referenced evidence report, docs.

**Spec:** docs/superpowers/specs/2026-07-11-ops-evidence-harness-design.md — binding for every path, JSONL shape, default, and verdict rule.

## Global Constraints

- All changes additive; no behavior change to serve/scheduler/paper/committee
- Never invent: missing/unparseable evidence sources degrade the report verdict with stated reasons
- New env `VIBE_OPS_ROOT` (default `~/.vibe-trading/ops`) MUST be added to the conftest paper-env-style autouse guard in the same task that introduces it (playbook hermeticity rule)
- Expected-firing math reuses `agent/src/scheduled_research/executor.py`'s cron logic — no parallel cron implementation
- Env fingerprint logs env NAMES only, never values (no key leakage)
- Tests socket-disabled; `.venv/bin/python -m pytest`; commit per task (dispatch authorizes)

### Task 1: Supervised runner + heartbeat (`scripts/ops/run72.sh`)

Files: Create `scripts/ops/run72.sh`; Modify `agent/tests/conftest.py` (VIBE_OPS_ROOT guard); Test `agent/tests/test_ops_runner.py` (stubbed-serve based, minimal).
Interfaces produced: supervisor.jsonl events `{ts, event: start|restart|stop, exit_code?, restart_count?, serve_cmd?, env_fingerprint?: [names]}`; heartbeat.jsonl rows `{ts, ok, http, latency_ms}`; `run72.pid`; subcommands `start|stop|status`. Heartbeat interval `VIBE_OPS_HEARTBEAT_S` (default 60). Read the real serve host/port envs from `agent/api_server.py` before writing the curl target.
Steps: failing tests (PID refusal; stop kills both loops; env fingerprint names-only; heartbeat rows appear with a stub server; restart event on stub crash) → implement → `bash -n` + shellcheck if available → commit `feat(ops): supervised 72h runner with heartbeat`.

### Task 2: Evidence report (`agent/src/ops/evidence.py` + `ops` CLI)

Files: Create `agent/src/ops/__init__.py`, `agent/src/ops/evidence.py`; Modify `agent/cli/_legacy.py` (`ops report` subcommand, mirror `paper` wiring); Test `agent/tests/test_ops_evidence.py`.
Interfaces produced: `build_evidence_report(window_start, window_end, *, ops_root, swarm_runs_root, paper_root, journal_path) -> dict` (pure; every source injectable) + `render_markdown(report) -> str`; CLI `vibe-trading ops report [--window 72h] [--json]` defaulting the window to the last supervisor `start` event. Verdict rule and section list exactly per spec §2.2.
Steps: failing fixture tests (uptime %, gap boundary at exactly 2× interval, restart counting, expected-vs-actual for `0 */2 * * *` via the executor's cron logic, missing-source degradation, malformed-line counts, verdict flips per condition, `--json` shape) → implement → run test_paper_* + scheduled suites for regression → commit `feat(ops): cross-referenced 72h evidence report`.

### Task 3: Docs + supervised smoke verification

Files: Modify `docs/crypto-committee.md` ("Proving a 72-hour run" section per spec §2.3), `agent/.env.example` (VIBE_OPS_* block, commented); no product code.
Steps: write docs; then LIVE SMOKE (allowed real side effects, no LLM spend): `scripts/ops/run72.sh start` against the real repo with scheduler DISABLED (unset VIBE_TRADING_ENABLE_SCHEDULER for the smoke; committee must not fire), wait ~3 heartbeats, `status`, `stop`; then `vibe-trading ops report` over that window and verify the report renders with heartbeat continuity and honest "no data" sections for jobs; clean up the smoke's ops artifacts or archive them; document actual output snippets in the docs sparingly → commit `docs(ops): proving a 72-hour run + smoke-verified quickstart`.

## Self-review notes
- Spec §2.1→T1, §2.2→T2, §2.3→T3; hermeticity guard lands with T1 (first task that introduces the env).
- T3's smoke is the live-verification step of the playbook, scoped to zero LLM spend.
