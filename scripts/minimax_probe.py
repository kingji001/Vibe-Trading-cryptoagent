#!/usr/bin/env python3
"""Phase 0 validation probes for the MiniMax M3 migration.

Empirically resolves the unknowns that pick "Path A" (OpenAI-compatible
``/v1/chat/completions``, Subscription Key) vs "Path B" (Anthropic-compatible
``/anthropic/v1/messages``) before any integration code is written. See
``docs/minimax-migration-notes.md`` for the write-up this script feeds.

Requires ``MINIMAX_API_KEY`` in the environment. No other dependency beyond
the stdlib and ``httpx`` (already a repo dependency).

Usage:
    python scripts/minimax_probe.py auth
    python scripts/minimax_probe.py models [--endpoint openai|anthropic]
    python scripts/minimax_probe.py reasoning
    python scripts/minimax_probe.py concurrency [--endpoint openai|anthropic]
    python scripts/minimax_probe.py all
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.parse import urlparse

import httpx

# Mainland domain — the default account this repo runs against lives here,
# not on the global api.minimax.io domain. Keep in sync with the fallback
# baked into agent/src/providers/llm.py:_minimax_base_url.
_DEFAULT_MINIMAX_BASE_URL = "https://api.minimaxi.com/v1"


def _minimax_root_url() -> str:
    """Return ``scheme://host`` for the configured MiniMax deployment.

    Reads ``MINIMAX_BASE_URL`` — the same env var the real adapter resolves
    in ``agent/src/providers/llm.py:_minimax_base_url`` — so this script can
    never silently probe the wrong host again. Falls back to the mainland
    domain (not ``api.minimax.io``) when the var is unset, matching what
    production actually uses.
    """
    base = os.environ.get("MINIMAX_BASE_URL", "").strip() or _DEFAULT_MINIMAX_BASE_URL
    parsed = urlparse(base)
    return f"{parsed.scheme}://{parsed.netloc}"


_MINIMAX_ROOT = _minimax_root_url()
OPENAI_CHAT_URL = f"{_MINIMAX_ROOT}/v1/chat/completions"
ANTHROPIC_MESSAGES_URL = f"{_MINIMAX_ROOT}/anthropic/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

DEFAULT_TIMEOUT = 30.0
# MiniMax requires temperature > 0; the platform docs list 1.0 as the default.
TEMPERATURE = 1.0

MODELS_TO_PROBE = ["MiniMax-M3", "MiniMax-M2.7-highspeed", "MiniMax-M2.5-highspeed"]

# A deliberately trivial tool so the model has an easy, cheap tool call to make.
_OPENAI_TOOL = {
    "type": "function",
    "function": {
        "name": "get_time",
        "description": "Return the current UTC time. Call this to answer the user's question.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}
_ANTHROPIC_TOOL = {
    "name": "get_time",
    "description": "Return the current UTC time. Call this to answer the user's question.",
    "input_schema": {"type": "object", "properties": {}, "required": []},
}

_TOOL_PROMPT = "What time is it? Use the get_time tool, then tell me."


def require_api_key() -> str:
    """Return MINIMAX_API_KEY or exit with a clear message.

    Returns:
        The API key value.

    Raises:
        SystemExit: If MINIMAX_API_KEY is unset or blank.
    """
    key = os.environ.get("MINIMAX_API_KEY", "").strip()
    if not key:
        print(
            "ERROR: MINIMAX_API_KEY is not set.\n"
            "Export a MiniMax API key (Subscription Key or pay-as-you-go key) "
            "before running probes, e.g.\n"
            "  export MINIMAX_API_KEY=sk-...\n"
            "  python scripts/minimax_probe.py all",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return key


def _openai_headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _anthropic_headers(key: str) -> dict[str, str]:
    return {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}


def _headers_for(endpoint: str, key: str) -> dict[str, str]:
    return _anthropic_headers(key) if endpoint == "anthropic" else _openai_headers(key)


def _url_for(endpoint: str) -> str:
    return ANTHROPIC_MESSAGES_URL if endpoint == "anthropic" else OPENAI_CHAT_URL


def _print_header(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def _request(
    client: httpx.Client,
    label: str,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
) -> tuple[httpx.Response | None, bool]:
    """POST body, print status + error shape on failure, return (response, ok)."""
    print(f"\n--- {label} ---")
    print(f"POST {url}")
    try:
        resp = client.post(url, headers=headers, json=body, timeout=DEFAULT_TIMEOUT)
    except httpx.HTTPError as exc:
        print(f"TRANSPORT ERROR: {exc!r}")
        return None, False
    ok = resp.status_code == 200
    print(f"status={resp.status_code} ({'OK' if ok else 'FAIL'})")
    if not ok:
        try:
            print("error body:")
            print(json.dumps(resp.json(), indent=2)[:1500])
        except ValueError:
            print("error body (non-JSON, first 500 chars):")
            print(resp.text[:500])
    return resp, ok


def _trivial_body(model: str, tag: str) -> dict[str, Any]:
    return {
        "model": model,
        "temperature": TEMPERATURE,
        "max_tokens": 16,
        "messages": [{"role": "user", "content": f"Reply with the single word: OK ({tag})"}],
    }


# --------------------------------------------------------------------------- 0.1
def cmd_auth(client: httpx.Client, key: str) -> str:
    """Probe 0.1: does the Subscription Key authenticate /v1 (Path A) and/or
    the Anthropic-compatible endpoint (Path B)?

    Returns:
        "openai" if Path A works, else "anthropic" if Path B works, else
        "openai" as a default for downstream probes (both failed).
    """
    _print_header("0.1 Auth probe: Path A (/v1/chat/completions) vs Path B (/anthropic/v1/messages)")

    openai_body = {
        "model": "MiniMax-M3",
        "temperature": TEMPERATURE,
        "max_tokens": 256,
        "tools": [_OPENAI_TOOL],
        "tool_choice": "auto",
        "messages": [{"role": "user", "content": _TOOL_PROMPT}],
    }
    _, a_ok = _request(
        client, "PATH A: OpenAI-compatible /v1/chat/completions", OPENAI_CHAT_URL, _openai_headers(key), openai_body
    )

    anthropic_body = {
        "model": "MiniMax-M3",
        "temperature": TEMPERATURE,
        "max_tokens": 256,
        "tools": [_ANTHROPIC_TOOL],
        "messages": [{"role": "user", "content": _TOOL_PROMPT}],
    }
    _, b_ok = _request(
        client,
        "PATH B: Anthropic-compatible /anthropic/v1/messages",
        ANTHROPIC_MESSAGES_URL,
        _anthropic_headers(key),
        anthropic_body,
    )

    print("\n--- Verdict ---")
    print(f"PATH A (OpenAI-compatible /v1, Subscription Key): {'OK' if a_ok else 'FAIL'}")
    print(f"PATH B (Anthropic-compatible /anthropic/v1/messages): {'OK' if b_ok else 'FAIL'}")

    if a_ok:
        return "openai"
    if b_ok:
        return "anthropic"
    print("Neither surface authenticated; downstream probes will default to the OpenAI-compatible endpoint.")
    return "openai"


# --------------------------------------------------------------------------- 0.2
def cmd_models(client: httpx.Client, key: str, endpoint: str) -> None:
    """Probe 0.2: invoke each Token Plan model with a tiny completion."""
    _print_header(f"0.2 Model coverage probe ({endpoint}-compatible endpoint)")
    url = _url_for(endpoint)
    headers = _headers_for(endpoint, key)

    rows: list[tuple[str, str, str, str]] = []
    for model in MODELS_TO_PROBE:
        resp, ok = _request(client, f"model={model}", url, headers, _trivial_body(model, "models-probe"))
        status = str(resp.status_code) if resp is not None else "n/a"
        note = "" if ok else (resp.text[:120] if resp is not None else "transport error")
        rows.append((model, status, "Y" if ok else "N", note))

    print("\n--- Model coverage table ---")
    print(f"{'model':<28} {'status':<8} {'usable':<7} note")
    for model, status, usable, note in rows:
        print(f"{model:<28} {status:<8} {usable:<7} {note}")


# --------------------------------------------------------------------------- 0.3
_THINK_TAG_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)

_REASONING_FIELDS = ("reasoning_details", "reasoning_content", "reasoning")


def _strip_reasoning(message: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of an assistant message with all reasoning removed.

    Removes both top-level reasoning fields and inline ``<think>...</think>``
    spans embedded in ``content`` (MiniMax's two documented representations),
    printing whatever was stripped so the replayed-vs-stripped comparison in
    probe 0.3 is auditable.
    """
    stripped = {k: v for k, v in message.items() if k not in _REASONING_FIELDS}
    dropped_fields = [k for k in _REASONING_FIELDS if k in message]
    if dropped_fields:
        print(f"stripped top-level reasoning fields: {dropped_fields}")

    content = stripped.get("content")
    if isinstance(content, str) and "<think>" in content:
        think_spans = _THINK_TAG_RE.findall(content)
        stripped["content"] = _THINK_TAG_RE.sub("", content)
        print(f"stripped {len(think_spans)} inline <think> span(s) from content:")
        for span in think_spans:
            print(span[:500])

    if not dropped_fields and not (isinstance(content, str) and "<think>" in content):
        print("nothing to strip: no reasoning fields or <think> tags found in turn-1 message.")
    return stripped


def _reasoning_openai(client: httpx.Client, key: str) -> None:
    print("\n### OpenAI-compatible surface (reasoning_split)")
    headers = _openai_headers(key)
    turn1_body = {
        "model": "MiniMax-M3",
        "temperature": TEMPERATURE,
        "max_tokens": 512,
        "reasoning_split": True,
        "tools": [_OPENAI_TOOL],
        "tool_choice": "auto",
        "messages": [{"role": "user", "content": _TOOL_PROMPT}],
    }
    resp1, ok1 = _request(client, "turn 1 (tool call, reasoning_split=true)", OPENAI_CHAT_URL, headers, turn1_body)
    if not ok1 or resp1 is None:
        print("Turn 1 failed; skipping turn-2 replay comparison.")
        return

    data1 = resp1.json()
    message = data1["choices"][0]["message"]
    print(f"turn 1 message keys: {sorted(message.keys())}")
    reasoning_field = next((k for k in _REASONING_FIELDS if k in message), None)
    print(f"reasoning field present: {reasoning_field!r}")
    if "<think>" in (message.get("content") or ""):
        print("Found literal <think> tag inline in message content.")

    tool_calls = message.get("tool_calls") or []
    if not tool_calls:
        print("No tool_calls in turn 1 response; cannot run the two-turn replay probe.")
        print(json.dumps(data1, indent=2)[:2000])
        return

    tool_call = tool_calls[0]
    tool_result_msg = {
        "role": "tool",
        "tool_call_id": tool_call.get("id", "call_0"),
        "content": "2026-07-10T00:00:00Z",
    }

    # Turn 2a: replay the full assistant message, including any reasoning fields.
    turn2a_body = {**turn1_body, "messages": turn1_body["messages"] + [message, tool_result_msg]}
    resp2a, _ = _request(client, "turn 2a (reasoning replayed)", OPENAI_CHAT_URL, headers, turn2a_body)

    # Turn 2b: same conversation, but with reasoning stripped from history —
    # both top-level fields and inline <think> tags in content.
    stripped_message = _strip_reasoning(message)
    turn2b_body = {**turn1_body, "messages": turn1_body["messages"] + [stripped_message, tool_result_msg]}
    resp2b, _ = _request(client, "turn 2b (reasoning stripped)", OPENAI_CHAT_URL, headers, turn2b_body)

    print("\nturn 2a response body (reasoning replayed):")
    print(resp2a.text[:2000] if resp2a is not None else "<no response>")
    print("\nturn 2b response body (reasoning stripped):")
    print(resp2b.text[:2000] if resp2b is not None else "<no response>")


def _reasoning_anthropic(client: httpx.Client, key: str) -> None:
    print("\n### Anthropic-compatible surface (thinking blocks)")
    headers = _anthropic_headers(key)
    body = {
        "model": "MiniMax-M3",
        "temperature": TEMPERATURE,
        "max_tokens": 512,
        "tools": [_ANTHROPIC_TOOL],
        "messages": [{"role": "user", "content": _TOOL_PROMPT}],
    }
    resp, ok = _request(client, "anthropic turn 1 (tool call)", ANTHROPIC_MESSAGES_URL, headers, body)
    if not ok or resp is None:
        print("Anthropic turn 1 failed; cannot inspect thinking blocks.")
        return

    data = resp.json()
    blocks = data.get("content", [])
    block_types = [b.get("type") for b in blocks if isinstance(b, dict)]
    print(f"content block types: {block_types}")
    thinking_blocks = [b for b in blocks if isinstance(b, dict) and b.get("type") == "thinking"]
    print(f"thinking block present: {bool(thinking_blocks)}")
    if thinking_blocks:
        print(json.dumps(thinking_blocks, indent=2)[:2000])


def cmd_reasoning(client: httpx.Client, key: str) -> None:
    """Probe 0.3: reasoning round-trip shapes on both surfaces."""
    _print_header("0.3 Reasoning round-trip probe")
    _reasoning_openai(client, key)
    _reasoning_anthropic(client, key)


# --------------------------------------------------------------------------- 0.4
def cmd_concurrency(client: httpx.Client, key: str, endpoint: str) -> None:
    """Probe 0.4: throttling behavior under 3x then 5x parallel load."""
    _print_header(f"0.4 Concurrency probe ({endpoint}-compatible endpoint)")
    url = _url_for(endpoint)
    headers = _headers_for(endpoint, key)

    def fire(tag: str) -> dict[str, Any]:
        t0 = time.monotonic()
        try:
            resp = client.post(url, headers=headers, json=_trivial_body("MiniMax-M3", tag), timeout=DEFAULT_TIMEOUT)
            return {
                "tag": tag,
                "status": resp.status_code,
                "elapsed_s": round(time.monotonic() - t0, 2),
                "retry_after": resp.headers.get("Retry-After"),
            }
        except httpx.HTTPError as exc:
            return {"tag": tag, "status": None, "elapsed_s": round(time.monotonic() - t0, 2), "error": repr(exc)}

    for burst_size in (3, 5):
        print(f"\nFiring {burst_size} parallel requests...")
        with ThreadPoolExecutor(max_workers=burst_size) as pool:
            futures = [pool.submit(fire, f"burst{burst_size}-{i}") for i in range(burst_size)]
            results = [f.result() for f in as_completed(futures)]
        for r in sorted(results, key=lambda r: r["tag"]):
            print(f"  {r}")
        n429 = sum(1 for r in results if r.get("status") == 429)
        retry_afters = sorted({r["retry_after"] for r in results if r.get("retry_after")})
        print(f"  429s: {n429}/{burst_size}; Retry-After values seen: {retry_afters or 'none'}")

    recovery_timeout_s = 90
    poll_interval_s = 5
    print(f"\nRecovery check: polling every {poll_interval_s}s for up to {recovery_timeout_s}s...")
    start = time.monotonic()
    recovered = False
    while time.monotonic() - start < recovery_timeout_s:
        r = fire("recovery")
        elapsed = time.monotonic() - start
        if r.get("status") == 200:
            print(f"  recovered after {elapsed:.1f}s: {r}")
            recovered = True
            break
        print(f"  not recovered ({elapsed:.1f}s elapsed): {r}")
        time.sleep(poll_interval_s)
    if not recovered:
        print(f"  did not recover within {recovery_timeout_s}s window.")


# --------------------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="minimax_probe.py",
        description=(
            "Phase 0 validation probes for the MiniMax M3 migration. "
            "Requires MINIMAX_API_KEY. See docs/minimax-migration-notes.md."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("auth", help="0.1: probe OpenAI-compatible vs Anthropic-compatible auth (Path A vs B).")

    p_models = sub.add_parser("models", help="0.2: probe model coverage across the Token Plan model set.")
    p_models.add_argument("--endpoint", choices=["openai", "anthropic"], default="openai")

    sub.add_parser("reasoning", help="0.3: probe reasoning round-trip shapes on both surfaces.")

    p_conc = sub.add_parser("concurrency", help="0.4: probe throttling under 3x and 5x parallel load.")
    p_conc.add_argument("--endpoint", choices=["openai", "anthropic"], default="openai")

    sub.add_parser("all", help="Run auth, models, reasoning, concurrency in order.")
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    key = require_api_key()

    with httpx.Client() as client:
        if args.command == "auth":
            cmd_auth(client, key)
        elif args.command == "models":
            cmd_models(client, key, args.endpoint)
        elif args.command == "reasoning":
            cmd_reasoning(client, key)
        elif args.command == "concurrency":
            cmd_concurrency(client, key, args.endpoint)
        elif args.command == "all":
            endpoint = cmd_auth(client, key)
            cmd_models(client, key, endpoint)
            cmd_reasoning(client, key)
            cmd_concurrency(client, key, endpoint)


if __name__ == "__main__":
    main()
