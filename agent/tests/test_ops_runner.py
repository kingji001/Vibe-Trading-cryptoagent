"""Tests for the supervised 72h runner + heartbeat (``scripts/ops/run72.sh``).

Socket-disabled convention note: ``pytest-socket`` is only wired up in
``agent/tests/factors/conftest.py`` (a directory-scoped ``pytest_runtest_setup``
hook), not in the top-level ``agent/tests/conftest.py`` — the top-level suite
relies on hand-written hermeticity (mocked loaders, tmp roots) rather than a
technical socket block. Since this test file lives outside ``tests/factors/``,
real localhost sockets are not blocked by any fixture here, so these tests use
a genuine ``python -m http.server`` bound to ``127.0.0.1`` on a free ephemeral
port as the stubbed ``vibe-trading serve`` process. That is the only way to
exercise the script's actual ``curl`` heartbeat path end-to-end rather than
just asserting on argv construction.

All subprocess-driven tests supervise real child processes (bash + python).
Each test stops the runner in a ``finally`` block and polls for process exit
so no test leaks a background loop into the rest of the suite.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "ops" / "run72.sh"


def _free_port() -> int:
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run(args, env, cwd=None, timeout=15):
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _base_env(ops_root: Path, **overrides) -> dict:
    env = dict(os.environ)
    env["VIBE_OPS_ROOT"] = str(ops_root)
    env["VIBE_OPS_HEARTBEAT_S"] = "1"
    env.update(overrides)
    return env


def _read_jsonl(path: Path):
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _wait_for(predicate, timeout=10, interval=0.2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _stop_runner(ops_root: Path, env):
    """Best-effort stop, tolerant of a runner that already exited."""
    with contextlib.suppress(Exception):
        _run(["stop"], env=env, timeout=20)
    pid_file = ops_root / "run72.pid"
    if pid_file.exists():
        with contextlib.suppress(Exception):
            pid = int(pid_file.read_text().strip())
            os.kill(pid, signal.SIGKILL)


def _sleepy_serve_cmd() -> str:
    """A stub serve command that just idles until killed."""
    return f"{sys.executable} -c \"import time; time.sleep(600)\""


def _health_server_cmd(directory: Path, port: int) -> str:
    """A stub serve command whose GET /health returns 200 (a bare static file
    named ``health`` served by ``python -m http.server``)."""
    (directory / "health").write_text("ok", encoding="utf-8")
    return (
        f"{sys.executable} -m http.server {port} "
        f"--bind 127.0.0.1 --directory {directory}"
    )


def _crash_serve_cmd(exit_code: int = 7) -> str:
    return f"{sys.executable} -c \"import sys; sys.exit({exit_code})\""


def _compound_serve_cmd() -> str:
    """A deliberately COMPOUND stub: an sh wrapper with two backgrounded
    long sleepers. Under a naive single-PID kill only the wrapper dies and
    the sleepers are silently orphaned — this is the shape that pins the
    process-group kill."""
    inner = (
        f"{sys.executable} -c 'import time; time.sleep(300)' & "
        f"sleep 300 & wait"
    )
    return f'sh -c "{inner}"'


def _descendants(pid: int) -> list[int]:
    """All live descendant PIDs of ``pid`` (depth-first via pgrep -P)."""
    out = subprocess.run(
        ["pgrep", "-P", str(pid)], capture_output=True, text=True
    )
    kids = [int(x) for x in out.stdout.split()]
    result: list[int] = []
    for kid in kids:
        result.append(kid)
        result.extend(_descendants(kid))
    return result


def test_bash_syntax_check():
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_start_refuses_when_pid_file_live(tmp_path):
    ops_root = tmp_path / "ops"
    env = _base_env(ops_root, VIBE_OPS_SERVE_CMD=_sleepy_serve_cmd())
    try:
        first = _run(["start"], env=env)
        assert first.returncode == 0, first.stderr
        pid_file = ops_root / "run72.pid"
        assert _wait_for(pid_file.exists)
        pid = int(pid_file.read_text().strip())
        assert _wait_for(lambda: _pid_alive(pid))

        second = _run(["start"], env=env)
        assert second.returncode != 0
        assert "run72" in (second.stdout + second.stderr).lower()
        # Refusal must not disturb the already-running supervisor's pid.
        assert int(pid_file.read_text().strip()) == pid
    finally:
        _stop_runner(ops_root, env)


def test_stop_kills_both_loops(tmp_path):
    ops_root = tmp_path / "ops"
    env = _base_env(ops_root, VIBE_OPS_SERVE_CMD=_sleepy_serve_cmd())
    try:
        started = _run(["start"], env=env)
        assert started.returncode == 0, started.stderr
        pid_file = ops_root / "run72.pid"
        assert _wait_for(pid_file.exists)
        pid = int(pid_file.read_text().strip())
        assert _wait_for(lambda: _pid_alive(pid))

        heartbeat_log = ops_root / "heartbeat.jsonl"
        assert _wait_for(lambda: len(_read_jsonl(heartbeat_log)) >= 1, timeout=10)

        stopped = _run(["stop"], env=env, timeout=20)
        assert stopped.returncode == 0, stopped.stderr

        assert _wait_for(lambda: not _pid_alive(pid), timeout=15)
        assert not pid_file.exists()

        rows_at_stop = len(_read_jsonl(heartbeat_log))
        time.sleep(2.5)
        rows_after = len(_read_jsonl(heartbeat_log))
        assert rows_after == rows_at_stop, "heartbeat loop kept appending after stop"

        events = [row["event"] for row in _read_jsonl(ops_root / "supervisor.jsonl")]
        assert "start" in events
        assert "stop" in events
    finally:
        _stop_runner(ops_root, env)


def test_env_fingerprint_names_only(tmp_path):
    ops_root = tmp_path / "ops"
    secret = "sk-test-super-secret-value-should-never-leak"
    env = _base_env(
        ops_root,
        VIBE_OPS_SERVE_CMD=_sleepy_serve_cmd(),
        MINIMAX_API_KEY=secret,
        SWARM_MODE="crypto_committee",
        LANGCHAIN_TRACING_V2="true",
    )
    try:
        started = _run(["start"], env=env)
        assert started.returncode == 0, started.stderr
        supervisor_log = ops_root / "supervisor.jsonl"
        assert _wait_for(supervisor_log.exists)
        assert _wait_for(lambda: len(_read_jsonl(supervisor_log)) >= 1)

        events = _read_jsonl(supervisor_log)
        start_events = [e for e in events if e["event"] == "start"]
        assert start_events, events
        fingerprint = start_events[0]["env_fingerprint"]
        assert "MINIMAX_API_KEY" in fingerprint
        assert "SWARM_MODE" in fingerprint
        assert "LANGCHAIN_TRACING_V2" in fingerprint
        assert "VIBE_OPS_ROOT" in fingerprint

        # The value must never appear anywhere under the ops root.
        for path in ops_root.rglob("*"):
            if path.is_file():
                assert secret not in path.read_text(encoding="utf-8", errors="ignore")
    finally:
        _stop_runner(ops_root, env)


def test_heartbeat_rows_appear_with_stub_server(tmp_path):
    ops_root = tmp_path / "ops"
    serve_dir = tmp_path / "servedir"
    serve_dir.mkdir()
    port = _free_port()
    env = _base_env(
        ops_root,
        VIBE_OPS_SERVE_CMD=_health_server_cmd(serve_dir, port),
        VIBE_TRADING_API_URL=f"http://127.0.0.1:{port}",
    )
    try:
        started = _run(["start"], env=env)
        assert started.returncode == 0, started.stderr

        heartbeat_log = ops_root / "heartbeat.jsonl"
        # The very first tick can race the stub server's bind/listen, and a
        # true positive (ok:false, honestly recorded) is expected in that
        # case — wait for an eventual healthy row rather than asserting on
        # row 0.
        assert _wait_for(
            lambda: any(row.get("ok") is True for row in _read_jsonl(heartbeat_log)),
            timeout=15,
        )
        ok_rows = [row for row in _read_jsonl(heartbeat_log) if row["ok"] is True]
        assert ok_rows
        row = ok_rows[0]
        assert row["http"] == 200
        assert isinstance(row["latency_ms"], int)
        assert set(row) == {"ts", "ok", "http", "latency_ms"}
    finally:
        _stop_runner(ops_root, env)


def test_restart_event_on_stub_crash(tmp_path):
    ops_root = tmp_path / "ops"
    env = _base_env(ops_root, VIBE_OPS_SERVE_CMD=_crash_serve_cmd(7))
    try:
        started = _run(["start"], env=env)
        assert started.returncode == 0, started.stderr

        supervisor_log = ops_root / "supervisor.jsonl"

        def has_restart():
            return any(e["event"] == "restart" for e in _read_jsonl(supervisor_log))

        assert _wait_for(has_restart, timeout=15)
        restart_events = [e for e in _read_jsonl(supervisor_log) if e["event"] == "restart"]
        first = restart_events[0]
        assert first["exit_code"] == 7
        assert first["restart_count"] == 1
    finally:
        _stop_runner(ops_root, env)


def test_stop_kills_entire_process_group_of_compound_serve_cmd(tmp_path):
    """Orphan safety must be structural: stop must kill EVERY descendant of
    the supervisor, even when the serve command is a compound shell pipeline
    whose top PID is just a wrapper. Under a single-PID kill the two
    backgrounded sleepers survive and this test fails."""
    ops_root = tmp_path / "ops"
    env = _base_env(ops_root, VIBE_OPS_SERVE_CMD=_compound_serve_cmd())
    tracked: list[int] = []
    try:
        started = _run(["start"], env=env)
        assert started.returncode == 0, started.stderr
        pid_file = ops_root / "run72.pid"
        assert _wait_for(pid_file.exists)
        supervisor_pid = int(pid_file.read_text().strip())

        # Wait until the full serve tree is up: heartbeat loop + wrapper +
        # both sleepers means at least 4 descendants of the supervisor.
        assert _wait_for(
            lambda: len(_descendants(supervisor_pid)) >= 4, timeout=15
        ), _descendants(supervisor_pid)
        tracked = _descendants(supervisor_pid)

        stopped = _run(["stop"], env=env, timeout=45)
        assert stopped.returncode == 0, stopped.stderr

        def all_dead():
            return not any(_pid_alive(p) for p in [supervisor_pid, *tracked])

        assert _wait_for(all_dead, timeout=15), [
            p for p in [supervisor_pid, *tracked] if _pid_alive(p)
        ]
    finally:
        _stop_runner(ops_root, env)
        for p in tracked:
            with contextlib.suppress(OSError):
                os.kill(p, signal.SIGKILL)


def test_serve_cmd_seam_warns_loudly_and_flags_start_event(tmp_path):
    """A 72h 'evidence' run against a stub must be self-incriminating: the
    test seam prints a loud warning (stderr + run72.log) and stamps
    serve_cmd_overridden:true into the supervisor start event."""
    ops_root = tmp_path / "ops"
    env = _base_env(ops_root, VIBE_OPS_SERVE_CMD=_sleepy_serve_cmd())
    try:
        started = _run(["start"], env=env)
        assert started.returncode == 0, started.stderr
        assert "TEST SEAM ACTIVE" in started.stderr

        supervisor_log = ops_root / "supervisor.jsonl"
        assert _wait_for(lambda: len(_read_jsonl(supervisor_log)) >= 1)
        start_event = [
            e for e in _read_jsonl(supervisor_log) if e["event"] == "start"
        ][0]
        assert start_event["serve_cmd_overridden"] is True

        run_log = ops_root / "run72.log"
        assert _wait_for(run_log.exists)
        assert "TEST SEAM ACTIVE" in run_log.read_text(encoding="utf-8")
    finally:
        _stop_runner(ops_root, env)


def test_no_seam_no_warning_no_override_flag(tmp_path):
    """With VIBE_OPS_SERVE_CMD unset the runner is silent about seams: no
    warning anywhere, no serve_cmd_overridden key in the start event. A fake
    `vibe-trading` shim on PATH stands in for the real server so the default
    command path is exercised hermetically."""
    ops_root = tmp_path / "ops"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "vibe-trading"
    shim.write_text("#!/bin/sh\nexec sleep 300\n", encoding="utf-8")
    shim.chmod(0o755)

    env = _base_env(ops_root)
    env.pop("VIBE_OPS_SERVE_CMD", None)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    try:
        started = _run(["start"], env=env)
        assert started.returncode == 0, started.stderr
        assert "TEST SEAM" not in started.stderr

        supervisor_log = ops_root / "supervisor.jsonl"
        assert _wait_for(lambda: len(_read_jsonl(supervisor_log)) >= 1)
        start_event = [
            e for e in _read_jsonl(supervisor_log) if e["event"] == "start"
        ][0]
        assert "serve_cmd_overridden" not in start_event

        run_log = ops_root / "run72.log"
        if run_log.exists():
            assert "TEST SEAM" not in run_log.read_text(encoding="utf-8")
    finally:
        _stop_runner(ops_root, env)


def test_start_replaces_stale_pid_file(tmp_path):
    """A dead PID planted in run72.pid must not block start: the stale file
    is replaced and the runner comes up normally."""
    ops_root = tmp_path / "ops"
    ops_root.mkdir(parents=True)
    # A PID guaranteed dead: spawn a no-op process and let it exit.
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    dead_pid = p.pid
    assert not _pid_alive(dead_pid)
    (ops_root / "run72.pid").write_text(f"{dead_pid}\n", encoding="utf-8")

    env = _base_env(ops_root, VIBE_OPS_SERVE_CMD=_sleepy_serve_cmd())
    try:
        started = _run(["start"], env=env)
        assert started.returncode == 0, started.stderr
        new_pid = int((ops_root / "run72.pid").read_text().strip())
        assert new_pid != dead_pid
        assert _wait_for(lambda: _pid_alive(new_pid))
    finally:
        _stop_runner(ops_root, env)


def test_status_reports_running_and_not_running(tmp_path):
    ops_root = tmp_path / "ops"
    env = _base_env(ops_root, VIBE_OPS_SERVE_CMD=_sleepy_serve_cmd())

    before = _run(["status"], env=env)
    assert before.returncode != 0
    assert "not running" in before.stdout

    try:
        started = _run(["start"], env=env)
        assert started.returncode == 0, started.stderr
        pid = int((ops_root / "run72.pid").read_text().strip())

        running = _run(["status"], env=env)
        assert running.returncode == 0
        assert "running" in running.stdout
        assert str(pid) in running.stdout
    finally:
        _stop_runner(ops_root, env)

    after = _run(["status"], env=env)
    assert after.returncode != 0
    assert "not running" in after.stdout
