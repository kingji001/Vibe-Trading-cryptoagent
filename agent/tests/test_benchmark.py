"""Tests for backtest/benchmark.py crypto benchmark routing (Phase 6).

``_fetch_benchmark`` previously fetched every ticker via yfinance only, which
cannot resolve OKX-format symbols like ``BTC-USDT`` — so a crypto backtest's
benchmark comparison silently produced no benchmark (fetch failure ->
``resolve_benchmark`` catches the exception and returns ``None``). These
tests pin the fix: crypto tickers must route through the backtest loader
registry (okx -> ccxt), exactly like the committee decision journal's alpha
path (``src/tools/committee_journal_tool.py::_loader_fetch_bars``), while
every other market keeps using yfinance unchanged. No real network calls —
loaders are patched into the registry.
"""

from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest

from backtest.benchmark import _fetch_benchmark, resolve_benchmark
from backtest.loaders.registry import LOADER_REGISTRY, _ensure_registered

# Force the lazy one-time loader-module import to happen now, before any test
# patches LOADER_REGISTRY. _ensure_registered() populates real loader classes
# (via each module's @register decorator) directly into the live registry
# dict; if that first-ever call happened *inside* a patch.dict(..., clear=True)
# block below, the real "okx"/"ccxt" classes would silently overwrite our
# fakes mid-test (patch.dict only restores on exit, it does not protect
# mutations during the `with` body). Running it here, once, up front makes
# these tests independent of suite run order.
_ensure_registered()


def _ohlcv_frame() -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=3, freq="D")
    return pd.DataFrame(
        {
            "open": [100.0, 101.0, 103.0],
            "high": [102.0, 104.0, 105.0],
            "low": [99.0, 100.0, 102.0],
            "close": [101.0, 103.0, 104.0],
            "volume": [10.0, 11.0, 12.0],
        },
        index=idx,
    )


class _FakeOkxLoader:
    name = "okx"
    markets = {"crypto"}
    requires_auth = False
    calls: list[tuple] = []

    def is_available(self) -> bool:
        return True

    def fetch(self, codes, start_date, end_date, fields=None, interval="1D"):
        _FakeOkxLoader.calls.append((tuple(codes), start_date, end_date, interval))
        return {codes[0]: _ohlcv_frame()}


class _FakeEmptyOkxLoader:
    """Mimics OKX returning nothing for the symbol (e.g. delisted/typo)."""

    name = "okx"
    markets = {"crypto"}
    requires_auth = False

    def is_available(self) -> bool:
        return True

    def fetch(self, codes, start_date, end_date, fields=None, interval="1D"):
        return {}


class _FakeCcxtLoader:
    name = "ccxt"
    markets = {"crypto"}
    requires_auth = False
    calls: list[tuple] = []

    def is_available(self) -> bool:
        return True

    def fetch(self, codes, start_date, end_date, fields=None, interval="1D"):
        _FakeCcxtLoader.calls.append((tuple(codes), start_date, end_date, interval))
        return {codes[0]: _ohlcv_frame()}


class _FakeUnavailableOkxLoader:
    name = "okx"
    markets = {"crypto"}
    requires_auth = False

    def is_available(self) -> bool:
        return False

    def fetch(self, codes, start_date, end_date, fields=None, interval="1D"):
        raise AssertionError("must not be called when unavailable")


@pytest.fixture(autouse=True)
def _reset_call_logs():
    _FakeOkxLoader.calls = []
    _FakeCcxtLoader.calls = []
    yield


def test_fetch_benchmark_crypto_routes_through_okx_loader():
    with patch.dict(LOADER_REGISTRY, {"okx": _FakeOkxLoader, "ccxt": _FakeCcxtLoader}, clear=True):
        df = _fetch_benchmark("BTC-USDT", "2026-01-01", "2026-01-03", "1D", market="crypto")

    assert not df.empty
    assert "close" in df.columns
    assert _FakeOkxLoader.calls == [(("BTC-USDT",), "2026-01-01", "2026-01-03", "1D")]
    assert _FakeCcxtLoader.calls == []  # okx succeeded; ccxt never tried


def test_fetch_benchmark_crypto_falls_back_to_ccxt_when_okx_empty():
    with patch.dict(
        LOADER_REGISTRY, {"okx": _FakeEmptyOkxLoader, "ccxt": _FakeCcxtLoader}, clear=True
    ):
        df = _fetch_benchmark("BTC-USDT", "2026-01-01", "2026-01-03", "1D", market="crypto")

    assert not df.empty
    assert _FakeCcxtLoader.calls == [(("BTC-USDT",), "2026-01-01", "2026-01-03", "1D")]


def test_fetch_benchmark_crypto_falls_back_to_ccxt_when_okx_unavailable():
    with patch.dict(
        LOADER_REGISTRY, {"okx": _FakeUnavailableOkxLoader, "ccxt": _FakeCcxtLoader}, clear=True
    ):
        df = _fetch_benchmark("BTC-USDT", "2026-01-01", "2026-01-03", "1D", market="crypto")

    assert not df.empty
    assert _FakeCcxtLoader.calls


def test_resolve_benchmark_crypto_end_to_end():
    """resolve_benchmark infers BTC-USDT for a crypto codes list and computes
    a return series from the loader-routed frame (no network, no yfinance)."""
    with patch.dict(LOADER_REGISTRY, {"okx": _FakeOkxLoader, "ccxt": _FakeCcxtLoader}, clear=True):
        result = resolve_benchmark(
            strategy_codes=["ETH-USDT"],
            source="okx",
            start_date="2026-01-01",
            end_date="2026-01-03",
            interval="1D",
        )

    assert result is not None
    assert result.ticker == "BTC-USDT"
    assert len(result.ret_series) == 3
    assert result.total_ret == pytest.approx(104.0 / 101.0 - 1.0)


def test_fetch_benchmark_explicit_yfinance_ticker_falls_back(monkeypatch):
    """Regression: an explicitly-passed ``BTC-USD`` is crypto-shaped (routes
    through the okx/ccxt loader path) but is a valid *yfinance* ticker that
    worked before this branch. When the loader path fails, ``_fetch_benchmark``
    must fall back to yfinance rather than silently losing the benchmark."""
    from backtest.loaders.base import NoAvailableSourceError

    calls = []

    class _FakeYfinance:
        def fetch(self, codes, start_date, end_date, interval="1D"):
            calls.append((tuple(codes), start_date, end_date, interval))
            return {codes[0]: _ohlcv_frame()}

    def _boom(*args, **kwargs):
        raise NoAvailableSourceError("no crypto loader resolves BTC-USD")

    monkeypatch.setattr("backtest.benchmark.YfinanceLoader", _FakeYfinance)
    monkeypatch.setattr(
        "backtest.loaders.registry.fetch_ohlcv_with_fallback", _boom
    )

    df = _fetch_benchmark("BTC-USD", "2026-01-01", "2026-01-03", "1D")

    assert not df.empty
    assert "close" in df.columns
    assert calls == [(("BTC-USD",), "2026-01-01", "2026-01-03", "1D")]


def test_fetch_benchmark_non_crypto_still_uses_yfinance(monkeypatch):
    """Guardrail: the routing fix must not touch the existing yfinance path
    for equities/other markets."""
    calls = []

    class _FakeYfinance:
        def fetch(self, codes, start_date, end_date, interval="1D"):
            calls.append((tuple(codes), start_date, end_date, interval))
            return {codes[0]: _ohlcv_frame()}

    monkeypatch.setattr("backtest.benchmark.YfinanceLoader", _FakeYfinance)
    df = _fetch_benchmark("SPY", "2026-01-01", "2026-01-03", "1D", market="us_equity")

    assert not df.empty
    assert calls == [(("SPY",), "2026-01-01", "2026-01-03", "1D")]
