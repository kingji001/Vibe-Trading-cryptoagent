"""Cross-referenced 72h operation evidence report.

Makes "the system ran uninterrupted for N hours" a claim provable from
artifacts, per ``docs/superpowers/specs/2026-07-11-ops-evidence-harness-design.md``
section 2.2. :func:`build_evidence_report` is pure and fixture-testable: every
source (ops root, swarm run store, paper store, committee journal) is passed
in explicitly, so tests never touch real environment variables or the real
home-dir stores. The only exception is the two module-level constants used as
*fallback* defaults (:data:`DEFAULT_HEARTBEAT_INTERVAL_S`), which callers may
always override with an explicit keyword argument.

Cross-referenced sources and the claims each supports:

- ``ops/heartbeat.jsonl``   -- uptime %, continuity gaps, first/last beat.
- ``ops/supervisor.jsonl``  -- restart count/times, start events, and whether
  any in-window ``start`` event ran against an overridden serve command
  (``VIBE_OPS_SERVE_CMD`` test seam -- see ``scripts/ops/run72.sh``). A
  stub-server run can never count as valid evidence, so any such event both
  appears in the report AND forces the verdict to degrade.
- swarm run store (``.swarm/runs/*/run.json``) -- committee-run cron
  expected-vs-actual firings (expected math reuses
  ``src.scheduled_research.executor.next_due`` -- no parallel cron impl) plus
  each matched run's status/wall-clock/token usage.
- paper store (``tick_state.json``/``equity.jsonl``/``ledger.jsonl``, read via
  :class:`src.paper.store.PaperStore` -- never re-parsed by hand) -- fills,
  conditional-order counts, daily mark-to-market snapshot coverage, and
  tick-state event-trigger watermarks.
- committee journal (``journal.jsonl``, read via
  :func:`src.committee.journal.load_entries`) -- decisions appended, horizons
  resolved, reflections written.

Median-interval rule (used for ALL gap math, heartbeat AND the verdict):
the interval is the median of consecutive-row deltas (in seconds) among the
IN-WINDOW heartbeat rows, sorted by timestamp. This is the "observed cadence"
rather than the configured one, because an operator running with a custom
``VIBE_OPS_HEARTBEAT_S`` (or a cadence that drifted under load) should still
get gap math scaled to what actually happened. When fewer than two in-window
rows exist (0 or 1 -- not enough to derive a delta), the interval falls back
to the ``heartbeat_interval_s`` argument (itself the ``VIBE_OPS_HEARTBEAT_S``
env value, resolved by the CLI layer, defaulting to
:data:`DEFAULT_HEARTBEAT_INTERVAL_S` = 60s).

Never invent: an absent or unparseable source makes its section report
"no data: <reason>" and degrades the verdict to the strongest supportable
claim (see :func:`compute_verdict`); malformed JSONL lines are counted and
surfaced, never silently skipped. Because a huge restart-on-crash storm (the
5s restart loop has no cap -- ~52k rows is the documented worst case over
72h) must still render, event lists (restarts, heartbeat gaps, ledger fills)
are capped for *display* at :data:`MAX_DISPLAYED_EVENTS` (head+tail sample)
while true counts/totals are always exact and never capped.
"""

from __future__ import annotations

import json
import re
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.committee.journal import load_entries
from src.paper.store import PaperStore
from src.scheduled_research.executor import next_due

DEFAULT_HEARTBEAT_INTERVAL_S = 60.0
COMMITTEE_PRESET_NAME = "crypto_committee"
# Head+tail sample size for potentially-huge event logs (e.g. a restart
# storm): keep the report readable and fast to render without ever hiding the
# true total count.
MAX_DISPLAYED_EVENTS = 20
_OPS_HEALTH_KEYWORDS = ("429", "backoff", "rate limit")


# --------------------------------------------------------------------------- #
# Small shared helpers
# --------------------------------------------------------------------------- #
def _parse_ts(value: str) -> datetime:
    """Parse an ISO-8601 timestamp (either ``...Z`` or ``...+00:00``) to UTC."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    """Render a UTC datetime as ``...Z`` (matches heartbeat/supervisor style)."""
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _try_parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return _parse_ts(value)
    except (ValueError, TypeError):
        return None


def _in_window(ts_str: str | None, start: datetime, end: datetime) -> bool:
    ts = _try_parse_ts(ts_str)
    if ts is None:
        return False
    return start <= ts <= end


def _read_jsonl(path: Path) -> tuple[list[dict], int]:
    """Parse a JSONL file, returning ``(rows, malformed_line_count)``.

    A malformed line (invalid JSON, or valid JSON that isn't an object) is
    counted, never silently skipped -- the rest of the file is still used,
    but the malformed count is surfaced so a partially-corrupted evidence
    stream is never mistaken for a clean one.
    """
    if not path.exists():
        return [], 0
    rows: list[dict] = []
    malformed = 0
    text = path.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            malformed += 1
            continue
        if not isinstance(obj, dict):
            malformed += 1
            continue
        rows.append(obj)
    return rows, malformed


def _cap_events(events: list[dict], limit: int = MAX_DISPLAYED_EVENTS) -> tuple[list[dict], int]:
    """Return ``(displayed, omitted_count)`` -- a head+tail sample for huge logs."""
    if len(events) <= limit:
        return events, 0
    half = max(1, limit // 2)
    displayed = events[:half] + events[-half:]
    return displayed, len(events) - len(displayed)


def _distinct_utc_days(start: datetime, end: datetime) -> list[str]:
    days = []
    cur = start.date()
    last = end.date()
    while cur <= last:
        days.append(cur.isoformat())
        cur += timedelta(days=1)
    return days


# --------------------------------------------------------------------------- #
# Heartbeat section
# --------------------------------------------------------------------------- #
def build_heartbeat_section(
    ops_root: Path,
    window_start: datetime,
    window_end: datetime,
    *,
    fallback_interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S,
) -> dict[str, Any]:
    """Uptime %, continuity gaps, and malformed-line count for ``heartbeat.jsonl``.

    Gap rule (spec sec 2.2/4): a gap is a consecutive-row delta STRICTLY
    greater than 2x the interval (exactly 2x is NOT a gap), OR a contiguous
    span of ``ok: false`` rows (this catches a service that answers /health
    on schedule but reports unhealthy -- a case the delta check alone would
    miss). An ``ok:false`` span's duration is measured from its first to its
    last row plus one interval, so even a single bad reading contributes
    roughly one interval of known-bad duration rather than reading as zero.
    """
    path = Path(ops_root) / "heartbeat.jsonl"
    if not path.exists():
        return {
            "available": False,
            "reason": f"no data: heartbeat.jsonl not found under {ops_root}",
            "total_rows": 0,
            "ok_rows": 0,
            "uptime_pct": None,
            "interval_s": fallback_interval_s,
            "interval_source": "fallback_default",
            "max_gap_s": None,
            "gaps": [],
            "gaps_omitted": 0,
            "malformed_lines": 0,
            "http_429_count": 0,
            "first_ts": None,
            "last_ts": None,
        }

    rows, malformed = _read_jsonl(path)
    in_window = [r for r in rows if _in_window(r.get("ts"), window_start, window_end)]
    in_window.sort(key=lambda r: _parse_ts(r["ts"]))

    total = len(in_window)
    ok_rows = sum(1 for r in in_window if r.get("ok") is True)
    uptime_pct = (ok_rows / total * 100.0) if total else None
    http_429_count = sum(1 for r in in_window if r.get("http") == 429)

    deltas = [
        (_parse_ts(b["ts"]) - _parse_ts(a["ts"])).total_seconds()
        for a, b in zip(in_window, in_window[1:])
    ]
    if deltas:
        interval_s = float(statistics.median(deltas))
        interval_source = "observed_median"
    else:
        interval_s = float(fallback_interval_s)
        interval_source = "fallback_default"

    threshold = 2 * interval_s
    gaps: list[dict[str, Any]] = []
    for a, b in zip(in_window, in_window[1:]):
        ta, tb = _parse_ts(a["ts"]), _parse_ts(b["ts"])
        delta = (tb - ta).total_seconds()
        if delta > threshold:
            gaps.append(
                {"start": _iso(ta), "end": _iso(tb), "duration_s": delta, "reason": "missing heartbeat"}
            )

    idx, n = 0, len(in_window)
    while idx < n:
        if in_window[idx].get("ok") is False:
            j = idx
            while j + 1 < n and in_window[j + 1].get("ok") is False:
                j += 1
            start_ts = _parse_ts(in_window[idx]["ts"])
            end_ts = _parse_ts(in_window[j]["ts"])
            duration = (end_ts - start_ts).total_seconds() + interval_s
            gaps.append(
                {
                    "start": _iso(start_ts),
                    "end": _iso(end_ts),
                    "duration_s": duration,
                    "reason": "health check failing (ok:false)",
                }
            )
            idx = j + 1
        else:
            idx += 1

    gaps.sort(key=lambda g: g["start"])
    if total >= 2:
        max_gap_s = max((g["duration_s"] for g in gaps), default=0.0)
    else:
        max_gap_s = None

    displayed_gaps, gaps_omitted = _cap_events(gaps)

    reason = None
    if total == 0:
        reason = "no data: no heartbeat rows in window"
    elif total == 1:
        reason = "insufficient heartbeat rows in window to evaluate continuity (only 1 row)"

    return {
        "available": True,
        "reason": reason,
        "total_rows": total,
        "ok_rows": ok_rows,
        "uptime_pct": uptime_pct,
        "interval_s": interval_s,
        "interval_source": interval_source,
        "max_gap_s": max_gap_s,
        "gaps": displayed_gaps,
        "gaps_omitted": gaps_omitted,
        "malformed_lines": malformed,
        "http_429_count": http_429_count,
        "first_ts": _iso(_parse_ts(in_window[0]["ts"])) if in_window else None,
        "last_ts": _iso(_parse_ts(in_window[-1]["ts"])) if in_window else None,
    }


# --------------------------------------------------------------------------- #
# Supervisor section
# --------------------------------------------------------------------------- #
def build_supervisor_section(
    ops_root: Path,
    window_start: datetime,
    window_end: datetime,
) -> dict[str, Any]:
    """Restart count/times, start events, and the overridden-serve-cmd flag.

    ``overridden_start_events`` lists every in-window ``start`` event carrying
    ``serve_cmd_overridden: true`` (set by ``scripts/ops/run72.sh`` only when
    ``VIBE_OPS_SERVE_CMD`` -- the test seam -- was active). Their presence
    always degrades the verdict: a run against a stub server can never read
    as valid 72h evidence.
    """
    path = Path(ops_root) / "supervisor.jsonl"
    if not path.exists():
        return {
            "available": False,
            "reason": f"no data: supervisor.jsonl not found under {ops_root}",
            "restart_count": None,
            "restarts": [],
            "restarts_omitted": 0,
            "start_events": [],
            "overridden_start_events": [],
            "malformed_lines": 0,
        }

    rows, malformed = _read_jsonl(path)
    in_window = [r for r in rows if _in_window(r.get("ts"), window_start, window_end)]
    in_window.sort(key=lambda r: _parse_ts(r["ts"]))

    restarts = [r for r in in_window if r.get("event") == "restart"]
    starts = [r for r in in_window if r.get("event") == "start"]
    overridden_starts = [r for r in starts if r.get("serve_cmd_overridden") is True]

    displayed_restarts, restarts_omitted = _cap_events(restarts)

    return {
        "available": True,
        "reason": None,
        "restart_count": len(restarts),
        "restarts": displayed_restarts,
        "restarts_omitted": restarts_omitted,
        "start_events": starts,
        "overridden_start_events": overridden_starts,
        "malformed_lines": malformed,
    }


# --------------------------------------------------------------------------- #
# Scheduled committee-run firings
# --------------------------------------------------------------------------- #
def _expected_cron_firings(schedule: str, window_start: datetime, window_end: datetime) -> list[datetime]:
    """Every cron-due epoch in ``[window_start, window_end]``, via ``next_due``.

    Reuses ``src.scheduled_research.executor.next_due`` verbatim -- no
    parallel cron implementation (playbook rule).
    """
    start_ms = int(window_start.timestamp() * 1000) - 1
    end_ms = int(window_end.timestamp() * 1000)
    out: list[datetime] = []
    cursor = start_ms
    while True:
        try:
            nxt = next_due(schedule, cursor)
        except ValueError:
            break
        if nxt > end_ms:
            break
        out.append(datetime.fromtimestamp(nxt / 1000.0, timezone.utc))
        cursor = nxt
    return out


def _load_swarm_runs(swarm_runs_root: Path) -> tuple[list[dict], int]:
    root = Path(swarm_runs_root)
    if not root.exists():
        return [], 0
    runs: list[dict] = []
    malformed = 0
    for entry in sorted(root.iterdir()):
        run_file = entry / "run.json"
        if not run_file.is_file():
            continue
        try:
            data = json.loads(run_file.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            malformed += 1
            continue
        if not isinstance(data, dict):
            malformed += 1
            continue
        runs.append(data)
    return runs, malformed


def build_scheduled_firings_section(
    swarm_runs_root: Path,
    window_start: datetime,
    window_end: datetime,
    committee_schedule: str | None,
) -> dict[str, Any]:
    """Expected-vs-actual committee-run cron firings.

    An expected firing at ``E`` is "accounted for" when a
    ``preset_name == "crypto_committee"`` swarm run's ``created_at`` falls in
    ``[E, next_due(E))`` -- the following expected slot is the natural,
    schedule-derived exclusive upper bound, so no arbitrary tolerance
    constant is needed as long as the cron period exceeds the scheduler's own
    poll tick (true for any interval coarser than a minute).

    When ``committee_schedule`` is unset the committee-run job simply isn't
    configured -- a valid operator choice, not a missing-evidence gap -- so
    ``configured`` is False and this never contributes to verdict
    degradation (0 expected, 0 missing is vacuously satisfied).
    """
    if not committee_schedule:
        return {
            "configured": False,
            "available": True,
            "reason": None,
            "schedule": None,
            "expected": [],
            "actual_runs": [],
            "missing": [],
            "malformed_runs": 0,
        }

    root = Path(swarm_runs_root)
    expected = _expected_cron_firings(committee_schedule, window_start, window_end)
    if not root.exists():
        return {
            "configured": True,
            "available": False,
            "reason": f"no data: swarm runs root not found under {swarm_runs_root}",
            "schedule": committee_schedule,
            "expected": [_iso(e) for e in expected],
            "actual_runs": [],
            "missing": [_iso(e) for e in expected],
            "malformed_runs": 0,
        }

    runs, malformed = _load_swarm_runs(root)
    committee_runs = []
    for r in runs:
        if r.get("preset_name") != COMMITTEE_PRESET_NAME:
            continue
        created = _try_parse_ts(r.get("created_at"))
        if created is None:
            continue
        committee_runs.append((created, r))
    committee_runs.sort(key=lambda pair: pair[0])

    missing: list[str] = []
    for e in expected:
        try:
            upper_ms = next_due(committee_schedule, int(e.timestamp() * 1000))
            upper = datetime.fromtimestamp(upper_ms / 1000.0, timezone.utc)
        except ValueError:
            upper = e + timedelta(days=3650)  # effectively unbounded
        match = next((created for created, _r in committee_runs if e <= created < upper), None)
        if match is None:
            missing.append(_iso(e))

    actual_summaries = []
    for created, r in committee_runs:
        if not (window_start <= created <= window_end):
            continue
        completed_raw = r.get("completed_at")
        completed = _try_parse_ts(completed_raw)
        wall_clock_s = (completed - created).total_seconds() if completed else None
        actual_summaries.append(
            {
                "run_id": r.get("id"),
                "status": r.get("status"),
                "created_at": _iso(created),
                "completed_at": completed_raw,
                "wall_clock_s": wall_clock_s,
                "input_tokens": r.get("total_input_tokens"),
                "output_tokens": r.get("total_output_tokens"),
            }
        )

    return {
        "configured": True,
        "available": True,
        "reason": None,
        "schedule": committee_schedule,
        "expected": [_iso(e) for e in expected],
        "actual_runs": actual_summaries,
        "missing": missing,
        "malformed_runs": malformed,
    }


# --------------------------------------------------------------------------- #
# Paper-trading activity
# --------------------------------------------------------------------------- #
def build_paper_section(
    paper_root: Path,
    window_start: datetime,
    window_end: datetime,
) -> dict[str, Any]:
    """Ledger fills, daily equity-snapshot coverage, and tick-state watermarks.

    Reads exclusively through :class:`src.paper.store.PaperStore` (per the
    reuse mandate) -- never re-parses ``ledger.jsonl``/``equity.jsonl`` by
    hand. Note: ``PaperStore.__init__`` creates its root directory
    (``mkdir(parents=True, exist_ok=True)``), so a genuinely-missing root is
    checked BEFORE constructing the store -- an evidence report must never
    have the side effect of creating the very directory it found missing.

    Expected daily snapshots: one per distinct UTC calendar day spanned by
    the window (``equity.jsonl`` appends at most once/day, idempotently).
    Retried decisions are not tracked in the ledger schema, so that claim
    always reports "no data" -- never invented.
    """
    root = Path(paper_root)
    if not root.exists():
        return {
            "available": False,
            "reason": f"no data: paper root not found under {paper_root}",
            "ledger_fills": [],
            "ledger_fills_omitted": 0,
            "ledger_fill_count": 0,
            "conditional_fill_count": 0,
            "equity_snapshots_in_window": [],
            "expected_snapshot_days": [],
            "missing_snapshot_days": [],
            "tick_state_watermarks": {"last_bar_ts": {}, "last_event_trigger_ts": {}},
        }

    store = PaperStore(root)
    tick_state = store.load_tick_state()

    ledger_rows = [r for r in store.iter_ledger() if _in_window(r.get("ts"), window_start, window_end)]
    equity_rows = [r for r in store.iter_equity() if _in_window(r.get("ts"), window_start, window_end)]

    expected_days = _distinct_utc_days(window_start, window_end)
    actual_days = sorted({r.get("date") or (r.get("ts") or "")[:10] for r in equity_rows})
    missing_days = [d for d in expected_days if d not in actual_days]

    displayed_fills, fills_omitted = _cap_events(ledger_rows)
    conditional_count = sum(1 for r in ledger_rows if r.get("order_type") in ("stop", "take_profit"))

    event_triggers = {
        sym: ts for sym, ts in (tick_state.get("last_event_trigger_ts") or {}).items() if ts
    }

    return {
        "available": True,
        "reason": None,
        "ledger_fills": displayed_fills,
        "ledger_fills_omitted": fills_omitted,
        "ledger_fill_count": len(ledger_rows),
        "conditional_fill_count": conditional_count,
        "equity_snapshots_in_window": [
            {"ts": r.get("ts"), "date": r.get("date")} for r in equity_rows
        ],
        "expected_snapshot_days": expected_days,
        "missing_snapshot_days": missing_days,
        "tick_state_watermarks": {
            "last_bar_ts": dict(tick_state.get("last_bar_ts") or {}),
            "last_event_trigger_ts": event_triggers,
        },
    }


# --------------------------------------------------------------------------- #
# Committee journal activity
# --------------------------------------------------------------------------- #
def build_journal_section(
    journal_path: Path,
    window_start: datetime,
    window_end: datetime,
) -> dict[str, Any]:
    """Decisions appended, horizons resolved, and reflections written in-window.

    Reads exclusively through :func:`src.committee.journal.load_entries` (per
    the reuse mandate). That reader has no per-line malformed-count API, so a
    totally-unparseable journal surfaces as a whole-file "no data" reason
    rather than a line-level malformed count (unlike heartbeat/supervisor,
    which this module parses directly).
    """
    path = Path(journal_path)
    empty = {
        "decisions_appended": [],
        "horizons_resolved": [],
        "reflections_written": [],
    }
    if not path.exists():
        return {"available": False, "reason": f"no data: journal not found at {journal_path}", **empty}

    try:
        entries = load_entries(path)
    except (ValueError, OSError) as exc:
        return {"available": False, "reason": f"no data: journal unparseable ({exc})", **empty}

    decisions = [e for e in entries if _in_window(e.get("decided_at"), window_start, window_end)]
    horizons_resolved = []
    for e in entries:
        for key, info in (e.get("horizons") or {}).items():
            if _in_window(info.get("resolved_at"), window_start, window_end):
                horizons_resolved.append(
                    {"id": e.get("id"), "symbol": e.get("symbol"), "horizon": key, "resolved_at": info.get("resolved_at")}
                )
    reflections = [e for e in entries if _in_window(e.get("reflected_at"), window_start, window_end)]

    return {
        "available": True,
        "reason": None,
        "decisions_appended": [
            {"id": e.get("id"), "symbol": e.get("symbol"), "rating": e.get("rating"), "decided_at": e.get("decided_at")}
            for e in decisions
        ],
        "horizons_resolved": horizons_resolved,
        "reflections_written": [
            {"id": e.get("id"), "symbol": e.get("symbol"), "reflected_at": e.get("reflected_at")} for e in reflections
        ],
    }


# --------------------------------------------------------------------------- #
# Ops health (429/backoff mentions)
# --------------------------------------------------------------------------- #
def build_ops_health_section(
    swarm_runs_root: Path,
    window_start: datetime,
    window_end: datetime,
) -> dict[str, Any]:
    """429/backoff mentions found in in-window swarm run artifacts, where available."""
    root = Path(swarm_runs_root)
    if not root.exists():
        return {
            "available": False,
            "reason": f"no data: swarm runs root not found under {swarm_runs_root}",
            "run_mentions": [],
        }

    runs, _malformed = _load_swarm_runs(root)
    mentions = []
    for r in runs:
        if not _in_window(r.get("created_at"), window_start, window_end):
            continue
        haystack = json.dumps(r, default=str).lower()
        hits = [kw for kw in _OPS_HEALTH_KEYWORDS if kw in haystack]
        if hits:
            mentions.append({"run_id": r.get("id"), "matched": hits})

    return {"available": True, "reason": None, "run_mentions": mentions}


# --------------------------------------------------------------------------- #
# Verdict
# --------------------------------------------------------------------------- #
def compute_verdict(
    heartbeat: dict[str, Any],
    supervisor: dict[str, Any],
    scheduled_firings: dict[str, Any],
) -> dict[str, Any]:
    """UNINTERRUPTED iff 0 restarts, max gap < 2x interval, every expected
    committee-run firing accounted for, AND no overridden-serve-cmd start
    events in-window. Any missing/unparseable source, or any malformed line,
    also degrades the verdict (never invent continuity that can't be
    verified) -- each condition below flips the verdict independently.
    """
    reasons: list[str] = []

    if not supervisor.get("available"):
        reasons.append(f"supervisor evidence unavailable: {supervisor.get('reason')}")
    else:
        restart_count = supervisor.get("restart_count") or 0
        if restart_count:
            reasons.append(f"{restart_count} restart(s) recorded in window")
        if supervisor.get("malformed_lines"):
            reasons.append(
                f"{supervisor['malformed_lines']} malformed supervisor.jsonl line(s) -- "
                "continuity not fully verifiable"
            )
        overridden = supervisor.get("overridden_start_events") or []
        if overridden:
            ts_list = ", ".join(str(e.get("ts", "?")) for e in overridden)
            reasons.append(
                "supervisor ran with VIBE_OPS_SERVE_CMD override (test seam) at "
                f"{ts_list} -- a stub-server run cannot count as valid evidence"
            )

    if not heartbeat.get("available"):
        reasons.append(f"heartbeat evidence unavailable: {heartbeat.get('reason')}")
    else:
        if heartbeat.get("reason"):
            reasons.append(f"heartbeat: {heartbeat['reason']}")
        max_gap_s = heartbeat.get("max_gap_s")
        interval_s = heartbeat.get("interval_s") or DEFAULT_HEARTBEAT_INTERVAL_S
        if max_gap_s is None:
            reasons.append("heartbeat continuity could not be evaluated (insufficient in-window rows)")
        elif max_gap_s > 2 * interval_s:
            reasons.append(
                f"max heartbeat gap {max_gap_s:.0f}s exceeds 2x interval ({2 * interval_s:.0f}s)"
            )
        if heartbeat.get("malformed_lines"):
            reasons.append(
                f"{heartbeat['malformed_lines']} malformed heartbeat.jsonl line(s) -- "
                "continuity not fully verifiable"
            )

    if scheduled_firings.get("configured"):
        if not scheduled_firings.get("available"):
            reasons.append(f"scheduled-firing evidence unavailable: {scheduled_firings.get('reason')}")
        else:
            missing = scheduled_firings.get("missing") or []
            if missing:
                reasons.append(
                    f"{len(missing)} expected committee-run firing(s) missing: {', '.join(missing)}"
                )
            if scheduled_firings.get("malformed_runs"):
                reasons.append(
                    f"{scheduled_firings['malformed_runs']} unparseable swarm run.json file(s)"
                )

    status = "UNINTERRUPTED" if not reasons else "INTERRUPTED/DEGRADED"
    return {"status": status, "reasons": reasons}


# --------------------------------------------------------------------------- #
# Top-level report
# --------------------------------------------------------------------------- #
def build_evidence_report(
    window_start: datetime,
    window_end: datetime,
    *,
    ops_root: Path,
    swarm_runs_root: Path,
    paper_root: Path,
    journal_path: Path,
    heartbeat_interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S,
    committee_schedule: str | None = None,
    window_start_source: str = "explicit",
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Build the full cross-referenced evidence report as a plain dict.

    Pure: every source is an explicit argument (a root path or a schedule
    string) -- no environment reads, no implicit clock use besides the
    optional ``generated_at`` override. See the module docstring for the
    median-interval gap-math rule and the honesty/degradation rules.
    """
    heartbeat = build_heartbeat_section(
        ops_root, window_start, window_end, fallback_interval_s=heartbeat_interval_s
    )
    supervisor = build_supervisor_section(ops_root, window_start, window_end)
    scheduled_firings = build_scheduled_firings_section(
        swarm_runs_root, window_start, window_end, committee_schedule
    )
    paper = build_paper_section(paper_root, window_start, window_end)
    journal = build_journal_section(journal_path, window_start, window_end)
    ops_health = build_ops_health_section(swarm_runs_root, window_start, window_end)
    verdict = compute_verdict(heartbeat, supervisor, scheduled_firings)

    return {
        "window": {
            "start": _iso(window_start),
            "end": _iso(window_end),
            "start_source": window_start_source,
        },
        "generated_at": _iso(generated_at or datetime.now(timezone.utc)),
        "verdict": verdict,
        "heartbeat": heartbeat,
        "supervisor": supervisor,
        "scheduled_firings": scheduled_firings,
        "paper": paper,
        "journal": journal,
        "ops_health": ops_health,
    }


# --------------------------------------------------------------------------- #
# Markdown rendering
# --------------------------------------------------------------------------- #
def render_markdown(report: dict[str, Any]) -> str:
    """Render :func:`build_evidence_report`'s dict as a Markdown document."""
    lines: list[str] = []
    w = report["window"]
    v = report["verdict"]

    lines.append("# 72h Operation Evidence Report")
    lines.append("")
    lines.append(f"Generated: {report['generated_at']}")
    lines.append(f"Window: {w['start']} .. {w['end']} (start source: {w['start_source']})")
    lines.append("")
    lines.append(f"## Verdict: {v['status']}")
    if v["reasons"]:
        lines.append("")
        for reason in v["reasons"]:
            lines.append(f"- {reason}")
    lines.append("")

    hb = report["heartbeat"]
    lines.append("## Heartbeat continuity")
    if not hb["available"]:
        lines.append(f"no data: {hb['reason']}")
    else:
        if hb["reason"]:
            lines.append(f"_{hb['reason']}_")
        uptime = f"{hb['uptime_pct']:.2f}%" if hb["uptime_pct"] is not None else "n/a"
        lines.append(f"- Uptime: {uptime} ({hb['ok_rows']}/{hb['total_rows']} rows)")
        lines.append(
            f"- Interval used for gap math: {hb['interval_s']:.1f}s ({hb['interval_source']})"
        )
        max_gap = f"{hb['max_gap_s']:.1f}s" if hb["max_gap_s"] is not None else "n/a"
        lines.append(f"- Max gap: {max_gap}")
        lines.append(f"- First/last beat: {hb['first_ts']} / {hb['last_ts']}")
        lines.append(f"- Malformed lines: {hb['malformed_lines']}")
        lines.append(f"- HTTP 429 responses: {hb['http_429_count']}")
        if hb["gaps"]:
            lines.append("")
            lines.append("| Start | End | Duration (s) | Reason |")
            lines.append("|---|---|---|---|")
            for g in hb["gaps"]:
                lines.append(f"| {g['start']} | {g['end']} | {g['duration_s']:.1f} | {g['reason']} |")
            if hb["gaps_omitted"]:
                lines.append(f"_(+{hb['gaps_omitted']} more gap(s) omitted)_")
    lines.append("")

    sup = report["supervisor"]
    lines.append("## Supervisor events")
    if not sup["available"]:
        lines.append(f"no data: {sup['reason']}")
    else:
        lines.append(f"- Restart count: {sup['restart_count']}")
        lines.append(f"- Start events: {len(sup['start_events'])}")
        lines.append(f"- Malformed lines: {sup['malformed_lines']}")
        if sup["overridden_start_events"]:
            lines.append("")
            lines.append(
                "**TEST SEAM WARNING** -- the following `start` events ran against an "
                "overridden `VIBE_OPS_SERVE_CMD` (stub server) and cannot count as valid evidence:"
            )
            for e in sup["overridden_start_events"]:
                lines.append(f"- {e.get('ts')}")
        if sup["restarts"]:
            lines.append("")
            lines.append("| ts | exit_code | restart_count |")
            lines.append("|---|---|---|")
            for r in sup["restarts"]:
                lines.append(f"| {r.get('ts')} | {r.get('exit_code')} | {r.get('restart_count')} |")
            if sup["restarts_omitted"]:
                lines.append(
                    f"_(+{sup['restarts_omitted']} more restart(s) omitted -- "
                    f"{sup['restart_count']} total)_"
                )
    lines.append("")

    sched = report["scheduled_firings"]
    lines.append("## Scheduled committee-run firings")
    if not sched.get("configured"):
        lines.append(
            "no data: committee schedule not configured (VIBE_COMMITTEE_SCHEDULE unset) -- "
            "no expected firings to check"
        )
    elif not sched["available"]:
        lines.append(f"no data: {sched['reason']}")
    else:
        lines.append(f"- Schedule: `{sched['schedule']}`")
        lines.append(f"- Expected firings: {len(sched['expected'])}")
        lines.append(f"- Actual runs in window: {len(sched['actual_runs'])}")
        lines.append(f"- Missing firings: {len(sched['missing'])}")
        if sched["malformed_runs"]:
            lines.append(f"- Unparseable run.json files: {sched['malformed_runs']}")
        if sched["expected"]:
            lines.append("")
            lines.append("| Expected | Status |")
            lines.append("|---|---|")
            missing_set = set(sched["missing"])
            for e in sched["expected"]:
                status = "MISSING" if e in missing_set else "ok"
                lines.append(f"| {e} | {status} |")
        if sched["actual_runs"]:
            lines.append("")
            lines.append("| run_id | status | created_at | wall_clock_s | input_tokens | output_tokens |")
            lines.append("|---|---|---|---|---|---|")
            for r in sched["actual_runs"]:
                lines.append(
                    f"| {r['run_id']} | {r['status']} | {r['created_at']} | "
                    f"{r['wall_clock_s']} | {r['input_tokens']} | {r['output_tokens']} |"
                )
    lines.append("")

    paper = report["paper"]
    lines.append("## Paper-trading activity")
    if not paper["available"]:
        lines.append(f"no data: {paper['reason']}")
    else:
        lines.append(
            f"- Ledger fills in window: {paper['ledger_fill_count']} "
            f"(conditional: {paper['conditional_fill_count']})"
        )
        if paper["ledger_fills_omitted"]:
            lines.append(f"  ({paper['ledger_fills_omitted']} fill row(s) omitted from listing)")
        lines.append(f"- Equity snapshots in window: {len(paper['equity_snapshots_in_window'])}")
        if paper["missing_snapshot_days"]:
            lines.append(f"- Missing daily snapshots: {', '.join(paper['missing_snapshot_days'])}")
        triggers = paper["tick_state_watermarks"]["last_event_trigger_ts"]
        if triggers:
            lines.append("- Event triggers recorded (tick_state watermark, not window-scoped):")
            for sym, ts in triggers.items():
                lines.append(f"  - {sym}: {ts}")
        lines.append("- Retried decisions: no data (not tracked in ledger schema)")
    lines.append("")

    journal = report["journal"]
    lines.append("## Committee journal activity")
    if not journal["available"]:
        lines.append(f"no data: {journal['reason']}")
    else:
        lines.append(f"- Decisions appended in window: {len(journal['decisions_appended'])}")
        lines.append(f"- Horizons resolved in window: {len(journal['horizons_resolved'])}")
        lines.append(f"- Reflections written in window: {len(journal['reflections_written'])}")
    lines.append("")

    health = report["ops_health"]
    lines.append("## Ops health (429/backoff mentions)")
    if not health["available"]:
        lines.append(f"no data: {health['reason']}")
    elif health["run_mentions"]:
        for m in health["run_mentions"]:
            lines.append(f"- run {m['run_id']}: matched {', '.join(m['matched'])}")
    else:
        lines.append("- none found in-window swarm run artifacts")

    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# CLI-facing helpers (window default resolution, filenames)
# --------------------------------------------------------------------------- #
_WINDOW_RE = re.compile(r"^\s*(\d+)\s*([hHdD])\s*$")


def parse_window_duration(value: str) -> timedelta:
    """Parse a ``--window`` flag like ``72h``/``48h``/``3d`` into a timedelta."""
    match = _WINDOW_RE.match(value)
    if not match:
        raise ValueError(f"invalid --window value {value!r}; expected e.g. '72h' or '3d'")
    n = int(match.group(1))
    unit = match.group(2).lower()
    return timedelta(hours=n) if unit == "h" else timedelta(days=n)


def default_window_start(ops_root: Path, now: datetime) -> tuple[datetime, str]:
    """Resolve the default window start: the last supervisor ``start`` event.

    Falls back to 72h before *now* when ``supervisor.jsonl`` is missing,
    empty, or has no ``start`` event -- a report can still be produced, just
    noted as a degraded default rather than a hard failure.
    """
    path = Path(ops_root) / "supervisor.jsonl"
    rows, _malformed = _read_jsonl(path)
    starts = [r for r in rows if r.get("event") == "start" and r.get("ts")]
    if starts:
        starts.sort(key=lambda r: _parse_ts(r["ts"]))
        return _parse_ts(starts[-1]["ts"]), "last supervisor start event"
    fallback = now - timedelta(hours=72)
    return fallback, "fallback: no supervisor start event found; defaulted to 72h"


def report_filename(ts: datetime) -> str:
    """Filename for the Markdown report: ``report-<UTC-ts>.md``."""
    stamp = ts.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"report-{stamp}.md"
