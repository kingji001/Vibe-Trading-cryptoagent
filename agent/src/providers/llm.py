"""LLM factory."""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from pydantic import PrivateAttr

from src.providers.capabilities import get_provider_capabilities, provider_env_names

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None  # type: ignore

try:
    from langchain_openai import ChatOpenAI
except ImportError:
    ChatOpenAI = None  # type: ignore


if ChatOpenAI is not None:
    class ChatOpenAIWithReasoning(ChatOpenAI):  # type: ignore[misc,valid-type]
        """ChatOpenAI that preserves provider reasoning across invoke + stream.

        langchain-openai 0.3.x drops non-standard fields in three paths:
          * _convert_dict_to_message — invoke / ainvoke (inbound)
          * _convert_delta_to_message_chunk — stream / astream (inbound)
          * _convert_message_to_dict — request serialization (outbound)
        Moonshot/DeepSeek emit `reasoning_content`; OpenRouter relays as
        `reasoning`. Inbound paths normalize to additional_kwargs["reasoning_content"];
        outbound path re-injects it so strict providers (kimi-k2.6) accept
        multi-turn continuations.
        """

        _vibe_provider: Optional[str] = PrivateAttr(default=None)

        def __init__(self, *args: Any, vibe_provider: str | None = None, **kwargs: Any) -> None:
            """Initialize while retaining the resolved provider name."""
            super().__init__(*args, **kwargs)
            self._vibe_provider = vibe_provider

        def _capabilities(self):
            model = (
                getattr(self, "model_name", None)
                or getattr(self, "model", None)
                or getattr(self, "model_name_", None)
                or ""
            )
            return get_provider_capabilities(self._vibe_provider, str(model))

        @staticmethod
        def _extract_tool_call_thought_signature(tool_call: Any) -> Optional[str]:
            if not isinstance(tool_call, dict):
                return None

            extra_content = tool_call.get("extra_content")
            if isinstance(extra_content, dict):
                google = extra_content.get("google")
                if isinstance(google, dict):
                    value = google.get("thought_signature") or google.get("thoughtSignature")
                    if value:
                        return value

            function = tool_call.get("function")
            containers = [tool_call]
            if isinstance(function, dict):
                containers.append(function)
            for container in containers:
                value = container.get("thought_signature") or container.get("thoughtSignature")
                if value:
                    return value
            return None

        @classmethod
        def _collect_tool_call_thought_signatures(cls, tool_calls: Any) -> list[dict[str, Any]]:
            if not isinstance(tool_calls, list):
                return []

            signatures = []
            for fallback_index, tool_call in enumerate(tool_calls):
                signature = cls._extract_tool_call_thought_signature(tool_call)
                if not signature or not isinstance(tool_call, dict):
                    continue

                index = tool_call.get("index")
                entry: dict[str, Any] = {
                    "index": index if isinstance(index, int) else fallback_index,
                    "thought_signature": signature,
                }
                if tool_call.get("id"):
                    entry["id"] = tool_call["id"]
                signatures.append(entry)
            return signatures

        def _capture(self, src: Any, msg: Any) -> None:
            if not isinstance(src, dict):
                return
            caps = self._capabilities()
            if caps.capture_reasoning:
                value = src.get("reasoning_content") or src.get("reasoning")
                # MiniMax M3 (reasoning_split) surfaces reasoning under the
                # ``reasoning_details`` field rather than ``reasoning_content``.
                if not value and caps.reasoning_split_extra_body:
                    value = src.get("reasoning_details")
                if value:
                    msg.additional_kwargs["reasoning_content"] = value
            if caps.gemini_thought_signatures and (
                signatures := self._collect_tool_call_thought_signatures(src.get("tool_calls"))
            ):
                msg.additional_kwargs["tool_call_thought_signatures"] = signatures

        def _convert_input(self, input: Any) -> Any:  # type: ignore[override]
            """Re-attach Gemini thought signatures dropped by dict->message conversion.

            The AgentLoop replays history as OpenAI-format dicts, stamping the
            signature into ``tool_calls[i].extra_content.google.thought_signature``
            (loop.py ``_attach_tool_call_thought_signatures``). LangChain's
            ``_convert_dict_to_message`` discards ``extra_content`` entirely, so by
            the time ``_get_request_payload`` runs the signature is gone and Gemini
            rejects the next turn with a ``missing thought_signature`` 400.

            ``_convert_input`` is the single chokepoint both ``invoke`` and
            ``stream`` call once at entry, while ``input`` is still raw dicts. Here
            we lift the signatures back onto the converted ``AIMessage`` in the
            same ``additional_kwargs["tool_call_thought_signatures"]`` shape the
            in-memory (#176) path produces, so the existing ``_signature_maps`` /
            ``_inject_tool_call_thought_signatures`` machinery handles both paths
            identically. The ``isinstance(raw, dict)`` guard makes it a no-op when
            re-invoked on already-converted ``BaseMessage`` objects (idempotent).
            """
            prompt_value = super()._convert_input(input)
            if not self._capabilities().gemini_thought_signatures:
                return prompt_value
            if isinstance(input, Sequence) and not isinstance(input, (str, bytes)):
                messages = prompt_value.to_messages()
                if len(messages) == len(input):
                    for raw, msg in zip(input, messages):
                        if (
                            isinstance(raw, dict)
                            and getattr(msg, "type", None) == "ai"
                            and not getattr(msg, "additional_kwargs", {}).get(
                                "tool_call_thought_signatures"
                            )
                        ):
                            sigs = self._collect_tool_call_thought_signatures(
                                raw.get("tool_calls")
                            )
                            if sigs:
                                msg.additional_kwargs["tool_call_thought_signatures"] = sigs
            return prompt_value

        @classmethod
        def _signature_maps(cls, message: Any) -> tuple[dict[str, str], dict[int, str]]:
            by_id: dict[str, str] = {}
            by_index: dict[int, str] = {}
            additional_kwargs = getattr(message, "additional_kwargs", {})

            entries = additional_kwargs.get("tool_call_thought_signatures", [])
            if isinstance(entries, dict):
                entries = [entries]
            if isinstance(entries, list):
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    signature = entry.get("thought_signature")
                    if not signature:
                        continue
                    if entry.get("id"):
                        by_id[str(entry["id"])] = signature
                    index = entry.get("index")
                    if isinstance(index, int):
                        by_index[index] = signature

            raw_tool_calls = additional_kwargs.get("tool_calls")
            if isinstance(raw_tool_calls, list):
                for index, tool_call in enumerate(raw_tool_calls):
                    signature = cls._extract_tool_call_thought_signature(tool_call)
                    if not signature or not isinstance(tool_call, dict):
                        continue
                    if tool_call.get("id"):
                        by_id[str(tool_call["id"])] = signature
                    by_index[index] = signature

            return by_id, by_index

        @staticmethod
        def _set_tool_call_thought_signature(tool_call: Any, signature: str) -> None:
            if not isinstance(tool_call, dict):
                return
            extra_content = tool_call.get("extra_content")
            if not isinstance(extra_content, dict):
                extra_content = {}
                tool_call["extra_content"] = extra_content
            google = extra_content.get("google")
            if not isinstance(google, dict):
                google = {}
                extra_content["google"] = google
            google["thought_signature"] = signature

        @classmethod
        def _inject_tool_call_thought_signatures(cls, outbound: Any, source_message: Any) -> None:
            if not isinstance(outbound, list):
                return

            by_id, by_index = cls._signature_maps(source_message)
            if not by_id and not by_index:
                return

            for index, tool_call in enumerate(outbound):
                signature = None
                if isinstance(tool_call, dict) and tool_call.get("id"):
                    signature = by_id.get(str(tool_call["id"]))
                signature = signature or by_index.get(index)
                if signature:
                    cls._set_tool_call_thought_signature(tool_call, signature)

        @staticmethod
        def _strip_tool_call_extra_content(outbound: Any) -> None:
            if not isinstance(outbound, list):
                return
            for tool_call in outbound:
                if isinstance(tool_call, dict):
                    tool_call.pop("extra_content", None)

        def _create_chat_result(self, response, generation_info=None):  # type: ignore[override]
            result = super()._create_chat_result(response, generation_info)
            raw = response if isinstance(response, dict) else response.model_dump()
            for gen, choice in zip(result.generations, raw["choices"]):
                self._capture(choice["message"], gen.message)
            return result

        def _convert_chunk_to_generation_chunk(  # type: ignore[override]
            self,
            chunk: dict,
            default_chunk_class: type,
            base_generation_info: Optional[dict],
        ):
            gen = super()._convert_chunk_to_generation_chunk(
                chunk, default_chunk_class, base_generation_info
            )
            if gen is None:
                return None
            choices = chunk.get("choices") or chunk.get("chunk", {}).get("choices")
            if choices:
                self._capture(choices[0]["delta"], gen.message)
            return gen

        def _get_request_payload(  # type: ignore[override]
            self,
            input_: Any,
            *,
            stop: Optional[list[str]] = None,
            **kwargs: Any,
        ) -> dict:
            """Re-inject reasoning_content and normalize assistant content.

            LangChain strips ``reasoning_content`` when serializing AIMessages
            back to OpenAI wire format. Moonshot kimi-k2.6 also rejects
            assistant turns where ``content`` is null or ``reasoning_content``
            is absent, breaking ReAct continuations after a tool call (#39).
            """
            payload = super()._get_request_payload(input_, stop=stop, **kwargs)
            messages = super()._convert_input(input_).to_messages()
            caps = self._capabilities()
            for i, m in enumerate(payload["messages"]):
                if m.get("role") != "assistant":
                    continue
                source_message = messages[i]
                if caps.normalize_assistant_content and m.get("content") is None:
                    m["content"] = ""
                if caps.send_reasoning_content:
                    reasoning = source_message.additional_kwargs.get("reasoning_content", "")
                    if caps.reasoning_split_extra_body:
                        # MiniMax M3 expects the reasoning replayed under its own
                        # ``reasoning_details`` field, not ``reasoning_content``.
                        m["reasoning_details"] = reasoning
                        m.pop("reasoning_content", None)
                    else:
                        m["reasoning_content"] = reasoning
                else:
                    m.pop("reasoning_content", None)
                if caps.gemini_thought_signatures:
                    self._inject_tool_call_thought_signatures(m.get("tool_calls"), source_message)
                else:
                    self._strip_tool_call_extra_content(m.get("tool_calls"))
            return payload
else:
    ChatOpenAIWithReasoning = None  # type: ignore

AGENT_DIR = Path(__file__).resolve().parents[2]

# .env search order: ~/.vibe-trading/.env → agent/.env → $CWD/.env
_ENV_CANDIDATES = [
    Path.home() / ".vibe-trading" / ".env",
    AGENT_DIR / ".env",
    Path.cwd() / ".env",
]

# Index-aligned with _ENV_CANDIDATES. CWE-209: never log the absolute
# .env path (it leaks the OS username / home / CWD). The label names
# which slot won - the entire P08 R1 signal - using compile-time
# constants only.
_ENV_LABELS = ("~/.vibe-trading/.env", "<AGENT_DIR>/.env", "<CWD>/.env")

logger = logging.getLogger(__name__)

_dotenv_loaded: bool = False


def _redact_env_source(loaded: Path | None) -> str:
    """Map a resolved `.env` candidate to a stable, leak-free label.

    Returns a symbolic slot label (never the absolute path) so a stale
    or shadowed `.env` stays diagnosable without exposing the OS
    username, home, or CWD (CWE-209). A candidate outside the fixed
    list (e.g. one injected by a test) collapses to a generic
    placeholder rather than echoing a real path.
    """
    if loaded is None:
        return "none (no .env file found)"
    for label, candidate in zip(_ENV_LABELS, _ENV_CANDIDATES):
        if loaded == candidate:
            return label
    return "<.env>"


def _redact_base_url_for_log(raw: str | None) -> str:
    """Return a diagnostic-safe base URL label for logs."""
    if not raw or not raw.strip():
        return "(unset)"

    try:
        parsed = urlsplit(raw.strip())
    except ValueError:
        return "<base-url>"

    if not parsed.scheme or not parsed.hostname:
        return "<base-url>"

    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"

    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is not None:
        host = f"{host}:{port}"

    return f"{parsed.scheme}://{host}"


def _package_version(package: str) -> str:
    """Return an installed package version or a stable missing label."""
    try:
        return version(package)
    except PackageNotFoundError:
        return "not_installed"


def _redact_env_flag(name: str) -> str:
    """Report whether an env var is set without exposing its value."""
    value = os.getenv(name, "")
    return "set" if value else "unset"


def _redact_proxy_url(name: str, raw: str | None) -> str:
    """Return a credential-free proxy URL label."""
    if not raw:
        return "unset"
    if name.upper().endswith("NO_PROXY"):
        return "set"
    return _redact_base_url_for_log(raw)


def _deepseek_adapter_mode() -> str:
    """Return the configured DeepSeek adapter mode."""
    mode = os.getenv("VIBE_TRADING_DEEPSEEK_ADAPTER", "auto").strip().lower()
    aliases = {
        "compat": "openai-compatible",
        "compatible": "openai-compatible",
        "openai": "openai-compatible",
        "openai_compatible": "openai-compatible",
    }
    return aliases.get(mode, mode or "auto")


def _build_native_deepseek(
    *,
    model: str,
    temperature: float,
    callbacks: Any = None,
) -> Any | None:
    """Build the optional native DeepSeek adapter when installed.

    Returns:
        A ChatDeepSeek instance, or ``None`` when the optional package is not
        available.
    """
    try:
        module = import_module("langchain_deepseek")
        chat_deepseek = getattr(module, "ChatDeepSeek")
    except Exception as exc:  # noqa: BLE001 - optional adapter fallback
        logger.info("DeepSeek native adapter unavailable; using OpenAI-compatible path: %s", exc)
        return None

    key_env, base_env = provider_env_names("deepseek", model)
    api_key = os.getenv(key_env or "", "") or os.getenv("OPENAI_API_KEY", "")
    base_url = os.getenv(base_env, "") or os.getenv("OPENAI_BASE_URL", "") or os.getenv("OPENAI_API_BASE", "")
    return chat_deepseek(
        model=model,
        temperature=temperature,
        timeout=int(os.getenv("TIMEOUT_SECONDS", "120")),
        max_retries=int(os.getenv("MAX_RETRIES", "2")),
        callbacks=callbacks,
        api_key=api_key or None,
        base_url=base_url or None,
    )


def _minimax_base_url() -> str:
    """Return the configured MiniMax base URL (provider-specific → OPENAI_* fallback)."""
    return (
        os.getenv("MINIMAX_BASE_URL", "")
        or os.getenv("OPENAI_BASE_URL", "")
        or os.getenv("OPENAI_API_BASE", "")
    ).strip()


def _minimax_uses_anthropic_endpoint() -> bool:
    """Return True when MiniMax should use Path B (Anthropic-compatible adapter).

    Selection rule (Phase 1 Adaptation): a base URL containing ``/anthropic``
    selects the native ``langchain-anthropic`` adapter (Path B); anything else
    (default ``https://api.minimax.io/v1``) keeps the OpenAI-compatible
    ``ChatOpenAIWithReasoning`` path (Path A).
    """
    return "/anthropic" in _minimax_base_url().lower()


def _minimax_thinking_mode() -> str | None:
    """Return the configured MiniMax ``thinking`` mode, or None when unset.

    ``MINIMAX_THINKING`` accepts ``adaptive`` (M3 default; upstream behavior)
    or ``disabled`` (quick tier — turn thinking off). Unset/unknown → None so
    the request defaults to upstream behavior.
    """
    mode = os.getenv("MINIMAX_THINKING", "").strip().lower()
    return mode if mode in {"adaptive", "disabled"} else None


def _build_native_minimax_anthropic(
    *,
    model: str,
    temperature: float,
    top_p: float | None = None,
    callbacks: Any = None,
) -> Any:
    """Build the MiniMax Path B adapter (Anthropic-compatible endpoint).

    Uses the optional ``langchain-anthropic`` package pointed at
    ``https://api.minimax.io/anthropic`` (Subscription-Key / ``x-api-key``
    surface). Raises a clear install hint when the optional package is missing.

    KNOWN LIMITATION (Phase 1, deferred): reasoning replay across ReAct tool
    turns is UNVERIFIED on this path. The loop replays history as OpenAI-format
    dicts carrying reasoning in ``reasoning_content`` /
    ``additional_kwargs["reasoning_content"]``; nothing here translates that
    into Anthropic ``thinking`` content blocks, so M3 reasoning is most likely
    NOT replayed on Path B. Implementing the translation is deferred until a
    live Phase 0 probe (``scripts/minimax_probe.py reasoning``) confirms the
    exact thinking-block round-trip shape. Path A is the evidence-backed path;
    see "Known limitations" in ``docs/minimax-migration-notes.md``.

    Raises:
        RuntimeError: If ``langchain-anthropic`` is not installed.
    """
    try:
        module = import_module("langchain_anthropic")
        chat_anthropic = getattr(module, "ChatAnthropic")
    except Exception as exc:  # noqa: BLE001 - surface a clear install hint
        raise RuntimeError(
            "MiniMax Path B (Anthropic-compatible endpoint) requires the optional "
            "'langchain-anthropic' package. Install it with: "
            'pip install "vibe-trading-ai[minimax]" '
            "(or: uv pip install langchain-anthropic), or point MINIMAX_BASE_URL "
            f"back at the OpenAI-compatible /v1 endpoint. Import error: {exc}"
        ) from exc

    key_env, _ = provider_env_names("minimax", model)
    api_key = os.getenv(key_env or "", "") or os.getenv("OPENAI_API_KEY", "")
    base_url = _minimax_base_url()
    kwargs: dict[str, Any] = {
        "model": model,
        "temperature": temperature,
        "timeout": int(os.getenv("TIMEOUT_SECONDS", "120")),
        "max_retries": int(os.getenv("MAX_RETRIES", "2")),
        "max_tokens": int(os.getenv("MINIMAX_MAX_TOKENS", "4096")),
        "callbacks": callbacks,
        "api_key": api_key or None,
        "base_url": base_url or None,
    }
    if top_p is not None:
        kwargs["top_p"] = top_p
    if _minimax_thinking_mode() == "disabled":
        kwargs["thinking"] = {"type": "disabled"}
    return chat_anthropic(**kwargs)


def _load_env_file(path: Path) -> None:
    """Load a single .env file into os.environ (setdefault, no override)."""
    if load_dotenv is not None:
        load_dotenv(dotenv_path=path, override=False)
    else:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key:
                os.environ.setdefault(key, value.strip().strip('"').strip("'"))


def _ensure_dotenv() -> None:
    """Load `.env` from the first found candidate path."""
    global _dotenv_loaded
    if _dotenv_loaded:
        return
    loaded = None
    for candidate in _ENV_CANDIDATES:
        if candidate.exists():
            _load_env_file(candidate)
            loaded = candidate
            break
    _dotenv_loaded = True
    # P08 R1: one-time, behavior-preserving diagnostic so a stale or
    # shadowed .env is observable instead of costing hours. The path is
    # redacted to a symbolic slot label and the API key is never logged.
    logger.info(
        "dotenv resolved from %s | provider=%s model=%s base=%s",
        _redact_env_source(loaded),
        os.getenv("LANGCHAIN_PROVIDER", "(unset)"),
        os.getenv("LANGCHAIN_MODEL_NAME", "(unset)"),
        _redact_base_url_for_log(os.getenv("OPENAI_BASE_URL") or os.getenv("OPENAI_API_BASE")),
    )


def _normalize_ollama_base_url(base_url: str) -> str:
    """Append ``/v1`` when missing so ChatOpenAI hits Ollama's OpenAI-compatible API."""
    url = base_url.strip().rstrip("/")
    if not url:
        return url
    if url.endswith("/v1"):
        return url
    return f"{url}/v1"


def _sync_provider_env() -> None:
    """Map provider-specific env vars to OPENAI_* for ChatOpenAI.

    Each entry: provider_name -> (api_key_env, base_url_env).
    All base URLs must be set explicitly in .env — no hardcoded defaults.
    api_key_env=None means no key required (e.g. Ollama local).
    """
    _ensure_dotenv()
    provider = os.getenv("LANGCHAIN_PROVIDER", "openai").lower()

    if provider in {"openai-codex", "openai_codex"}:
        codex_url = os.getenv("OPENAI_CODEX_BASE_URL", "https://chatgpt.com/backend-api/codex/responses")
        os.environ["OPENAI_API_BASE"] = codex_url
        os.environ["OPENAI_BASE_URL"] = codex_url
        os.environ.pop("OPENAI_API_KEY", None)
        return

    key_env, base_env = provider_env_names(provider, os.getenv("LANGCHAIN_MODEL_NAME", ""))

    # Resolve API key: provider-specific env → OPENAI_API_KEY fallback
    if key_env is not None:
        api_key = os.getenv(key_env, "") or os.getenv("OPENAI_API_KEY", "")
    else:
        api_key = os.getenv("OPENAI_API_KEY", "") or "ollama"

    # Resolve base URL: provider-specific env → OPENAI_BASE_URL fallback
    base_url = os.getenv(base_env, "") or os.getenv("OPENAI_BASE_URL", "") or os.getenv("OPENAI_API_BASE", "")
    if provider == "ollama" and base_url:
        base_url = _normalize_ollama_base_url(base_url)

    if api_key:
        os.environ["OPENAI_API_KEY"] = api_key
    if base_url:
        os.environ["OPENAI_API_BASE"] = base_url
        os.environ.setdefault("OPENAI_BASE_URL", base_url)


def provider_diagnostics() -> dict[str, Any]:
    """Build a redacted provider diagnostic snapshot.

    Returns:
        Redacted provider/model/package/env/proxy/capability details.
    """
    _sync_provider_env()
    provider = os.getenv("LANGCHAIN_PROVIDER", "openai").strip().lower()
    model = os.getenv("LANGCHAIN_MODEL_NAME", "").strip()
    caps = get_provider_capabilities(provider, model)
    key_env, base_env = provider_env_names(provider, model)
    base_url = os.getenv(base_env, "") or os.getenv("OPENAI_BASE_URL", "") or os.getenv("OPENAI_API_BASE", "")
    proxy_names = ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy"]
    package_names = ["langchain-openai", "langchain-core", "langchain", "openai", "langchain-deepseek"]
    native_package_version = (
        _package_version(caps.native_adapter_package)
        if caps.native_adapter_package
        else None
    )
    adapter_mode = _deepseek_adapter_mode() if caps.name == "deepseek" else "openai-compatible"
    adapter_type = (
        "native"
        if caps.name == "deepseek"
        and adapter_mode != "openai-compatible"
        and native_package_version not in {None, "not_installed"}
        else "openai-compatible"
    )
    return {
        "provider": caps.name if provider in {"kimi", "openai_codex"} else provider,
        "model": model,
        "base_url": _redact_base_url_for_log(base_url),
        "api_key": {key_env: _redact_env_flag(key_env)} if key_env else {},
        "env": {
            "LANGCHAIN_PROVIDER": _redact_env_flag("LANGCHAIN_PROVIDER"),
            "LANGCHAIN_MODEL_NAME": _redact_env_flag("LANGCHAIN_MODEL_NAME"),
            "OPENAI_API_KEY": _redact_env_flag("OPENAI_API_KEY"),
            "OPENAI_BASE_URL": _redact_base_url_for_log(os.getenv("OPENAI_BASE_URL")),
            "OPENAI_API_BASE": _redact_base_url_for_log(os.getenv("OPENAI_API_BASE")),
        },
        "proxy": {
            name: _redact_proxy_url(name, os.getenv(name))
            for name in proxy_names
            if os.getenv(name)
        },
        "packages": {name: _package_version(name) for name in package_names},
        "timeout_seconds": int(os.getenv("TIMEOUT_SECONDS", "120")),
        "max_retries": int(os.getenv("MAX_RETRIES", "2")),
        "reasoning_effort": os.getenv("LANGCHAIN_REASONING_EFFORT", "").strip().lower(),
        "adapter": {
            "type": adapter_type,
            "mode": adapter_mode,
            "native_package": caps.native_adapter_package,
            "native_package_version": native_package_version,
        },
        "capabilities": {
            "capture_reasoning": caps.capture_reasoning,
            "send_reasoning_content": caps.send_reasoning_content,
            "gemini_thought_signatures": caps.gemini_thought_signatures,
            "openrouter_reasoning_body": caps.openrouter_reasoning_body,
            "reasoning_split_extra_body": caps.reasoning_split_extra_body,
        },
    }


def _minimax_endpoint_reachable(base_url: str) -> str:
    """Best-effort MiniMax endpoint reachability probe (never raises)."""
    try:
        import httpx

        with httpx.Client(timeout=8.0) as client:
            resp = client.get(base_url)
        # Any HTTP response (even 401/404) proves the host is reachable.
        return f"reachable (HTTP {resp.status_code})"
    except Exception as exc:  # noqa: BLE001 - doctor must degrade, never crash
        return f"unreachable ({type(exc).__name__})"


def _minimax_reasoning_self_test() -> str:
    """Best-effort M3 reasoning round-trip self-test (never raises).

    Sends a tiny prompt and asserts reasoning was captured, exercising the
    Path A reasoning_split / Path B thinking-block capture end to end.
    """
    try:
        from src.providers.chat import ChatLLM

        client = ChatLLM()
        response = client.chat(
            [{"role": "user", "content": "Think briefly, then reply with the single word OK."}],
            timeout=int(os.getenv("TIMEOUT_SECONDS", "120")),
        )
        if response.reasoning_content:
            return "ok (reasoning captured)"
        return "degraded (completed but no reasoning field returned)"
    except Exception as exc:  # noqa: BLE001 - doctor must degrade, never crash
        return f"degraded ({type(exc).__name__})"


def minimax_provider_checks() -> dict[str, Any]:
    """Return MiniMax-specific ``provider doctor`` checks.

    Degrades gracefully: with no ``MINIMAX_API_KEY`` configured it reports a
    ``degraded`` status and skips all network checks (never a hard failure).
    With a key it adds endpoint reachability and a reasoning round-trip
    self-test. Path A vs Path B is reported from the configured base URL.
    """
    _sync_provider_env()
    key = os.getenv("MINIMAX_API_KEY", "").strip()
    base_url = _minimax_base_url() or "https://api.minimax.io/v1"
    path = (
        "B (Anthropic-compatible /anthropic)"
        if _minimax_uses_anthropic_endpoint()
        else "A (OpenAI-compatible /v1)"
    )
    checks: dict[str, Any] = {
        "path": path,
        "base_url": _redact_base_url_for_log(base_url),
        "api_key": {"MINIMAX_API_KEY": "set" if key else "unset"},
        "thinking": _minimax_thinking_mode() or "adaptive (default)",
    }
    if _minimax_uses_anthropic_endpoint():
        checks["path_b_note"] = (
            "reasoning replay via Anthropic thinking blocks is unverified/deferred "
            "(see docs/minimax-migration-notes.md, Known limitations)"
        )

    if not key:
        checks["status"] = "degraded"
        checks["note"] = (
            "no MINIMAX_API_KEY configured; skipping endpoint reachability and "
            "reasoning self-test"
        )
        checks["endpoint_reachable"] = "skipped (no key configured)"
        checks["reasoning_round_trip"] = "skipped (no key configured)"
        return checks

    # Token Plan Subscription Keys are documented as Anthropic-endpoint-only;
    # pay-as-you-go keys authenticate the OpenAI-compatible /v1 surface. We
    # cannot detect the key *type* offline, so surface the operative note.
    checks["key_type_note"] = (
        "Token Plan Subscription Keys authenticate the /anthropic endpoint "
        "(Path B); pay-as-you-go keys use /v1 (Path A). Active path: " + path
    )
    checks["endpoint_reachable"] = _minimax_endpoint_reachable(base_url)
    reasoning = _minimax_reasoning_self_test()
    checks["reasoning_round_trip"] = reasoning
    checks["status"] = (
        "ok"
        if checks["endpoint_reachable"].startswith("reachable") and reasoning.startswith("ok")
        else "degraded"
    )
    return checks


def build_llm(*, model_name: Optional[str] = None, callbacks: Any = None) -> Any:
    """Construct a ChatOpenAI instance.

    Args:
        model_name: Model name; defaults to LANGCHAIN_MODEL_NAME.
        callbacks: Optional LangChain callbacks.

    Returns:
        ChatOpenAI instance.

    Raises:
        RuntimeError: If langchain-openai is missing or LANGCHAIN_MODEL_NAME is unset.
    """
    _sync_provider_env()
    name = model_name or os.getenv("LANGCHAIN_MODEL_NAME", "").strip()
    if not name:
        raise RuntimeError("LANGCHAIN_MODEL_NAME is not set")
    temperature = float(os.getenv("LANGCHAIN_TEMPERATURE", "0.0"))
    provider = os.getenv("LANGCHAIN_PROVIDER", "openai").lower()
    caps = get_provider_capabilities(provider, name)
    if provider in {"openai-codex", "openai_codex"}:
        from src.providers.openai_codex import OpenAICodexLLM

        effort = os.getenv("LANGCHAIN_REASONING_EFFORT", "").strip().lower()
        return OpenAICodexLLM(
            model=name,
            temperature=temperature,
            timeout=int(os.getenv("TIMEOUT_SECONDS", "120")),
            reasoning_effort=effort or None,
        )

    if provider == "deepseek":
        adapter_mode = _deepseek_adapter_mode()
        if adapter_mode != "openai-compatible":
            native_llm = _build_native_deepseek(
                model=name,
                temperature=temperature,
                callbacks=callbacks,
            )
            if native_llm is not None:
                return native_llm
            if adapter_mode == "native":
                raise RuntimeError(
                    "VIBE_TRADING_DEEPSEEK_ADAPTER=native requires langchain-deepseek"
                )

    # MiniMax requires temperature strictly > 0. When the user left
    # LANGCHAIN_TEMPERATURE at the global 0.0 default, apply M3's documented
    # defaults (temperature=1.0, top_p=0.95) rather than silently running at
    # 0.01 — the recommended sampling settings, not just an error rescue.
    top_p: float | None = None
    if provider == "minimax" and temperature <= 0.0:
        temperature = 1.0
        top_p = 0.95

    # MiniMax Path B: an Anthropic-compatible base URL (`/anthropic`) selects
    # the native langchain-anthropic adapter instead of ChatOpenAI.
    if provider == "minimax" and _minimax_uses_anthropic_endpoint():
        return _build_native_minimax_anthropic(
            model=name,
            temperature=temperature,
            top_p=top_p,
            callbacks=callbacks,
        )

    if ChatOpenAI is None:
        raise RuntimeError("langchain-openai is not installed")
    # Moonshot kimi-k2.x reasoning models reject any temperature other than 1
    # ("invalid temperature: only 1 is allowed for this model").
    if caps.name == "moonshot" and name.lower().startswith("kimi-k2") and temperature != 1.0:
        logger.info("Forcing temperature=1.0 for %s (provider requirement)", name)
        temperature = 1.0
    # Optional reasoning activation for relays requiring opt-in (e.g. OpenRouter).
    # Moonshot/DeepSeek official APIs emit reasoning by default and ignore this field.
    effort = os.getenv("LANGCHAIN_REASONING_EFFORT", "").strip().lower()
    extra_body: dict[str, Any] | None = (
        {"reasoning": {"effort": effort}} if effort and caps.openrouter_reasoning_body else None
    )
    # MiniMax M3 (Path A): opt into reasoning capture via reasoning_split and,
    # optionally, disable thinking for a quick tier via MINIMAX_THINKING.
    if caps.reasoning_split_extra_body:
        extra_body = dict(extra_body or {})
        extra_body["reasoning_split"] = True
        thinking = _minimax_thinking_mode()
        if thinking == "disabled":
            extra_body["thinking"] = {"type": "disabled"}
    kwargs: dict[str, Any] = {
        "model": name,
        "temperature": temperature,
        "timeout": int(os.getenv("TIMEOUT_SECONDS", "120")),
        "max_retries": int(os.getenv("MAX_RETRIES", "2")),
        "callbacks": callbacks,
        "extra_body": extra_body,
        "vibe_provider": provider,
    }
    if top_p is not None:
        kwargs["top_p"] = top_p
    if caps.default_headers:
        headers = dict(caps.default_headers)
        if caps.name == "moonshot":
            custom_ua = os.getenv("MOONSHOT_USER_AGENT", "").strip()
            if custom_ua:
                headers["User-Agent"] = custom_ua
        kwargs["default_headers"] = headers
    return ChatOpenAIWithReasoning(**kwargs)
