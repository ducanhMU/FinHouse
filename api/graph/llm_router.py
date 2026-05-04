"""
FinHouse — Per-agent LLM routing.

Each node in the graph asks `get_llm("rewriter", session_model)` and
receives an `LLMHandle` exposing the same `chat_sync` / `chat_stream`
contract used elsewhere in the codebase. This keeps node code provider-
agnostic — Ollama, Gemini, or any OpenAI-compatible endpoint look the
same from the caller's side.

Config strings live in `.env`:
    REWRITER_AGENT_LLM=ollama:qwen2.5:14b
    DB_AGENT_LLM=gemini:gemini-2.0-flash
    WEB_AGENT_LLM=openai:gpt-4o-mini
    VIS_AGENT_LLM=                       # empty → fall back to session model

Empty string falls back to the chat session's selected model on local
Ollama (which itself respects OLLAMA_MODE: local / backup / auto), so
the existing single-brain behavior is preserved by default.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import AsyncGenerator, Optional

import httpx

from config import get_settings
from services.ollama import chat_sync as ollama_chat_sync
from services.ollama import chat_stream as ollama_chat_stream

log = logging.getLogger("finhouse.graph.llm")
settings = get_settings()


# ── Provider parsing ────────────────────────────────────────


@dataclass(frozen=True)
class LLMSpec:
    provider: str   # "ollama" | "gemini" | "openai"
    model: str


def parse_spec(raw: str, fallback_model: str) -> LLMSpec:
    """
    "ollama:qwen2.5:14b" → LLMSpec("ollama", "qwen2.5:14b")
    "gemini:gemini-2.0-flash" → LLMSpec("gemini", "gemini-2.0-flash")
    ""                  → LLMSpec("ollama", fallback_model)
    """
    raw = (raw or "").strip()
    if not raw:
        return LLMSpec("ollama", fallback_model)
    if ":" not in raw:
        # Bare model name, assume ollama.
        return LLMSpec("ollama", raw)
    provider, _, model = raw.partition(":")
    provider = provider.strip().lower()
    model = model.strip()
    if provider not in {"ollama", "gemini", "openai"}:
        log.warning(
            "Unknown LLM provider %r in spec %r — falling back to ollama:%s",
            provider, raw, fallback_model,
        )
        return LLMSpec("ollama", fallback_model)
    if not model:
        return LLMSpec(provider, fallback_model)
    return LLMSpec(provider, model)


# ── Handle ──────────────────────────────────────────────────


class LLMHandle:
    """
    Uniform sync/stream chat surface across providers.
    Always returns / yields Ollama-shaped dicts:
        {"message": {"role": ..., "content": ..., "tool_calls": [...]}, "done": bool}
    """

    def __init__(self, spec: LLMSpec):
        self.spec = spec

    @property
    def label(self) -> str:
        return f"{self.spec.provider}:{self.spec.model}"

    async def chat_sync(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        timeout: Optional[float] = None,
        options: Optional[dict] = None,
    ) -> dict:
        if self.spec.provider == "ollama":
            return await ollama_chat_sync(
                self.spec.model, messages, tools=tools,
                timeout=timeout, options=options,
            )
        if self.spec.provider == "gemini":
            return await _gemini_chat_sync(
                self.spec.model, messages, tools, timeout, options,
            )
        if self.spec.provider == "openai":
            return await _openai_compat_chat_sync(
                self.spec.model, messages, tools, timeout, options,
            )
        raise RuntimeError(f"Unsupported provider {self.spec.provider!r}")

    async def chat_stream(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
    ) -> AsyncGenerator[dict, None]:
        if self.spec.provider == "ollama":
            async for chunk in ollama_chat_stream(self.spec.model, messages, tools=tools):
                yield chunk
            return
        if self.spec.provider == "gemini":
            async for chunk in _gemini_chat_stream(self.spec.model, messages, tools):
                yield chunk
            return
        if self.spec.provider == "openai":
            async for chunk in _openai_compat_chat_stream(self.spec.model, messages, tools):
                yield chunk
            return
        raise RuntimeError(f"Unsupported provider {self.spec.provider!r}")


# ── Public entry point ──────────────────────────────────────

_AGENT_ENV = {
    "rewriter":     "REWRITER_AGENT_LLM",
    "orchestrator": "ORCHESTRATOR_AGENT_LLM",
    "web":          "WEB_AGENT_LLM",
    "database":     "DB_AGENT_LLM",
    "visualize":    "VIS_AGENT_LLM",
    "collector":    "COLLECTOR_AGENT_LLM",
}


def get_llm(agent: str, session_model: str) -> LLMHandle:
    """Return the LLM handle configured for the given agent role.

    `session_model` is the user-selected model on the chat session; it
    is used as the fallback when the agent's env var is empty so the
    out-of-the-box behavior matches the legacy single-brain pipeline.
    """
    env_name = _AGENT_ENV.get(agent)
    raw = ""
    if env_name:
        raw = getattr(settings, env_name, "") or ""
    spec = parse_spec(raw, session_model)
    return LLMHandle(spec)


# ════════════════════════════════════════════════════════════
# OpenAI-compatible providers (Gemini, generic OpenAI, …)
# ════════════════════════════════════════════════════════════
#
# Both Gemini's OpenAI bridge and any generic OpenAI-compatible endpoint
# (FPT Cloud, Together, OpenAI itself) share the same wire format. The
# difference is only the URL + key. We translate Ollama-shaped messages
# / tool_calls in the same way services/ollama.py does.

from services.ollama import (   # noqa: E402  — circular avoidance, late import OK
    _ollama_messages_to_openai,
    _openai_message_to_ollama,
)


def _gemini_credentials() -> tuple[str, str]:
    if not settings.GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY not configured")
    return settings.GEMINI_API_URL.rstrip("/"), settings.GEMINI_API_KEY


def _openai_credentials() -> tuple[str, str]:
    if not (settings.OLLAMA_API_URL and settings.OLLAMA_API_KEY):
        raise RuntimeError(
            "openai:* providers require OLLAMA_API_URL / OLLAMA_API_KEY "
            "(reused as the OpenAI-compat endpoint)."
        )
    return settings.OLLAMA_API_URL.rstrip("/"), settings.OLLAMA_API_KEY


def _build_openai_payload(
    model: str,
    messages: list[dict],
    tools: Optional[list[dict]],
    options: Optional[dict],
    stream: bool,
) -> dict:
    payload = {
        "model": model,
        "messages": _ollama_messages_to_openai(messages),
        "stream": stream,
    }
    if tools:
        payload["tools"] = tools
    if options:
        if "temperature" in options:
            payload["temperature"] = options["temperature"]
        if "num_predict" in options:
            payload["max_tokens"] = int(options["num_predict"])
        if "top_p" in options:
            payload["top_p"] = options["top_p"]
    return payload


async def _post_openai_compat_sync(
    base_url: str, key: str, payload: dict, timeout: Optional[float],
) -> dict:
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
    total_timeout = timeout if timeout is not None else 300.0
    async with httpx.AsyncClient(timeout=httpx.Timeout(total_timeout, connect=10.0)) as client:
        resp = await client.post(f"{base_url}/chat/completions", json=payload, headers=headers)
        if resp.status_code >= 400:
            log.error("LLM API %s error %s: %s", base_url, resp.status_code, resp.text[:500])
        resp.raise_for_status()
        data = resp.json()
    if isinstance(data, dict) and "choices" not in data and "data" in data:
        data = data["data"]
    choices = data.get("choices") or []
    if not choices:
        return {"message": {"role": "assistant", "content": ""}, "done": True}
    msg = choices[0].get("message") or {}
    return {"message": _openai_message_to_ollama(msg), "done": True}


async def _stream_openai_compat(
    base_url: str, key: str, payload: dict,
) -> AsyncGenerator[dict, None]:
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
    accumulated_tool_calls: dict[int, dict] = {}
    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0)) as client:
        async with client.stream(
            "POST", f"{base_url}/chat/completions",
            json=payload, headers=headers,
        ) as resp:
            if resp.status_code >= 400:
                body = await resp.aread()
                log.error("LLM API %s stream error %s: %s",
                          base_url, resp.status_code, body.decode()[:500])
            resp.raise_for_status()
            async for raw_line in resp.aiter_lines():
                line = raw_line.strip() if raw_line else ""
                if not line or not line.startswith("data:"):
                    continue
                payload_str = line[len("data:"):].strip()
                if payload_str == "[DONE]":
                    if accumulated_tool_calls:
                        ol_tcs = []
                        for idx in sorted(accumulated_tool_calls.keys()):
                            tc = accumulated_tool_calls[idx]
                            args_str = tc.get("arguments", "")
                            try:
                                args_obj = json.loads(args_str) if args_str.strip() else {}
                            except json.JSONDecodeError:
                                args_obj = {"_raw": args_str}
                            ol_tcs.append({
                                "function": {
                                    "name": tc.get("name", ""),
                                    "arguments": args_obj,
                                },
                            })
                        yield {
                            "message": {"role": "assistant", "content": "", "tool_calls": ol_tcs},
                            "done": True,
                        }
                    else:
                        yield {"message": {"role": "assistant", "content": ""}, "done": True}
                    return
                try:
                    chunk = json.loads(payload_str)
                except json.JSONDecodeError:
                    continue
                if isinstance(chunk, dict) and "choices" not in chunk and "data" in chunk:
                    chunk = chunk["data"]
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                for tc_delta in delta.get("tool_calls") or []:
                    idx = tc_delta.get("index", 0)
                    bucket = accumulated_tool_calls.setdefault(idx, {"name": "", "arguments": ""})
                    fn = tc_delta.get("function") or {}
                    if fn.get("name"):
                        bucket["name"] = fn["name"]
                    if fn.get("arguments"):
                        bucket["arguments"] += fn["arguments"]
                content_delta = delta.get("content")
                if content_delta:
                    yield {
                        "message": {"role": "assistant", "content": content_delta},
                        "done": False,
                    }


# ── Concrete provider entry points ─────────────────────────


async def _gemini_chat_sync(
    model: str, messages: list[dict], tools: Optional[list[dict]],
    timeout: Optional[float], options: Optional[dict],
) -> dict:
    base_url, key = _gemini_credentials()
    payload = _build_openai_payload(model, messages, tools, options, stream=False)
    return await _post_openai_compat_sync(base_url, key, payload, timeout)


async def _gemini_chat_stream(
    model: str, messages: list[dict], tools: Optional[list[dict]],
) -> AsyncGenerator[dict, None]:
    base_url, key = _gemini_credentials()
    payload = _build_openai_payload(model, messages, tools, options=None, stream=True)
    async for chunk in _stream_openai_compat(base_url, key, payload):
        yield chunk


async def _openai_compat_chat_sync(
    model: str, messages: list[dict], tools: Optional[list[dict]],
    timeout: Optional[float], options: Optional[dict],
) -> dict:
    base_url, key = _openai_credentials()
    payload = _build_openai_payload(model, messages, tools, options, stream=False)
    return await _post_openai_compat_sync(base_url, key, payload, timeout)


async def _openai_compat_chat_stream(
    model: str, messages: list[dict], tools: Optional[list[dict]],
) -> AsyncGenerator[dict, None]:
    base_url, key = _openai_credentials()
    payload = _build_openai_payload(model, messages, tools, options=None, stream=True)
    async for chunk in _stream_openai_compat(base_url, key, payload):
        yield chunk
