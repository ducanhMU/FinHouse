"""FinHouse — Ollama LLM Client."""

import json
from typing import AsyncGenerator, Optional

import httpx
from config import get_settings

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
) -> dict:
    """Non-streaming chat completion from Ollama."""
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
    }
    if tools:
        payload["tools"] = tools

    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0)) as client:
        resp = await client.post(
            f"{settings.OLLAMA_HOST}/api/chat",
            json=payload,
        )
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
