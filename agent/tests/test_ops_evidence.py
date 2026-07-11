"""Fixture tests for the cross-referenced 72h evidence report (Task 2).

``build_evidence_report`` is pure -- every source root is passed explicitly
-- so these tests build small fixture trees under ``tmp_path`` and never rely
on (or mutate) real environment state beyond what the top-level
``_paper_env_guard`` autouse fixture already pins.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.ops.evidence import (
    MAX_DISPLAYED_EVENTS,
    build_evidence_report,
    default_window_start,
    parse_window_duration,
    render_markdown,
    report_filename,
)

UTC = timezone.utc


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


def _write_jsonl(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for r in rows:
        lines.append(r if isinstance(r, str) else json.dumps(r))
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _hb_row(ts: str, ok: bool = True, http: int = 200, latency_ms: int = 5) -> dict:
    return {"ts": ts, "ok": ok, "http": http, "latency_ms": latency_ms}


def _sup_start(ts: str, overridden: bool = False) -> dict:
    row = {"ts": ts, "event": "start", "serve_cmd": "vibe-trading serve", "env_fingerprint": []}
    if overridden:
        row["serve_cmd_overridden"] = True
    return row


def _sup_restart(ts: str, exit_code: int, restart_count: int) -> dict:
    return {"ts": ts, "event": "restart", "exit_code": exit_code, "restart_count": restart_count}


def _swarm_run(
    run_dir: Path,
    run_id: str,
    *,
    preset_name: str = "crypto_committee",
    created_at: str,
    completed_at: str | None = None,
    status: str = "completed",
    input_tokens: int = 1000,
    output_tokens: int = 200,
) -> None:
    d = run_dir / run_id
    d.mkdir(parents=True, exist_ok=True)
    payload = {
        "id": run_id,
        "preset_name": preset_name,
        "status": status,
        "user_vars": {},
        "agents": [],
        "tasks": [],
        "created_at": created_at,
        "completed_at": completed_at,
        "total_input_tokens": input_tokens,
        "total_output_tokens": output_tokens,
    }
    (d / "run.json").write_text(json.dumps(payload), encoding="utf-8")


class Fixture:
    """Bundles the four injectable roots + a default window."""

    def __init__(self, tmp_path: Path):
        self.ops_root = tmp_path / "ops"
        self.swarm_runs_root = tmp_path / "swarm-runs"
        self.paper_root = tmp_path / "paper"
        self.journal_path = tmp_path / "journal.jsonl"
        self.window_start = _dt("2026-07-01T00:00:00")
        self.window_end = _dt("2026-07-04T00:00:00")  # 72h window

    def build(self, **overrides):
        kwargs = dict(
            ops_root=self.ops_root,
            swarm_runs_root=self.swarm_runs_root,
            paper_root=self.paper_root,
            journal_path=self.journal_path,
        )
        kwargs.update(overrides)
        return build_evidence_report(self.window_start, self.window_end, **kwargs)


@pytest.fixture
def fx(tmp_path):
    return Fixture(tmp_path)


# --------------------------------------------------------------------------- #
# Heartbeat: uptime %, gap boundary, malformed lines
# --------------------------------------------------------------------------- #
def test_heartbeat_uptime_pct_exact(fx):
    rows = [
        _hb_row("2026-07-01T00:00:00Z", ok=True),
        _hb_row("2026-07-01T00:01:00Z", ok=True),
        _hb_row("2026-07-01T00:02:00Z", ok=False, http=503),
        _hb_row("2026-07-01T00:03:00Z", ok=True),
    ]
    _write_jsonl(fx.ops_root / "heartbeat.jsonl", rows)
    report = fx.build()
    hb = report["heartbeat"]
    assert hb["available"] is True
    assert hb["total_rows"] == 4
    assert hb["ok_rows"] == 3
    assert hb["uptime_pct"] == pytest.approx(75.0)


def test_gap_boundary_exactly_two_times_interval_is_not_a_gap(fx):
    # Regular 60s cadence establishes the median interval (60s), then one
    # delta of EXACTLY 120s (2x) -- must NOT be flagged as a gap.
    rows = [
        _hb_row("2026-07-01T00:00:00Z"),
        _hb_row("2026-07-01T00:01:00Z"),
        _hb_row("2026-07-01T00:03:00Z"),  # +120s from previous
        _hb_row("2026-07-01T00:04:00Z"),
    ]
    _write_jsonl(fx.ops_root / "heartbeat.jsonl", rows)
    report = fx.build()
    hb = report["heartbeat"]
    assert hb["interval_s"] == pytest.approx(60.0)
    assert hb["gaps"] == []
    assert hb["max_gap_s"] == pytest.approx(0.0)
    assert report["verdict"]["status"] != "n/a"  # sanity: verdict computed


def test_gap_strictly_greater_than_two_times_interval_is_flagged(fx):
    rows = [
        _hb_row("2026-07-01T00:00:00Z"),
        _hb_row("2026-07-01T00:01:00Z"),
        _hb_row("2026-07-01T00:03:01Z"),  # +121s > 2*60
        _hb_row("2026-07-01T00:04:01Z"),
    ]
    _write_jsonl(fx.ops_root / "heartbeat.jsonl", rows)
    report = fx.build()
    hb = report["heartbeat"]
    assert hb["interval_s"] == pytest.approx(60.0)
    assert len(hb["gaps"]) == 1
    assert hb["gaps"][0]["duration_s"] == pytest.approx(121.0)
    assert hb["max_gap_s"] == pytest.approx(121.0)


def test_ok_false_span_counts_as_a_gap_even_with_regular_cadence(fx):
    # Cadence stays regular (60s) throughout, so the delta-based check alone
    # would miss this -- the ok:false span sub-rule must catch it.
    rows = [
        _hb_row("2026-07-01T00:00:00Z", ok=True),
        _hb_row("2026-07-01T00:01:00Z", ok=False, http=503),
        _hb_row("2026-07-01T00:02:00Z", ok=False, http=503),
        _hb_row("2026-07-01T00:03:00Z", ok=True),
    ]
    _write_jsonl(fx.ops_root / "heartbeat.jsonl", rows)
    report = fx.build()
    hb = report["heartbeat"]
    down_gaps = [g for g in hb["gaps"] if "ok:false" in g["reason"]]
    assert len(down_gaps) == 1
    # first false (00:01) to last false (00:02) = 60s, + interval (60s) = 120s
    assert down_gaps[0]["duration_s"] == pytest.approx(120.0)


def test_heartbeat_malformed_lines_counted_not_skipped(fx):
    path = fx.ops_root / "heartbeat.jsonl"
    _write_jsonl(
        path,
        [
            _hb_row("2026-07-01T00:00:00Z"),
            "{not valid json",
            _hb_row("2026-07-01T00:01:00Z"),
            "[1, 2, 3]",  # valid JSON, not an object -> also malformed
        ],
    )
    report = fx.build()
    hb = report["heartbeat"]
    assert hb["malformed_lines"] == 2
    assert hb["total_rows"] == 2  # malformed lines never silently counted as rows


def test_heartbeat_missing_file_degrades_with_reason(fx):
    report = fx.build()  # no heartbeat.jsonl written at all
    hb = report["heartbeat"]
    assert hb["available"] is False
    assert "no data" in hb["reason"]
    assert hb["uptime_pct"] is None


# --------------------------------------------------------------------------- #
# Supervisor: restart counting, malformed lines, overridden-seam flag
# --------------------------------------------------------------------------- #
def test_restart_counting_in_window_excludes_out_of_window_events(fx):
    rows = [
        _sup_start("2026-06-30T00:00:00Z"),  # before window
        _sup_restart("2026-06-30T00:05:00Z", 1, 1),  # before window
        _sup_start("2026-07-01T00:00:00Z"),  # in window
        _sup_restart("2026-07-01T01:00:00Z", 1, 1),
        _sup_restart("2026-07-01T02:00:00Z", 1, 2),
        _sup_restart("2026-07-05T00:00:00Z", 1, 3),  # after window
    ]
    _write_jsonl(fx.ops_root / "supervisor.jsonl", rows)
    report = fx.build()
    sup = report["supervisor"]
    assert sup["restart_count"] == 2


def test_supervisor_malformed_lines_counted(fx):
    _write_jsonl(
        fx.ops_root / "supervisor.jsonl",
        [_sup_start("2026-07-01T00:00:00Z"), "{bad", _sup_restart("2026-07-01T01:00:00Z", 1, 1)],
    )
    report = fx.build()
    assert report["supervisor"]["malformed_lines"] == 1


def test_overridden_start_event_flagged_and_listed(fx):
    _write_jsonl(
        fx.ops_root / "supervisor.jsonl",
        [_sup_start("2026-07-01T00:00:00Z", overridden=True)],
    )
    report = fx.build()
    sup = report["supervisor"]
    assert len(sup["overridden_start_events"]) == 1
    assert sup["overridden_start_events"][0]["serve_cmd_overridden"] is True


# --------------------------------------------------------------------------- #
# Expected-firing math for `0 */2 * * *` (reuses executor's cron logic)
# --------------------------------------------------------------------------- #
def test_expected_firing_math_matches_manual_two_hourly_schedule(fx):
    # Window is exactly 72h starting at midnight -- "0 */2 * * *" should fire
    # at every even hour: 00:00, 02:00, ..., 70:00 -> 36 expected firings.
    fx.swarm_runs_root.mkdir(parents=True, exist_ok=True)  # exists, but empty
    report = fx.build(committee_schedule="0 */2 * * *")
    sched = report["scheduled_firings"]
    assert sched["configured"] is True
    assert sched["available"] is True
    expected_hours = [h for h in range(0, 73, 2)]  # window_end (72h) is itself a due firing
    assert len(sched["expected"]) == len(expected_hours)
    first = fx.window_start
    from src.ops.evidence import _parse_ts

    parsed = [_parse_ts(e) for e in sched["expected"]]
    assert parsed[0] == first
    assert parsed[1] == first + timedelta(hours=2)
    assert parsed[-1] == first + timedelta(hours=72)


def test_expected_vs_actual_missing_firing_detected(fx):
    # Two expected firings (00:00 and 02:00 within a 4h window); only the
    # first has a matching actual run -> exactly one missing.
    fx.window_end = fx.window_start + timedelta(hours=4)
    _swarm_run(
        fx.swarm_runs_root,
        "run-1",
        created_at="2026-07-01T00:00:30+00:00",
        completed_at="2026-07-01T00:10:00+00:00",
    )
    report = fx.build(committee_schedule="0 */2 * * *")
    sched = report["scheduled_firings"]
    assert len(sched["expected"]) == 3  # 00:00, 02:00, 04:00 (04:00 boundary included)
    assert sched["missing"] == ["2026-07-01T02:00:00Z", "2026-07-01T04:00:00Z"]
    assert len(sched["actual_runs"]) == 1
    assert sched["actual_runs"][0]["run_id"] == "run-1"
    assert sched["actual_runs"][0]["wall_clock_s"] == pytest.approx(570.0)


def test_expected_firing_ignores_non_committee_preset_runs(fx):
    fx.window_end = fx.window_start + timedelta(hours=2)
    _swarm_run(
        fx.swarm_runs_root,
        "other-run",
        preset_name="research_team",
        created_at="2026-07-01T00:00:30+00:00",
    )
    report = fx.build(committee_schedule="0 */2 * * *")
    sched = report["scheduled_firings"]
    assert sched["missing"] == ["2026-07-01T00:00:00Z", "2026-07-01T02:00:00Z"]


def test_committee_schedule_unconfigured_is_not_a_degradation(fx):
    report = fx.build()  # committee_schedule left as default None
    sched = report["scheduled_firings"]
    assert sched["configured"] is False
    assert sched["expected"] == []
    assert sched["missing"] == []


# --------------------------------------------------------------------------- #
# Missing-source degradation across every section
# --------------------------------------------------------------------------- #
def test_missing_sources_degrade_every_section_and_verdict(fx):
    report = fx.build(committee_schedule="0 */2 * * *")
    assert report["heartbeat"]["available"] is False
    assert report["supervisor"]["available"] is False
    assert report["scheduled_firings"]["available"] is False
    assert report["paper"]["available"] is False
    assert report["journal"]["available"] is False
    assert report["ops_health"]["available"] is False
    assert report["verdict"]["status"] == "INTERRUPTED/DEGRADED"
    assert any("supervisor evidence unavailable" in r for r in report["verdict"]["reasons"])
    assert any("heartbeat evidence unavailable" in r for r in report["verdict"]["reasons"])
    assert any("scheduled-firing evidence unavailable" in r for r in report["verdict"]["reasons"])


def test_paper_root_missing_reports_no_data_without_creating_directory(fx):
    report = fx.build()
    assert report["paper"]["available"] is False
    assert not fx.paper_root.exists()  # must never side-effect-create it


def test_journal_missing_reports_no_data(fx):
    report = fx.build()
    assert report["journal"]["available"] is False
    assert "no data" in report["journal"]["reason"]


# --------------------------------------------------------------------------- #
# Verdict: each condition independently flips it (incl. overridden seam)
# --------------------------------------------------------------------------- #
def _clean_fixture(fx: Fixture) -> None:
    """A fully clean 4h fixture: 0 restarts, tight heartbeat, all firings met."""
    fx.window_end = fx.window_start + timedelta(hours=4)
    hb_rows = []
    t = fx.window_start
    while t <= fx.window_end:
        hb_rows.append(_hb_row(t.strftime("%Y-%m-%dT%H:%M:%SZ")))
        t += timedelta(minutes=1)
    _write_jsonl(fx.ops_root / "heartbeat.jsonl", hb_rows)
    _write_jsonl(fx.ops_root / "supervisor.jsonl", [_sup_start(fx.window_start.strftime("%Y-%m-%dT%H:%M:%SZ"))])
    _swarm_run(
        fx.swarm_runs_root, "run-1",
        created_at="2026-07-01T00:00:10+00:00", completed_at="2026-07-01T00:05:00+00:00",
    )
    _swarm_run(
        fx.swarm_runs_root, "run-2",
        created_at="2026-07-01T02:00:10+00:00", completed_at="2026-07-01T02:05:00+00:00",
    )
    _swarm_run(
        fx.swarm_runs_root, "run-3",
        created_at="2026-07-01T04:00:10+00:00", completed_at="2026-07-01T04:05:00+00:00",
    )


def test_verdict_uninterrupted_on_clean_fixture(fx):
    _clean_fixture(fx)
    report = fx.build(committee_schedule="0 */2 * * *")
    assert report["verdict"] == {"status": "UNINTERRUPTED", "reasons": []}


def test_verdict_flips_on_restart(fx):
    _clean_fixture(fx)
    # Append a restart event on top of the clean supervisor log.
    with (fx.ops_root / "supervisor.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(_sup_restart("2026-07-01T01:00:00Z", 1, 1)) + "\n")
    report = fx.build(committee_schedule="0 */2 * * *")
    assert report["verdict"]["status"] == "INTERRUPTED/DEGRADED"
    assert any("restart" in r for r in report["verdict"]["reasons"])


def test_verdict_flips_on_heartbeat_gap(fx):
    _clean_fixture(fx)
    # Rewrite heartbeat with one big gap (> 2x the 60s cadence).
    hb_rows = [
        _hb_row("2026-07-01T00:00:00Z"),
        _hb_row("2026-07-01T00:01:00Z"),
        _hb_row("2026-07-01T00:10:00Z"),  # big gap
        _hb_row("2026-07-01T00:11:00Z"),
    ]
    _write_jsonl(fx.ops_root / "heartbeat.jsonl", hb_rows)
    report = fx.build(committee_schedule="0 */2 * * *")
    assert report["verdict"]["status"] == "INTERRUPTED/DEGRADED"
    assert any("gap" in r for r in report["verdict"]["reasons"])


def test_verdict_flips_on_missing_expected_firing(fx):
    _clean_fixture(fx)
    # Remove one of the three committee runs so a firing goes missing.
    import shutil

    shutil.rmtree(fx.swarm_runs_root / "run-2")
    report = fx.build(committee_schedule="0 */2 * * *")
    assert report["verdict"]["status"] == "INTERRUPTED/DEGRADED"
    assert any("missing" in r for r in report["verdict"]["reasons"])


def test_verdict_flips_on_overridden_serve_cmd_seam(fx):
    _clean_fixture(fx)
    # Rewrite the supervisor log's start event to carry the test-seam flag.
    _write_jsonl(
        fx.ops_root / "supervisor.jsonl",
        [_sup_start(fx.window_start.strftime("%Y-%m-%dT%H:%M:%SZ"), overridden=True)],
    )
    report = fx.build(committee_schedule="0 */2 * * *")
    assert report["verdict"]["status"] == "INTERRUPTED/DEGRADED"
    assert any("VIBE_OPS_SERVE_CMD override" in r for r in report["verdict"]["reasons"])
    assert any("stub-server run cannot count as valid evidence" in r for r in report["verdict"]["reasons"])


# --------------------------------------------------------------------------- #
# Huge restart count must degrade cleanly (no choking)
# --------------------------------------------------------------------------- #
def test_huge_restart_count_renders_without_choking(fx):
    n = 20000
    rows = [_sup_start("2026-07-01T00:00:00Z")]
    t = fx.window_start + timedelta(seconds=5)
    for i in range(1, n + 1):
        rows.append(_sup_restart(t.strftime("%Y-%m-%dT%H:%M:%SZ"), 1, i))
        t += timedelta(seconds=5)
    _write_jsonl(fx.ops_root / "supervisor.jsonl", rows)

    fx.window_end = t + timedelta(seconds=5)
    report = fx.build()
    sup = report["supervisor"]
    assert sup["restart_count"] == n  # exact total, never capped
    assert len(sup["restarts"]) <= MAX_DISPLAYED_EVENTS  # display list IS capped
    assert sup["restarts_omitted"] == n - len(sup["restarts"])
    assert report["verdict"]["status"] == "INTERRUPTED/DEGRADED"

    markdown = render_markdown(report)
    assert "20000" in markdown
    assert "omitted" in markdown
    # The rendered doc must stay small even with 20k restart rows recorded.
    assert len(markdown) < 50_000


# --------------------------------------------------------------------------- #
# --json shape (top-level keys + JSON-serializable)
# --------------------------------------------------------------------------- #
def test_json_shape_is_stable_and_serializable(fx):
    _clean_fixture(fx)
    report = fx.build(committee_schedule="0 */2 * * *")
    assert set(report.keys()) == {
        "window", "generated_at", "verdict", "heartbeat", "supervisor",
        "scheduled_firings", "paper", "journal", "ops_health",
    }
    assert set(report["verdict"].keys()) == {"status", "reasons"}
    # Must round-trip through json.dumps with no custom encoder.
    dumped = json.dumps(report)
    reloaded = json.loads(dumped)
    assert reloaded["verdict"]["status"] == "UNINTERRUPTED"


# --------------------------------------------------------------------------- #
# render_markdown smoke (sections present)
# --------------------------------------------------------------------------- #
def test_render_markdown_includes_all_spec_sections(fx):
    _clean_fixture(fx)
    report = fx.build(committee_schedule="0 */2 * * *")
    markdown = render_markdown(report)
    for heading in (
        "## Verdict: UNINTERRUPTED",
        "## Heartbeat continuity",
        "## Supervisor events",
        "## Scheduled committee-run firings",
        "## Paper-trading activity",
        "## Committee journal activity",
        "## Ops health",
    ):
        assert heading in markdown


# --------------------------------------------------------------------------- #
# CLI-facing helpers: window parsing + default-window resolution
# --------------------------------------------------------------------------- #
def test_parse_window_duration_hours_and_days():
    assert parse_window_duration("72h") == timedelta(hours=72)
    assert parse_window_duration("3d") == timedelta(days=3)
    with pytest.raises(ValueError):
        parse_window_duration("nonsense")


def test_default_window_start_uses_last_supervisor_start_event(fx, tmp_path):
    _write_jsonl(
        fx.ops_root / "supervisor.jsonl",
        [
            _sup_start("2026-07-01T00:00:00Z"),
            _sup_restart("2026-07-01T01:00:00Z", 1, 1),
            _sup_start("2026-07-02T00:00:00Z"),  # a later start (e.g. after a manual stop/start)
        ],
    )
    now = _dt("2026-07-05T00:00:00")
    start, source = default_window_start(fx.ops_root, now)
    assert start == _dt("2026-07-02T00:00:00")
    assert "last supervisor start event" in source


def test_default_window_start_falls_back_to_72h_when_no_start_event(fx):
    now = _dt("2026-07-05T00:00:00")
    start, source = default_window_start(fx.ops_root, now)
    assert start == now - timedelta(hours=72)
    assert "fallback" in source


def test_report_filename_format():
    ts = _dt("2026-07-01T00:00:05")
    assert report_filename(ts) == "report-20260701T000005Z.md"


# --------------------------------------------------------------------------- #
# CLI: `vibe-trading ops report` smoke + --json shape
# --------------------------------------------------------------------------- #
class TestOpsReportCli:
    """Exercises ``cmd_ops_report`` end-to-end against fixture env roots.

    ``SWARM_DIR`` is a module-level constant in ``cli._legacy`` (not an env
    var), so it is monkeypatched directly -- same technique needed for any
    CLI test that wants an isolated swarm run store.
    """

    def _set_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("VIBE_OPS_ROOT", str(tmp_path / "ops"))
        monkeypatch.setenv("VIBE_PAPER_ROOT", str(tmp_path / "paper"))
        monkeypatch.setenv("VIBE_TRADING_COMMITTEE_JOURNAL", str(tmp_path / "journal.jsonl"))
        monkeypatch.delenv("VIBE_COMMITTEE_SCHEDULE", raising=False)
        monkeypatch.setattr("cli._legacy.SWARM_DIR", tmp_path / "swarm-runs")

    def test_smoke_writes_markdown_report_and_returns_success(self, monkeypatch, tmp_path, capsys):
        from cli._legacy import EXIT_SUCCESS, cmd_ops_report

        self._set_env(monkeypatch, tmp_path)
        rc = cmd_ops_report(window="4h")
        out = capsys.readouterr().out
        assert rc == EXIT_SUCCESS
        assert "Evidence Report" in out

        ops_root = tmp_path / "ops"
        reports = list(ops_root.glob("report-*.md"))
        assert len(reports) == 1
        content = reports[0].read_text(encoding="utf-8")
        assert "# 72h Operation Evidence Report" in content
        assert "## Verdict:" in content

    def test_json_flag_prints_full_report_shape(self, monkeypatch, tmp_path, capsys):
        from cli._legacy import EXIT_SUCCESS, cmd_ops_report

        self._set_env(monkeypatch, tmp_path)
        rc = cmd_ops_report(window="4h", json_mode=True)
        out = capsys.readouterr().out
        assert rc == EXIT_SUCCESS
        payload = json.loads(out)
        assert set(payload.keys()) == {
            "window", "generated_at", "verdict", "heartbeat", "supervisor",
            "scheduled_firings", "paper", "journal", "ops_health",
        }
        assert payload["verdict"]["status"] == "INTERRUPTED/DEGRADED"  # no artifacts at all yet

    def test_invalid_window_flag_is_a_usage_error(self, monkeypatch, tmp_path, capsys):
        from cli._legacy import EXIT_USAGE_ERROR, cmd_ops_report

        self._set_env(monkeypatch, tmp_path)
        rc = cmd_ops_report(window="not-a-window")
        assert rc == EXIT_USAGE_ERROR

    def test_default_window_reads_last_supervisor_start_event(self, monkeypatch, tmp_path, capsys):
        from cli._legacy import EXIT_SUCCESS, cmd_ops_report

        self._set_env(monkeypatch, tmp_path)
        ops_root = tmp_path / "ops"
        ops_root.mkdir(parents=True)
        _write_jsonl(ops_root / "supervisor.jsonl", [_sup_start("2026-01-01T00:00:00Z")])

        rc = cmd_ops_report(json_mode=True)
        out = capsys.readouterr().out
        assert rc == EXIT_SUCCESS
        payload = json.loads(out)
        assert payload["window"]["start"] == "2026-01-01T00:00:00Z"
        assert payload["window"]["start_source"] == "last supervisor start event"
