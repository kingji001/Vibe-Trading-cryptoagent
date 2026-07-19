"""Unit tests for `vibe-trading ui` (Task C1).

Three branches from docs/superpowers/specs/2026-07-19-committee-observatory-mcp-design.md
§3.3: no dist / serve down / serve up. Network (`_probe_health`), browser
(`webbrowser.open`), and subprocess (`subprocess.Popen`, `subprocess.run` via
`_run_step`) are all faked — no real server, no real browser, no real npm.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import cli


def _frontend_with_dist(tmp_path: Path) -> Path:
    frontend_dir = tmp_path / "frontend"
    dist = frontend_dir / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html></html>")
    return frontend_dir


def _frontend_without_dist(tmp_path: Path) -> Path:
    frontend_dir = tmp_path / "frontend"
    frontend_dir.mkdir()
    return frontend_dir


class TestNoDist:
    """Branch 1: frontend/dist/index.html missing."""

    def test_builds_when_npm_present(self, tmp_path: Path) -> None:
        frontend_dir = _frontend_without_dist(tmp_path)

        def _fake_run(cmd, **kwargs):
            # Simulate the build actually producing dist/index.html.
            if cmd[:2] == ["npm", "run"] or cmd[:2] == ["npm", "install"]:
                (frontend_dir / "dist").mkdir(exist_ok=True)
                (frontend_dir / "dist" / "index.html").write_text("<html></html>")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch.object(cli._legacy, "_resolve_node_and_npm", return_value=("/usr/bin/node", "/usr/bin/npm")):
            with patch.object(cli._legacy, "_is_windows", return_value=False):
                with patch("cli._legacy.subprocess.run", side_effect=_fake_run) as mock_run:
                    with patch.object(cli._legacy, "_wait_for_health", return_value=True):
                        with patch("cli._legacy.subprocess.Popen") as mock_popen:
                            with patch.object(cli._legacy.webbrowser, "open") as mock_open:
                                rc = cli._legacy.cmd_ui(frontend_dir=frontend_dir)

        assert rc == cli._legacy.EXIT_SUCCESS
        assert mock_run.call_count == 2  # npm install + npm run build
        mock_popen.assert_called_once()  # serve was down -> started
        mock_open.assert_called_once_with("http://127.0.0.1:8000/committee")

    def test_windows_build_invokes_resolved_npm_path(self, tmp_path: Path) -> None:
        """On Windows npm ships as npm.cmd; subprocess.run does not consult
        PATHEXT for bare names, so the build must invoke the resolved path
        (cmd_setup already does — cmd_ui's build loop must too)."""
        frontend_dir = _frontend_without_dist(tmp_path)
        npm_path = r"C:\Program Files\nodejs\npm.cmd"

        def _fake_run(cmd, **kwargs):
            (frontend_dir / "dist").mkdir(exist_ok=True)
            (frontend_dir / "dist" / "index.html").write_text("<html></html>")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch.object(cli._legacy, "_resolve_node_and_npm", return_value=(r"C:\node.exe", npm_path)):
            with patch.object(cli._legacy, "_is_windows", return_value=True):
                with patch("cli._legacy.subprocess.run", side_effect=_fake_run) as mock_run:
                    with patch.object(cli._legacy, "_wait_for_health", return_value=True):
                        with patch("cli._legacy.subprocess.Popen"):
                            with patch.object(cli._legacy.webbrowser, "open"):
                                rc = cli._legacy.cmd_ui(frontend_dir=frontend_dir)

        assert rc == cli._legacy.EXIT_SUCCESS
        invoked = [call.args[0][0] for call in mock_run.call_args_list]
        assert invoked and all(head == npm_path for head in invoked), invoked

    def test_refusal_names_node_when_only_node_missing(self, tmp_path: Path, capsys) -> None:
        """node absent but npm present must not claim "npm is not on PATH"."""
        frontend_dir = _frontend_without_dist(tmp_path)
        with patch.object(cli._legacy, "_resolve_node_and_npm", return_value=(None, "/usr/bin/npm")):
            with patch("cli._legacy.subprocess.Popen") as mock_popen:
                with patch.object(cli._legacy.webbrowser, "open") as mock_open:
                    rc = cli._legacy.cmd_ui(frontend_dir=frontend_dir)
        out = capsys.readouterr().out
        assert rc == cli._legacy.EXIT_USAGE_ERROR
        # Own short line so rich's wrapping can't split the phrase; must name
        # exactly what's missing (node), not blame the npm that IS present.
        assert "Required tools not on PATH: node." in out
        mock_popen.assert_not_called()
        mock_open.assert_not_called()

    def test_refuses_when_npm_missing(self, tmp_path: Path, capsys) -> None:
        frontend_dir = _frontend_without_dist(tmp_path)
        with patch.object(cli._legacy, "_resolve_node_and_npm", return_value=(None, None)):
            with patch("cli._legacy.subprocess.Popen") as mock_popen:
                with patch.object(cli._legacy.webbrowser, "open") as mock_open:
                    rc = cli._legacy.cmd_ui(frontend_dir=frontend_dir)
        out = capsys.readouterr().out
        assert rc == cli._legacy.EXIT_USAGE_ERROR
        assert "npm --prefix frontend run build" in out
        mock_popen.assert_not_called()
        mock_open.assert_not_called()

    def test_run_failed_when_build_step_fails(self, tmp_path: Path) -> None:
        frontend_dir = _frontend_without_dist(tmp_path)
        with patch.object(cli._legacy, "_resolve_node_and_npm", return_value=("/usr/bin/node", "/usr/bin/npm")):
            with patch.object(cli._legacy, "_is_windows", return_value=False):
                with patch("cli._legacy.subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="boom")
                    with patch("cli._legacy.subprocess.Popen") as mock_popen:
                        rc = cli._legacy.cmd_ui(frontend_dir=frontend_dir)
        assert rc == cli._legacy.EXIT_RUN_FAILED
        mock_popen.assert_not_called()


class TestServeDown:
    """Branch 2: dist present, /health not answering -> start serve, wait, open browser."""

    def test_starts_serve_and_opens_browser(self, tmp_path: Path) -> None:
        frontend_dir = _frontend_with_dist(tmp_path)
        with patch.object(cli._legacy, "_wait_for_health", return_value=True) as mock_wait:
            with patch.object(cli._legacy, "_probe_health", return_value=False):
                with patch("cli._legacy.subprocess.Popen") as mock_popen:
                    with patch.object(cli._legacy.webbrowser, "open") as mock_open:
                        rc = cli._legacy.cmd_ui(host="127.0.0.1", port=8123, frontend_dir=frontend_dir)

        assert rc == cli._legacy.EXIT_SUCCESS
        mock_popen.assert_called_once()
        popen_cmd = mock_popen.call_args.args[0]
        assert popen_cmd[:4] == [cli._legacy.sys.executable, "-m", "cli._legacy", "serve"]
        assert "--host" in popen_cmd and "--port" in popen_cmd
        assert popen_cmd[popen_cmd.index("--port") + 1] == "8123"
        mock_wait.assert_called_once()
        mock_open.assert_called_once_with("http://127.0.0.1:8123/committee")

    def test_run_failed_when_never_healthy(self, tmp_path: Path, capsys) -> None:
        frontend_dir = _frontend_with_dist(tmp_path)
        with patch.object(cli._legacy, "_probe_health", return_value=False):
            with patch.object(cli._legacy, "_wait_for_health", return_value=False):
                with patch("cli._legacy.subprocess.Popen") as mock_popen:
                    with patch.object(cli._legacy.webbrowser, "open") as mock_open:
                        rc = cli._legacy.cmd_ui(frontend_dir=frontend_dir)
        assert rc == cli._legacy.EXIT_RUN_FAILED
        mock_popen.assert_called_once()
        mock_open.assert_not_called()


class TestServeUp:
    """Branch 3: /health already answering -> attach, never double-start."""

    def test_attaches_without_starting_serve(self, tmp_path: Path) -> None:
        frontend_dir = _frontend_with_dist(tmp_path)
        with patch.object(cli._legacy, "_probe_health", return_value=True):
            with patch("cli._legacy.subprocess.Popen") as mock_popen:
                with patch.object(cli._legacy.webbrowser, "open") as mock_open:
                    rc = cli._legacy.cmd_ui(frontend_dir=frontend_dir)

        assert rc == cli._legacy.EXIT_SUCCESS
        mock_popen.assert_not_called()
        mock_open.assert_called_once_with("http://127.0.0.1:8000/committee")


class TestWaitForHealth:
    """`_wait_for_health` polls `_probe_health` until healthy or timeout, sleeping between polls."""

    def test_returns_true_as_soon_as_healthy(self) -> None:
        calls = {"n": 0}

        def _fake_probe(url, **kwargs):
            calls["n"] += 1
            return calls["n"] >= 3

        with patch.object(cli._legacy, "_probe_health", side_effect=_fake_probe):
            with patch.object(cli._legacy.time, "sleep") as mock_sleep:
                ok = cli._legacy._wait_for_health("http://x/health", timeout_s=10.0, poll_interval=0.1)
        assert ok is True
        assert calls["n"] == 3
        assert mock_sleep.call_count == 2  # slept between polls 1->2 and 2->3, not after success

    def test_returns_false_on_timeout(self) -> None:
        # time.monotonic() sequence: start, then jump straight past the deadline.
        with patch.object(cli._legacy, "_probe_health", return_value=False):
            with patch.object(cli._legacy.time, "sleep"):
                with patch.object(cli._legacy.time, "monotonic", side_effect=[0.0, 100.0]):
                    ok = cli._legacy._wait_for_health("http://x/health", timeout_s=10.0, poll_interval=0.1)
        assert ok is False
