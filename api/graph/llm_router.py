"""
FinHouse — Per-agent LLM routing with fallback chain.

Each node in the graph asks `get_llm("rewriter", session_model)` and
receives an `LLMHandle` exposing the same `chat_sync` / `chat_stream`
contract used elsewhere in the codebase. This keeps node code provider-
agnostic — Ollama, DashScope, Gemini, or any OpenAI-compatible endpoint
look the same from the caller's side.

Each `*_AGENT_LLM` env var is a COMMA-SEPARATED CHAIN: primary first,
then fallbacks. The handle tries each spec in order; on quota / rate-
limit (HTTP 429) or transient 5xx / network errors it rotates to the
next entry. This lets us spread agents across multiple DashScope models
(each with its own ~1M token daily budget) and survive single-model
exhaustion automatically.

Config strings live in `.env`:
    DB_AGENT_LLM=dashscope:qwen3-coder-plus,dashscope:qwen3.6-plus,dashscope:qwen2.5-coder-32b-instruct
    REWRITER_AGENT_LLM=dashscope:qwen2.5-7b-instruct,dashscope:qwen-turbo
    VIS_AGENT_LLM=                       # empty → fall back to session model

Per-agent reasoning toggle (DashScope thinking models only):
    DB_AGENT_THINKING=true
    ORCHESTRATOR_AGENT_THINKING=true

Empty chain falls back to the chat session's selected model on local
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
    provider: str   # "ollama" | "gemini" | "openai" | "dashscope"
    model: str

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model}"


_VALID_PROVIDERS = {"ollama", "gemini", "openai", "dashscope"}


def _parse_one(part: str, fallback_model: str) -> Optional[LLMSpec]:
    """Parse a single 'provider:model' token. Returns None if unusable."""
    part = part.strip()
    if not part:
        return None
    if ":" not in part:
        # Bare model name — assume ollama.
        return LLMSpec("ollama", part)
    provider, _, model = part.partition(":")
    provider = provider.strip().lower()
    model = model.strip()
    if provider not in _VALID_PROVIDERS:
        log.warning("Unknown LLM provider %r in spec %r — skipping", provider, part)
        return None
    if not model:
        return LLMSpec(provider, fallback_model)
    return LLMSpec(provider, model)


def parse_chain(raw: str, fallback_model: str) -> list[LLMSpec]:
    """
    Parse a comma-separated chain of specs.

    "dashscope:qwen3-coder-plus,dashscope:qwen3.6-plus"
        → [LLMSpec("dashscope","qwen3-coder-plus"),
           LLMSpec("dashscope","qwen3.6-plus")]
    "ollama:qwen2.5:14b"  → [LLMSpec("ollama","qwen2.5:14b")]
    ""                    → [LLMSpec("ollama", fallback_model)]
    """
    raw = (raw or "").strip()
    if not raw:
        return [LLMSpec("ollama", fallback_model)]
    chain: list[LLMSpec] = []
    for token in raw.split(","):
        spec = _parse_one(token, fallback_model)
        if spec is not None:
            chain.append(spec)
    if not chain:
        return [LLMSpec("ollama", fallback_model)]
    return chain


def parse_spec(raw: str, fallback_model: str) -> LLMSpec:
    """Legacy single-spec parser. Returns the primary entry of the chain.

    Kept for /agents endpoint backward compatibility — new code should
    use `parse_chain` and operate on the full chain.
    """
    return parse_chain(raw, fallback_model)[0]


# ── Error classification ────────────────────────────────────


def _is_rotatable_error(e: BaseException) -> bool:
    """Should we rotate to the next spec on this error?

    True for quota / rate-limit / transient infrastructure failures.
    False for auth errors, malformed requests, or programming bugs —
    rotating won't help.
    """
    if isinstance(e, httpx.HTTPStatusError):
        return e.response.status_code in {408, 425, 429, 500, 502, 503, 504, 529}
    if isinstance(e, (
        httpx.TimeoutException,
        httpx.ConnectError,
        httpx.ReadError,
        httpx.RemoteProtocolError,
        httpx.NetworkError,
    )):
        return True
    return False


# ── Handle ──────────────────────────────────────────────────


class LLMHandle:
    """
    Uniform sync/stream chat surface across providers + a fallback chain.
    Always returns / yields Ollama-shaped dicts:
        {"message": {"role": ..., "content": ..., "tool_calls": [...]}, "done": bool}
    """

    def __init__(self, chain: list[LLMSpec], enable_thinking: bool = False):
        if not chain:
            raise ValueError("LLMHandle requires at least one LLMSpec")
        self.chain: list[LLMSpec] = list(chain)
        self.enable_thinking = enable_thinking

    @property
    def primary(self) -> LLMSpec:
        return self.chain[0]

    @property
    def label(self) -> str:
        suffix = f"+{len(self.chain) - 1}fb" if len(self.chain) > 1 else ""
        return f"{self.primary.label}{suffix}"

    @property
    def chain_labels(self) -> list[str]:
        return [s.label for s in self.chain]

    def _merge_options(self, options: Optional[dict]) -> dict:
        """Inject per-agent enable_thinking into call options.

        Per-call `options["enable_thinking"]` (if set by the caller)
        wins over the agent-level setting — the rewriter / collector can
        force-disable reasoning for a particular turn.
        """
        merged = dict(options or {})
        merged.setdefault("enable_thinking", self.enable_thinking)
        return merged

    async def _call_one_sync(
        self,
        spec: LLMSpec,
        messages: list[dict],
        tools: Optional[list[dict]],
        timeout: Optional[float],
        options: Optional[dict],
    ) -> dict:
        if spec.provider == "ollama":
            return await ollama_chat_sync(
                spec.model, messages, tools=tools,
                timeout=timeout, options=options,
            )
        if spec.provider == "dashscope":
            return await _dashscope_chat_sync(
                spec.model, messages, tools, timeout, options,
            )
        if spec.provider == "gemini":
            return await _gemini_chat_sync(
                spec.model, messages, tools, timeout, options,
            )
        if spec.provider == "openai":
            return await _openai_compat_chat_sync(
                spec.model, messages, tools, timeout, options,
            )
        raise RuntimeError(f"Unsupported provider {spec.provider!r}")

    async def chat_sync(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        timeout: Optional[float] = None,
        options: Optional[dict] = None,
    ) -> dict:
        merged_options = self._merge_options(options)
        last_err: Optional[BaseException] = None
        for i, spec in enumerate(self.chain):
            try:
                return await self._call_one_sync(
                    spec, messages, tools, timeout, merged_options,
                )
            except Exception as e:
                last_err = e
                has_next = i < len(self.chain) - 1
                if has_next and _is_rotatable_error(e):
                    nxt = self.chain[i + 1]
                    log.warning(
                        "[llm] sync %s failed (%s: %s) — rotating to %s",
                        spec.label, type(e).__name__, e, nxt.label,
                    )
                    continue
                raise
        # Should be unreachable — last spec either returned or raised.
        assert last_err is not None
        raise last_err

    async def _stream_one(
        self,
        spec: LLMSpec,
        messages: list[dict],
        tools: Optional[list[dict]],
        options: Optional[dict],
    ) -> AsyncGenerator[dict, None]:
        if spec.provider == "ollama":
            async for chunk in ollama_chat_stream(
                spec.model, messages, tools=tools, options=options,
            ):
                yield chunk
            return
        if spec.provider == "dashscope":
            async for chunk in _dashscope_chat_stream(
                spec.model, messages, tools, options,
            ):
                yield chunk
            return
        if spec.provider == "gemini":
            async for chunk in _gemini_chat_stream(spec.model, messages, tools, options):
                yield chunk
            return
        if spec.provider == "openai":
            async for chunk in _openai_compat_chat_stream(spec.model, messages, tools, options):
                yield chunk
            return
        raise RuntimeError(f"Unsupported provider {spec.provider!r}")

    async def chat_stream(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        options: Optional[dict] = None,
    ) -> AsyncGenerator[dict, None]:
        merged_options = self._merge_options(options)
        last_err: Optional[BaseException] = None
        for i, spec in enumerate(self.chain):
            yielded = False
            try:
                async for chunk in self._stream_one(
                    spec, messages, tools, merged_options,
                ):
                    yielded = True
                    yield chunk
                return
            except Exception as e:
                # Mid-stream errors can't be recovered — partial content
                # has already gone to the client. Just propagate.
                if yielded:
                    raise
                last_err = e
                has_next = i < len(self.chain) - 1
                if has_next and _is_rotatable_error(e):
                    nxt = self.chain[i + 1]
                    log.warning(
                        "[llm] stream %s failed (%s: %s) — rotating to %s",
                        spec.label, type(e).__name__, e, nxt.label,
                    )
                    continue
                raise
        assert last_err is not None
        raise last_err


# ── Public entry point ──────────────────────────────────────

# Maps the agent name used in get_llm() to the (LLM_chain, THINKING) env
# var pair on the Settings object.
_AGENT_ENV: dict[str, tuple[str, str]] = {
    "rewriter":     ("REWRITER_AGENT_LLM",     "REWRITER_AGENT_THINKING"),
    "orchestrator": ("ORCHESTRATOR_AGENT_LLM", "ORCHESTRATOR_AGENT_THINKING"),
    "web":          ("WEB_AGENT_LLM",          "WEB_AGENT_THINKING"),
    "database":     ("DB_AGENT_LLM",           "DB_AGENT_THINKING"),
    "visualize":    ("VIS_AGENT_LLM",          "VIS_AGENT_THINKING"),
    "collector":    ("COLLECTOR_AGENT_LLM",    "COLLECTOR_AGENT_THINKING"),
}


def get_llm(agent: str, session_model: str) -> LLMHandle:
    """Return the LLM handle configured for the given agent role.

    `session_model` is the user-selected model on the chat session; it
    is used as the fallback when the agent's env var is empty so the
    out-of-the-box behavior matches the legacy single-brain pipeline.
    """
    pair = _AGENT_ENV.get(agent)
    raw_chain = ""
    enable_thinking = False
    if pair:
        env_chain, env_thinking = pair
        raw_chain = (getattr(settings, env_chain, "") or "").strip()
        enable_thinking = bool(getattr(settings, env_thinking, False))
    chain = parse_chain(raw_chain, session_model)
    return LLMHandle(chain, enable_thinking=enable_thinking)


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
        # JSON-mode / structured output. Caller passes either:
        #   {"type": "json_object"}              — any valid JSON
        #   {"type": "json_schema", "json_schema": {...}} — schema-bound
        if options.get("response_format"):
            payload["response_format"] = options["response_format"]
    if stream:
        # Always ask providers to include token usage on the final chunk
        # so we can track per-agent quota burn. Caller can override.
        stream_opts = (options or {}).get("stream_options") or {}
        payload["stream_options"] = {
            "include_usage": True,
            **stream_opts,
        }
    return payload


def _parse_usage(usage_obj: Optional[dict]) -> Optional[dict]:
    """Normalise an OpenAI-shape usage block into Ollama-shape dict.

    Returns None when `usage_obj` is missing or empty so callers can
    skip attaching a zeroed `usage` field.
    """
    if not isinstance(usage_obj, dict) or not usage_obj:
        return None
    pt = int(usage_obj.get("prompt_tokens") or usage_obj.get("input_tokens") or 0)
    ct = int(usage_obj.get("completion_tokens") or usage_obj.get("output_tokens") or 0)
    tt = int(usage_obj.get("total_tokens") or (pt + ct))
    if pt == 0 and ct == 0 and tt == 0:
        return None
    return {
        "input_tokens": pt,
        "output_tokens": ct,
        "total_tokens": tt,
        "calls": 1,
    }


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
    usage = _parse_usage(data.get("usage"))
    if not choices:
        out: dict = {"message": {"role": "assistant", "content": ""}, "done": True}
        if usage:
            out["usage"] = usage
        return out
    msg = choices[0].get("message") or {}
    out = {"message": _openai_message_to_ollama(msg), "done": True}
    if usage:
        out["usage"] = usage
    return out


async def _stream_openai_compat(
    base_url: str, key: str, payload: dict,
) -> AsyncGenerator[dict, None]:
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
    accumulated_tool_calls: dict[int, dict] = {}
    last_usage: Optional[dict] = None
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
                    final_msg: dict = {"role": "assistant", "content": ""}
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
                        final_msg["tool_calls"] = ol_tcs
                    final_chunk: dict = {"message": final_msg, "done": True}
                    if last_usage is not None:
                        final_chunk["usage"] = last_usage
                    yield final_chunk
                    return
                try:
                    chunk = json.loads(payload_str)
                except json.JSONDecodeError:
                    continue
                if isinstance(chunk, dict) and "choices" not in chunk and "data" in chunk:
                    chunk = chunk["data"]
                # Usage may arrive in its own chunk (with empty choices)
                # when stream_options.include_usage is set — capture it
                # for emission alongside the final [DONE] marker.
                u = _parse_usage(chunk.get("usage"))
                if u is not None:
                    last_usage = u
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
    options: Optional[dict] = None,
) -> AsyncGenerator[dict, None]:
    base_url, key = _gemini_credentials()
    payload = _build_openai_payload(model, messages, tools, options=options, stream=True)
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
    options: Optional[dict] = None,
) -> AsyncGenerator[dict, None]:
    base_url, key = _openai_credentials()
    payload = _build_openai_payload(model, messages, tools, options=options, stream=True)
    async for chunk in _stream_openai_compat(base_url, key, payload):
        yield chunk


# ════════════════════════════════════════════════════════════
# Alibaba DashScope (Model Studio) — OpenAI-compat + thinking
# ════════════════════════════════════════════════════════════
#
# Same wire format as OpenAI / Gemini, plus two extras:
#   • `extra_body: {"enable_thinking": true|false}` toggles Qwen-3
#     reasoning. Streamed reasoning shows up as `reasoning_content` in
#     deltas (separate from `content`).
#   • Sync responses on thinking models include
#     `choices[0].message.reasoning_content`.
#
# We surface reasoning to the rest of the graph as Ollama-shaped
# `thinking` chunks, identical to how services/ollama.py emits them
# for gpt-oss / Qwen-thinking on local Ollama.


def _dashscope_credentials() -> tuple[str, str]:
    if not settings.DASHSCOPE_API_KEY:
        raise RuntimeError("DASHSCOPE_API_KEY not configured")
    return settings.DASHSCOPE_API_URL.rstrip("/"), settings.DASHSCOPE_API_KEY


def _build_dashscope_payload(
    model: str,
    messages: list[dict],
    tools: Optional[list[dict]],
    options: Optional[dict],
    stream: bool,
) -> dict:
    """OpenAI-shape payload + DashScope `extra_body.enable_thinking`.

    Resolution order for `enable_thinking`:
      1. `options["enable_thinking"]` — explicit per-call value (set by
         LLMHandle from the per-agent flag, or by callers overriding).
      2. `settings.DASHSCOPE_ENABLE_THINKING` — global default.
    """
    payload = _build_openai_payload(model, messages, tools, options, stream)
    enable_thinking = settings.DASHSCOPE_ENABLE_THINKING
    if options and "enable_thinking" in options:
        enable_thinking = bool(options["enable_thinking"])
    payload["extra_body"] = {"enable_thinking": enable_thinking}
    return payload


async def _dashscope_chat_sync(
    model: str, messages: list[dict], tools: Optional[list[dict]],
    timeout: Optional[float], options: Optional[dict],
) -> dict:
    base_url, key = _dashscope_credentials()
    payload = _build_dashscope_payload(model, messages, tools, options, stream=False)
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
    total_timeout = timeout if timeout is not None else 300.0

    async with httpx.AsyncClient(timeout=httpx.Timeout(total_timeout, connect=10.0)) as client:
        resp = await client.post(f"{base_url}/chat/completions", json=payload, headers=headers)
        if resp.status_code >= 400:
            log.error("DashScope chat_sync %s: %s", resp.status_code, resp.text[:500])
        resp.raise_for_status()
        data = resp.json()

    if isinstance(data, dict) and "choices" not in data and "data" in data:
        data = data["data"]
    choices = data.get("choices") or []
    usage = _parse_usage(data.get("usage"))
    if not choices:
        out_resp: dict = {"message": {"role": "assistant", "content": ""}, "done": True}
        if usage:
            out_resp["usage"] = usage
        return out_resp
    msg = choices[0].get("message") or {}
    out = _openai_message_to_ollama(msg)
    # DashScope thinking models put reasoning text here on sync calls
    reasoning = msg.get("reasoning_content")
    if reasoning:
        out["thinking"] = reasoning
    out_resp = {"message": out, "done": True}
    if usage:
        out_resp["usage"] = usage
    return out_resp


async def _dashscope_chat_stream(
    model: str, messages: list[dict], tools: Optional[list[dict]],
    options: Optional[dict] = None,
) -> AsyncGenerator[dict, None]:
    """Streaming with DashScope reasoning_content surfaced as `thinking`."""
    base_url, key = _dashscope_credentials()
    payload = _build_dashscope_payload(model, messages, tools, options, stream=True)
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}

    accumulated_tool_calls: dict[int, dict] = {}
    last_usage: Optional[dict] = None

    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0)) as client:
        async with client.stream(
            "POST", f"{base_url}/chat/completions",
            json=payload, headers=headers,
        ) as resp:
            if resp.status_code >= 400:
                body = await resp.aread()
                log.error("DashScope stream error %s: %s",
                          resp.status_code, body.decode()[:500])
            resp.raise_for_status()

            async for raw_line in resp.aiter_lines():
                line = raw_line.strip() if raw_line else ""
                if not line or not line.startswith("data:"):
                    continue
                payload_str = line[len("data:"):].strip()
                if payload_str == "[DONE]":
                    final_msg: dict = {"role": "assistant", "content": ""}
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
                        final_msg["tool_calls"] = ol_tcs
                    final_chunk: dict = {"message": final_msg, "done": True}
                    if last_usage is not None:
                        final_chunk["usage"] = last_usage
                    yield final_chunk
                    return
                try:
                    chunk = json.loads(payload_str)
                except json.JSONDecodeError:
                    continue
                if isinstance(chunk, dict) and "choices" not in chunk and "data" in chunk:
                    chunk = chunk["data"]
                u = _parse_usage(chunk.get("usage"))
                if u is not None:
                    last_usage = u
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}

                # Accumulate tool_calls
                for tc_delta in delta.get("tool_calls") or []:
                    idx = tc_delta.get("index", 0)
                    bucket = accumulated_tool_calls.setdefault(idx, {"name": "", "arguments": ""})
                    fn = tc_delta.get("function") or {}
                    if fn.get("name"):
                        bucket["name"] = fn["name"]
                    if fn.get("arguments"):
                        bucket["arguments"] += fn["arguments"]

                # Reasoning (thinking) — DashScope only field
                reasoning_delta = delta.get("reasoning_content")
                if reasoning_delta:
                    yield {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "thinking": reasoning_delta,
                        },
                        "done": False,
                    }

                # Final content delta
                content_delta = delta.get("content")
                if content_delta:
                    yield {
                        "message": {"role": "assistant", "content": content_delta},
                        "done": False,
                    }
