"""Tests for src/committee/journal.py (decision journal / learning loop).

All bar data comes from a deterministic fake fetcher — no network. The fake
market: the symbol gains +1% every 24h from a 100.0 open; the benchmark is
flat at 50.0. So raw return at 72h is ~+3%, benchmark return 0, alpha == raw.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from src.committee import journal

T0 = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)


def fake_bars(symbol: str, start: datetime, end: datetime):
    """1H bars from T0-12h to `end`. SYMBOL trends +1%/day; BENCH is flat."""
    bars = []
    t = T0 - timedelta(hours=12)
    while t <= end:
        hours = (t - T0).total_seconds() / 3600.0
        if symbol == journal.DEFAULT_BENCHMARK:
            price = 50.0
        else:
            price = 100.0 * (1.0 + 0.01 * hours / 24.0)
        bars.append({"ts": t, "open": price, "close": price})
        t += timedelta(hours=1)
    return bars


@pytest.fixture()
def jpath(tmp_path):
    return tmp_path / "journal.jsonl"


def _append(jpath, **overrides):
    kwargs = dict(
        symbol="ETH-USDT",
        rating="Buy",
        time_horizon="72h swing",
        price_target=110.0,
        run_id="run-1",
        decided_at=T0,
        path=jpath,
    )
    kwargs.update(overrides)
    return journal.append_decision(**kwargs)


def test_append_is_idempotent_per_run_and_symbol(jpath):
    e1 = _append(jpath)
    e2 = _append(jpath)  # same run_id + symbol
    assert e1["id"] == e2["id"]
    assert len(journal.load_entries(jpath)) == 1


def test_primary_horizon_parsing():
    assert journal.primary_horizon("72h swing") == "72h"
    assert journal.primary_horizon("intraday scalp") == "24h"
    assert journal.primary_horizon("2-4 week position") == "7d"
    assert journal.primary_horizon("") == "72h"


def test_resolve_due_partial_horizons(jpath):
    _append(jpath)
    now = T0 + timedelta(hours=80)  # 24h + 72h due, 7d not
    result = journal.resolve_due(fake_bars, now=now, path=jpath)

    entry = journal.load_entries(jpath)[0]
    assert set(entry["horizons"]) == {"24h", "72h"}
    assert entry["status"] == "pending"  # 7d outstanding
    assert not result["errors"]

    h72 = entry["horizons"]["72h"]
    assert h72["raw_return"] == pytest.approx(0.03, abs=2e-3)
    assert h72["benchmark_return"] == pytest.approx(0.0, abs=1e-9)
    assert h72["alpha"] == pytest.approx(h72["raw_return"], abs=1e-9)
    assert h72["direction_correct"] is True  # Buy + positive alpha

    # Buy call is in reflection_due once the primary horizon (72h) resolved.
    assert [e["id"] for e in result["reflection_due"]] == [entry["id"]]


def test_resolve_completes_and_entry_becomes_resolved(jpath):
    _append(jpath)
    journal.resolve_due(fake_bars, now=T0 + timedelta(hours=200), path=jpath)
    entry = journal.load_entries(jpath)[0]
    assert set(entry["horizons"]) == {"24h", "72h", "7d"}
    assert entry["status"] == "resolved"


def test_benchmark_asset_scored_on_raw_return(jpath):
    _append(jpath, symbol=journal.DEFAULT_BENCHMARK, rating="Sell", run_id="run-2")
    journal.resolve_due(fake_bars, now=T0 + timedelta(hours=80), path=jpath)
    entry = journal.load_entries(jpath)[0]
    h72 = entry["horizons"]["72h"]
    # benchmark is flat: raw 0, alpha 0 -> a Sell is NOT directionally right
    assert h72["alpha"] == pytest.approx(0.0, abs=1e-9)
    assert h72["direction_correct"] is False


def test_hold_judged_by_band(jpath):
    _append(jpath, symbol=journal.DEFAULT_BENCHMARK, rating="Hold", run_id="run-3")
    journal.resolve_due(fake_bars, now=T0 + timedelta(hours=80), path=jpath)
    entry = journal.load_entries(jpath)[0]
    assert entry["horizons"]["72h"]["direction_correct"] is True  # flat within band


def test_reflection_roundtrip_and_lessons_block(jpath):
    e = _append(jpath)
    journal.resolve_due(fake_bars, now=T0 + timedelta(hours=80), path=jpath)
    journal.write_reflection(e["id"], "Buy was right; +3% alpha. Lesson: trust flow.", path=jpath)

    entry = journal.load_entries(jpath)[0]
    assert entry["reflection"].startswith("Buy was right")
    assert entry["reflected_at"] is not None

    # Reflected entries drop out of reflection_due on the next resolve pass.
    again = journal.resolve_due(fake_bars, now=T0 + timedelta(hours=81), path=jpath)
    assert again["reflection_due"] == []

    same = journal.lessons_block("ETH-USDT", path=jpath)
    assert "[2026-07-01 | ETH-USDT | Buy | " in same
    assert "trust flow" in same
    assert "+3.0" in same or "+2.9" in same  # rendered raw/alpha percentages

    cross = journal.lessons_block("SOL-USDT", path=jpath)
    assert "Recent lessons from other assets" in cross and "ETH-USDT" in cross


def test_lessons_block_empty_journal(jpath):
    assert journal.lessons_block("BTC-USDT", path=jpath) == (
        "No prior committee decisions recorded."
    )


def test_fetch_failure_is_reported_not_raised(jpath):
    _append(jpath)

    def broken(symbol, start, end):
        raise RuntimeError("exchange down")

    result = journal.resolve_due(broken, now=T0 + timedelta(hours=80), path=jpath)
    assert result["resolved"] == []
    assert result["errors"] and "exchange down" in result["errors"][0]
    # entry untouched, retried next run
    assert journal.load_entries(jpath)[0]["horizons"] == {}


def test_journal_file_stays_valid_jsonl(jpath):
    _append(jpath)
    _append(jpath, symbol="SOL-USDT", run_id="run-4")
    journal.resolve_due(fake_bars, now=T0 + timedelta(hours=80), path=jpath)
    for line in jpath.read_text().splitlines():
        json.loads(line)  # every line independently parseable


def test_env_var_overrides_path(jpath, monkeypatch):
    monkeypatch.setenv(journal.JOURNAL_PATH_ENV, str(jpath))
    journal.append_decision(symbol="BTC-USDT", rating="Hold", time_horizon="72h")
    assert len(journal.load_entries()) == 1


# --------------------------------------------------------------------------- #
# Phase 6 — idempotency regression: the scheduled reflection job now calls
# resolve_due/reflect independently of a committee run's reflection officer,
# so the same (run_id, symbol) entry can be resolved by BOTH the daily
# scheduled trigger and a same-day in-run trigger. Both drive the exact same
# journal.resolve_due/write_reflection functions (no separate code path), so
# this pins that double-firing them against the same journal file never
# double-resolves a horizon, never re-surfaces an already-reflected entry for
# a second reflection, and — since resolve_due short-circuits before ever
# calling fetch_bars once nothing is due — never makes a redundant network
# call either.
# --------------------------------------------------------------------------- #


def test_double_resolution_scheduled_then_in_run_is_idempotent(jpath):
    calls: list[str] = []

    def counting_bars(symbol, start, end):
        calls.append(symbol)
        return fake_bars(symbol, start, end)

    entry = _append(jpath)

    # 1) The scheduled job fires first: resolves 24h+72h, then the officer
    #    (real or scheduled-agent) writes the primary-horizon reflection.
    scheduled_result = journal.resolve_due(counting_bars, now=T0 + timedelta(hours=80), path=jpath)
    assert {h for _, h in scheduled_result["resolved"]} == {"24h", "72h"}
    assert [e["id"] for e in scheduled_result["reflection_due"]] == [entry["id"]]
    journal.write_reflection(entry["id"], "Buy was right; +3% alpha.", path=jpath)
    after_scheduled = journal.load_entries(jpath)

    # 2) A same-day in-run trigger (committee's own reflection officer, or a
    #    second scheduler tick) calls resolve_due again before 7d is due.
    calls.clear()
    in_run_result = journal.resolve_due(counting_bars, now=T0 + timedelta(hours=81), path=jpath)
    assert in_run_result["resolved"] == []  # nothing newly due
    assert in_run_result["reflection_due"] == []  # already reflected, not re-surfaced
    assert not in_run_result["errors"]
    assert calls == []  # no due horizons -> fetch_bars never called again
    assert journal.load_entries(jpath) == after_scheduled  # byte-for-byte unchanged

    # 3) Only one journal entry ever exists for this (run_id, symbol) pair —
    #    double-firing resolve_due/reflect never appended a duplicate.
    assert len(journal.load_entries(jpath)) == 1

    # 4) A later trigger (next day's scheduled tick) resolves the final 7d
    #    horizon; the already-written reflection is still not re-surfaced.
    final_result = journal.resolve_due(counting_bars, now=T0 + timedelta(hours=200), path=jpath)
    assert {h for _, h in final_result["resolved"]} == {"7d"}
    assert final_result["reflection_due"] == []
    final_entry = journal.load_entries(jpath)[0]
    assert final_entry["status"] == "resolved"
    assert final_entry["reflection"] == "Buy was right; +3% alpha."  # untouched by re-resolution
