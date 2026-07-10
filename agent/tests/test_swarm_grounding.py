"""Unit tests for the swarm grounding module.

Network-touching paths (the actual loader fetch) are exercised through a
monkeypatched stub so the suite stays offline; symbol extraction and the
markdown formatter are pure-function tests.
"""

from __future__ import annotations

import threading
from datetime import date
from unittest.mock import MagicMock

import pandas as pd
import pytest

from src.swarm import grounding
from src.swarm.models import (
    RunStatus,
    SwarmAgentSpec,
    SwarmRun,
    SwarmTask,
    TaskStatus,
    WorkerResult,
)
from src.swarm.runtime import SwarmRuntime
from src.swarm.task_store import TaskStore
from src.swarm.worker import build_worker_prompt


# --------------------------------------------------------------------------- #
# extract_symbols_from_user_vars
# --------------------------------------------------------------------------- #

def test_extract_us_hk_a_share_and_crypto_symbols() -> None:
    user_vars = {
        "target": "NVDA.US",
        "secondary": "Compare with 700.HK and 600519.SH",
        "crypto": "Hedge with BTC-USDT",
        "shenzhen": "000001.SZ for liquidity",
        "beijing": "Listed on 430090.BJ recently",
    }
    found = grounding.extract_symbols_from_user_vars(user_vars)
    assert set(found) == {
        "NVDA.US", "700.HK", "600519.SH", "BTC-USDT", "000001.SZ", "430090.BJ",
    }


def test_extract_preserves_first_occurrence_order() -> None:
    user_vars = {
        "a": "Look at NVDA.US",
        "b": "Compare to AAPL.US",
        "c": "And NVDA.US again",
    }
    assert grounding.extract_symbols_from_user_vars(user_vars) == ["NVDA.US", "AAPL.US"]


def test_extract_returns_empty_when_no_symbol_present() -> None:
    user_vars = {
        "goal": "Q2 2026 outlook",
        "market": "US equities",  # no suffixed symbol
    }
    assert grounding.extract_symbols_from_user_vars(user_vars) == []


def test_extract_skips_non_string_values() -> None:
    user_vars = {
        "weight": 0.5,                  # type: ignore[dict-item]  — not a str
        "real_target": "TSLA.US",
    }
    assert grounding.extract_symbols_from_user_vars(user_vars) == ["TSLA.US"]


def test_extract_promotes_bare_us_ticker() -> None:
    # The #198 reporter's exact shape: investment_committee target text with
    # a bare US ticker and no loader suffix anywhere.
    user_vars = {
        "target": "Evaluate whether to go long or short on NVDA given current market conditions",
        "market": "A-shares",
    }
    assert grounding.extract_symbols_from_user_vars(user_vars) == ["NVDA.US"]


def test_extract_bare_ticker_skips_common_acronyms() -> None:
    user_vars = {
        "goal": "US CPI and FED policy impact on AI ETF flows; CEO guidance, PE ratios, USD strength",
    }
    assert grounding.extract_symbols_from_user_vars(user_vars) == []


def test_extract_bare_ticker_does_not_duplicate_suffixed_symbol() -> None:
    user_vars = {"goal": "Compare NVDA.US against a bare NVDA mention"}
    assert grounding.extract_symbols_from_user_vars(user_vars) == ["NVDA.US"]


def test_extract_bare_scan_does_not_split_suffixed_symbols() -> None:
    # BTC-USDT must stay one crypto pair; neither BTC.US nor USDT.US may leak.
    user_vars = {"goal": "Hedge BTC-USDT exposure into quarter end"}
    assert grounding.extract_symbols_from_user_vars(user_vars) == ["BTC-USDT"]


def test_extract_explicit_symbols_rank_before_bare_promotions() -> None:
    # Explicit suffixed symbols must win the max-symbols cap, so they sort
    # first even when a bare ticker appears earlier in the text.
    user_vars = {"goal": "MSTR leverage versus 600519.SH stability"}
    assert grounding.extract_symbols_from_user_vars(user_vars) == ["600519.SH", "MSTR.US"]


def test_extract_ignores_lowercase_and_single_letter_tokens() -> None:
    user_vars = {"goal": "buy nvda now, grade A balance sheet"}
    assert grounding.extract_symbols_from_user_vars(user_vars) == []


def test_extract_does_not_match_substrings_inside_words() -> None:
    user_vars = {
        # \b boundary should keep "FOO.USDA" / "BLAH.USA" from matching .US
        "noisy": "regulator FOO.USDA approved BLAH.USAID rules",
    }
    assert grounding.extract_symbols_from_user_vars(user_vars) == []


# --------------------------------------------------------------------------- #
# fetch_grounding_data — monkeypatched loader
# --------------------------------------------------------------------------- #

class _StubLoader:
    """Mimics enough of the loader contract for grounding.fetch."""

    def __init__(self, frame: pd.DataFrame) -> None:
        self._frame = frame

    def fetch(self, codes, start_date, end_date, *, interval="1D"):
        return {code: self._frame for code in codes}


def _three_bar_frame() -> pd.DataFrame:
    idx = pd.to_datetime(["2026-05-06", "2026-05-07", "2026-05-08"])
    return pd.DataFrame(
        {
            "open":   [200.0, 208.3, 213.0],
            "high":   [208.3, 214.2, 217.8],
            "low":    [198.6, 206.5, 212.9],
            "close":  [207.8, 211.5, 215.2],
            "volume": [188e6, 168e6, 136e6],
        },
        index=idx,
    )


def test_fetch_returns_normalized_bars(monkeypatch) -> None:
    """Real call path: ``_detect_market(code)`` → ``resolve_loader(market)``
    returns a ready loader instance. The stub mirrors that contract so a
    regression that drops or rewrites the dispatch shows up here.
    """
    frame = _three_bar_frame()
    import backtest.loaders.registry as reg
    captured_markets: list[str] = []

    def _fake_resolve(market: str):
        captured_markets.append(market)
        return _StubLoader(frame)

    monkeypatch.setattr(reg, "resolve_loader", _fake_resolve)

    bars = grounding.fetch_grounding_data(["NVDA.US"], today=date(2026, 5, 9))

    # ``NVDA.US`` must dispatch through the us_equity branch — guards
    # against a regression where the code is passed as the market key.
    assert captured_markets == ["us_equity"]
    assert "NVDA.US" in bars
    rows = bars["NVDA.US"]
    assert len(rows) == 3
    assert rows[-1]["close"] == pytest.approx(215.2)
    assert rows[0]["trade_date"].startswith("2026-05-06")


def test_fetch_skips_symbols_with_no_data(monkeypatch) -> None:
    import backtest.loaders.registry as reg
    monkeypatch.setattr(
        reg, "resolve_loader",
        lambda market: _StubLoader(pd.DataFrame()),  # empty frame
    )

    bars = grounding.fetch_grounding_data(["NOPE.US"])
    assert bars == {}


def test_fetch_returns_empty_for_empty_input() -> None:
    assert grounding.fetch_grounding_data([]) == {}


def test_max_grounding_symbols_uses_env(monkeypatch) -> None:
    monkeypatch.setenv("SWARM_GROUNDING_MAX_SYMBOLS", "3")
    assert grounding.max_grounding_symbols() == 3


def test_max_grounding_symbols_falls_back_on_invalid_env(monkeypatch) -> None:
    monkeypatch.setenv("SWARM_GROUNDING_MAX_SYMBOLS", "nope")
    assert grounding.max_grounding_symbols() == grounding.DEFAULT_MAX_SYMBOLS


# --------------------------------------------------------------------------- #
# format_grounding_block
# --------------------------------------------------------------------------- #

def test_format_returns_empty_for_empty_grounding() -> None:
    assert grounding.format_grounding_block({}) == ""
    assert grounding.format_grounding_block({"NVDA.US": []}) == ""


def test_format_renders_table_and_range() -> None:
    rows = [
        {"trade_date": "2026-05-06T00:00:00", "open": 200.0, "high": 208.3,
         "low": 198.6, "close": 207.8, "volume": 188_000_000.0},
        {"trade_date": "2026-05-07T00:00:00", "open": 208.3, "high": 214.2,
         "low": 206.5, "close": 211.5, "volume": 168_000_000.0},
        {"trade_date": "2026-05-08T00:00:00", "open": 213.0, "high": 217.8,
         "low": 212.9, "close": 215.2, "volume": 136_000_000.0},
    ]
    block = grounding.format_grounding_block({"NVDA.US": rows})

    assert "Ground Truth" in block
    assert "NVDA.US" in block
    assert "215.20" in block            # last close
    assert "207.80 – 215.20" in block   # window range (min/max close)
    assert "2026-05-06 → 2026-05-08" in block
    # The instruction text must survive — it's the whole point.
    assert "Do NOT cite prices" in block


# --------------------------------------------------------------------------- #
# Worker prompt integration
# --------------------------------------------------------------------------- #

def _spec() -> SwarmAgentSpec:
    return SwarmAgentSpec(
        id="dummy",
        role="research analyst",
        system_prompt="Analyse the asset.",
    )


def test_worker_prompt_includes_grounding_block_when_provided() -> None:
    block = "## Ground Truth — Recent Market Data\n\nNVDA.US ..."
    prompt = build_worker_prompt(_spec(), {}, "(no matching skills)", grounding_block=block)
    assert block in prompt
    # Block must appear before the Execution Rules so it's in scope when
    # the worker plans its first call.
    assert prompt.index(block) < prompt.index("## Execution Rules")


def test_worker_prompt_omits_grounding_section_when_block_empty() -> None:
    prompt = build_worker_prompt(_spec(), {}, "(no matching skills)")
    # No rendered Ground Truth section. The Data Citation Discipline below
    # may reference "Ground Truth block above (if present)" as a referent;
    # that's fine. What must not appear is the actual section header.
    assert "## Ground Truth" not in prompt


def test_worker_prompt_always_includes_data_citation_discipline() -> None:
    """Universal anti-fabrication rule: must appear regardless of whether
    grounding_block or upstream_summaries were provided.  Issue #106 reported
    swarm reports citing prices the agent never actually fetched; the
    grounding-block rule only covered runs with explicit symbols in user_vars."""
    bare = build_worker_prompt(_spec(), {}, "(no matching skills)")
    assert "Data Citation Discipline" in bare
    assert "HARD RULE" in bare
    assert "may NOT cite numbers from memory or training data" in bare

    with_grounding = build_worker_prompt(
        _spec(), {}, "(no matching skills)",
        grounding_block="## Ground Truth — Recent Market Data\n\nNVDA.US ...",
    )
    assert with_grounding.count("Data Citation Discipline") == 1


def test_worker_prompt_data_citation_discipline_precedes_execution_rules() -> None:
    """The discipline must be in scope before Phase 1/2/3 execution rules
    so the worker sees it while planning the first tool call."""
    prompt = build_worker_prompt(_spec(), {}, "(no matching skills)")
    assert prompt.index("Data Citation Discipline") < prompt.index("## Execution Rules")


def test_worker_prompt_data_citation_rule_targets_aggregator_roles() -> None:
    """The rule must explicitly address synthesis / aggregator agents
    that lack data tools, since those were the worst-case path in #106
    (equity_research_team aggregator has [bash, read_file, write_file] only)."""
    prompt = build_worker_prompt(_spec(), {}, "(no matching skills)")
    # Aggregators must be told not to invent numbers upstream omitted.
    lowered = prompt.lower()
    assert "synthesis" in lowered or "aggregator" in lowered
    assert "upstream did not provide" in prompt


def test_runtime_threads_grounding_block_into_layer_workers(tmp_path, monkeypatch) -> None:
    """Regression: _execute_layer must receive the run-level grounding block."""
    store = MagicMock()
    runtime = SwarmRuntime(store=store, max_workers=1)
    run_dir = tmp_path / "run"
    task_store = TaskStore(run_dir)
    agent = _spec()
    task = SwarmTask(id="task1", agent_id=agent.id, prompt_template="Analyze.")
    task_store.save_task(task)
    run = SwarmRun(
        id="run1",
        preset_name="dummy",
        created_at="2026-05-13T00:00:00+00:00",
        agents=[agent],
        tasks=[task],
    )
    seen: list[str] = []

    def _fake_worker(**kwargs):
        seen.append(kwargs["grounding_block"])
        return WorkerResult(status="completed", summary="done")

    monkeypatch.setattr(runtime, "_run_worker_with_retries", _fake_worker)

    results = runtime._execute_layer(
        run=run,
        task_store=task_store,
        agent_map={agent.id: agent},
        layer_task_ids=[task.id],
        task_summaries={},
        run_dir=run_dir,
        cancel_event=threading.Event(),
        grounding_block="GROUNDING",
    )

    assert results[task.id].summary == "done"
    assert seen == ["GROUNDING"]


# --------------------------------------------------------------------------- #
# Instrument identity anchor (Phase 5)
# --------------------------------------------------------------------------- #

def test_identity_anchor_var_scoped_to_crypto_committee_only() -> None:
    assert grounding.identity_anchor_var("crypto_committee") == "target"
    # A preset that also declares a {target} var but doesn't unanimously vote
    # on ONE instrument (free-text framing, per variables: in its own YAML)
    # keeps the legacy silent-drop behavior.
    assert grounding.identity_anchor_var("investment_committee") is None
    assert grounding.identity_anchor_var("equity_research_team") is None
    assert grounding.identity_anchor_var("some_unknown_preset") is None


def test_resolve_identity_symbol_matches_suffixed_symbol() -> None:
    assert grounding.resolve_identity_symbol("BTC-USDT") == "BTC-USDT"


def test_resolve_identity_symbol_returns_none_for_unrecognizable_text() -> None:
    assert grounding.resolve_identity_symbol("long the market") is None
    assert grounding.resolve_identity_symbol("") is None


def test_format_identity_anchor_renders_expected_line() -> None:
    rows = [
        {"trade_date": "2026-07-09T00:00:00", "close": 66000.0},
        {"trade_date": "2026-07-10T02:00:00", "close": 67432.1},
    ]
    line = grounding.format_identity_anchor("BTC-USDT", rows)
    assert line == (
        "You are analyzing **BTC-USDT** (OKX spot, last 67,432.1 @ "
        "2026-07-10T02:00:00). Do not substitute any other instrument."
    )


def test_format_identity_anchor_raises_on_empty_rows() -> None:
    with pytest.raises(ValueError):
        grounding.format_identity_anchor("BTC-USDT", [])


def test_format_grounding_block_prepends_identity_anchor() -> None:
    rows = [
        {"trade_date": "2026-07-10T00:00:00", "open": 66000.0, "high": 67500.0,
         "low": 65800.0, "close": 67432.1, "volume": 12345.0},
    ]
    block = grounding.format_grounding_block(
        {"BTC-USDT": rows}, identity_anchor="You are analyzing **BTC-USDT** ..."
    )
    assert block.startswith("**Instrument identity:** You are analyzing **BTC-USDT** ...")
    assert "## Ground Truth" in block
    assert block.index("Instrument identity") < block.index("Ground Truth")


def test_format_grounding_block_anchor_only_when_no_bars() -> None:
    block = grounding.format_grounding_block({}, identity_anchor="ANCHOR LINE")
    assert block == "**Instrument identity:** ANCHOR LINE"


def test_format_grounding_block_omits_anchor_line_when_none() -> None:
    rows = [
        {"trade_date": "2026-07-10T00:00:00", "open": 1.0, "high": 1.0,
         "low": 1.0, "close": 1.0, "volume": 1.0},
    ]
    block = grounding.format_grounding_block({"X": rows})
    assert "Instrument identity" not in block


# --------------------------------------------------------------------------- #
# _prefetch_grounding_data — fail-fast gating
# --------------------------------------------------------------------------- #

def _committee_run(target: str, *, preset_name: str = "crypto_committee") -> SwarmRun:
    return SwarmRun(
        id="run-anchor",
        preset_name=preset_name,
        created_at="2026-07-10T00:00:00+00:00",
        user_vars={"target": target, "timeframe": "72h swing"},
    )


def test_prefetch_raises_immediately_when_target_has_no_symbol(monkeypatch) -> None:
    """No recognizable symbol in {target} -> fail fast with NO network call."""
    def _must_not_be_called(*args, **kwargs):
        raise AssertionError("fetch_grounding_data must not be called")

    monkeypatch.setattr(grounding, "fetch_grounding_data", _must_not_be_called)
    runtime = SwarmRuntime(store=MagicMock(), max_workers=1)
    run = _committee_run("please analyze the market")

    with pytest.raises(grounding.InstrumentResolutionError) as excinfo:
        runtime._prefetch_grounding_data(run)
    assert "no recognizable instrument symbol" in str(excinfo.value)


def test_prefetch_raises_when_symbol_fails_to_resolve_market_data(monkeypatch) -> None:
    monkeypatch.setattr(grounding, "fetch_grounding_data", lambda symbols: {})
    runtime = SwarmRuntime(store=MagicMock(), max_workers=1)
    run = _committee_run("BTC-USDT")

    with pytest.raises(grounding.InstrumentResolutionError) as excinfo:
        runtime._prefetch_grounding_data(run)
    assert excinfo.value.symbol == "BTC-USDT"
    assert run.identity_anchor is None


def test_prefetch_sets_identity_anchor_on_successful_resolution(monkeypatch) -> None:
    rows = [{"trade_date": "2026-07-10T02:00:00", "open": 66000.0, "high": 68000.0,
              "low": 65000.0, "close": 67432.1, "volume": 1000.0}]
    monkeypatch.setattr(grounding, "fetch_grounding_data", lambda symbols: {"BTC-USDT": rows})
    store = MagicMock()
    runtime = SwarmRuntime(store=store, max_workers=1)
    run = _committee_run("BTC-USDT")

    runtime._prefetch_grounding_data(run)

    assert run.identity_anchor is not None
    assert "BTC-USDT" in run.identity_anchor
    assert "Do not substitute any other instrument" in run.identity_anchor
    assert run.grounding_data == {"BTC-USDT": rows}
    store.update_run.assert_called()


def test_prefetch_does_not_fail_fast_for_non_committee_presets(monkeypatch) -> None:
    """Gating check: a preset NOT in IDENTITY_ANCHOR_VARS keeps today's
    silent-drop behavior even when {target} is unresolvable free text."""
    monkeypatch.setattr(grounding, "fetch_grounding_data", lambda symbols: {})
    runtime = SwarmRuntime(store=MagicMock(), max_workers=1)
    run = _committee_run("please analyze the market", preset_name="investment_committee")

    runtime._prefetch_grounding_data(run)  # must not raise

    assert run.identity_anchor is None


# --------------------------------------------------------------------------- #
# _execute_run — end-to-end fail-fast for crypto_committee
# --------------------------------------------------------------------------- #

def test_execute_run_fails_at_start_when_target_unresolvable(tmp_path, monkeypatch) -> None:
    """No worker may ever see an ungrounded {target}: the run must fail
    before any task executes, with every task left cancelled."""
    monkeypatch.setattr(grounding, "fetch_grounding_data", lambda symbols: {})

    store = MagicMock()
    store.run_dir.return_value = tmp_path / "run"
    runtime = SwarmRuntime(store=store, max_workers=1)

    agent = SwarmAgentSpec(
        id="market_analyst", role="analyst", system_prompt="Analyze {upstream_context}",
    )
    task = SwarmTask(id="task-market", agent_id=agent.id, prompt_template="Analyze {target}.")
    run = SwarmRun(
        id="run-anchor-fail",
        preset_name="crypto_committee",
        created_at="2026-07-10T00:00:00+00:00",
        user_vars={"target": "BTC-USDT", "timeframe": "72h swing"},
        agents=[agent],
        tasks=[task],
    )

    def _must_not_run(**kwargs):
        raise AssertionError("no worker may run when the identity symbol failed to resolve")

    monkeypatch.setattr(runtime, "_run_worker_with_retries", _must_not_run)

    runtime._execute_run(run, threading.Event())

    assert run.status == RunStatus.failed
    assert run.tasks[0].status == TaskStatus.cancelled

    error_events = [
        call.args[1] for call in store.append_event.call_args_list
        if call.args[1].type == "run_error"
    ]
    assert any(e.data.get("phase") == "grounding" for e in error_events)
