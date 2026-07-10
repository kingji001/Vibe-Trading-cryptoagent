"""get_verified_crypto_snapshot: deterministic, LLM-free OKX market snapshot.

TradingAgents' "no-data sentinel" pattern: rather than let an LLM worker
narrate numbers from OKX (funding rate, open interest, mark/index price) by
reading tool descriptions and free-text pages, this tool fetches all six
fields directly from OKX's public REST API — with a ccxt fallback on the
same exchange when the direct call fails — and returns a strict JSON
envelope where EACH field independently resolves to either a real value or
an instructive ``NO_DATA_AVAILABLE: <reason> — do not estimate this value``
sentinel. No field's failure blocks another field's fetch.

This is the committee's source of truth for exact numbers (price, funding,
OI, mark/index price). Worker prompts are told to report discrepancies
against other sources rather than reconcile them — see crypto_committee.yaml.

Replaces the ad-hoc `okx-market` skill bash scripts previously used to pull
funding rate / open interest data by hand.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from backtest.loaders._http import resolve_min_interval, throttled_get_json
from src.agent.tools import BaseTool

logger = logging.getLogger(__name__)

_OKX_BASE_URL = "https://www.okx.com/api/v5"
_HOST_KEY = "okx-public-snapshot"
_MIN_INTERVAL_ENV = "VIBE_OKX_SNAPSHOT_MIN_INTERVAL"
_DEFAULT_MIN_INTERVAL = 0.15
_TIMEOUT_S = 10.0

NO_DATA_PREFIX = "NO_DATA_AVAILABLE"

# Top-level JSON keys build_snapshot() returns that carry the six fields the
# brief calls out (last price+timestamp, 24h stats, funding, OI, mark price,
# index price). Preset prompts that treat this tool as the source of truth
# for exact numbers reference these names; a prompt-contract test asserts
# they stay in sync (see tests/test_crypto_committee_preset.py).
SNAPSHOT_FIELD_NAMES: tuple[str, ...] = (
    "last_price",
    "stats_24h",
    "funding_rate",
    "open_interest",
    "mark_price",
    "index_price",
)


def _sentinel(reason: str) -> str:
    """Build the instructive no-data sentinel for one field."""
    return f"{NO_DATA_PREFIX}: {reason} — do not estimate this value"


def _swap_inst_id(symbol: str) -> str:
    symbol = symbol.strip().upper()
    return symbol if symbol.endswith("-SWAP") else f"{symbol}-SWAP"


def _index_inst_id(symbol: str) -> str:
    base = symbol.strip().upper().split("-")[0]
    return f"{base}-USD"


def _min_interval() -> float:
    return resolve_min_interval(_MIN_INTERVAL_ENV, _DEFAULT_MIN_INTERVAL)


def _direct_get(path: str, params: dict[str, str]) -> Any:
    """Direct OKX public REST call, throttled and session-reused."""
    return throttled_get_json(
        f"{_OKX_BASE_URL}{path}",
        host_key=_HOST_KEY,
        min_interval=_min_interval(),
        params=params,
        timeout=_TIMEOUT_S,
    )


def _ccxt_get(method_name: str, params: dict[str, str]) -> Any:
    """ccxt fallback: OKX's implicit REST methods mirror the public API
    1:1 (same JSON shape as ``_direct_get``), so callers can reuse the same
    parsers regardless of which transport succeeded."""
    import ccxt

    exchange = ccxt.okx({"timeout": int(_TIMEOUT_S * 1000), "enableRateLimit": True})
    method = getattr(exchange, method_name)
    return method(params)


def _first_data_row(payload: Any) -> dict[str, Any]:
    """Extract data[0] from an OKX-shaped envelope, raising on any anomaly."""
    if not isinstance(payload, dict):
        raise ValueError("unexpected response shape (not a JSON object)")
    code = str(payload.get("code", ""))
    if code not in ("0", "0.0", ""):
        raise ValueError(f"OKX API error code={payload.get('code')!r} msg={payload.get('msg')!r}")
    rows = payload.get("data") or []
    if not rows:
        raise ValueError("empty data array")
    row = rows[0]
    if not isinstance(row, dict):
        raise ValueError("data[0] is not an object")
    return row


def _fetch_row(
    *,
    direct_path: str,
    direct_params: dict[str, str],
    ccxt_method: str,
    ccxt_params: dict[str, str],
    label: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Try the direct OKX REST endpoint, then the ccxt fallback.

    Returns ``(row, None)`` on success or ``(None, reason)`` on failure of
    BOTH transports. Never raises — every failure mode degrades to a
    sentinel reason string for the caller to render.
    """
    try:
        return _first_data_row(_direct_get(direct_path, direct_params)), None
    except Exception as direct_exc:
        logger.info("crypto_snapshot: direct fetch failed for %s: %s", label, direct_exc)
        try:
            return _first_data_row(_ccxt_get(ccxt_method, ccxt_params)), None
        except Exception as ccxt_exc:
            reason = (
                f"OKX REST and ccxt fallback both failed for {label} "
                f"(direct: {direct_exc}; ccxt: {ccxt_exc})"
            )
            logger.warning("crypto_snapshot: %s", reason)
            return None, reason


def _safe_float(row: dict[str, Any], key: str) -> float | None:
    raw = row.get(key)
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _ms_to_iso(raw: Any) -> str | None:
    if raw in (None, ""):
        return None
    try:
        ms = int(raw)
    except (TypeError, ValueError):
        return None
    return (
        datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _build_last_price(symbol: str, fetch_row: Callable[..., tuple]) -> tuple[Any, Any]:
    row, err = fetch_row(
        direct_path="/market/ticker",
        direct_params={"instId": symbol},
        ccxt_method="publicGetMarketTicker",
        ccxt_params={"instId": symbol},
        label="spot ticker",
    )
    if row is None:
        return _sentinel(err or "spot ticker fetch failed"), None
    price = _safe_float(row, "last")
    ts = _ms_to_iso(row.get("ts"))
    if price is None or ts is None:
        return _sentinel("spot ticker response missing 'last' or 'ts'"), row
    return {"value": price, "timestamp": ts}, row


def _build_stats_24h(row: dict[str, Any] | None, err_hint: str) -> Any:
    if row is None:
        return _sentinel(err_hint)
    fields = {
        "open": _safe_float(row, "open24h"),
        "high": _safe_float(row, "high24h"),
        "low": _safe_float(row, "low24h"),
        "vol_base": _safe_float(row, "vol24h"),
        "vol_quote": _safe_float(row, "volCcy24h"),
    }
    if any(v is None for v in fields.values()):
        return _sentinel("spot ticker response missing one or more 24h stat fields")
    return fields


def _build_funding_rate(symbol: str, fetch_row: Callable[..., tuple]) -> Any:
    swap_id = _swap_inst_id(symbol)
    row, err = fetch_row(
        direct_path="/public/funding-rate",
        direct_params={"instId": swap_id},
        ccxt_method="publicGetPublicFundingRate",
        ccxt_params={"instId": swap_id},
        label="funding rate",
    )
    if row is None:
        return _sentinel(err or "funding rate fetch failed")

    current_rate = _safe_float(row, "fundingRate")
    current_time = _ms_to_iso(row.get("fundingTime"))
    if current_rate is None or current_time is None:
        return _sentinel("funding rate response missing 'fundingRate' or 'fundingTime'")

    predicted_rate = _safe_float(row, "nextFundingRate")
    next_time = _ms_to_iso(row.get("nextFundingTime"))
    predicted: Any
    if predicted_rate is None:
        predicted = _sentinel(
            "OKX has not published a predicted rate for the next settlement"
        )
    else:
        predicted = predicted_rate

    return {
        "current_rate": current_rate,
        "current_settlement_time": current_time,
        "predicted_rate": predicted,
        "next_settlement_time": next_time or _sentinel("next settlement time not published"),
    }


def _build_open_interest(symbol: str, fetch_row: Callable[..., tuple]) -> Any:
    swap_id = _swap_inst_id(symbol)
    row, err = fetch_row(
        direct_path="/public/open-interest",
        direct_params={"instType": "SWAP", "instId": swap_id},
        ccxt_method="publicGetPublicOpenInterest",
        ccxt_params={"instType": "SWAP", "instId": swap_id},
        label="open interest",
    )
    if row is None:
        return _sentinel(err or "open interest fetch failed")

    contracts = _safe_float(row, "oi")
    value_ccy = _safe_float(row, "oiCcy")
    ts = _ms_to_iso(row.get("ts"))
    if contracts is None or value_ccy is None or ts is None:
        return _sentinel("open interest response missing 'oi', 'oiCcy', or 'ts'")
    value_usd = _safe_float(row, "oiUsd")
    result: dict[str, Any] = {
        "contracts": contracts,
        "value_ccy": value_ccy,
        "timestamp": ts,
    }
    if value_usd is not None:
        result["value_usd"] = value_usd
    return result


def _build_mark_price(symbol: str, fetch_row: Callable[..., tuple]) -> Any:
    swap_id = _swap_inst_id(symbol)
    row, err = fetch_row(
        direct_path="/public/mark-price",
        direct_params={"instType": "SWAP", "instId": swap_id},
        ccxt_method="publicGetPublicMarkPrice",
        ccxt_params={"instType": "SWAP", "instId": swap_id},
        label="mark price",
    )
    if row is None:
        return _sentinel(err or "mark price fetch failed")
    price = _safe_float(row, "markPx")
    ts = _ms_to_iso(row.get("ts"))
    if price is None or ts is None:
        return _sentinel("mark price response missing 'markPx' or 'ts'")
    return {"value": price, "timestamp": ts}


def _build_index_price(symbol: str, fetch_row: Callable[..., tuple]) -> Any:
    index_id = _index_inst_id(symbol)
    row, err = fetch_row(
        direct_path="/market/index-tickers",
        direct_params={"instId": index_id},
        ccxt_method="publicGetMarketIndexTickers",
        ccxt_params={"instId": index_id},
        label="index price",
    )
    if row is None:
        return _sentinel(err or "index price fetch failed")
    price = _safe_float(row, "idxPx")
    ts = _ms_to_iso(row.get("ts"))
    if price is None or ts is None:
        return _sentinel("index price response missing 'idxPx' or 'ts'")
    return {"value": price, "timestamp": ts}


def build_snapshot(symbol: str, *, fetch_row: Callable[..., tuple] | None = None) -> dict[str, Any]:
    """Build the verified crypto snapshot envelope for *symbol*.

    Every top-level field is fetched and evaluated independently: a failure
    fetching the funding rate has no effect on whether the mark price
    resolves, and so on. ``fetch_row`` is injectable for tests; ``None``
    (the default) resolves ``_fetch_row`` by module-global lookup at call
    time rather than binding it eagerly as a default argument value, so
    tests may monkeypatch ``crypto_snapshot_tool._fetch_row`` and have
    callers that don't pass ``fetch_row`` explicitly (e.g. the tool's
    ``execute``) still pick up the patched version.
    """
    if fetch_row is None:
        fetch_row = _fetch_row
    symbol = symbol.strip().upper()
    last_price, ticker_row = _build_last_price(symbol, fetch_row)
    ticker_err_hint = "spot ticker fetch failed" if ticker_row is None else ""
    stats_24h = _build_stats_24h(ticker_row, ticker_err_hint)

    return {
        "status": "ok",
        "symbol": symbol,
        "swap_inst_id": _swap_inst_id(symbol),
        "index_inst_id": _index_inst_id(symbol),
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "last_price": last_price,
        "stats_24h": stats_24h,
        "funding_rate": _build_funding_rate(symbol, fetch_row),
        "open_interest": _build_open_interest(symbol, fetch_row),
        "mark_price": _build_mark_price(symbol, fetch_row),
        "index_price": _build_index_price(symbol, fetch_row),
    }


class VerifiedCryptoSnapshotTool(BaseTool):
    """Deterministic, LLM-free OKX snapshot: price, funding, OI, mark/index."""

    name = "get_verified_crypto_snapshot"
    description = (
        "Fetch a deterministic, LLM-free market snapshot for a crypto instrument "
        "directly from OKX public REST endpoints (ccxt fallback on failure): "
        "last spot price + timestamp, 24h stats, perpetual funding rate "
        "(current + predicted), open interest, mark price, and index price. "
        "Every field independently resolves to a real value or a "
        "'NO_DATA_AVAILABLE: <reason> — do not estimate this value' sentinel. "
        "This is the SOURCE OF TRUTH for any exact number you cite — if another "
        "tool disagrees, report the discrepancy, never reconcile it."
    )
    parameters = {
        "type": "object",
        "properties": {
            "symbol": {
                "type": "string",
                "description": "Spot symbol in loader format, e.g. BTC-USDT.",
            }
        },
        "required": ["symbol"],
    }
    is_readonly = True

    def execute(self, **kwargs: Any) -> str:
        symbol = str(kwargs.get("symbol", "")).strip()
        if not symbol:
            return json.dumps(
                {"status": "error", "error": "symbol is required, e.g. BTC-USDT"},
                ensure_ascii=False,
            )
        snapshot = build_snapshot(symbol)
        return json.dumps(snapshot, ensure_ascii=False, indent=2)
