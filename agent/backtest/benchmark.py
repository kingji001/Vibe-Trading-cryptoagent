"""Benchmark ticker resolution and fetch for backtest comparison.

Provides a lightweight, zero-dependency way to fetch benchmark reference
data given a set of strategy codes and a data source.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

from backtest.loaders.yfinance_loader import DataLoader as YfinanceLoader


# -------------------------------------------------------------------
# Benchmark map: market type → default ticker
# -------------------------------------------------------------------

MARKET_BENCHMARKS: dict[str, Optional[str]] = {
    "us_equity":  "SPY",
    "hk_equity":  "HK.03100",   # Hang Seng China Enterprises ETF
    "a_share":    "000300.SH",  # CSI 300 (China A-share core index)
    "crypto":     "BTC-USDT",
    "futures":    "ES.CME",      # E-mini S&P 500 futures
    "forex":      None,         # no universal benchmark
}


@dataclass
class BenchmarkResult:
    ticker:     str
    ret_series: pd.Series       # per-bar returns, index = timestamps
    total_ret: float          # total return over the period


def resolve_benchmark(
    strategy_codes: list[str],
    source:       str,
    start_date:   str,
    end_date:     str,
    interval:     str = "1D",
    explicit:     Optional[str] = None,
) -> Optional[BenchmarkResult]:
    """Resolve the appropriate benchmark ticker and fetch its return series.

    Args:
        strategy_codes: Instruments being backtested (used for market inference).
        source:         Data source name (tushare / yfinance / okx / akshare / ccxt).
        start_date:     Backtest start date.
        end_date:       Backtest end date.
        interval:       Bar interval (1m / 5m / 15m / 30m / 1H / 4H / 1D).
        explicit:       Override ticker (e.g. "SPY" passed via config).

    Returns:
        BenchmarkResult with return series and total return, or None if no
        benchmark applies (forex, or fetch failure).
    """
    ticker = _resolve_ticker(strategy_codes, source, explicit)
    if ticker is None:
        return None

    try:
        bench_df = _fetch_benchmark(ticker, start_date, end_date, interval)
    except Exception:
        return None

    if bench_df.empty or "close" not in bench_df.columns:
        return None

    close = bench_df["close"].dropna()
    if len(close) < 2:
        return None

    ret_series = close.pct_change().fillna(0.0)
    total_ret   = float((1 + ret_series).prod() - 1)

    return BenchmarkResult(ticker=ticker, ret_series=ret_series, total_ret=total_ret)


# -------------------------------------------------------------------
# Internal helpers
# -------------------------------------------------------------------

def _resolve_ticker(
    codes:     list[str],
    source:    str,
    explicit:  Optional[str],
) -> Optional[str]:
    """Pick the benchmark ticker to use."""

    if explicit:
        return explicit

    # Infer market from source + first code pattern
    market = _infer_market(codes, source)
    return MARKET_BENCHMARKS.get(market)


def _infer_market(codes: list[str], source: str) -> str:
    """Rough market inference from symbol patterns and source."""
    if not codes:
        return "us_equity"

    first = codes[0].upper()

    if source in ("okx", "ccxt") or "-" in first or "/" in first:
        return "crypto"
    if first.endswith(".US"):
        return "us_equity"
    if first.endswith(".HK"):
        return "hk_equity"
    if source in ("tushare", "akshare"):
        if first.isdigit() and len(first) == 6:
            return "a_share"
        if first.startswith(("IF", "IC", "IH", "IM", "T", "TF")):
            return "futures"
        return "a_share"

    return "us_equity"


def _is_crypto_ticker(ticker: str) -> bool:
    """OKX/ccxt-format tickers (e.g. ``BTC-USDT``) are the only benchmark
    tickers with a ``-`` or ``/`` in ``MARKET_BENCHMARKS`` — every other
    market's default (``SPY``, ``HK.03100``, ``000300.SH``, ``ES.CME``) uses
    a dot or no separator. Mirrors ``_infer_market``'s symbol heuristic."""
    upper = ticker.upper()
    return "-" in upper or "/" in upper


def _fetch_benchmark(
    ticker:    str,
    start_date: str,
    end_date:   str,
    interval:   str,
    market:    Optional[str] = None,
) -> pd.DataFrame:
    """Fetch benchmark OHLCV data.

    Crypto tickers route through the backtest loader registry (okx -> ccxt),
    exactly like the committee decision journal's alpha path
    (``src.tools.committee_journal_tool._loader_fetch_bars``) — yfinance
    cannot resolve every OKX-format symbol, so crypto benchmark comparison
    was previously silently broken (fetch failure -> ``resolve_benchmark``
    swallows the exception and returns ``None``). Every other market keeps
    using yfinance, unchanged.

    Args:
        ticker: Benchmark ticker to fetch.
        start_date: Start date (YYYY-MM-DD).
        end_date: End date (YYYY-MM-DD).
        interval: Bar interval (1m/5m/15m/30m/1H/4H/1D).
        market: Optional explicit market override; inferred from the
            ticker's own shape when omitted (see ``_is_crypto_ticker``).
    """
    if market is None:
        market = "crypto" if _is_crypto_ticker(ticker) else "us_equity"

    if market == "crypto":
        from backtest.loaders.registry import fetch_ohlcv_with_fallback

        return fetch_ohlcv_with_fallback(
            ["okx", "ccxt"], ticker, start_date, end_date, interval=interval
        )

    loader = YfinanceLoader()
    result = loader.fetch([ticker], start_date, end_date, interval=interval)

    if isinstance(result, dict):
        df = result.get(ticker)
    elif isinstance(result, pd.DataFrame):
        df = result
    else:
        return pd.DataFrame()

    if df is None or (isinstance(df, pd.DataFrame) and df.empty):
        return pd.DataFrame()

    return df