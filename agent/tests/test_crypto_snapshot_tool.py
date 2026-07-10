"""Tests for get_verified_crypto_snapshot: OKX-direct + ccxt-fallback,
per-field independent sentinel degradation.

All HTTP/ccxt access is mocked — no test ever reaches a live network. Fixture
payloads mirror real OKX response shapes captured from the documented public
endpoints (see agent/src/skills/okx-market/references/).
"""

from __future__ import annotations

import json

import pytest

from src.tools import crypto_snapshot_tool as mod
from src.tools.crypto_snapshot_tool import VerifiedCryptoSnapshotTool, build_snapshot

# --------------------------------------------------------------------------- #
# Real-shaped fixtures (captured from OKX's public REST API)
# --------------------------------------------------------------------------- #

SPOT_TICKER_ROW = {
    "instType": "SPOT",
    "instId": "BTC-USDT",
    "last": "64416.4",
    "lastSz": "0.00007099",
    "askPx": "64416.5",
    "askSz": "1.81986882",
    "bidPx": "64416.4",
    "bidSz": "2.19735232",
    "open24h": "62762.7",
    "high24h": "64500",
    "low24h": "62462.1",
    "volCcy24h": "580768643.35130223",
    "vol24h": "9137.50555104",
    "ts": "1783684961461",
    "sodUtc0": "63228.6",
    "sodUtc8": "62860.8",
}

FUNDING_ROW_NO_PREDICTED = {
    "formulaType": "withRate",
    "fundingRate": "0.0000604121295194",
    "fundingTime": "1783699200000",
    "instId": "BTC-USDT-SWAP",
    "instType": "SWAP",
    "nextFundingRate": "",
    "nextFundingTime": "1783728000000",
    "prevFundingTime": "1783670400000",
}

FUNDING_ROW_WITH_PREDICTED = {
    **FUNDING_ROW_NO_PREDICTED,
    "nextFundingRate": "0.0000512",
}

OI_ROW = {
    "instId": "BTC-USDT-SWAP",
    "instType": "SWAP",
    "oi": "3133489.89000001505",
    "oiCcy": "31334.8989000001505",
    "oiUsd": "2017265587.4246496888288",
    "ts": "1783684962507",
}

MARK_ROW = {
    "instId": "BTC-USDT-SWAP",
    "instType": "SWAP",
    "markPx": "64379.5",
    "ts": "1783684962609",
}

INDEX_ROW = {
    "instId": "BTC-USD",
    "idxPx": "64375.5",
    "high24h": "64448.4",
    "sodUtc0": "63181.5",
    "open24h": "62702.3",
    "low24h": "62413.4",
    "sodUtc8": "62812.4",
    "ts": "1783684961534",
}

_LABEL_TO_ROW = {
    "spot ticker": SPOT_TICKER_ROW,
    "funding rate": FUNDING_ROW_NO_PREDICTED,
    "open interest": OI_ROW,
    "mark price": MARK_ROW,
    "index price": INDEX_ROW,
}


def _all_succeed_fetch_row(*, label, **kwargs):
    return dict(_LABEL_TO_ROW[label]), None


def _fetch_row_with_failure(*failing_labels: str):
    """Build a fake fetch_row where the given labels fail, others succeed."""

    def _fake(*, label, **kwargs):
        if label in failing_labels:
            return None, f"{label} fetch failed: simulated network error"
        return dict(_LABEL_TO_ROW[label]), None

    return _fake


# --------------------------------------------------------------------------- #
# build_snapshot — all sources succeed
# --------------------------------------------------------------------------- #


class TestAllSourcesSucceed:
    def test_envelope_shape(self):
        snap = build_snapshot("BTC-USDT", fetch_row=_all_succeed_fetch_row)
        assert snap["status"] == "ok"
        assert snap["symbol"] == "BTC-USDT"
        assert snap["swap_inst_id"] == "BTC-USDT-SWAP"
        assert snap["index_inst_id"] == "BTC-USD"
        assert "fetched_at" in snap

    def test_last_price(self):
        snap = build_snapshot("BTC-USDT", fetch_row=_all_succeed_fetch_row)
        assert snap["last_price"] == {
            "value": pytest.approx(64416.4),
            "timestamp": "2026-07-10T12:02:41Z",
        }

    def test_stats_24h(self):
        snap = build_snapshot("BTC-USDT", fetch_row=_all_succeed_fetch_row)
        stats = snap["stats_24h"]
        assert stats["open"] == pytest.approx(62762.7)
        assert stats["high"] == pytest.approx(64500)
        assert stats["low"] == pytest.approx(62462.1)
        assert stats["vol_base"] == pytest.approx(9137.50555104)
        assert stats["vol_quote"] == pytest.approx(580768643.35130223)

    def test_funding_rate_predicted_unavailable_when_okx_omits_it(self):
        # Real OKX behavior: nextFundingRate is frequently "" until close to
        # settlement — this must degrade only the predicted_rate sub-field,
        # not the whole funding_rate field.
        snap = build_snapshot("BTC-USDT", fetch_row=_all_succeed_fetch_row)
        funding = snap["funding_rate"]
        assert funding["current_rate"] == pytest.approx(0.0000604121295194)
        assert funding["current_settlement_time"] == "2026-07-10T16:00:00Z"
        assert funding["predicted_rate"].startswith(mod.NO_DATA_PREFIX)
        assert "do not estimate this value" in funding["predicted_rate"]
        assert funding["next_settlement_time"] == "2026-07-11T00:00:00Z"

    def test_funding_rate_predicted_populates_when_present(self):
        def fetch_row(*, label, **kwargs):
            if label == "funding rate":
                return dict(FUNDING_ROW_WITH_PREDICTED), None
            return dict(_LABEL_TO_ROW[label]), None

        snap = build_snapshot("BTC-USDT", fetch_row=fetch_row)
        assert snap["funding_rate"]["predicted_rate"] == pytest.approx(0.0000512)

    def test_open_interest(self):
        snap = build_snapshot("BTC-USDT", fetch_row=_all_succeed_fetch_row)
        oi = snap["open_interest"]
        assert oi["contracts"] == pytest.approx(3133489.89000001505)
        assert oi["value_ccy"] == pytest.approx(31334.8989000001505)
        assert oi["value_usd"] == pytest.approx(2017265587.4246496888288)

    def test_mark_price(self):
        snap = build_snapshot("BTC-USDT", fetch_row=_all_succeed_fetch_row)
        assert snap["mark_price"]["value"] == pytest.approx(64379.5)

    def test_index_price(self):
        snap = build_snapshot("BTC-USDT", fetch_row=_all_succeed_fetch_row)
        assert snap["index_price"]["value"] == pytest.approx(64375.5)

    def test_lowercase_symbol_is_normalized(self):
        snap = build_snapshot("btc-usdt", fetch_row=_all_succeed_fetch_row)
        assert snap["symbol"] == "BTC-USDT"


# --------------------------------------------------------------------------- #
# Each source failing independently -> only that field degrades
# --------------------------------------------------------------------------- #


class TestIndependentSentinelDegradation:
    def test_spot_ticker_failure_sentinels_last_price_and_stats(self):
        snap = build_snapshot(
            "BTC-USDT", fetch_row=_fetch_row_with_failure("spot ticker")
        )
        assert isinstance(snap["last_price"], str)
        assert snap["last_price"].startswith(mod.NO_DATA_PREFIX)
        assert isinstance(snap["stats_24h"], str)
        assert snap["stats_24h"].startswith(mod.NO_DATA_PREFIX)
        # Everything else still resolves — one source's failure is isolated.
        assert isinstance(snap["funding_rate"], dict)
        assert isinstance(snap["open_interest"], dict)
        assert isinstance(snap["mark_price"], dict)
        assert isinstance(snap["index_price"], dict)

    def test_funding_rate_failure_only_sentinels_funding(self):
        snap = build_snapshot(
            "BTC-USDT", fetch_row=_fetch_row_with_failure("funding rate")
        )
        assert isinstance(snap["funding_rate"], str)
        assert snap["funding_rate"].startswith(mod.NO_DATA_PREFIX)
        assert isinstance(snap["last_price"], dict)
        assert isinstance(snap["stats_24h"], dict)
        assert isinstance(snap["open_interest"], dict)
        assert isinstance(snap["mark_price"], dict)
        assert isinstance(snap["index_price"], dict)

    def test_open_interest_failure_only_sentinels_oi(self):
        snap = build_snapshot(
            "BTC-USDT", fetch_row=_fetch_row_with_failure("open interest")
        )
        assert isinstance(snap["open_interest"], str)
        assert snap["open_interest"].startswith(mod.NO_DATA_PREFIX)
        assert isinstance(snap["last_price"], dict)
        assert isinstance(snap["funding_rate"], dict)
        assert isinstance(snap["mark_price"], dict)
        assert isinstance(snap["index_price"], dict)

    def test_mark_price_failure_only_sentinels_mark(self):
        snap = build_snapshot(
            "BTC-USDT", fetch_row=_fetch_row_with_failure("mark price")
        )
        assert isinstance(snap["mark_price"], str)
        assert snap["mark_price"].startswith(mod.NO_DATA_PREFIX)
        assert isinstance(snap["open_interest"], dict)
        assert isinstance(snap["index_price"], dict)

    def test_index_price_failure_only_sentinels_index(self):
        snap = build_snapshot(
            "BTC-USDT", fetch_row=_fetch_row_with_failure("index price")
        )
        assert isinstance(snap["index_price"], str)
        assert snap["index_price"].startswith(mod.NO_DATA_PREFIX)
        assert isinstance(snap["mark_price"], dict)
        assert isinstance(snap["open_interest"], dict)

    def test_all_sources_fail_every_field_sentinels(self):
        snap = build_snapshot(
            "BTC-USDT",
            fetch_row=_fetch_row_with_failure(
                "spot ticker", "funding rate", "open interest", "mark price", "index price"
            ),
        )
        for key in (
            "last_price", "stats_24h", "funding_rate",
            "open_interest", "mark_price", "index_price",
        ):
            assert isinstance(snap[key], str), key
            assert snap[key].startswith(mod.NO_DATA_PREFIX), key
            assert snap[key].endswith("do not estimate this value"), key

    def test_sentinel_format_is_instructive(self):
        snap = build_snapshot(
            "BTC-USDT", fetch_row=_fetch_row_with_failure("mark price")
        )
        sentinel = snap["mark_price"]
        assert sentinel.startswith("NO_DATA_AVAILABLE: ")
        assert sentinel.endswith(" — do not estimate this value")
        assert "simulated network error" in sentinel


# --------------------------------------------------------------------------- #
# _fetch_row — direct OKX REST -> ccxt fallback chain
# --------------------------------------------------------------------------- #


class TestFetchRowFallback:
    def test_uses_direct_result_when_direct_succeeds(self, monkeypatch):
        monkeypatch.setattr(mod, "_direct_get", lambda path, params: {"code": "0", "data": [{"a": 1}]})
        monkeypatch.setattr(
            mod, "_ccxt_get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ccxt should not be called"))
        )
        row, err = mod._fetch_row(
            direct_path="/x", direct_params={}, ccxt_method="m", ccxt_params={}, label="x"
        )
        assert row == {"a": 1}
        assert err is None

    def test_falls_back_to_ccxt_when_direct_fails(self, monkeypatch):
        def _boom(path, params):
            raise ConnectionError("direct down")

        monkeypatch.setattr(mod, "_direct_get", _boom)
        monkeypatch.setattr(mod, "_ccxt_get", lambda method, params: {"code": "0", "data": [{"b": 2}]})
        row, err = mod._fetch_row(
            direct_path="/x", direct_params={}, ccxt_method="m", ccxt_params={}, label="x"
        )
        assert row == {"b": 2}
        assert err is None

    def test_reason_returned_when_both_transports_fail(self, monkeypatch):
        monkeypatch.setattr(
            mod, "_direct_get", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("direct down"))
        )
        monkeypatch.setattr(
            mod, "_ccxt_get", lambda *a, **k: (_ for _ in ()).throw(TimeoutError("ccxt timed out"))
        )
        row, err = mod._fetch_row(
            direct_path="/x", direct_params={}, ccxt_method="m", ccxt_params={}, label="funding rate"
        )
        assert row is None
        assert "funding rate" in err
        assert "direct down" in err
        assert "ccxt timed out" in err

    def test_okx_error_code_treated_as_failure(self, monkeypatch):
        monkeypatch.setattr(
            mod, "_direct_get", lambda *a, **k: {"code": "51001", "msg": "Instrument ID does not exist"}
        )
        monkeypatch.setattr(
            mod, "_ccxt_get", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("also down"))
        )
        row, err = mod._fetch_row(
            direct_path="/x", direct_params={}, ccxt_method="m", ccxt_params={}, label="mark price"
        )
        assert row is None
        assert "mark price" in err

    def test_empty_data_array_treated_as_failure(self, monkeypatch):
        monkeypatch.setattr(mod, "_direct_get", lambda *a, **k: {"code": "0", "data": []})
        monkeypatch.setattr(
            mod, "_ccxt_get", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("also down"))
        )
        row, err = mod._fetch_row(
            direct_path="/x", direct_params={}, ccxt_method="m", ccxt_params={}, label="index price"
        )
        assert row is None


# --------------------------------------------------------------------------- #
# Symbol derivation helpers
# --------------------------------------------------------------------------- #


class TestSymbolHelpers:
    def test_swap_inst_id(self):
        assert mod._swap_inst_id("BTC-USDT") == "BTC-USDT-SWAP"
        assert mod._swap_inst_id("btc-usdt") == "BTC-USDT-SWAP"
        assert mod._swap_inst_id("BTC-USDT-SWAP") == "BTC-USDT-SWAP"

    def test_index_inst_id(self):
        assert mod._index_inst_id("BTC-USDT") == "BTC-USD"
        assert mod._index_inst_id("eth-usdt") == "ETH-USD"


# --------------------------------------------------------------------------- #
# VerifiedCryptoSnapshotTool — BaseTool contract
# --------------------------------------------------------------------------- #


class TestToolContract:
    def test_name_and_schema(self):
        tool = VerifiedCryptoSnapshotTool()
        assert tool.name == "get_verified_crypto_snapshot"
        assert tool.is_readonly is True
        assert tool.parameters["required"] == ["symbol"]

    def test_execute_requires_symbol(self):
        out = json.loads(VerifiedCryptoSnapshotTool().execute(symbol=""))
        assert out["status"] == "error"

    def test_execute_returns_valid_json_envelope(self, monkeypatch):
        monkeypatch.setattr(mod, "_fetch_row", _all_succeed_fetch_row)
        raw = VerifiedCryptoSnapshotTool().execute(symbol="BTC-USDT")
        out = json.loads(raw)
        assert out["status"] == "ok"
        assert out["symbol"] == "BTC-USDT"
        assert isinstance(out["last_price"], dict)

    def test_check_available_default_true(self):
        assert VerifiedCryptoSnapshotTool.check_available() is True
