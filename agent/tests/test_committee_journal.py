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


# ------------------------------------- decision_journal tool append (paper loop Task 1 review fix)
# The PM reaches the journal ONLY through the decision_journal tool, so the
# execution fields must be exposed on the tool's append action too — otherwise
# the prompt-requested stop/TP/size could never reach journal.append_decision
# in production.


@pytest.fixture()
def jtool(jpath, monkeypatch):
    from src.tools.committee_journal_tool import DecisionJournalTool

    monkeypatch.setenv(journal.JOURNAL_PATH_ENV, str(jpath))
    return DecisionJournalTool()


def test_tool_append_threads_execution_fields(jtool, jpath):
    out = json.loads(
        jtool.execute(
            action="append",
            symbol="ETH-USDT",
            rating="Buy",
            time_horizon="72h swing",
            price_target=110.0,
            stop_loss=95.0,
            take_profit=125.0,
            position_size_pct=10.0,
            run_id="run-tool-1",
        )
    )
    assert out["status"] == "ok"
    assert out["entry"]["stop_loss"] == 95.0
    assert out["entry"]["take_profit"] == 125.0
    assert out["entry"]["position_size_pct"] == 10.0

    on_disk = journal.load_entries(jpath)[0]
    assert on_disk["stop_loss"] == 95.0
    assert on_disk["take_profit"] == 125.0
    assert on_disk["position_size_pct"] == 10.0


def test_tool_append_without_execution_fields_omits_keys(jtool, jpath):
    """Byte-shape regression at the tool layer: an append without the new
    fields must produce an entry with NO stop/TP/size keys (not nulls)."""
    out = json.loads(
        jtool.execute(
            action="append",
            symbol="ETH-USDT",
            rating="Hold",
            time_horizon="72h swing",
            run_id="run-tool-2",
        )
    )
    assert out["status"] == "ok"
    on_disk = journal.load_entries(jpath)[0]
    for key in ("stop_loss", "take_profit", "position_size_pct"):
        assert key not in out["entry"]
        assert key not in on_disk


def test_tool_append_nullish_execution_strings_coerce_to_omitted(jtool, jpath):
    """A '<unavailable>'/'n/a' string arriving through the tool coerces to
    None (same rule as the schema fields) — journaled with no key, no error."""
    out = json.loads(
        jtool.execute(
            action="append",
            symbol="ETH-USDT",
            rating="Buy",
            time_horizon="72h swing",
            stop_loss="<unavailable>",
            take_profit="n/a",
            position_size_pct="",
            run_id="run-tool-3",
        )
    )
    assert out["status"] == "ok"
    on_disk = journal.load_entries(jpath)[0]
    for key in ("stop_loss", "take_profit", "position_size_pct"):
        assert key not in out["entry"]
        assert key not in on_disk


def test_tool_append_numeric_strings_coerce_to_floats(jtool, jpath):
    out = json.loads(
        jtool.execute(
            action="append",
            symbol="ETH-USDT",
            rating="Buy",
            time_horizon="72h swing",
            stop_loss="61200 USDT",
            take_profit="$70,000",
            position_size_pct="10",
            run_id="run-tool-4",
        )
    )
    assert out["status"] == "ok"
    on_disk = journal.load_entries(jpath)[0]
    assert on_disk["stop_loss"] == pytest.approx(61200.0)
    assert on_disk["take_profit"] == pytest.approx(70000.0)
    assert on_disk["position_size_pct"] == pytest.approx(10.0)


@pytest.mark.parametrize("bad", [-5, 105, -0.01, 100.5])
def test_tool_append_position_size_pct_out_of_range_rejected(jtool, jpath, bad):
    """Final review C1(a): the tool layer must reject position_size_pct outside
    [0, 100] (fail-before-write, like the unparseable path) — otherwise a
    negative pct reaches the translator and mints cash / negative-qty positions."""
    out = json.loads(
        jtool.execute(
            action="append",
            symbol="ETH-USDT",
            rating="Buy",
            time_horizon="72h swing",
            position_size_pct=bad,
            run_id="run-range",
        )
    )
    assert out["status"] == "error"
    assert "position_size_pct" in out["error"]
    assert journal.load_entries(jpath) == []  # nothing half-journaled


def test_tool_append_position_size_pct_boundaries_accepted(jtool, jpath):
    """0 and 100 are valid (inclusive bounds)."""
    out = json.loads(
        jtool.execute(
            action="append",
            symbol="ETH-USDT",
            rating="Hold",
            time_horizon="72h swing",
            position_size_pct=0,
            run_id="run-b0",
        )
    )
    assert out["status"] == "ok"
    assert out["entry"]["position_size_pct"] == 0.0


def test_tool_append_derives_run_id_from_run_dir(jtool, jpath, monkeypatch):
    """Final review C3: the swarm runtime injects only run_dir; the tool must
    derive the run_id from the .swarm/runs/<run_id>/... path when run_id is
    absent, so PM-retry idempotency (keyed on (run_id, symbol)) actually works."""
    monkeypatch.setenv("VIBE_PAPER_ENABLED", "")  # isolate: no hook
    out = json.loads(
        jtool.execute(
            action="append",
            symbol="ETH-USDT",
            rating="Hold",
            time_horizon="72h swing",
            run_dir="/tmp/x/.swarm/runs/run-abc123/artifacts/portfolio_manager",
        )
    )
    assert out["status"] == "ok"
    assert out["entry"]["run_id"] == "run-abc123"


def test_derive_run_id_anchored_to_swarm_runs_segment():
    """Post-review polish: a checkout path containing a bare /runs/ segment
    must not poison the derivation (a constant wrong run_id would silently
    dedupe across runs) — the regex anchors to `.swarm/runs/`."""
    from src.tools.committee_journal_tool import _derive_run_id

    poisoned = "/home/user/runs/checkout/.swarm/runs/run-x/artifacts/pm"
    assert _derive_run_id(poisoned) == "run-x"
    # no .swarm/runs segment at all -> None, never the bare /runs/ match
    assert _derive_run_id("/home/user/runs/checkout/artifacts/pm") is None
    assert _derive_run_id(None) is None
    assert _derive_run_id("") is None


@pytest.mark.parametrize("bad", [0, 0.0, "0", -5])
def test_tool_append_non_positive_price_levels_become_null(jtool, jpath, monkeypatch, bad):
    """Live incident 2026-07-20: a Hold was journaled with stop_loss=0.0,
    take_profit=0.0, price_target=0. Zero is not a price — carried downstream
    it becomes a stop that can never trigger. Non-positive PRICE fields
    resolve to None (the "not specified" contract the translator already
    handles), and the drop is surfaced rather than silent."""
    monkeypatch.setenv("VIBE_PAPER_ENABLED", "")
    out = json.loads(
        jtool.execute(
            action="append", symbol="BTC-USDT", rating="Hold",
            time_horizon="72h swing", run_id=f"run-zero-{bad}",
            stop_loss=bad, take_profit=bad, price_target=bad,
        )
    )
    assert out["status"] == "ok"
    entry = out["entry"]
    # append_decision omits null execution fields entirely (byte-shape
    # preservation for pre-existing rows) — absent is the "not specified"
    # shape, and satisfies the contract as well as an explicit null.
    assert entry.get("stop_loss") is None
    assert entry.get("take_profit") is None
    assert entry.get("price_target") is None
    assert sorted(out["dropped_price_fields"]) == ["price_target", "stop_loss", "take_profit"]


def test_tool_append_position_size_pct_zero_is_still_valid(jtool, jpath, monkeypatch):
    """Guard the boundary: 0 is meaningless for a PRICE but legitimate for
    position_size_pct (no position). It must not be swept up by the fix."""
    monkeypatch.setenv("VIBE_PAPER_ENABLED", "")
    out = json.loads(
        jtool.execute(
            action="append", symbol="BTC-USDT", rating="Hold",
            time_horizon="72h swing", run_id="run-size-zero",
            position_size_pct=0, stop_loss=61000.0,
        )
    )
    assert out["entry"]["position_size_pct"] == 0.0
    assert out["entry"]["stop_loss"] == 61000.0
    assert "dropped_price_fields" not in out


def test_tool_append_derived_run_id_overrides_mismatched_explicit(jtool, jpath, monkeypatch):
    """Live incident 2026-07-19 (run swarm-20260719-204809-6500134e): the PM
    appended six rows by mutating its run_id ("-corrected-no-execution",
    "-final", ...), defeating (run_id, symbol) idempotency. The runtime-injected
    run_dir is ground truth — when it derives a run_id, a mismatched
    model-supplied run_id must be overridden (and the correction surfaced)."""
    monkeypatch.setenv("VIBE_PAPER_ENABLED", "")
    out = json.loads(
        jtool.execute(
            action="append",
            symbol="ETH-USDT",
            rating="Hold",
            time_horizon="72h swing",
            run_id="derived-run-corrected-no-execution",
            run_dir="/tmp/x/.swarm/runs/derived-run/artifacts/pm",
        )
    )
    assert out["status"] == "ok"
    assert out["entry"]["run_id"] == "derived-run"
    assert out["run_id_corrected"] == {
        "from": "derived-run-corrected-no-execution", "to": "derived-run",
    }


def test_tool_append_explicit_run_id_used_when_run_dir_underivable(jtool, jpath, monkeypatch):
    """Without a derivable run_dir (CLI/manual use) the caller's run_id still
    stands — the override only fires when the injected path knows better."""
    monkeypatch.setenv("VIBE_PAPER_ENABLED", "")
    out = json.loads(
        jtool.execute(
            action="append",
            symbol="ETH-USDT",
            rating="Hold",
            time_horizon="72h swing",
            run_id="explicit-run",
            run_dir="/home/user/checkout/artifacts/pm",  # no .swarm/runs segment
        )
    )
    assert out["entry"]["run_id"] == "explicit-run"
    assert "run_id_corrected" not in out


def test_tool_append_mutated_run_ids_collapse_to_one_entry_with_dedupe_signal(
    jtool, jpath, monkeypatch
):
    """The incident's exact shape: repeated appends with mutated run_id strings
    but the same run_dir must (1) produce exactly ONE journal entry and
    (2) tell the model so explicitly (deduplicated: true) instead of returning
    an indistinguishable success it will keep 'correcting'."""
    monkeypatch.setenv("VIBE_PAPER_ENABLED", "")
    run_dir = "/tmp/x/.swarm/runs/run-abc/artifacts/portfolio_manager"
    first = json.loads(
        jtool.execute(
            action="append", symbol="BTC-USDT", rating="Hold",
            time_horizon="72h swing", run_id="run-abc", run_dir=run_dir,
        )
    )
    assert "deduplicated" not in first
    for mutated in ("run-abc-corrected-no-execution", "run-abc-final", ""):
        out = json.loads(
            jtool.execute(
                action="append", symbol="BTC-USDT", rating="Hold",
                time_horizon="72h swing", run_id=mutated, run_dir=run_dir,
            )
        )
        assert out["status"] == "ok"
        assert out["deduplicated"] is True
        assert out["entry"]["id"] == first["entry"]["id"]
    entries = journal.load_entries(jpath)
    assert len(entries) == 1
    assert entries[0]["run_id"] == "run-abc"


def test_tool_append_retry_same_run_dir_idempotent_hook_runs_once(
    jtool, jpath, monkeypatch, tmp_path
):
    """Final review C3: a retried PM task re-appends with the same run_dir; the
    derived run_id makes the append idempotent (same decision id) AND the
    paper-execution hook does not buy twice (ledger length stays 1)."""
    paper_root = tmp_path / "paper_root"
    _set_paper_env(monkeypatch, paper_root)  # VIBE_PAPER_ENABLED=1

    from src.tools import crypto_snapshot_tool as snap

    monkeypatch.setattr(
        snap,
        "_fetch_row",
        lambda **k: ({"last": "100.5", "ts": "1700000000000"}, None),
    )

    run_dir = "/tmp/x/.swarm/runs/run-xyz/artifacts/portfolio_manager"
    out1 = json.loads(
        jtool.execute(
            action="append",
            symbol="BTC-USDT",
            rating="Buy",
            time_horizon="72h swing",
            run_dir=run_dir,
        )
    )
    out2 = json.loads(
        jtool.execute(
            action="append",
            symbol="BTC-USDT",
            rating="Buy",
            time_horizon="72h swing",
            run_dir=run_dir,
        )
    )

    assert out1["entry"]["run_id"] == "run-xyz"
    assert out1["entry"]["id"] == out2["entry"]["id"]  # idempotent append

    from src.paper.store import PaperStore

    ledger = list(PaperStore(paper_root).iter_ledger())
    assert len(ledger) == 1  # hook executed exactly once, not twice


def test_tool_append_unparseable_execution_value_is_actionable_error(jtool, jpath):
    out = json.loads(
        jtool.execute(
            action="append",
            symbol="ETH-USDT",
            rating="Buy",
            time_horizon="72h swing",
            stop_loss="watch the 200 SMA area",  # prose, not a level
            run_id="run-tool-5",
        )
    )
    assert out["status"] == "error"
    assert "stop_loss" in out["error"]
    assert journal.load_entries(jpath) == []  # nothing half-journaled


# ----------------------------------------------- execution field passthrough (paper loop Task 1)


def test_append_decision_writes_execution_fields_when_present(jpath):
    entry = journal.append_decision(
        symbol="ETH-USDT",
        rating="Buy",
        time_horizon="72h swing",
        price_target=110.0,
        stop_loss=95.0,
        take_profit=125.0,
        position_size_pct=10.0,
        run_id="run-exec-1",
        decided_at=T0,
        path=jpath,
    )
    assert entry["stop_loss"] == 95.0
    assert entry["take_profit"] == 125.0
    assert entry["position_size_pct"] == 10.0

    on_disk = journal.load_entries(jpath)[0]
    assert on_disk["stop_loss"] == 95.0
    assert on_disk["take_profit"] == 125.0
    assert on_disk["position_size_pct"] == 10.0


def test_append_decision_omits_execution_keys_when_absent(jpath):
    """Byte-shape regression: entries written without the new fields must stay
    identical in shape to pre-existing production journal entries — no null
    keys added, unlike price_target which is always present."""
    entry = _append(jpath)
    for key in ("stop_loss", "take_profit", "position_size_pct"):
        assert key not in entry

    on_disk = journal.load_entries(jpath)[0]
    for key in ("stop_loss", "take_profit", "position_size_pct"):
        assert key not in on_disk
    assert set(on_disk) == {
        "id",
        "decided_at",
        "symbol",
        "rating",
        "time_horizon",
        "primary_horizon",
        "price_target",
        "run_id",
        "status",
        "ref_price",
        "horizons",
        "reflection",
        "reflected_at",
    }


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


# --------------------------------------------------------------------------- #
# Task 5 — post-append paper execution hook seam                              #
#
# The tool's append success path calls maybe_execute_paper(entry) and, when
# it returns non-None, adds a paper_execution key to the JSON response. When
# disabled (VIBE_PAPER_ENABLED falsy), the key is ABSENT — not null. The
# append's own success semantics (status/entry_id/entry, and the on-disk
# journal write) must be byte-identical regardless of what the executor does
# or whether it crashes.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("value", ["0", "false", ""])
def test_append_disabled_paper_key_absent(jtool, jpath, monkeypatch, value):
    monkeypatch.setenv("VIBE_PAPER_ENABLED", value)
    out = json.loads(
        jtool.execute(
            action="append",
            symbol="ETH-USDT",
            rating="Hold",
            time_horizon="72h swing",
            run_id="run-paper-1",
        )
    )
    assert out["status"] == "ok"
    assert "paper_execution" not in out

    on_disk = journal.load_entries(jpath)[0]
    assert "paper_execution" not in on_disk  # never leaks into the journal file


def test_append_enabled_calls_hook_and_adds_key(jtool, jpath, monkeypatch):
    monkeypatch.setenv("VIBE_PAPER_ENABLED", "1")
    monkeypatch.setattr(
        "src.paper.hook.maybe_execute_paper",
        lambda entry: {"decision_id": entry["id"], "actions": [], "skipped": None},
    )
    out = json.loads(
        jtool.execute(
            action="append",
            symbol="ETH-USDT",
            rating="Hold",
            time_horizon="72h swing",
            run_id="run-paper-2",
        )
    )
    assert out["status"] == "ok"
    assert out["paper_execution"]["decision_id"] == out["entry_id"]


def test_append_executor_crash_is_isolated_from_append_success(jtool, jpath, monkeypatch):
    """Monkeypatch execute_decision (not maybe_execute_paper — that function's
    own contract is to never raise) to prove the hook's try/except actually
    isolates a downstream crash: the append still succeeds and the response
    carries paper_execution: {"error": ...} rather than propagating."""
    monkeypatch.setenv("VIBE_PAPER_ENABLED", "1")
    monkeypatch.setenv("VIBE_PAPER_ROOT", str(jpath.parent / "paper_root"))

    def boom(entry, broker):
        raise RuntimeError("executor exploded")

    monkeypatch.setattr("src.paper.translator.execute_decision", boom)

    out = json.loads(
        jtool.execute(
            action="append",
            symbol="ETH-USDT",
            rating="Buy",
            time_horizon="72h swing",
            run_id="run-paper-3",
        )
    )
    assert out["status"] == "ok"
    assert out["entry"]["symbol"] == "ETH-USDT"
    assert out["paper_execution"] == {"error": "executor exploded"}

    # The append's own success semantics are untouched: the entry landed on
    # disk exactly as it would have without the hook at all.
    on_disk = journal.load_entries(jpath)[0]
    assert on_disk["symbol"] == "ETH-USDT"
    assert on_disk["rating"] == "Buy"


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


# --------------------------------------------------------------------------- #
# Task 6 — decision_journal action="pnl"                                      #
# --------------------------------------------------------------------------- #
NOT_EXECUTED_MESSAGE = "not executed — no paper-trading data"


def _set_paper_env(monkeypatch, paper_root, **overrides):
    env = {
        "VIBE_PAPER_ENABLED": "1",
        "VIBE_PAPER_START_CASH": "100000",
        "VIBE_PAPER_SLIPPAGE_BPS": "5",
        "VIBE_PAPER_FEE_BPS": "10",
        "VIBE_PAPER_MAX_POSITIONS": "3",
        "VIBE_PAPER_MAX_SYMBOL_PCT": "25",
        "VIBE_PAPER_DEFAULT_SIZE_PCT": "10",
        "VIBE_PAPER_DEFAULT_STOP_PCT": "8",
        "VIBE_PAPER_ROOT": str(paper_root),
    }
    env.update(overrides)
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))


def test_pnl_requires_decision_id_or_symbol(jtool):
    out = json.loads(jtool.execute(action="pnl"))
    assert out["status"] == "error"


def test_pnl_by_decision_id_no_paper_root_not_executed(jtool, jpath, monkeypatch, tmp_path):
    monkeypatch.setenv("VIBE_PAPER_ROOT", str(tmp_path / "no-such-paper-root"))
    entry = _append(jpath)

    out = json.loads(jtool.execute(action="pnl", decision_id=entry["id"]))
    assert out["status"] == "ok"
    assert out["executed"] is False
    # instructive headline first, then decision_pnl's evidence block
    assert out["summary"].startswith(NOT_EXECUTED_MESSAGE)
    assert "no paper-trading ledger rows" in out["summary"]


def test_pnl_noop_only_decision_tool_response_carries_noop_evidence(jtool, jpath, monkeypatch, tmp_path):
    """Review Important 1: the tool's not-executed response must not discard
    decision_pnl's evidence-bearing summary — the reflection officer needs
    the WHY (noop note) in addition to the instructive headline."""
    paper_root = tmp_path / "paper_root"
    _set_paper_env(monkeypatch, paper_root)
    entry = _append(jpath)

    from src.paper.store import PaperStore

    store = PaperStore(paper_root)
    store.append_ledger(
        {
            "ts": "2026-07-01T00:00:00Z",
            "trade_id": "t-noop",
            "symbol": entry["symbol"],
            "side": "sell",
            "qty": 0.0,
            "fill_price": None,
            "slippage_paid": 0.0,
            "fee_paid": 0.0,
            "order_type": "noop",
            "decision_id": entry["id"],
            "realized_pnl": None,
            "note": "sell signal with no position",
        }
    )

    out = json.loads(jtool.execute(action="pnl", decision_id=entry["id"]))
    assert out["status"] == "ok"
    assert out["executed"] is False
    # instructive headline preserved AND the noop note surfaces
    assert NOT_EXECUTED_MESSAGE in out["summary"]
    assert "sell signal with no position" in out["summary"]

    # same evidence through the symbol path
    out_sym = json.loads(jtool.execute(action="pnl", symbol=entry["symbol"]))
    assert out_sym["executed"] is False
    assert "sell signal with no position" in out_sym["summary"]


def test_pnl_by_symbol_no_candidates_not_executed(jtool, monkeypatch, tmp_path):
    monkeypatch.setenv("VIBE_PAPER_ROOT", str(tmp_path / "no-such-paper-root"))
    out = json.loads(jtool.execute(action="pnl", symbol="ZZZ-USDT"))
    assert out["status"] == "ok"
    assert out["executed"] is False
    assert out["summary"] == NOT_EXECUTED_MESSAGE


def test_pnl_by_decision_id_executed_returns_full_shape(jtool, jpath, monkeypatch, tmp_path):
    paper_root = tmp_path / "paper_root"
    _set_paper_env(monkeypatch, paper_root)
    entry = _append(jpath)

    from src.paper.store import PaperStore

    store = PaperStore(paper_root)
    store.append_ledger(
        {
            "ts": "2026-07-01T00:00:00Z",
            "trade_id": "t1",
            "symbol": entry["symbol"],
            "side": "buy",
            "qty": 10.0,
            "fill_price": 100.0,
            "slippage_paid": 0.0,
            "fee_paid": 1.0,
            "order_type": "market",
            "decision_id": entry["id"],
            "realized_pnl": None,
            "note": None,
        }
    )
    store.save_positions(
        [
            {
                "symbol": entry["symbol"],
                "qty": 10.0,
                "avg_entry": 100.0,
                "stop": None,
                "take_profits": [],
                "opened_at": "2026-07-01T00:00:00Z",
                "decision_id": entry["id"],
            }
        ]
    )

    out = json.loads(jtool.execute(action="pnl", decision_id=entry["id"]))
    assert out["status"] == "ok"
    assert out["executed"] is True
    assert out["decision_id"] == entry["id"]
    assert out["exit_kind"] == "open"
    assert out["position_open"] is True
    assert "summary" in out


def test_pnl_by_symbol_finds_most_recent_executed(jtool, jpath, monkeypatch, tmp_path):
    paper_root = tmp_path / "paper_root"
    _set_paper_env(monkeypatch, paper_root)
    older = _append(jpath, decided_at=T0, run_id="run-older")
    newer = _append(jpath, decided_at=T0 + timedelta(hours=5), run_id="run-newer")

    from src.paper.store import PaperStore

    store = PaperStore(paper_root)
    for decision_id, ts in ((older["id"], "2026-07-01T00:00:00Z"), (newer["id"], "2026-07-01T05:00:00Z")):
        store.append_ledger(
            {
                "ts": ts,
                "trade_id": f"t-{decision_id}",
                "symbol": "ETH-USDT",
                "side": "buy",
                "qty": 10.0,
                "fill_price": 100.0,
                "slippage_paid": 0.0,
                "fee_paid": 1.0,
                "order_type": "market",
                "decision_id": decision_id,
                "realized_pnl": None,
                "note": None,
            }
        )

    out = json.loads(jtool.execute(action="pnl", symbol="ETH-USDT"))
    assert out["status"] == "ok"
    assert out["executed"] is True
    assert out["decision_id"] == newer["id"]  # most recent, not the older one


# --------------------------------------------------------------------------- #
# Task 6 regression guard — resolve_due/reflect/lessons/list are byte-        #
# identical whether or not VIBE_PAPER_ROOT points at an empty/nonexistent     #
# dir: those four actions never touch src.paper at all (only append's hook    #
# and the new pnl action do), so the paper package's mere presence/absence    #
# must have zero effect on them.                                              #
# --------------------------------------------------------------------------- #
def test_existing_actions_byte_identical_with_empty_paper_root(jtool, jpath, monkeypatch, tmp_path):
    empty_root = tmp_path / "does-not-exist-yet"
    monkeypatch.setenv("VIBE_PAPER_ROOT", str(empty_root))
    assert not empty_root.exists()

    # decided "now" -- no horizon is due yet, so resolve_due's `due` check
    # short-circuits before ever calling fetch_bars (see journal.resolve_due:
    # `if not due: continue`), keeping this test network-free without a fake
    # bars fixture.
    journal.append_decision(
        symbol="ETH-USDT",
        rating="Buy",
        time_horizon="72h swing",
        path=jpath,
        run_id="run-regress",
    )

    resolve_out = json.loads(jtool.execute(action="resolve_due"))
    assert resolve_out["status"] == "ok"
    assert resolve_out["resolved"] == []
    assert resolve_out["reflection_due"] == []
    assert resolve_out["errors"] == []

    lessons_out = json.loads(jtool.execute(action="lessons", symbol="ETH-USDT"))
    assert lessons_out["status"] == "ok"
    assert "lessons_markdown" in lessons_out

    list_out = json.loads(jtool.execute(action="list"))
    assert list_out["status"] == "ok"
    assert len(list_out["entries"]) == 1

    entry_id = list_out["entries"][0]["id"]
    reflect_out = json.loads(
        jtool.execute(action="reflect", entry_id=entry_id, reflection="n/a — not yet due")
    )
    assert reflect_out == {"status": "ok", "entry_id": entry_id}

    # None of the four actions ever touched the paper package -- the root
    # directory was never even created.
    assert not empty_root.exists()
