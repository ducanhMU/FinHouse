"""
FinHouse — LLM Client (local Ollama + OpenAI-compatible managed backup)

Public API (unchanged for callers):
    chat_sync(model, messages, tools, timeout, options) -> dict
    chat_stream(model, messages, tools) -> AsyncGenerator[dict, None]
    list_models() -> list[dict]
    check_health() -> bool

Both `chat_sync` and `chat_stream` always return / yield Ollama-shaped
dicts (i.e. `{"message": {"content": ..., "tool_calls": [...]}, "done": bool}`).
When the call is routed to the managed API, the OpenAI-compatible
response is translated into that shape so the rest of the codebase
(especially routers/chat.py) needs no awareness of the routing.

Mode (settings.OLLAMA_MODE):
    "local"  — local Ollama only; errors propagate
    "backup" — managed API only; errors propagate
    "auto"   — try local first; after LOCAL_FAILURE_THRESHOLD consecutive
               local failures, sticky-switch to API for the rest of the
               process lifetime. Counter resets on first local recovery.
"""

import asyncio
import json
import logging
from typing import AsyncGenerator, Optional

import httpx

from config import get_settings

log = logging.getLogger("finhouse.ollama")

settings = get_settings()

# Tool-capable models (native function calling on local Ollama).
TOOL_CAPABLE_MODELS = {
    "qwen2.5:14b", "qwen2.5:32b", "qwen2.5:7b", "qwen2.5:3b",
    "llama3.1:8b", "llama3.1:70b",
    "mistral-small:24b",
}


# ── Failure tracking for sticky auto-fallback ───────────────
_local_chat_failures = 0
_use_chat_api = False    # sticky: once switched, stay on API


def _api_configured() -> bool:
    return bool(settings.OLLAMA_API_URL and settings.OLLAMA_API_KEY)


def _resolve_api_model(model: str) -> str:
    """Pick the model name to send to the managed API."""
    return settings.OLLAMA_API_MODEL or model


# ════════════════════════════════════════════════════════════
# Format translation: OpenAI ↔ Ollama
# ════════════════════════════════════════════════════════════

def _ollama_messages_to_openai(messages: list[dict]) -> list[dict]:
    """
    Convert Ollama-style messages to OpenAI chat-completion messages.

    Differences:
      • Ollama tool result role is "tool" with no tool_call_id; OpenAI
        requires tool_call_id. We synthesize a stable id when missing.
      • Ollama assistant tool_calls: arguments is an OBJECT; OpenAI
        expects a JSON-encoded STRING.
    """
    out = []
    last_synth_id_counter = 0
    last_emitted_tool_call_id: Optional[str] = None

    for m in messages:
        role = m.get("role")
        content = m.get("content", "")

        if role == "assistant" and m.get("tool_calls"):
            tc_out = []
            for i, tc in enumerate(m["tool_calls"]):
                fn = tc.get("function", {})
                args = fn.get("arguments")
                if isinstance(args, (dict, list)):
                    args_str = json.dumps(args, ensure_ascii=False)
                elif isinstance(args, str):
                    args_str = args
                else:
                    args_str = "{}"
                last_synth_id_counter += 1
                tc_id = tc.get("id") or f"call_{last_synth_id_counter:06d}"
                last_emitted_tool_call_id = tc_id
                tc_out.append({
                    "id": tc_id,
                    "type": "function",
                    "function": {
                        "name": fn.get("name", ""),
                        "arguments": args_str,
                    },
                })
            out.append({
                "role": "assistant",
                "content": content or "",
                "tool_calls": tc_out,
            })
            continue

        if role == "tool":
            # OpenAI requires tool_call_id. We pair with the last
            # assistant tool_call we emitted; this is correct for the
            # one-tool-per-round pattern the codebase actually uses.
            out.append({
                "role": "tool",
                "content": content or "",
                "tool_call_id": last_emitted_tool_call_id or "call_unknown",
            })
            continue

        if role in ("system", "user", "assistant"):
            out.append({"role": role, "content": content or ""})

    return out


def _openai_message_to_ollama(msg: dict) -> dict:
    """
    Convert an OpenAI chat-completion `message` block back to the
    Ollama shape the rest of the codebase consumes.
    """
    out = {"role": msg.get("role", "assistant"), "content": msg.get("content") or ""}
    raw_tcs = msg.get("tool_calls") or []
    if raw_tcs:
        ol_tcs = []
        for tc in raw_tcs:
            fn = tc.get("function", {}) if isinstance(tc, dict) else {}
            args = fn.get("arguments", "")
            # Ollama callers expect arguments as a dict, not a string.
            if isinstance(args, str):
                try:
                    args_obj = json.loads(args) if args.strip() else {}
                except json.JSONDecodeError:
                    args_obj = {"_raw": args}
            elif isinstance(args, dict):
                args_obj = args
            else:
                args_obj = {}
            ol_tcs.append({
                "function": {
                    "name": fn.get("name", ""),
                    "arguments": args_obj,
                },
            })
        out["tool_calls"] = ol_tcs
    return out


# ════════════════════════════════════════════════════════════
# Local (Ollama) implementations
# ════════════════════════════════════════════════════════════

async def _list_models_local() -> list[dict]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{settings.OLLAMA_HOST}/api/tags")
        resp.raise_for_status()
        data = resp.json()
        out = []
        for m in data.get("models", []):
            name = m["name"]
            out.append({
                "name": name,
                "size": m.get("size", 0),
                "modified_at": m.get("modified_at", ""),
                "tool_capable": any(
                    name.startswith(tc.split(":")[0])
                    for tc in TOOL_CAPABLE_MODELS
                ),
            })
        return out


def _ollama_local_format(options: Optional[dict]) -> Optional[str]:
    """Translate OpenAI-style `response_format` → Ollama-native `format`.

    Ollama supports `format="json"` (any valid JSON) on its native
    `/api/chat` endpoint. We accept the same caller contract as the
    OpenAI-compat path so nodes don't branch on backend.
    """
    if not options:
        return None
    rf = options.get("response_format")
    if isinstance(rf, dict):
        t = rf.get("type")
        if t in ("json_object", "json_schema"):
            return "json"
    return None


async def _chat_stream_local(
    model: str,
    messages: list[dict],
    tools: Optional[list[dict]],
    options: Optional[dict] = None,
) -> AsyncGenerator[dict, None]:
    payload = {"model": model, "messages": messages, "stream": True}
    if tools:
        payload["tools"] = tools
    fmt = _ollama_local_format(options)
    if fmt:
        payload["format"] = fmt

    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0)) as client:
        async with client.stream(
            "POST",
            f"{settings.OLLAMA_HOST}/api/chat",
            json=payload,
        ) as resp:
            if resp.status_code >= 400:
                body = await resp.aread()
                log.error("Ollama chat_stream %s: %s", resp.status_code, body.decode()[:500])
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if line.strip():
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue


async def _chat_sync_local(
    model: str,
    messages: list[dict],
    tools: Optional[list[dict]],
    timeout: Optional[float],
    options: Optional[dict],
) -> dict:
    payload = {"model": model, "messages": messages, "stream": False}
    if tools:
        payload["tools"] = tools
    if options:
        # Pass through Ollama-native `options` (temperature, num_predict…)
        # but exclude OpenAI-style fields we translate separately.
        ollama_opts = {
            k: v for k, v in options.items()
            if k not in {"response_format", "stream_options", "enable_thinking"}
        }
        if ollama_opts:
            payload["options"] = ollama_opts
    fmt = _ollama_local_format(options)
    if fmt:
        payload["format"] = fmt

    total_timeout = timeout if timeout is not None else 300.0

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(total_timeout, connect=10.0)
    ) as client:
        resp = await client.post(f"{settings.OLLAMA_HOST}/api/chat", json=payload)
        if resp.status_code >= 400:
            log.error("Ollama chat_sync %s: %s", resp.status_code, resp.text[:500])
        resp.raise_for_status()
        data = resp.json()
    # Ollama's native response carries token counts at the top level.
    # Surface them in the same Ollama-shape envelope as the API path.
    pt = int(data.get("prompt_eval_count") or 0)
    ct = int(data.get("eval_count") or 0)
    if pt or ct:
        data["usage"] = {
            "input_tokens": pt,
            "output_tokens": ct,
            "total_tokens": pt + ct,
            "calls": 1,
        }
    return data


# ════════════════════════════════════════════════════════════
# OpenAI-compatible API (managed backup)
# ════════════════════════════════════════════════════════════

def _openai_payload(
    model: str,
    messages: list[dict],
    tools: Optional[list[dict]],
    options: Optional[dict],
    stream: bool,
) -> dict:
    """Build a standard OpenAI chat.completions request body."""
    payload = {
        "model": _resolve_api_model(model),
        "messages": _ollama_messages_to_openai(messages),
        "stream": stream,
    }
    if tools:
        payload["tools"] = tools
    if options:
        # Ollama-style options → OpenAI chat-completion fields.
        if "temperature" in options:
            payload["temperature"] = options["temperature"]
        if "num_predict" in options:
            payload["max_tokens"] = int(options["num_predict"])
        if "top_p" in options:
            payload["top_p"] = options["top_p"]
        if "top_k" in options:
            payload["top_k"] = options["top_k"]
        # JSON-mode / structured output — passes straight through to
        # OpenAI-compat providers (FPT Cloud, etc.).
        if options.get("response_format"):
            payload["response_format"] = options["response_format"]
    if stream:
        stream_opts = (options or {}).get("stream_options") or {}
        payload["stream_options"] = {
            "include_usage": True,
            **stream_opts,
        }
    return payload


def _parse_openai_usage(usage_obj) -> Optional[dict]:
    """Normalise OpenAI-shape usage block into our standard dict."""
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


def _openai_headers() -> dict:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings.OLLAMA_API_KEY}",
    }


def _openai_url() -> str:
    return f"{settings.OLLAMA_API_URL.rstrip('/')}/chat/completions"


async def _chat_sync_api(
    model: str,
    messages: list[dict],
    tools: Optional[list[dict]],
    timeout: Optional[float],
    options: Optional[dict],
) -> dict:
    """Non-streaming completion via OpenAI-compatible API."""
    if not _api_configured():
        raise RuntimeError("OLLAMA_API_URL / OLLAMA_API_KEY not configured")

    payload = _openai_payload(model, messages, tools, options, stream=False)
    total_timeout = timeout if timeout is not None else 300.0

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(total_timeout, connect=10.0)
    ) as client:
        resp = await client.post(_openai_url(), json=payload, headers=_openai_headers())
        if resp.status_code >= 400:
            log.error("API chat_sync %s: %s", resp.status_code, resp.text[:500])
        resp.raise_for_status()
        data = resp.json()

    # Some providers wrap the OpenAI envelope with their own
    # `{code, message, data: {...}}` (FPT Cloud sample). Unwrap.
    if isinstance(data, dict) and "choices" not in data and "data" in data:
        data = data["data"]

    choices = data.get("choices") or []
    usage = _parse_openai_usage(data.get("usage"))
    if not choices:
        out: dict = {"message": {"role": "assistant", "content": ""}, "done": True}
        if usage:
            out["usage"] = usage
        return out

    msg = choices[0].get("message") or {}
    out = {
        "message": _openai_message_to_ollama(msg),
        "done": True,
    }
    if usage:
        out["usage"] = usage
    return out


async def _chat_stream_api(
    model: str,
    messages: list[dict],
    tools: Optional[list[dict]],
    options: Optional[dict] = None,
) -> AsyncGenerator[dict, None]:
    """
    Streaming completion via OpenAI-compatible SSE.

    OpenAI delta format:
        data: {"choices":[{"delta":{"content":"..."}}]}
        data: [DONE]
    Tool calls in deltas have the same shape as in non-streaming
    responses but are split across chunks (function name in one delta,
    arguments accumulated across many). We accumulate them and emit
    one Ollama-shaped final chunk with the full tool_calls block, plus
    incremental content chunks.
    """
    if not _api_configured():
        raise RuntimeError("OLLAMA_API_URL / OLLAMA_API_KEY not configured")

    payload = _openai_payload(model, messages, tools, options=options, stream=True)

    # Accumulators across deltas
    accumulated_tool_calls: dict[int, dict] = {}
    last_usage: Optional[dict] = None

    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0)) as client:
        async with client.stream(
            "POST", _openai_url(), json=payload, headers=_openai_headers(),
        ) as resp:
            if resp.status_code >= 400:
                body = await resp.aread()
                log.error("API chat_stream %s: %s", resp.status_code, body.decode()[:500])
            resp.raise_for_status()

            async for raw_line in resp.aiter_lines():
                line = raw_line.strip() if raw_line else ""
                if not line:
                    continue
                if not line.startswith("data:"):
                    continue
                payload_str = line[len("data:"):].strip()
                if payload_str == "[DONE]":
                    # Final chunk — flush any accumulated tool_calls.
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

                # Some providers wrap streaming chunks too
                if isinstance(chunk, dict) and "choices" not in chunk and "data" in chunk:
                    chunk = chunk["data"]

                u = _parse_openai_usage(chunk.get("usage"))
                if u is not None:
                    last_usage = u

                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}

                # Accumulate tool_calls if present
                for tc_delta in delta.get("tool_calls") or []:
                    idx = tc_delta.get("index", 0)
                    bucket = accumulated_tool_calls.setdefault(
                        idx, {"name": "", "arguments": ""}
                    )
                    fn = tc_delta.get("function") or {}
                    if fn.get("name"):
                        bucket["name"] = fn["name"]
                    if fn.get("arguments"):
                        bucket["arguments"] += fn["arguments"]

                # Emit incremental content as an Ollama-shaped chunk.
                content_delta = delta.get("content")
                if content_delta:
                    yield {
                        "message": {"role": "assistant", "content": content_delta},
                        "done": False,
                    }

                # Some providers put final tool_calls only in the last
                # non-delta `message` field. Handle that defensively:
                final_msg = choices[0].get("message")
                if final_msg and final_msg.get("tool_calls") and not accumulated_tool_calls:
                    yield {
                        "message": _openai_message_to_ollama(final_msg),
                        "done": True,
                    }
                    return


async def _list_models_api() -> list[dict]:
    """Best-effort model listing for the API. Many providers gate
    `/v1/models`; if it fails, just expose the configured override
    (or DEFAULT_MODEL) so the UI has at least one entry."""
    if not _api_configured():
        return []
    url = f"{settings.OLLAMA_API_URL.rstrip('/')}/models"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=_openai_headers())
            resp.raise_for_status()
            data = resp.json()
            items = data.get("data") or data.get("models") or []
            out = []
            for it in items:
                name = it.get("id") or it.get("name")
                if not name:
                    continue
                out.append({
                    "name": name,
                    "size": 0,
                    "modified_at": "",
                    "tool_capable": True,  # assume yes; provider-specific
                })
            if out:
                return out
    except Exception as e:
        log.info("API list_models fallback: %s", e)

    fallback = settings.OLLAMA_API_MODEL or settings.DEFAULT_MODEL
    return [{"name": fallback, "size": 0, "modified_at": "", "tool_capable": True}]


# ════════════════════════════════════════════════════════════
# Public dispatchers — handle mode + sticky auto-fallback
# ════════════════════════════════════════════════════════════

def _mode() -> str:
    return (settings.OLLAMA_MODE or "local").lower().strip()


async def list_models() -> list[dict]:
    mode = _mode()
    if mode == "backup":
        return await _list_models_api()
    if mode == "auto" and _use_chat_api:
        return await _list_models_api()
    try:
        return await _list_models_local()
    except Exception as e:
        log.warning("list_models local failed: %s", e)
        if _api_configured():
            return await _list_models_api()
        raise


async def chat_sync(
    model: str,
    messages: list[dict],
    tools: Optional[list[dict]] = None,
    timeout: Optional[float] = None,
    options: Optional[dict] = None,
) -> dict:
    """
    Non-streaming chat completion. Always returns Ollama-shaped dict:
        {"message": {"role": "assistant", "content": "...", "tool_calls": [...]}, "done": true}

    Routing follows OLLAMA_MODE:
        local  → Ollama only
        backup → API only
        auto   → Ollama, with sticky API fallback after repeated failures
    """
    global _local_chat_failures, _use_chat_api
    mode = _mode()

    if mode == "backup":
        return await _chat_sync_api(model, messages, tools, timeout, options)

    if mode == "local":
        return await _chat_sync_local(model, messages, tools, timeout, options)

    # auto
    if _use_chat_api and _api_configured():
        return await _chat_sync_api(model, messages, tools, timeout, options)

    try:
        result = await _chat_sync_local(model, messages, tools, timeout, options)
        if _local_chat_failures > 0:
            log.info("Local chat recovered, resetting failure counter")
            _local_chat_failures = 0
        return result
    except Exception as local_err:
        _local_chat_failures += 1
        log.warning(
            "Local chat failed (%d/%d): %s",
            _local_chat_failures, settings.LOCAL_FAILURE_THRESHOLD, local_err,
        )
        if not _api_configured():
            raise
        if _local_chat_failures >= settings.LOCAL_FAILURE_THRESHOLD:
            log.warning("🔀 Sticky-switch to managed chat API: %s", settings.OLLAMA_API_URL)
            _use_chat_api = True
        try:
            return await _chat_sync_api(model, messages, tools, timeout, options)
        except Exception as api_err:
            raise RuntimeError(
                f"Both chat providers failed. Local: {local_err}. API: {api_err}"
            ) from api_err


async def chat_stream(
    model: str,
    messages: list[dict],
    tools: Optional[list[dict]] = None,
    options: Optional[dict] = None,
) -> AsyncGenerator[dict, None]:
    """
    Streaming chat completion. Always yields Ollama-shaped chunks:
        {"message": {"role": "assistant", "content": "delta"}, "done": false/true}

    The optional `options` dict carries the same keys as `chat_sync`
    (response_format, stream_options, temperature…) so callers can
    request JSON-mode or schema-bound output during streaming too.

    See `chat_sync` for routing rules. For auto-mode, fallback to API
    only happens BEFORE the stream starts; once we begin yielding, we
    don't switch providers mid-stream (would corrupt token order).
    """
    global _local_chat_failures, _use_chat_api
    mode = _mode()

    if mode == "backup":
        async for chunk in _chat_stream_api(model, messages, tools, options):
            yield chunk
        return

    if mode == "local":
        async for chunk in _chat_stream_local(model, messages, tools, options):
            yield chunk
        return

    # auto
    if _use_chat_api and _api_configured():
        async for chunk in _chat_stream_api(model, messages, tools, options):
            yield chunk
        return

    # Try to OPEN the local stream; if that throws, fall over to API.
    try:
        gen = _chat_stream_local(model, messages, tools, options)
        first = await gen.__anext__()
    except StopAsyncIteration:
        return
    except Exception as local_err:
        _local_chat_failures += 1
        log.warning(
            "Local chat_stream open failed (%d/%d): %s",
            _local_chat_failures, settings.LOCAL_FAILURE_THRESHOLD, local_err,
        )
        if not _api_configured():
            raise
        if _local_chat_failures >= settings.LOCAL_FAILURE_THRESHOLD:
            log.warning("🔀 Sticky-switch to managed chat API: %s", settings.OLLAMA_API_URL)
            _use_chat_api = True
        async for chunk in _chat_stream_api(model, messages, tools, options):
            yield chunk
        return

    # Local opened cleanly — count it as a recovery and stream through.
    if _local_chat_failures > 0:
        log.info("Local chat_stream recovered, resetting failure counter")
        _local_chat_failures = 0
    yield first
    async for chunk in gen:
        yield chunk


async def check_health() -> bool:
    """Health check. Considered healthy if the routed provider is up."""
    mode = _mode()
    if mode == "backup":
        return _api_configured()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{settings.OLLAMA_HOST}/")
            if resp.status_code == 200:
                return True
    except Exception:
        pass
    if mode == "auto" and _api_configured():
        return True
    return False
