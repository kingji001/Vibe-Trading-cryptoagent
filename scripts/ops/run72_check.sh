#!/usr/bin/env bash
# scripts/ops/run72_check.sh — one-screen health check for a live run72 window.
#
# Answers, from evidence only: is the supervisor up, is the verdict still clean,
# did every scheduled analysis actually complete, and is the agent silently
# failing? Prints ALERT lines for anything wrong.
#
# Exit code: 0 = healthy, 1 = degraded (something needs a human).
#
# Usage:
#   scripts/ops/run72_check.sh          # full status block
#   scripts/ops/run72_check.sh --quiet  # ALERT lines only (for polling loops)
#
# Env: VIBE_OPS_ROOT (default ~/.vibe-trading/ops), same as run72.sh.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OPS_ROOT="${VIBE_OPS_ROOT:-$HOME/.vibe-trading/ops}"
PY="$REPO_ROOT/.venv/bin/python"
QUIET=0
[ "${1:-}" = "--quiet" ] && QUIET=1

cd "$REPO_ROOT" || exit 1

OPS_ROOT="$OPS_ROOT" QUIET="$QUIET" "$PY" - <<'PYEOF'
import json, os, pathlib, re, sqlite3, subprocess, sys, datetime
from collections import Counter

ops = pathlib.Path(os.environ["OPS_ROOT"])
quiet = os.environ.get("QUIET") == "1"
alerts, lines = [], []

# In-flight work is not failure. A crypto_committee cycle takes ~25 min (13 seats,
# 2 concurrent), so a job or swarm younger than this is simply still thinking --
# alerting on it would fire every cycle and train the reader to ignore the watch.
# Past the grace it is genuinely stuck and worth waking someone.
GRACE_MIN = 60

# A failed swarm dir is permanent, but the alert should be edge-triggered: fire
# once when first seen, not every 5-minute poll for the rest of the run. Persist
# the set of already-reported failures so a restart of the watch does not replay
# them. (The verdict/regression checks stay level-triggered on purpose -- those
# describe the current run state, not a one-time event.)
SEEN_FILE = ops / ".run72_check_alerted.json"
try:
    _seen = set(json.loads(SEEN_FILE.read_text()))
except Exception:
    _seen = set()
_seen_new = set()

def alert_once(key: str, msg: str) -> None:
    if key in _seen:
        return
    _seen_new.add(key)
    alerts.append(msg)

def out(s):
    if not quiet:
        lines.append(s)

# --- window start: the last supervisor start event -------------------------
sup = ops / "supervisor.jsonl"
if not sup.exists():
    print("ALERT run72: no supervisor.jsonl — is the run started?")
    sys.exit(1)

events = [json.loads(l) for l in sup.read_text().splitlines() if l.strip()]
starts = [e for e in events if e["event"] == "start"]
restarts = [e for e in events if e["event"] == "restart"]
window_start = starts[-1]["ts"] if starts else None
ws = datetime.datetime.fromisoformat(window_start.replace("Z", "+00:00"))
now = datetime.datetime.now(datetime.timezone.utc)
elapsed_h = (now - ws).total_seconds() / 3600

out(f"window   : {window_start}  (+{elapsed_h:.1f}h of 72h)")

# --- supervisor alive? ------------------------------------------------------
pid_file = ops / "run72.pid"
alive = False
if pid_file.exists():
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)
        alive = True
        out(f"supervisor: running (pid {pid})")
    except (OSError, ValueError):
        pass
if not alive:
    alerts.append("ALERT supervisor: NOT RUNNING (pid file stale or missing)")

if restarts:
    alerts.append(f"ALERT supervisor: {len(restarts)} restart(s) — the server crashed")
if starts[-1].get("serve_cmd_overridden"):
    alerts.append("ALERT supervisor: VIBE_OPS_SERVE_CMD set — this is a STUB run, not real")

# --- heartbeat --------------------------------------------------------------
hb = ops / "heartbeat.jsonl"
if hb.exists():
    rows = [json.loads(l) for l in hb.read_text().splitlines() if l.strip()]
    rows = [r for r in rows if r["ts"] >= window_start]
    ok = sum(1 for r in rows if r["ok"])
    pct = 100 * ok / len(rows) if rows else 0
    out(f"heartbeat : {pct:.2f}% uptime ({len(rows)} beats)")

# --- scheduled jobs: dispatched vs actually completed ------------------------
db = pathlib.Path(os.path.expanduser("~/.vibe-trading/sessions.db"))
if db.exists():
    c = sqlite3.connect(db)
    cut = ws.timestamp()
    rows = list(c.execute(
        """SELECT s.title, s.started_at, SUM(CASE WHEN m.role='assistant' THEN 1 ELSE 0 END)
           FROM sessions s LEFT JOIN messages m ON m.session_id = s.id
           WHERE s.title LIKE 'scheduled-research:%' AND s.started_at >= ?
           GROUP BY s.id""", (cut,)))
    stale_cut = (now - datetime.timedelta(minutes=GRACE_MIN)).timestamp()
    kinds = Counter(t.split(":", 1)[1] for t, _, _ in rows)
    for kind, n in sorted(kinds.items()):
        mine = [(st, r) for t, st, r in rows if t.endswith(kind)]
        done = sum(r for _, r in mine)
        inflight = sum(1 for st, r in mine if not r and st >= stale_cut)
        stuck = sum(1 for st, r in mine if not r and st < stale_cut)
        suffix = f" in_flight={inflight}" if inflight else ""
        out(f"job       : {kind:28} dispatched={n:3} completed={done:3}{suffix}")
        if stuck:
            alert_once(
                f"job:{kind}:stuck:{stuck}",
                f"ALERT job: {kind} has {stuck} dispatch(es) with NO agent reply "
                f"after {GRACE_MIN}m — the turn died silently")

# --- committee swarms: did the analysis actually finish? ---------------------
swarms = []
for d in pathlib.Path("agent/.swarm/runs").glob("swarm-*"):
    rj = d / "run.json"
    if not rj.exists():
        continue
    try:
        s = json.loads(rj.read_text())
    except Exception:
        continue
    if s.get("preset_name") != "crypto_committee":
        continue
    mt = datetime.datetime.fromtimestamp(rj.stat().st_mtime, datetime.timezone.utc)
    if mt >= ws:
        swarms.append((d.name, s.get("status"), mt))
tally = Counter(st for _, st, _ in swarms)
out(f"swarms    : {len(swarms)} committee runs {dict(tally)}")
for name, st, mt in swarms:
    age_min = (now - mt).total_seconds() / 60
    if st == "completed":
        continue
    if st == "running" and age_min < GRACE_MIN:
        continue  # still deliberating — not a failure
    reason = "stuck (no progress)" if st == "running" else f"status={st}"
    alert_once(f"swarm:{name}:{st}",
               f"ALERT swarm: {name} {reason} — no portfolio decision for that cycle")

# --- the regression we fixed: reasoning_content must stay at zero ------------
hits = 0
for d in pathlib.Path("agent/.swarm/runs").glob("swarm-*"):
    ev = d / "events.jsonl"
    if not ev.exists():
        continue
    if datetime.datetime.fromtimestamp(ev.stat().st_mtime, datetime.timezone.utc) < ws:
        continue
    hits += ev.read_text().count("reasoning_content")
out(f"reasoning : {hits} reasoning_content merge errors (must stay 0)")
if hits:
    alerts.append(f"ALERT regression: {hits} reasoning_content merge error(s) — the fix is NOT holding")

# --- token burn: zero means the agent is not really thinking -----------------
ti = to = calls = 0
for p in pathlib.Path("agent/runs").glob("*/llm_usage.json"):
    if datetime.datetime.fromtimestamp(p.stat().st_mtime, datetime.timezone.utc) < ws:
        continue
    try:
        t = json.loads(p.read_text())["totals"]
    except Exception:
        continue
    ti += t["input_tokens"]; to += t["output_tokens"]; calls += t["calls"]
out(f"tokens    : {ti + to:,} ({calls} LLM calls)")
if elapsed_h > 2.5 and calls == 0:
    alerts.append("ALERT tokens: 0 LLM calls — the agent is not reaching the model")

# --- the harness's own verdict (authoritative) ------------------------------
try:
    r = subprocess.run([".venv/bin/vibe-trading", "ops", "report"],
                       capture_output=True, text=True, timeout=120)
    verdict = "UNKNOWN"
    for l in r.stdout.splitlines():
        if "UNINTERRUPTED" in l or "DEGRADED" in l:
            verdict = "INTERRUPTED/DEGRADED" if "DEGRADED" in l else "UNINTERRUPTED"
            break
    out(f"verdict   : {verdict}")
    if verdict != "UNINTERRUPTED":
        reasons = [l.strip() for l in r.stdout.splitlines() if l.strip().startswith("-")]

        # Boundary artifact: for the first ~1-2 min after an even-hour cron
        # fires, the committee agent has not yet called run_swarm, so ops
        # report transiently sees the firing as "missing" until the swarm dir
        # exists. Suppress the page only when EVERY reason is a
        # missing-firing whose expected time is within the grace window --
        # a real miss (older, or any non-firing reason) still alerts.
        def _is_fresh_firing_miss(reason: str) -> bool:
            m = re.search(r"firing\(s\) missing:\s*([0-9T:\-]+Z)", reason)
            if not m:
                return False
            exp = datetime.datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
            return (now - exp).total_seconds() / 60 < GRACE_MIN

        if reasons and all(_is_fresh_firing_miss(x) for x in reasons):
            out("          (transient: firing just fired, swarm not yet created — not alerting)")
        else:
            alerts.append(f"ALERT verdict: {verdict} :: {'; '.join(reasons) or 'see ops report'}")
except Exception as exc:
    alerts.append(f"ALERT verdict: ops report failed: {exc}")

if _seen_new:
    try:
        SEEN_FILE.write_text(json.dumps(sorted(_seen | _seen_new)))
    except Exception:
        pass  # best-effort; a re-alert next poll beats crashing the watch

for l in lines:
    print(l)
for a in alerts:
    print(a)
if not alerts and not quiet:
    print("OK        : everything nominal")
sys.exit(1 if alerts else 0)
PYEOF
