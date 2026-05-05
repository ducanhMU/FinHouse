"""
FinHouse — SSE event helpers for the graph runtime.

Nodes emit events into an asyncio.Queue carried in the `RunnableConfig`.
The chat router drains this queue concurrently with the graph
invocation, both forwarding events as Server-Sent Events to the UI and
persisting selected ones to the chat_event table.

Event shape (Pydantic):
    {
        "type": "<sse_type>",
        "payload": { ... },         # forwarded to UI as JSON
        "persist": {                # optional, drives chat_event insert
            "role": "user|assistant|system",
            "text": "...",
            "event_type": "message|tool_call|tool_result|rag_context",
        } | None,
    }

The set of `type` values mirrors the SSE protocol the UI already
consumes (token, tool_start, tool_end, rag_sources, …) so the front-end
needs no changes.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel


# ── Event model ──────────────────────────────────────────────


class PersistSpec(BaseModel):
    role: str
    text: str
    event_type: str


class GraphEvent(BaseModel):
    type: str
    payload: dict[str, Any] = {}
    persist: Optional[PersistSpec] = None


# ── Queue helper ─────────────────────────────────────────────


CONFIG_QUEUE_KEY = "sse_queue"


def get_queue(config: Optional[RunnableConfig]) -> Optional[asyncio.Queue]:
    """Pull the shared SSE queue out of the runtime config (if any)."""
    if not config:
        return None
    cfg = config.get("configurable") or {}
    q = cfg.get(CONFIG_QUEUE_KEY)
    if isinstance(q, asyncio.Queue):
        return q
    return None


async def emit(
    config: Optional[RunnableConfig],
    type: str,
    payload: Optional[dict] = None,
    *,
    persist: Optional[PersistSpec] = None,
) -> None:
    """Publish an event to the SSE queue, if one is configured.

    Silently no-ops when no queue is wired up — useful for unit tests
    that want to invoke a node without a streaming consumer.
    """
    q = get_queue(config)
    if q is None:
        return
    evt = GraphEvent(type=type, payload=payload or {}, persist=persist)
    await q.put(evt)


# ── Convenience wrappers ─────────────────────────────────────


async def emit_token(config: RunnableConfig, content: str) -> None:
    await emit(config, "token", {"content": content})


async def emit_reasoning(config: RunnableConfig, content: str) -> None:
    await emit(config, "reasoning", {"content": content})


async def emit_tool_start(
    config: RunnableConfig, tool: str, args: dict, agent: str = "",
) -> None:
    await emit(
        config, "tool_start",
        {"tool": tool, "args": args, "agent": agent},
        persist=PersistSpec(
            role="assistant",
            text='{"tool": "%s", "args": %s}' % (tool, _safe_json(args)),
            event_type="tool_call",
        ),
    )


async def emit_tool_end(
    config: RunnableConfig, tool: str, content: str,
    error: bool = False, agent: str = "",
) -> None:
    # Stream full content (no 500-char cap). Old behavior truncated for
    # SSE-perf reasons but it meant the live UI showed empty/partial
    # results that only filled in after a page reload — confusing for
    # auditing tool calls. The renderer side caps display itself.
    #
    # Persisted text is JSON-wrapped {tool, content} so the reload path
    # can attribute each result to its tool even when multiple agents
    # ran in parallel and emitted interleaved start/end pairs. Old raw
    # events still load fine — load_session_events falls back to
    # last-seen-tool-name pairing when the wrapper is missing.
    import json as _json
    persisted_text = _json.dumps(
        {"tool": tool, "content": content},
        ensure_ascii=False,
    )
    await emit(
        config, "tool_end",
        {"tool": tool, "error": error, "content": content, "agent": agent},
        persist=PersistSpec(
            role="system",
            text=persisted_text,
            event_type="tool_result",
        ),
    )


async def emit_done(config: RunnableConfig) -> None:
    await emit(config, "done", {})


# Sentinel pushed into the queue when the graph is finished, so the
# drain coroutine can exit cleanly.
SENTINEL = object()


async def push_sentinel(queue: asyncio.Queue) -> None:
    await queue.put(SENTINEL)


def _safe_json(obj: Any) -> str:
    import json
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        return "{}"
