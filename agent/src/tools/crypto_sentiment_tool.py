"""get_crypto_sentiment_data: pre-fetched, LLM-free crypto sentiment sources.

TradingAgents found that letting the sentiment analyst decide *when and how*
to fetch social data caused models to fabricate posts, quotes, and vote
counts once a fetch failed mid-reasoning. The fix is structural: fetch every
source in code, before the worker ever reasons about them, and hand back a
fixed envelope with an ``<unavailable>`` placeholder for any source that
failed — never a gap the worker is tempted to fill from memory.

Fetches exactly the three sources the sentiment analyst prompt used to pull
via ad-hoc ``read_url`` calls:
  1. alternative.me Fear & Greed Index (14-day series)
  2. r/CryptoCurrency top-week RSS
  3. StockTwits stream for the asset's ``<BASE>.X`` symbol

Each source fetch is fully independent: one failing has no effect on the
others. ``sources_available`` / ``sources_total`` let the analyst prompt
apply its existing confidence down-rating rule without re-deriving it.
"""

from __future__ import annotations

import json
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Callable

from backtest.loaders._http import resolve_min_interval, throttled_get, throttled_get_json
from src.agent.tools import BaseTool

logger = logging.getLogger(__name__)

UNAVAILABLE = "<unavailable>"

_TIMEOUT_S = 10.0

_FNG_URL = "https://api.alternative.me/fng/"
_FNG_HOST_KEY = "alternative-me-fng"
_FNG_MIN_INTERVAL_ENV = "VIBE_FNG_MIN_INTERVAL"
_FNG_DEFAULT_MIN_INTERVAL = 1.0
_FNG_LIMIT_DAYS = 14

_REDDIT_URL = "https://www.reddit.com/r/CryptoCurrency/top/.rss?t=week"
_REDDIT_HOST_KEY = "reddit-rss"
_REDDIT_MIN_INTERVAL_ENV = "VIBE_REDDIT_RSS_MIN_INTERVAL"
_REDDIT_DEFAULT_MIN_INTERVAL = 1.0
_REDDIT_MAX_ENTRIES = 10
_ATOM_NS = "{http://www.w3.org/2005/Atom}"

_STOCKTWITS_URL_TMPL = "https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json"
_STOCKTWITS_HOST_KEY = "stocktwits"
_STOCKTWITS_MIN_INTERVAL_ENV = "VIBE_STOCKTWITS_MIN_INTERVAL"
_STOCKTWITS_DEFAULT_MIN_INTERVAL = 1.0
_STOCKTWITS_MAX_MESSAGES = 20


def base_asset(symbol: str) -> str:
    """Map a loader-format symbol (``BTC-USDT``) to its base asset (``BTC``)."""
    return symbol.strip().upper().split("-")[0]


def stocktwits_symbol(symbol: str) -> str:
    """Map a loader-format symbol to its StockTwits ``<BASE>.X`` symbol."""
    return f"{base_asset(symbol)}.X"


# --------------------------------------------------------------------------- #
# Source 1 — alternative.me Fear & Greed Index
# --------------------------------------------------------------------------- #

def fetch_fear_greed() -> tuple[list[dict[str, Any]] | None, str | None]:
    """Fetch the 14-day Fear & Greed series. Returns (rows, None) or (None, reason)."""
    try:
        payload = throttled_get_json(
            _FNG_URL,
            host_key=_FNG_HOST_KEY,
            min_interval=resolve_min_interval(_FNG_MIN_INTERVAL_ENV, _FNG_DEFAULT_MIN_INTERVAL),
            params={"limit": str(_FNG_LIMIT_DAYS), "format": "json"},
            timeout=_TIMEOUT_S,
        )
    except Exception as exc:
        return None, f"alternative.me Fear & Greed request failed: {exc}"

    rows = payload.get("data") if isinstance(payload, dict) else None
    if not rows:
        return None, "alternative.me Fear & Greed response had no 'data' rows"
    return rows, None


def format_fear_greed(rows: list[dict[str, Any]]) -> str:
    lines = [
        f"### Fear & Greed Index — alternative.me ({len(rows)}-day series)",
        "",
        "| Date | Value | Classification |",
        "| --- | ---: | --- |",
    ]
    for row in rows:
        raw_ts = row.get("timestamp")
        try:
            date = datetime.fromtimestamp(int(raw_ts), tz=timezone.utc).date().isoformat()
        except (TypeError, ValueError):
            date = "?"
        lines.append(
            f"| {date} | {row.get('value', '?')} | {row.get('value_classification', '?')} |"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Source 2 — r/CryptoCurrency top-week RSS
# --------------------------------------------------------------------------- #

def fetch_reddit_top_week() -> tuple[list[dict[str, str]] | None, str | None]:
    """Fetch top-of-week posts from r/CryptoCurrency's Atom feed."""
    try:
        resp = throttled_get(
            _REDDIT_URL,
            host_key=_REDDIT_HOST_KEY,
            min_interval=resolve_min_interval(_REDDIT_MIN_INTERVAL_ENV, _REDDIT_DEFAULT_MIN_INTERVAL),
            headers={"Accept": "application/atom+xml"},
            timeout=_TIMEOUT_S,
        )
        resp.raise_for_status()
    except Exception as exc:
        return None, f"r/CryptoCurrency RSS request failed: {exc}"

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as exc:
        return None, f"r/CryptoCurrency RSS response was not valid XML: {exc}"

    entries: list[dict[str, str]] = []
    for entry in root.findall(f"{_ATOM_NS}entry")[:_REDDIT_MAX_ENTRIES]:
        title_el = entry.find(f"{_ATOM_NS}title")
        author_el = entry.find(f"{_ATOM_NS}author/{_ATOM_NS}name")
        link_el = entry.find(f"{_ATOM_NS}link")
        entries.append(
            {
                "title": (title_el.text or "").strip() if title_el is not None else "",
                "author": (author_el.text or "").strip() if author_el is not None else "",
                "link": (link_el.get("href") or "") if link_el is not None else "",
            }
        )

    if not entries:
        return None, "r/CryptoCurrency RSS response had zero entries"
    return entries, None


def format_reddit_top_week(entries: list[dict[str, str]]) -> str:
    lines = [f"### r/CryptoCurrency — Top This Week ({len(entries)} posts)", ""]
    for entry in entries:
        author = entry.get("author") or "unknown"
        lines.append(f"- {entry.get('title', '')} ({author}) — {entry.get('link', '')}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Source 3 — StockTwits stream
# --------------------------------------------------------------------------- #

def fetch_stocktwits(symbol: str) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Fetch the StockTwits message stream for ``<BASE>.X``."""
    st_symbol = stocktwits_symbol(symbol)
    try:
        payload = throttled_get_json(
            _STOCKTWITS_URL_TMPL.format(symbol=st_symbol),
            host_key=_STOCKTWITS_HOST_KEY,
            min_interval=resolve_min_interval(
                _STOCKTWITS_MIN_INTERVAL_ENV, _STOCKTWITS_DEFAULT_MIN_INTERVAL
            ),
            timeout=_TIMEOUT_S,
        )
    except Exception as exc:
        return None, f"StockTwits request failed for {st_symbol}: {exc}"

    messages = payload.get("messages") if isinstance(payload, dict) else None
    if not messages:
        return None, f"StockTwits response had no messages for {st_symbol}"
    return messages[:_STOCKTWITS_MAX_MESSAGES], None


def format_stocktwits(symbol: str, messages: list[dict[str, Any]]) -> str:
    st_symbol = stocktwits_symbol(symbol)
    lines = [f"### StockTwits — {st_symbol} ({len(messages)} messages)", ""]
    bulls = bears = 0
    for msg in messages:
        sentiment = (
            (msg.get("entities") or {}).get("sentiment") or {}
        ).get("basic")
        if sentiment == "Bullish":
            bulls += 1
        elif sentiment == "Bearish":
            bears += 1
        user = (msg.get("user") or {}).get("username", "?")
        body = (msg.get("body") or "").replace("\n", " ").strip()
        tag = f" [{sentiment}]" if sentiment else ""
        lines.append(f"- @{user}{tag}: {body}")
    lines.append("")
    lines.append(f"**Message sentiment tally:** {bulls} bullish / {bears} bearish / "
                  f"{len(messages) - bulls - bears} unlabeled")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Envelope assembly
# --------------------------------------------------------------------------- #

def build_sentiment_snapshot(
    symbol: str,
    *,
    fetch_fear_greed_fn: Callable[[], tuple] | None = None,
    fetch_reddit_fn: Callable[[], tuple] | None = None,
    fetch_stocktwits_fn: Callable[[str], tuple] | None = None,
) -> dict[str, Any]:
    """Fetch all three sentiment sources independently and build the envelope.

    Each ``fetch_*_fn`` is injectable so tests can force one source to fail
    while the others succeed, without touching the network. ``None`` (the
    default) resolves the module-level ``fetch_*`` function by global lookup
    at call time rather than binding it eagerly as a default argument value,
    so tests may monkeypatch this module's ``fetch_fear_greed`` /
    ``fetch_reddit_top_week`` / ``fetch_stocktwits`` and have callers that
    don't pass the ``fetch_*_fn`` overrides explicitly (e.g. the tool's
    ``execute``) still pick up the patched version.
    """
    if fetch_fear_greed_fn is None:
        fetch_fear_greed_fn = fetch_fear_greed
    if fetch_reddit_fn is None:
        fetch_reddit_fn = fetch_reddit_top_week
    if fetch_stocktwits_fn is None:
        fetch_stocktwits_fn = fetch_stocktwits
    symbol = symbol.strip().upper()
    reasons: dict[str, str] = {}

    fng_rows, fng_err = fetch_fear_greed_fn()
    fear_greed_block = format_fear_greed(fng_rows) if fng_rows else UNAVAILABLE
    if fng_err:
        reasons["fear_greed_index"] = fng_err

    reddit_entries, reddit_err = fetch_reddit_fn()
    reddit_block = format_reddit_top_week(reddit_entries) if reddit_entries else UNAVAILABLE
    if reddit_err:
        reasons["reddit_top_week"] = reddit_err

    st_messages, st_err = fetch_stocktwits_fn(symbol)
    stocktwits_block = format_stocktwits(symbol, st_messages) if st_messages else UNAVAILABLE
    if st_err:
        reasons["stocktwits_stream"] = st_err

    sources_available = sum(1 for v in (fng_rows, reddit_entries, st_messages) if v)

    envelope: dict[str, Any] = {
        "status": "ok",
        "symbol": symbol,
        "stocktwits_symbol": stocktwits_symbol(symbol),
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "fear_greed_index": fear_greed_block,
        "reddit_top_week": reddit_block,
        "stocktwits_stream": stocktwits_block,
        "sources_available": sources_available,
        "sources_total": 3,
    }
    if reasons:
        envelope["unavailable_reasons"] = reasons
    return envelope


class CryptoSentimentTool(BaseTool):
    """Pre-fetched crypto sentiment: Fear & Greed, Reddit top-week, StockTwits."""

    name = "get_crypto_sentiment_data"
    description = (
        "Fetch all three crypto sentiment sources in one deterministic, "
        "LLM-free call: alternative.me Fear & Greed Index (14-day), "
        "r/CryptoCurrency top-week RSS, and the StockTwits stream for the "
        "asset's <BASE>.X symbol. Each source is fetched independently and "
        "returned as a delimited text block, or '<unavailable>' if that "
        "source's fetch failed. Use 'sources_available' (0-3) to set your "
        "confidence: three=high ceiling, two=medium, one or zero=low. Never "
        "invent posts, quotes, or counts beyond what this tool returned."
    )
    parameters = {
        "type": "object",
        "properties": {
            "symbol": {
                "type": "string",
                "description": (
                    "Spot symbol in loader format, e.g. BTC-USDT. The base "
                    "asset (BTC) is mapped to its StockTwits <BASE>.X symbol."
                ),
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
        snapshot = build_sentiment_snapshot(symbol)
        return json.dumps(snapshot, ensure_ascii=False, indent=2)
