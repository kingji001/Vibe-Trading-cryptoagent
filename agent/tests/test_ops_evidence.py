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
    # delta of EXACTLY 120s (2x) between HEALTHY beats -- must NOT be flagged
    # as a gap (spec sec 4 boundary rule; distinct from an ok:false span or
    # a recorded edge gap of 2x, which DO fail the verdict).
    fx.window_end = _dt("2026-07-01T00:04:00")  # tight window: no edge slack
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
    # No heartbeat-continuity reason may appear: exactly-2x delta passes.
    # (Match specific phrases, not "gap" -- pytest's tmp dir name contains
    # this test's own name, which leaks "gap" into path-bearing reasons.)
    assert not any("heartbeat gap" in r or "coverage" in r or "unhealthy" in r
                   for r in report["verdict"]["reasons"])


def test_gap_strictly_greater_than_two_times_interval_is_flagged(fx):
    fx.window_end = _dt("2026-07-01T00:04:01")  # tight window: no edge slack
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


def test_ok_false_span_counts_as_a_gap_and_degrades_the_verdict(fx):
    # Cadence stays regular (60s) throughout, so the delta-based check alone
    # would miss this -- the ok:false span sub-rule must catch it AND the
    # VERDICT must degrade (an unhealthy server is not an uninterrupted run,
    # whatever the span length).
    fx.window_end = _dt("2026-07-01T00:03:00")  # tight window: no edge slack
    rows = [
        _hb_row("2026-07-01T00:00:00Z", ok=True),
        _hb_row("2026-07-01T00:01:00Z", ok=False, http=503),
        _hb_row("2026-07-01T00:02:00Z", ok=False, http=503),
        _hb_row("2026-07-01T00:03:00Z", ok=True),
    ]
    _write_jsonl(fx.ops_root / "heartbeat.jsonl", rows)
    _write_jsonl(fx.ops_root / "supervisor.jsonl", [_sup_start("2026-07-01T00:00:00Z")])
    report = fx.build()
    hb = report["heartbeat"]
    down_gaps = [g for g in hb["gaps"] if "ok:false" in g["reason"]]
    assert len(down_gaps) == 1
    # first false (00:01) to last false (00:02) = 60s, + interval (60s) = 120s
    assert down_gaps[0]["duration_s"] == pytest.approx(120.0)
    assert report["verdict"]["status"] == "INTERRUPTED/DEGRADED"
    assert any("ok:false" in r for r in report["verdict"]["reasons"])


def test_reviewer_repro_short_ok_false_span_degrades_despite_regular_cadence(fx):
    # Reviewer's C2 reproduction: two consecutive ok:false readings at a
    # perfectly regular 60s cadence -> uptime 60%, but pre-fix the verdict
    # read UNINTERRUPTED with reasons [] because the span duration (120s)
    # did not exceed 2x interval. Any unhealthy reading must degrade.
    fx.window_end = _dt("2026-07-01T00:04:00")
    rows = [
        _hb_row("2026-07-01T00:00:00Z", ok=True),
        _hb_row("2026-07-01T00:01:00Z", ok=False, http=503),
        _hb_row("2026-07-01T00:02:00Z", ok=False, http=503),
        _hb_row("2026-07-01T00:03:00Z", ok=True),
        _hb_row("2026-07-01T00:04:00Z", ok=True),
    ]
    _write_jsonl(fx.ops_root / "heartbeat.jsonl", rows)
    _write_jsonl(fx.ops_root / "supervisor.jsonl", [_sup_start("2026-07-01T00:00:00Z")])
    report = fx.build()
    assert report["heartbeat"]["uptime_pct"] == pytest.approx(60.0)
    assert report["verdict"]["status"] == "INTERRUPTED/DEGRADED"
    assert any("unhealthy" in r for r in report["verdict"]["reasons"])


def test_reviewer_repro_heartbeat_stream_that_stops_is_not_uninterrupted(fx):
    # Reviewer's C1 reproduction: a 72h window whose heartbeat stream covers
    # only the first 10 minutes (power loss / machine death -- the supervisor
    # dies too, so 1 start and 0 restarts). Pre-fix this scored 100% uptime
    # and UNINTERRUPTED because gaps were only computed BETWEEN in-window
    # rows. The trailing window-edge gap must now be recorded and named, and
    # the verdict must be INTERRUPTED/DEGRADED.
    rows = [
        _hb_row(f"2026-07-01T00:{m:02d}:00Z") for m in range(11)  # first 10 minutes only
    ]
    _write_jsonl(fx.ops_root / "heartbeat.jsonl", rows)
    _write_jsonl(fx.ops_root / "supervisor.jsonl", [_sup_start("2026-07-01T00:00:00Z")])
    report = fx.build()  # window: 72h (2026-07-01 .. 2026-07-04)
    hb = report["heartbeat"]
    edge_gaps = [g for g in hb["gaps"] if "window end" in g["reason"]]
    assert len(edge_gaps) == 1
    assert edge_gaps[0]["start"] == "2026-07-01T00:10:00Z"
    assert edge_gaps[0]["end"] == "2026-07-04T00:00:00Z"
    assert hb["coverage_pct"] is not None and hb["coverage_pct"] < 1.0
    assert report["verdict"]["status"] == "INTERRUPTED/DEGRADED"
    assert any("window end" in r for r in report["verdict"]["reasons"])


def test_late_start_leading_edge_gap_is_flagged(fx):
    # Symmetric C1 case: first beat long after window start.
    fx.window_end = _dt("2026-07-01T01:03:00")
    rows = [
        _hb_row("2026-07-01T01:00:00Z"),
        _hb_row("2026-07-01T01:01:00Z"),
        _hb_row("2026-07-01T01:02:00Z"),
        _hb_row("2026-07-01T01:03:00Z"),
    ]
    _write_jsonl(fx.ops_root / "heartbeat.jsonl", rows)
    report = fx.build()
    hb = report["heartbeat"]
    edge_gaps = [g for g in hb["gaps"] if "window start" in g["reason"]]
    assert len(edge_gaps) == 1
    assert edge_gaps[0]["duration_s"] == pytest.approx(3600.0)
    assert report["verdict"]["status"] == "INTERRUPTED/DEGRADED"


def test_combined_edge_slack_below_per_edge_threshold_still_degrades_coverage(fx):
    # Each edge alone is within 2x interval (120s), but together the window
    # has 190s of uncovered edge time -- the coverage rule must catch what
    # the per-edge gap rule alone cannot.
    fx.window_start = _dt("2026-07-01T00:00:00")
    fx.window_end = _dt("2026-07-01T00:06:10")
    rows = [
        _hb_row("2026-07-01T00:01:30Z"),  # lead: 90s (<= 120s threshold)
        _hb_row("2026-07-01T00:02:30Z"),
        _hb_row("2026-07-01T00:03:30Z"),
        _hb_row("2026-07-01T00:04:30Z"),  # trail: 100s (<= 120s threshold)
    ]
    _write_jsonl(fx.ops_root / "heartbeat.jsonl", rows)
    _write_jsonl(fx.ops_root / "supervisor.jsonl", [_sup_start("2026-07-01T00:00:00Z")])
    report = fx.build()
    hb = report["heartbeat"]
    assert not any("window" in g["reason"] for g in hb["gaps"])  # neither edge alone
    assert hb["uncovered_edge_s"] == pytest.approx(190.0)
    assert report["verdict"]["status"] == "INTERRUPTED/DEGRADED"
    assert any("coverage" in r for r in report["verdict"]["reasons"])


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
    # "heartbeat gap" specifically -- a bare "gap" would match pytest's own
    # tmp dir name inside path-bearing reasons.
    assert any("heartbeat gap" in r for r in report["verdict"]["reasons"])


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
# Startup-grace rule: cold-start ok:false beats must not forbid UNINTERRUPTED
# --------------------------------------------------------------------------- #
def test_cold_start_first_beat_false_within_grace_still_uninterrupted(fx):
    # Task 3 smoke repro: the heartbeat loop starts before uvicorn finishes
    # booting, so the FIRST beat of every real run is ok:false. Under the
    # pre-grace rule ("uptime < 100% => never UNINTERRUPTED") no genuine 72h
    # run could ever earn the verdict. An ok:false reading after a start
    # event, before the first ok:true, within the grace window, is startup
    # grace: excluded from the verdict and from unhealthy-span gap math.
    fx.window_end = _dt("2026-07-01T00:04:05")
    _write_jsonl(fx.ops_root / "supervisor.jsonl", [_sup_start("2026-07-01T00:00:00Z")])
    rows = [
        _hb_row("2026-07-01T00:00:05Z", ok=False, http=None),  # cold start
        _hb_row("2026-07-01T00:01:05Z", ok=True),
        _hb_row("2026-07-01T00:02:05Z", ok=True),
        _hb_row("2026-07-01T00:03:05Z", ok=True),
        _hb_row("2026-07-01T00:04:05Z", ok=True),
    ]
    _write_jsonl(fx.ops_root / "heartbeat.jsonl", rows)
    report = fx.build()
    hb = report["heartbeat"]
    assert hb["startup_grace_rows"] == 1
    assert hb["unhealthy_rows"] == 0
    assert hb["gaps"] == []  # grace beats excluded from unhealthy-span gap math
    assert report["verdict"] == {"status": "UNINTERRUPTED", "reasons": []}

    markdown = render_markdown(report)
    assert (
        "1 startup-grace reading(s) excluded from verdict per "
        "VIBE_OPS_STARTUP_GRACE_S=120" in markdown
    )


def test_ok_false_beyond_grace_window_with_no_healthy_beat_yet_degrades(fx):
    # Server never became healthy within the grace window: readings past the
    # 120s grace horizon are real unhealthiness even though no ok:true has
    # been seen yet. The 120s reading itself (exactly at the horizon) is
    # still grace (inclusive bound).
    fx.window_end = _dt("2026-07-01T00:05:00")
    _write_jsonl(fx.ops_root / "supervisor.jsonl", [_sup_start("2026-07-01T00:00:00Z")])
    rows = [
        _hb_row("2026-07-01T00:00:00Z", ok=False, http=None),  # +0s   grace
        _hb_row("2026-07-01T00:01:00Z", ok=False, http=None),  # +60s  grace
        _hb_row("2026-07-01T00:02:00Z", ok=False, http=None),  # +120s grace (inclusive)
        _hb_row("2026-07-01T00:03:00Z", ok=False, http=None),  # +180s BEYOND -> unhealthy
        _hb_row("2026-07-01T00:04:00Z", ok=True),
        _hb_row("2026-07-01T00:05:00Z", ok=True),
    ]
    _write_jsonl(fx.ops_root / "heartbeat.jsonl", rows)
    report = fx.build()
    hb = report["heartbeat"]
    assert hb["startup_grace_rows"] == 3
    assert hb["unhealthy_rows"] == 1
    assert report["verdict"]["status"] == "INTERRUPTED/DEGRADED"
    assert any("unhealthy" in r for r in report["verdict"]["reasons"])


def test_ok_false_after_first_healthy_beat_is_never_grace(fx):
    # Once the server has answered healthy, any later ok:false is real
    # unhealthiness -- even if it lands within 120s of the start event.
    fx.window_end = _dt("2026-07-01T00:03:00")
    _write_jsonl(fx.ops_root / "supervisor.jsonl", [_sup_start("2026-07-01T00:00:00Z")])
    rows = [
        _hb_row("2026-07-01T00:00:00Z", ok=True),
        _hb_row("2026-07-01T00:01:00Z", ok=False, http=503),  # within 120s of start, but after ok:true
        _hb_row("2026-07-01T00:02:00Z", ok=True),
        _hb_row("2026-07-01T00:03:00Z", ok=True),
    ]
    _write_jsonl(fx.ops_root / "heartbeat.jsonl", rows)
    report = fx.build()
    assert report["heartbeat"]["startup_grace_rows"] == 0
    assert report["heartbeat"]["unhealthy_rows"] == 1
    assert report["verdict"]["status"] == "INTERRUPTED/DEGRADED"


def test_restart_event_opens_a_new_grace_window(fx):
    # Each start AND restart event gets its own grace window: the beat right
    # after a crash-restart is a cold uvicorn again. The restart itself still
    # degrades the verdict (restart count), but no unhealthy-reading reason
    # may be added for the grace beat.
    fx.window_end = _dt("2026-07-01T00:05:00")
    _write_jsonl(
        fx.ops_root / "supervisor.jsonl",
        [
            _sup_start("2026-07-01T00:00:00Z"),
            _sup_restart("2026-07-01T00:02:30Z", 1, 1),
        ],
    )
    rows = [
        _hb_row("2026-07-01T00:00:00Z", ok=True),
        _hb_row("2026-07-01T00:01:00Z", ok=True),
        _hb_row("2026-07-01T00:02:00Z", ok=True),
        _hb_row("2026-07-01T00:03:00Z", ok=False, http=None),  # 30s after restart, pre-first-true
        _hb_row("2026-07-01T00:04:00Z", ok=True),
        _hb_row("2026-07-01T00:05:00Z", ok=True),
    ]
    _write_jsonl(fx.ops_root / "heartbeat.jsonl", rows)
    report = fx.build()
    assert report["heartbeat"]["startup_grace_rows"] == 1
    assert report["heartbeat"]["unhealthy_rows"] == 0
    assert report["verdict"]["status"] == "INTERRUPTED/DEGRADED"  # the restart itself
    assert any("restart" in r for r in report["verdict"]["reasons"])
    assert not any("unhealthy" in r for r in report["verdict"]["reasons"])


def test_grace_beats_never_hide_a_real_edge_gap(fx):
    # Startup grace only reclassifies unhealthy READINGS; the coverage /
    # edge-gap math is untouched, so a stream that goes silent after a graced
    # cold start still fails the window.
    _write_jsonl(fx.ops_root / "supervisor.jsonl", [_sup_start("2026-07-01T00:00:00Z")])
    rows = [_hb_row("2026-07-01T00:00:30Z", ok=False, http=None)]  # graced cold start
    rows += [_hb_row(f"2026-07-01T00:{m:02d}:30Z") for m in range(1, 11)]  # 10 min, then silence
    _write_jsonl(fx.ops_root / "heartbeat.jsonl", rows)
    report = fx.build()  # 72h window
    assert report["heartbeat"]["startup_grace_rows"] == 1
    assert report["verdict"]["status"] == "INTERRUPTED/DEGRADED"
    assert any("window end" in r for r in report["verdict"]["reasons"])
    assert not any("unhealthy" in r for r in report["verdict"]["reasons"])


def test_startup_grace_horizon_is_configurable(fx):
    # With the grace horizon pinned to 30s, a cold-start beat at +60s is NOT
    # grace and must degrade.
    fx.window_end = _dt("2026-07-01T00:03:00")
    _write_jsonl(fx.ops_root / "supervisor.jsonl", [_sup_start("2026-07-01T00:00:00Z")])
    rows = [
        _hb_row("2026-07-01T00:01:00Z", ok=False, http=None),  # +60s > 30s grace
        _hb_row("2026-07-01T00:02:00Z", ok=True),
        _hb_row("2026-07-01T00:03:00Z", ok=True),
    ]
    _write_jsonl(fx.ops_root / "heartbeat.jsonl", rows)
    report = fx.build(startup_grace_s=30.0)
    assert report["heartbeat"]["startup_grace_rows"] == 0
    assert report["heartbeat"]["unhealthy_rows"] == 1
    assert report["verdict"]["status"] == "INTERRUPTED/DEGRADED"


def _sup_stop(ts: str) -> dict:
    return {"ts": ts, "event": "stop"}


def test_probe_p3_stop_start_cycle_inside_explicit_window_degrades(fx):
    # Adversarial probe P3: an explicit --window spans a stop + second start.
    # The downtime slips between heartbeats (delta < 2x interval, the loop is
    # dead during it so no beats are recorded), the cold-boot ok:false beats
    # after the second start are startup-graced, and a stop/start cycle
    # writes NO restart event -- so pre-fix this read UNINTERRUPTED with
    # empty reasons. Any in-window stop, or any start strictly inside the
    # window, must degrade: the run was not continuous.
    fx.window_start = _dt("2026-07-01T00:00:00")
    fx.window_end = _dt("2026-07-01T04:00:00")
    _write_jsonl(
        fx.ops_root / "supervisor.jsonl",
        [
            _sup_start("2026-07-01T00:00:00Z"),
            _sup_stop("2026-07-01T01:59:30Z"),
            _sup_start("2026-07-01T02:00:00Z"),
        ],
    )
    rows = [_hb_row(f"2026-07-01T00:{m:02d}:00Z") for m in range(60)]  # 00:00..00:59
    rows += [_hb_row(f"2026-07-01T01:{m:02d}:00Z") for m in range(60)]  # 01:00..01:59
    # Loop dead during the stop; second start's cold-boot beats, graced:
    rows += [
        _hb_row("2026-07-01T02:00:30Z", ok=False, http=None),
        _hb_row("2026-07-01T02:01:30Z", ok=False, http=None),
    ]
    rows += [
        _hb_row(f"2026-07-01T02:{m:02d}:30Z") for m in range(2, 60)
    ]  # 02:02:30..02:59:30
    rows += [_hb_row(f"2026-07-01T03:{m:02d}:30Z") for m in range(60)]  # ..03:59:30
    _write_jsonl(fx.ops_root / "heartbeat.jsonl", rows)

    report = fx.build()
    hb = report["heartbeat"]
    sup = report["supervisor"]
    # Pre-conditions proving this is exactly the probe's blind spot: the
    # boot beats were graced, no restart event exists, no delta gap tripped.
    assert hb["startup_grace_rows"] == 2
    assert hb["unhealthy_rows"] == 0
    assert sup["restart_count"] == 0
    assert not any("heartbeat gap" in r for r in report["verdict"]["reasons"])
    # ... and the new independent condition catches the cycle anyway:
    assert report["verdict"]["status"] == "INTERRUPTED/DEGRADED"
    assert any(
        "start/stop cycle inside the window" in r and "not continuous" in r
        for r in report["verdict"]["reasons"]
    )


def test_in_window_stop_event_alone_degrades(fx):
    # A stop with no second start (operator stopped the run mid-window) is
    # just as fatal to the continuity claim.
    _clean_fixture(fx)
    with (fx.ops_root / "supervisor.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(_sup_stop("2026-07-01T03:00:00Z")) + "\n")
    report = fx.build(committee_schedule="0 */2 * * *")
    assert report["verdict"]["status"] == "INTERRUPTED/DEGRADED"
    assert any("start/stop cycle" in r for r in report["verdict"]["reasons"])


def test_window_opening_start_event_does_not_trip_the_cycle_rule(fx):
    # The DEFAULT window opens AT the last supervisor start event, so a start
    # exactly at window_start must never count as an interior start -- the
    # clean-run verdict stays earnable. An earlier start/stop cycle BEFORE
    # the window is equally irrelevant.
    _clean_fixture(fx)  # writes the opening start exactly at window_start
    # Prepend an out-of-window earlier cycle.
    existing = (fx.ops_root / "supervisor.jsonl").read_text(encoding="utf-8")
    earlier = (
        json.dumps(_sup_start("2026-06-30T00:00:00Z"))
        + "\n"
        + json.dumps(_sup_stop("2026-06-30T12:00:00Z"))
        + "\n"
    )
    (fx.ops_root / "supervisor.jsonl").write_text(earlier + existing, encoding="utf-8")
    report = fx.build(committee_schedule="0 */2 * * *")
    assert report["verdict"] == {"status": "UNINTERRUPTED", "reasons": []}


def test_no_supervisor_events_means_no_grace(fx):
    # Without a start/restart anchor there is nothing to grace against: an
    # ok:false first beat stays a real degradation (and the missing
    # supervisor evidence degrades independently anyway).
    fx.window_end = _dt("2026-07-01T00:02:00")
    rows = [
        _hb_row("2026-07-01T00:00:00Z", ok=False, http=None),
        _hb_row("2026-07-01T00:01:00Z", ok=True),
        _hb_row("2026-07-01T00:02:00Z", ok=True),
    ]
    _write_jsonl(fx.ops_root / "heartbeat.jsonl", rows)
    report = fx.build()
    assert report["heartbeat"]["startup_grace_rows"] == 0
    assert report["heartbeat"]["unhealthy_rows"] == 1
    assert any("unhealthy" in r for r in report["verdict"]["reasons"])


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
    # I1: paper/journal sections must state their informational-only status.
    assert markdown.count("informational -- not part of the uninterrupted verdict") == 2
    # Minor (a): firing-accounted != run-succeeded must be stated.
    assert "not that it succeeded" in markdown


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
        # Pin the scheduled-research job store (read by the persisted-first
        # schedule resolution) away from the real ~/.vibe-trading runtime root.
        import src.scheduled_research.store as sched_store

        monkeypatch.setattr(
            sched_store, "_default_store_path", lambda: tmp_path / "sched" / "jobs.json"
        )

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

    def test_malformed_heartbeat_env_is_a_usage_error(self, monkeypatch, tmp_path, capsys):
        """VIBE_OPS_HEARTBEAT_S must fail with a clean usage error naming the
        offending value -- not a raw ValueError traceback -- and must not
        write a report file."""
        from cli._legacy import EXIT_USAGE_ERROR, cmd_ops_report

        self._set_env(monkeypatch, tmp_path)
        monkeypatch.setenv("VIBE_OPS_HEARTBEAT_S", "not-a-number")

        rc = cmd_ops_report(window="4h")
        out = capsys.readouterr().out
        assert rc == EXIT_USAGE_ERROR
        assert "VIBE_OPS_HEARTBEAT_S" in out
        assert "not-a-number" in out
        assert not list((tmp_path / "ops").glob("report-*.md"))

    def test_malformed_startup_grace_env_is_a_usage_error(self, monkeypatch, tmp_path, capsys):
        """Same clean-usage-error contract for VIBE_OPS_STARTUP_GRACE_S."""
        from cli._legacy import EXIT_USAGE_ERROR, cmd_ops_report

        self._set_env(monkeypatch, tmp_path)
        monkeypatch.setenv("VIBE_OPS_STARTUP_GRACE_S", "abc123")

        rc = cmd_ops_report(window="4h")
        out = capsys.readouterr().out
        assert rc == EXIT_USAGE_ERROR
        assert "VIBE_OPS_STARTUP_GRACE_S" in out
        assert "abc123" in out
        assert not list((tmp_path / "ops").glob("report-*.md"))

    def test_persisted_job_schedule_preferred_over_env(self, monkeypatch, tmp_path, capsys):
        # VIBE_COMMITTEE_SCHEDULE only seeds the job at first registration; a
        # hand-edited persisted job is what the executor actually runs, so the
        # report's expected-firing math must read the persisted schedule first.
        from cli._legacy import EXIT_SUCCESS, cmd_ops_report
        from src.scheduled_research.models import ScheduledResearchJob
        from src.scheduled_research.store import ScheduledResearchJobStore

        self._set_env(monkeypatch, tmp_path)
        monkeypatch.setenv("VIBE_COMMITTEE_SCHEDULE", "0 */6 * * *")  # stale env value
        store = ScheduledResearchJobStore()  # resolves to the pinned tmp path
        store.upsert(
            ScheduledResearchJob(id="committee-run", prompt="run it", schedule="0 */2 * * *")
        )

        rc = cmd_ops_report(window="4h", json_mode=True)
        out = capsys.readouterr().out
        assert rc == EXIT_SUCCESS
        payload = json.loads(out)
        assert payload["scheduled_firings"]["schedule"] == "0 */2 * * *"

    def test_env_schedule_used_when_no_persisted_job(self, monkeypatch, tmp_path, capsys):
        from cli._legacy import EXIT_SUCCESS, cmd_ops_report

        self._set_env(monkeypatch, tmp_path)
        monkeypatch.setenv("VIBE_COMMITTEE_SCHEDULE", "0 */6 * * *")

        rc = cmd_ops_report(window="4h", json_mode=True)
        out = capsys.readouterr().out
        assert rc == EXIT_SUCCESS
        payload = json.loads(out)
        assert payload["scheduled_firings"]["schedule"] == "0 */6 * * *"

    def test_startup_grace_env_is_honored(self, monkeypatch, tmp_path, capsys):
        from cli._legacy import EXIT_SUCCESS, cmd_ops_report

        self._set_env(monkeypatch, tmp_path)
        monkeypatch.setenv("VIBE_OPS_STARTUP_GRACE_S", "45")
        rc = cmd_ops_report(window="4h", json_mode=True)
        out = capsys.readouterr().out
        assert rc == EXIT_SUCCESS
        payload = json.loads(out)
        assert payload["heartbeat"]["startup_grace_s"] == 45.0

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
