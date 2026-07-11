"""Tests for get_crypto_sentiment_data: three sources fetched in code,
each independently degrading to '<unavailable>' on failure.

All HTTP access is mocked — no test ever reaches a live network.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.tools import crypto_sentiment_tool as mod
from src.tools.crypto_sentiment_tool import CryptoSentimentTool, build_sentiment_snapshot

# --------------------------------------------------------------------------- #
# Fixtures modeled on real API shapes
# --------------------------------------------------------------------------- #

FNG_ROWS = [
    {"value": "54", "value_classification": "Neutral", "timestamp": "1783651200"},
    {"value": "48", "value_classification": "Neutral", "timestamp": "1783564800"},
]

REDDIT_ATOM_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>BTC breaks 65k, thoughts?</title>
    <link href="https://www.reddit.com/r/CryptoCurrency/comments/abc123/"/>
    <author><name>/u/cryptouser1</name></author>
    <published>2026-07-08T12:00:00+00:00</published>
  </entry>
  <entry>
    <title>Weekly discussion thread</title>
    <link href="https://www.reddit.com/r/CryptoCurrency/comments/def456/"/>
    <author><name>/u/cryptouser2</name></author>
    <published>2026-07-07T09:00:00+00:00</published>
  </entry>
</feed>
"""

ST_MESSAGES = [
    {
        "id": 1,
        "body": "BTC breaking out here, huge volume",
        "user": {"username": "trader1"},
        "entities": {"sentiment": {"basic": "Bullish"}},
    },
    {
        "id": 2,
        "body": "careful, this looks like a trap",
        "user": {"username": "trader2"},
        "entities": {"sentiment": {"basic": "Bearish"}},
    },
    {
        "id": 3,
        "body": "just watching for now",
        "user": {"username": "trader3"},
        "entities": {},
    },
]


def _ok_fear_greed():
    return list(FNG_ROWS), None


def _ok_reddit():
    return [
        {"title": "BTC breaks 65k, thoughts?", "author": "/u/cryptouser1", "link": "https://x/abc"},
        {"title": "Weekly discussion thread", "author": "/u/cryptouser2", "link": "https://x/def"},
    ], None


def _ok_stocktwits(symbol):
    return list(ST_MESSAGES), None


def _fail(reason):
    def _fn(*args, **kwargs):
        return None, reason
    return _fn


# --------------------------------------------------------------------------- #
# Symbol helpers
# --------------------------------------------------------------------------- #


class TestSymbolHelpers:
    def test_base_asset(self):
        assert mod.base_asset("BTC-USDT") == "BTC"
        assert mod.base_asset("eth-usdt") == "ETH"

    def test_stocktwits_symbol(self):
        assert mod.stocktwits_symbol("BTC-USDT") == "BTC.X"
        assert mod.stocktwits_symbol("ETH-USDT") == "ETH.X"


# --------------------------------------------------------------------------- #
# build_sentiment_snapshot — all sources succeed
# --------------------------------------------------------------------------- #


class TestAllSourcesSucceed:
    def test_envelope_shape(self):
        snap = build_sentiment_snapshot(
            "BTC-USDT",
            fetch_fear_greed_fn=_ok_fear_greed,
            fetch_reddit_fn=_ok_reddit,
            fetch_stocktwits_fn=_ok_stocktwits,
        )
        assert snap["status"] == "ok"
        assert snap["symbol"] == "BTC-USDT"
        assert snap["stocktwits_symbol"] == "BTC.X"
        assert snap["sources_available"] == 3
        assert snap["sources_total"] == 3
        assert "unavailable_reasons" not in snap

    def test_fear_greed_block_content(self):
        snap = build_sentiment_snapshot(
            "BTC-USDT",
            fetch_fear_greed_fn=_ok_fear_greed,
            fetch_reddit_fn=_ok_reddit,
            fetch_stocktwits_fn=_ok_stocktwits,
        )
        block = snap["fear_greed_index"]
        assert "Fear & Greed" in block
        assert "54" in block
        assert "Neutral" in block
        assert "2026-07-10" in block

    def test_reddit_block_content(self):
        snap = build_sentiment_snapshot(
            "BTC-USDT",
            fetch_fear_greed_fn=_ok_fear_greed,
            fetch_reddit_fn=_ok_reddit,
            fetch_stocktwits_fn=_ok_stocktwits,
        )
        block = snap["reddit_top_week"]
        assert "r/CryptoCurrency" in block
        assert "BTC breaks 65k, thoughts?" in block
        assert "/u/cryptouser1" in block

    def test_stocktwits_block_content_and_tally(self):
        snap = build_sentiment_snapshot(
            "BTC-USDT",
            fetch_fear_greed_fn=_ok_fear_greed,
            fetch_reddit_fn=_ok_reddit,
            fetch_stocktwits_fn=_ok_stocktwits,
        )
        block = snap["stocktwits_stream"]
        assert "BTC.X" in block
        assert "@trader1" in block
        assert "1 bullish / 1 bearish / 1 unlabeled" in block


# --------------------------------------------------------------------------- #
# Each source failing independently -> '<unavailable>' for that block only
# --------------------------------------------------------------------------- #


class TestIndependentUnavailableDegradation:
    def test_fear_greed_failure_isolated(self):
        snap = build_sentiment_snapshot(
            "BTC-USDT",
            fetch_fear_greed_fn=_fail("alternative.me timed out"),
            fetch_reddit_fn=_ok_reddit,
            fetch_stocktwits_fn=_ok_stocktwits,
        )
        assert snap["fear_greed_index"] == mod.UNAVAILABLE
        assert snap["reddit_top_week"] != mod.UNAVAILABLE
        assert snap["stocktwits_stream"] != mod.UNAVAILABLE
        assert snap["sources_available"] == 2
        assert snap["unavailable_reasons"] == {"fear_greed_index": "alternative.me timed out"}

    def test_reddit_failure_isolated(self):
        snap = build_sentiment_snapshot(
            "BTC-USDT",
            fetch_fear_greed_fn=_ok_fear_greed,
            fetch_reddit_fn=_fail("reddit blocked the request (403)"),
            fetch_stocktwits_fn=_ok_stocktwits,
        )
        assert snap["reddit_top_week"] == mod.UNAVAILABLE
        assert snap["fear_greed_index"] != mod.UNAVAILABLE
        assert snap["stocktwits_stream"] != mod.UNAVAILABLE
        assert snap["sources_available"] == 2
        assert "reddit_top_week" in snap["unavailable_reasons"]

    def test_stocktwits_failure_isolated(self):
        snap = build_sentiment_snapshot(
            "BTC-USDT",
            fetch_fear_greed_fn=_ok_fear_greed,
            fetch_reddit_fn=_ok_reddit,
            fetch_stocktwits_fn=_fail("stocktwits blocked the request (403)"),
        )
        assert snap["stocktwits_stream"] == mod.UNAVAILABLE
        assert snap["fear_greed_index"] != mod.UNAVAILABLE
        assert snap["reddit_top_week"] != mod.UNAVAILABLE
        assert snap["sources_available"] == 2
        assert "stocktwits_stream" in snap["unavailable_reasons"]

    def test_all_sources_fail(self):
        snap = build_sentiment_snapshot(
            "BTC-USDT",
            fetch_fear_greed_fn=_fail("a"),
            fetch_reddit_fn=_fail("b"),
            fetch_stocktwits_fn=_fail("c"),
        )
        assert snap["fear_greed_index"] == mod.UNAVAILABLE
        assert snap["reddit_top_week"] == mod.UNAVAILABLE
        assert snap["stocktwits_stream"] == mod.UNAVAILABLE
        assert snap["sources_available"] == 0
        assert len(snap["unavailable_reasons"]) == 3


# --------------------------------------------------------------------------- #
# Low-level fetchers — real HTTP layer mocked
# --------------------------------------------------------------------------- #


class TestFetchFearGreed:
    def test_success(self, monkeypatch):
        monkeypatch.setattr(mod, "throttled_get_json", lambda *a, **k: {"data": list(FNG_ROWS)})
        rows, err = mod.fetch_fear_greed()
        assert rows == FNG_ROWS
        assert err is None

    def test_request_exception(self, monkeypatch):
        def _boom(*a, **k):
            raise ConnectionError("dns failure")

        monkeypatch.setattr(mod, "throttled_get_json", _boom)
        rows, err = mod.fetch_fear_greed()
        assert rows is None
        assert "dns failure" in err

    def test_missing_data_key(self, monkeypatch):
        monkeypatch.setattr(mod, "throttled_get_json", lambda *a, **k: {"metadata": {}})
        rows, err = mod.fetch_fear_greed()
        assert rows is None
        assert err is not None


class TestFetchRedditTopWeek:
    def test_success(self, monkeypatch):
        resp = SimpleNamespace(
            content=REDDIT_ATOM_XML.encode("utf-8"),
            raise_for_status=lambda: None,
        )
        monkeypatch.setattr(mod, "throttled_get", lambda *a, **k: resp)
        entries, err = mod.fetch_reddit_top_week()
        assert err is None
        assert len(entries) == 2
        assert entries[0]["title"] == "BTC breaks 65k, thoughts?"
        assert entries[0]["author"] == "/u/cryptouser1"

    def test_http_error(self, monkeypatch):
        def _boom(*a, **k):
            raise ConnectionError("blocked (403)")

        monkeypatch.setattr(mod, "throttled_get", _boom)
        entries, err = mod.fetch_reddit_top_week()
        assert entries is None
        assert "blocked (403)" in err

    def test_malformed_xml(self, monkeypatch):
        resp = SimpleNamespace(content=b"<not valid xml", raise_for_status=lambda: None)
        monkeypatch.setattr(mod, "throttled_get", lambda *a, **k: resp)
        entries, err = mod.fetch_reddit_top_week()
        assert entries is None
        assert "XML" in err

    def test_zero_entries(self, monkeypatch):
        empty_feed = b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>'
        resp = SimpleNamespace(content=empty_feed, raise_for_status=lambda: None)
        monkeypatch.setattr(mod, "throttled_get", lambda *a, **k: resp)
        entries, err = mod.fetch_reddit_top_week()
        assert entries is None
        assert "zero entries" in err


class TestFetchStocktwits:
    def test_success(self, monkeypatch):
        captured = {}

        def _fake(url, **kwargs):
            captured["url"] = url
            return {"messages": list(ST_MESSAGES)}

        monkeypatch.setattr(mod, "throttled_get_json", _fake)
        messages, err = mod.fetch_stocktwits("BTC-USDT")
        assert err is None
        assert len(messages) == 3
        assert "BTC.X" in captured["url"]

    def test_request_exception(self, monkeypatch):
        def _boom(*a, **k):
            raise TimeoutError("read timed out")

        monkeypatch.setattr(mod, "throttled_get_json", _boom)
        messages, err = mod.fetch_stocktwits("BTC-USDT")
        assert messages is None
        assert "read timed out" in err

    def test_no_messages(self, monkeypatch):
        monkeypatch.setattr(mod, "throttled_get_json", lambda *a, **k: {"messages": []})
        messages, err = mod.fetch_stocktwits("BTC-USDT")
        assert messages is None
        assert "BTC.X" in err


# --------------------------------------------------------------------------- #
# CryptoSentimentTool — BaseTool contract
# --------------------------------------------------------------------------- #


class TestToolContract:
    def test_name_and_schema(self):
        tool = CryptoSentimentTool()
        assert tool.name == "get_crypto_sentiment_data"
        assert tool.is_readonly is True
        assert tool.parameters["required"] == ["symbol"]

    def test_execute_requires_symbol(self):
        out = json.loads(CryptoSentimentTool().execute(symbol=""))
        assert out["status"] == "error"

    def test_execute_returns_valid_json_envelope(self, monkeypatch):
        monkeypatch.setattr(mod, "fetch_fear_greed", _ok_fear_greed)
        monkeypatch.setattr(mod, "fetch_reddit_top_week", _ok_reddit)
        monkeypatch.setattr(mod, "fetch_stocktwits", _ok_stocktwits)
        raw = CryptoSentimentTool().execute(symbol="BTC-USDT")
        out = json.loads(raw)
        assert out["status"] == "ok"
        assert out["sources_available"] == 3

    def test_execute_degrades_gracefully_when_all_sources_fail(self, monkeypatch):
        monkeypatch.setattr(mod, "fetch_fear_greed", _fail("down"))
        monkeypatch.setattr(mod, "fetch_reddit_top_week", _fail("down"))
        monkeypatch.setattr(mod, "fetch_stocktwits", _fail("down"))
        raw = CryptoSentimentTool().execute(symbol="BTC-USDT")
        out = json.loads(raw)
        assert out["status"] == "ok"
        assert out["sources_available"] == 0
        assert out["fear_greed_index"] == mod.UNAVAILABLE
        assert out["reddit_top_week"] == mod.UNAVAILABLE
        assert out["stocktwits_stream"] == mod.UNAVAILABLE
