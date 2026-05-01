"""FinHouse — Ollama LLM Client."""

import json
import logging
from typing import AsyncGenerator, Optional

import httpx
from config import get_settings

log = logging.getLogger("finhouse.ollama")

settings = get_settings()

# Tool-capable models (native function calling)
TOOL_CAPABLE_MODELS = {
    "qwen2.5:14b", "qwen2.5:32b", "qwen2.5:7b", "qwen2.5:3b",
    "llama3.1:8b", "llama3.1:70b",
    "mistral-small:24b",
}


async def list_models() -> list[dict]:
    """Fetch available models from Ollama."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{settings.OLLAMA_HOST}/api/tags")
        resp.raise_for_status()
        data = resp.json()
        models = []
        for m in data.get("models", []):
            name = m["name"]
            models.append({
                "name": name,
                "size": m.get("size", 0),
                "modified_at": m.get("modified_at", ""),
                "tool_capable": any(
                    name.startswith(tc.split(":")[0])
                    for tc in TOOL_CAPABLE_MODELS
                ),
            })
        return models


async def chat_stream(
    model: str,
    messages: list[dict],
    tools: Optional[list[dict]] = None,
) -> AsyncGenerator[dict, None]:
    """Stream chat completion from Ollama. Yields parsed JSON chunks."""
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
    }
    if tools:
        payload["tools"] = tools

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
                        chunk = json.loads(line)
                        yield chunk
                    except json.JSONDecodeError:
                        continue


async def chat_sync(
    model: str,
    messages: list[dict],
    tools: Optional[list[dict]] = None,
    timeout: Optional[float] = None,
    options: Optional[dict] = None,
) -> dict:
    """
    Non-streaming chat completion from Ollama.

    Args:
        model: Ollama model tag (e.g. "qwen2.5:14b")
        messages: OpenAI-style message list
        tools: optional tool definitions (function calling)
        timeout: total request timeout in seconds; defaults to 300s
        options: Ollama options dict (temperature, num_predict, etc.)
    """
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
    }
    if tools:
        payload["tools"] = tools
    if options:
        payload["options"] = options

    total_timeout = timeout if timeout is not None else 300.0

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(total_timeout, connect=10.0)
    ) as client:
        resp = await client.post(
            f"{settings.OLLAMA_HOST}/api/chat",
            json=payload,
        )
        if resp.status_code >= 400:
            log.error("Ollama chat_sync %s: %s", resp.status_code, resp.text[:500])
        resp.raise_for_status()
        return resp.json()


async def check_health() -> bool:
    """Check if Ollama is reachable."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{settings.OLLAMA_HOST}/")
            return resp.status_code == 200
    except Exception:
        return False
