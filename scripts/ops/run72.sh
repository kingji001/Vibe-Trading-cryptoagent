#!/usr/bin/env bash
# scripts/ops/run72.sh — supervised runner + heartbeat for `vibe-trading serve`,
# built to make "the system ran uninterrupted for 72 hours" a provable claim.
#
# Subcommands:
#   run72.sh start   Refuses if already running (live PID file). Wraps the
#                    server in `caffeinate -dims` on macOS (degrades with a
#                    logged warning elsewhere), restarts it on crash, and runs
#                    a background heartbeat loop against GET /health.
#   run72.sh stop    Stops both the restart-on-crash loop and the heartbeat
#                    loop cleanly.
#   run72.sh status  Reports whether a supervisor is currently running.
#
# All analysis logic (uptime %, gap detection, evidence report) lives in
# Python (`vibe-trading ops report`), not here — this script only supervises
# and appends raw JSONL rows.
#
# Env (all optional):
#   VIBE_OPS_ROOT         Artifacts root. Default: ~/.vibe-trading/ops
#   VIBE_OPS_HEARTBEAT_S  Heartbeat interval in seconds. Default: 60
#   VIBE_TRADING_API_URL  Base URL the API server already uses for client
#                         discovery (see agent/cli/_legacy.py,
#                         agent/cli/main.py); heartbeat GETs "$URL/health".
#                         Default: http://127.0.0.1:8000 (matches
#                         agent/api_server.py serve_main()'s own --host/--port
#                         defaults).
#   VIBE_OPS_SERVE_CMD    TEST-ONLY override for the supervised command.
#                         Default: "vibe-trading serve". Production operators
#                         must never set this — it exists purely so tests can
#                         substitute a stub server/crash binary. Parsed by
#                         `bash -c`, so normal shell quoting works, e.g.
#                         VIBE_OPS_SERVE_CMD='python -c "import sys; sys.exit(7)"'.
#                         When set, `start` prints a loud TEST SEAM warning
#                         (stderr + run72.log) and the supervisor start event
#                         carries "serve_cmd_overridden":true so the evidence
#                         report can flag stub runs.
#
# Process-group kill: the serve child is launched under bash job control
# (`set -m`) so it owns a fresh process group (macOS has no setsid); stop
# TERMs the whole group (`kill -- -$pid`), falling back to a single-PID kill,
# so compound serve commands cannot leave orphaned descendants.
#
# Artifacts (all written under VIBE_OPS_ROOT):
#   run72.pid          PID of the running supervisor process.
#   supervisor.jsonl   {"ts","event":"start|restart|stop","exit_code"?,
#                       "restart_count"?,"serve_cmd"?,"env_fingerprint"?}
#   heartbeat.jsonl    {"ts","ok":bool,"http":code|null,"latency_ms"}
#   run72.log          stdout/stderr of the supervised process + warnings
#                       (e.g. caffeinate unavailable).
#
# Honest limits: caffeinate cannot survive power loss, a forced reboot, or a
# lid-closed-without-power case — those show up as gaps/restarts here and are
# reported, never hidden, by `vibe-trading ops report`.

set -uo pipefail

OPS_ROOT="${VIBE_OPS_ROOT:-$HOME/.vibe-trading/ops}"
HEARTBEAT_S="${VIBE_OPS_HEARTBEAT_S:-60}"
API_URL="${VIBE_TRADING_API_URL:-http://127.0.0.1:8000}"
SERVE_CMD="${VIBE_OPS_SERVE_CMD:-vibe-trading serve}"

PID_FILE="$OPS_ROOT/run72.pid"
SUPERVISOR_LOG="$OPS_ROOT/supervisor.jsonl"
HEARTBEAT_LOG="$OPS_ROOT/heartbeat.jsonl"
RUN_LOG="$OPS_ROOT/run72.log"
STOP_FLAG="$OPS_ROOT/.stop"

# Mutable state shared between _supervisor_main, supervise_loop, and the TERM
# trap; deliberately not `local` so the trap handler (same process) sees them.
HEARTBEAT_PID=""
CURRENT_SERVE_PID=""

now_iso() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

is_live_pid() {
  local pid="$1"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

_json_escape() {
  # Minimal escaping for values we construct ourselves (serve_cmd, names).
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

env_fingerprint_json() {
  # NAMES ONLY of set VIBE_*/SWARM_*/LANGCHAIN_*/MINIMAX_* vars — never
  # values. No key leakage, ever.
  local names=() var
  while IFS='=' read -r var _rest; do
    case "$var" in
      VIBE_*|SWARM_*|LANGCHAIN_*|MINIMAX_*)
        names+=("\"$(_json_escape "$var")\"")
        ;;
    esac
  done < <(env | LC_ALL=C sort)
  local joined=""
  if [ "${#names[@]}" -gt 0 ]; then
    joined=$(IFS=,; echo "${names[*]}")
  fi
  printf '[%s]' "$joined"
}

append_supervisor_event() {
  # append_supervisor_event <event> [exit_code] [restart_count]
  local event="$1" exit_code="${2:-}" restart_count="${3:-}"
  local line
  line="{\"ts\":\"$(now_iso)\",\"event\":\"$event\""
  if [ "$event" = "start" ]; then
    line="$line,\"serve_cmd\":\"$(_json_escape "$SERVE_CMD")\""
    if [ -n "${VIBE_OPS_SERVE_CMD:-}" ]; then
      # Self-incriminating evidence: a run against a stubbed server must be
      # flaggable by `vibe-trading ops report`. Key present only when the
      # test seam is active.
      line="$line,\"serve_cmd_overridden\":true"
    fi
    line="$line,\"env_fingerprint\":$(env_fingerprint_json)"
  fi
  if [ -n "$exit_code" ]; then
    line="$line,\"exit_code\":$exit_code"
  fi
  if [ -n "$restart_count" ]; then
    line="$line,\"restart_count\":$restart_count"
  fi
  line="$line}"
  printf '%s\n' "$line" >> "$SUPERVISOR_LOG"
}

heartbeat_loop() {
  while [ ! -e "$STOP_FLAG" ]; do
    local out http_code time_total ok http_field latency_ms
    out=$(curl -s -o /dev/null --max-time 5 -w '%{http_code} %{time_total}' "$API_URL/health" 2>/dev/null)
    if [ -z "$out" ]; then
      out="000 0"
    fi
    http_code="${out%% *}"
    time_total="${out##* }"
    if [ "$http_code" = "200" ]; then
      ok=true
    else
      ok=false
    fi
    if [ "$http_code" = "000" ]; then
      http_field="null"
    else
      http_field="$http_code"
    fi
    latency_ms=$(awk -v t="$time_total" 'BEGIN{printf "%d", (t * 1000)}')
    printf '{"ts":"%s","ok":%s,"http":%s,"latency_ms":%s}\n' \
      "$(now_iso)" "$ok" "$http_field" "$latency_ms" >> "$HEARTBEAT_LOG"
    sleep "$HEARTBEAT_S"
  done
}

supervise_loop() {
  local restart_count=0
  local exit_code

  append_supervisor_event "start"

  while [ ! -e "$STOP_FLAG" ]; do
    # Run SERVE_CMD through `bash -c` (rather than naive whitespace
    # word-splitting) so operator/test-supplied commands with quoted
    # arguments parse correctly, e.g. VIBE_OPS_SERVE_CMD='python -c "import
    # sys; sys.exit(7)"'.
    #
    # `set -m` (bash job control) around the launch puts the serve child in
    # its OWN process group — macOS ships no setsid — so stop/restart can
    # TERM the entire descendant tree via `kill -- -$pid` (pgid == child
    # pid). Without this, a compound serve command would leave orphans: a
    # single-PID kill only reaches the top wrapper process.
    set -m
    if command -v caffeinate >/dev/null 2>&1; then
      caffeinate -dims bash -c "$SERVE_CMD" &
    else
      echo "[warn] caffeinate not found; sleep prevention disabled (non-macOS?)" >> "$RUN_LOG"
      bash -c "$SERVE_CMD" &
    fi
    CURRENT_SERVE_PID=$!
    set +m

    exit_code=0
    wait "$CURRENT_SERVE_PID" || exit_code=$?
    CURRENT_SERVE_PID=""

    if [ -e "$STOP_FLAG" ]; then
      break
    fi

    restart_count=$((restart_count + 1))
    append_supervisor_event "restart" "$exit_code" "$restart_count"
    sleep 5
  done

  append_supervisor_event "stop"
}

kill_serve_tree() {
  # Terminate the serve child's entire process group (it got its own pgid
  # via `set -m` at launch, and descendants inherit it); fall back to the
  # single-PID kill if the group signal fails for any reason.
  local pid="$1"
  [ -n "$pid" ] || return 0
  kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
}

_on_terminate() {
  touch "$STOP_FLAG"
  if [ -n "$CURRENT_SERVE_PID" ]; then
    kill_serve_tree "$CURRENT_SERVE_PID"
  fi
  if [ -n "$HEARTBEAT_PID" ]; then
    kill "$HEARTBEAT_PID" 2>/dev/null || true
  fi
}

_supervisor_main() {
  trap _on_terminate TERM INT
  heartbeat_loop &
  HEARTBEAT_PID=$!
  supervise_loop
  if [ -n "$HEARTBEAT_PID" ]; then
    kill "$HEARTBEAT_PID" 2>/dev/null || true
    wait "$HEARTBEAT_PID" 2>/dev/null || true
  fi
}

start() {
  mkdir -p "$OPS_ROOT"
  if [ -n "${VIBE_OPS_SERVE_CMD:-}" ]; then
    # Loud, twice-recorded, and stamped into the start event
    # (serve_cmd_overridden:true): an evidence run against a stub must be
    # self-incriminating.
    local seam_warning="[warn] TEST SEAM ACTIVE — not running the real server (VIBE_OPS_SERVE_CMD is set)"
    echo "$seam_warning" >&2
    echo "$seam_warning" >> "$RUN_LOG"
  fi
  if [ -f "$PID_FILE" ]; then
    local existing_pid
    existing_pid=$(cat "$PID_FILE" 2>/dev/null || echo "")
    if is_live_pid "$existing_pid"; then
      echo "run72: already running (pid $existing_pid) — run '$0 stop' first" >&2
      exit 1
    fi
    rm -f "$PID_FILE"
  fi

  rm -f "$STOP_FLAG"
  # Redirect the background supervisor's fds to the log file (and close
  # stdin) BEFORE backgrounding: otherwise it inherits this process's stdout
  # pipe, and a caller reading that pipe to EOF (e.g. Python's
  # subprocess.run(capture_output=True)) would block until the 72h loop
  # exits instead of returning once `start` itself is done.
  _supervisor_main >>"$RUN_LOG" 2>&1 </dev/null &
  local pid=$!
  disown "$pid" 2>/dev/null || true
  echo "$pid" > "$PID_FILE"
  echo "run72: started (pid $pid); artifacts in $OPS_ROOT"
}

stop() {
  if [ ! -f "$PID_FILE" ]; then
    echo "run72: not running (no pid file)"
    exit 1
  fi
  local pid
  pid=$(cat "$PID_FILE" 2>/dev/null || echo "")
  if ! is_live_pid "$pid"; then
    echo "run72: not running (stale pid file removed)"
    rm -f "$PID_FILE"
    exit 1
  fi

  touch "$STOP_FLAG"
  kill -TERM "$pid" 2>/dev/null || true

  local waited=0
  while is_live_pid "$pid" && [ "$waited" -lt 30 ]; do
    sleep 1
    waited=$((waited + 1))
  done
  if is_live_pid "$pid"; then
    kill -KILL "$pid" 2>/dev/null || true
  fi

  rm -f "$PID_FILE" "$STOP_FLAG"
  echo "run72: stopped"
}

status() {
  if [ -f "$PID_FILE" ]; then
    local pid
    pid=$(cat "$PID_FILE" 2>/dev/null || echo "")
    if is_live_pid "$pid"; then
      echo "run72: running (pid $pid)"
      exit 0
    fi
  fi
  echo "run72: not running"
  exit 1
}

main() {
  case "${1:-}" in
    start) start ;;
    stop) stop ;;
    status) status ;;
    *)
      echo "usage: $0 start|stop|status" >&2
      exit 2
      ;;
  esac
}

main "$@"
