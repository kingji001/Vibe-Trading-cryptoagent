# 72-Hour Operation Evidence Harness — Design

**Date:** 2026-07-11
**Branch:** `feat/ops-evidence-harness` (base: `b176604`, on main)
**Status:** Approved by user (build only; user starts the run themselves)

## 1. Goal

Make "the system ran uninterrupted for 72 hours" a claim PROVABLE from
artifacts. The harness (a) keeps the server alive and awake on macOS, (b)
records a tamper-evident-by-redundancy trail (heartbeat + supervisor events +
the system's own artifact streams), and (c) generates an evidence report that
cross-references five independent sources. Honesty rule: gaps, restarts, and
missed firings are REPORTED, never hidden — an interrupted run produces a
report that says exactly where it was interrupted.

**Out of scope:** cryptographic attestation, remote monitoring/alerting,
non-macOS supervisors (launchd plist is documented as an alternative, not
built), auto-restarting the 72h window after a failure.

## 2. Components

### 2.1 Supervised runner — `scripts/ops/run72.sh`

- Wraps `vibe-trading serve` under `caffeinate -dims` (prevents idle/display/
  disk sleep; documented limit: not power loss or reboot).
- Writes `~/.vibe-trading/ops/run72.pid`; refuses to start when a live PID
  exists (instructive message). `stop` subcommand kills cleanly.
- Restart-on-crash loop: server exit → append
  `{"ts", "event": "restart", "exit_code", "restart_count"}` to
  `~/.vibe-trading/ops/supervisor.jsonl`, restart after 5s. `start` appends
  `{"event": "start", "serve_cmd", "env_fingerprint"}` (names of the
  VIBE_*/SWARM_*/LANGCHAIN_* vars set — NAMES ONLY, never values: no key
  leakage).
- Heartbeat loop (same script, background): every 60s `curl` the server's
  `GET /health` (agent/src/api/system_routes.py:81); append
  `{"ts", "ok": bool, "http": code, "latency_ms"}` to
  `~/.vibe-trading/ops/heartbeat.jsonl`. Failures append `ok: false` rows —
  the loop itself never stops while the supervisor lives.
- Bash kept minimal and shellcheck-clean; all analysis logic lives in Python
  (2.2), not the shell script.

### 2.2 Evidence report — `vibe-trading ops report`

New CLI subcommand (pattern: the existing `paper` subcommand in
`agent/cli/_legacy.py`), core logic in `agent/src/ops/evidence.py` (pure,
fixture-testable). Inputs and the claims each supports:

| Source | Claim |
|---|---|
| `ops/heartbeat.jsonl` | uptime % over the window; max gap (gap = consecutive-row delta > 2× interval, or ok:false spans); first/last beat |
| `ops/supervisor.jsonl` | start events, restart count + times (uninterrupted ⇔ 0 restarts AND max gap < 2× interval) |
| swarm run store (`agent/.swarm/runs/*/run.json`) | committee firings: expected-vs-actual per the `committee-run` cron (expected count computed from the SAME simplified-cron semantics — reuse `agent/src/scheduled_research/executor.py`'s next-due logic, never a parallel cron impl), each run's status/wall-clock/tokens |
| paper store (`tick_state.json` watermarks, `equity.jsonl`, `ledger.jsonl`) | tick firings vs expected, conditional fills, retried decisions, event triggers (from tick job artifacts where recorded) |
| committee journal (`journal.jsonl`) | decisions appended in-window, horizons resolved, reflections written |

Output: Markdown report to `~/.vibe-trading/ops/report-<UTC-ts>.md` plus a
one-screen terminal summary. Report sections: window + verdict line
("UNINTERRUPTED" only when 0 restarts, max gap < 2× heartbeat interval, and
every expected firing accounted for — else "INTERRUPTED/DEGRADED" with the
specifics), heartbeat continuity, scheduled-firing table (expected/actual/
missing timestamps per job), committee-run outcomes, paper activity, journal
activity, ops health (429/backoff mentions found in run artifacts where
available). Flags: `--window 72h|48h|...` (default: since the last
supervisor `start` event), `--json` for machine consumption.

- Never invent: absent/unparseable source ⇒ the section reports
  "no data: <reason>" and the verdict degrades to the strongest supportable
  claim; malformed JSONL lines are counted and reported, not skipped silently.

### 2.3 Docs — `docs/crypto-committee.md` "Proving a 72-hour run" section

Start (`scripts/ops/run72.sh start`), stop, where artifacts live, what the
verdict line means, machine requirements (plugged in; caffeinate covers sleep
but not reboots — a reboot honestly fails the window), launchd alternative
sketch, and the post-run one-liner (`vibe-trading ops report --window 72h`).

## 3. Config / paths (all additive)

- `VIBE_OPS_ROOT` (default `~/.vibe-trading/ops`) — heartbeat/supervisor/
  report location; the tests pin it to tmp (extend the conftest guard the
  same way VIBE_PAPER_ROOT is pinned — new side-effectful env ⇒ guard entry,
  per the playbook).
- `VIBE_OPS_HEARTBEAT_S` (default 60) — heartbeat interval; the report reads
  the actual observed cadence, not just the env.
- Server URL for the heartbeat from the serve host/port envs the API server
  already uses (read them, don't hardcode 8899 — verify the real default).

## 4. Testing (socket-disabled)

- Evidence core: fixture heartbeat/supervisor/run-store/journal/paper files →
  exact uptime %, gap detection (boundary: gap == 2× interval is NOT a gap,
  > is), restart counting, expected-firing math for `0 */2 * * *` over a
  fixture window incl. DST-free UTC handling, missing-source degradation,
  malformed-line counting, verdict logic (each condition independently flips
  the verdict).
- CLI: `ops report` smoke against a fixture VIBE_OPS_ROOT; `--json` shape.
- Shell: `bash -n` syntax check + a unit test for the PID-refusal and
  env-fingerprint-names-only behavior via a stubbed serve command (a fake
  binary that exits after N seconds) — keep shell tests minimal.

## 5. Honest limits

- Heartbeat proves the API server answered /health — not that every subsystem
  was healthy; scheduled-firing evidence covers the subsystems that matter.
- caffeinate cannot survive power loss, forced reboot, or the lid-closed
  no-power case; those appear as gaps/restarts and honestly fail the window.
- The report trusts local files; this is operational evidence for the
  operator, not third-party-auditable attestation.
