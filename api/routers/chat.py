"""FinHouse — Chat Router (message send, stream, events).

The answer-generation pipeline is now a LangGraph multi-ReAct topology
(see `api/graph/`). This router:

    1. Authorizes the request and persists the user message.
    2. Builds the trimmed conversation history (intent-change aware).
    3. Invokes the compiled graph in a background task while draining
       the graph's SSE event queue, forwarding events to the client and
       persisting selected ones (`tool_call`, `tool_result`,
       `rag_context`, `message`) to the chat_event table.
    4. Handles cancellation via the active-streams map.
"""

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from database import async_session_factory, get_db
from graph import (
    ChatState,
    GraphEvent,
    PersistSpec,
    SENTINEL,
    get_graph,
    push_sentinel,
)
from models import ChatEvent, ChatSession
from routers.auth import get_current_user
from routers.sessions import authorize_session

router = APIRouter(prefix="/chat", tags=["chat"])
log = logging.getLogger("finhouse.chat")

# Max chars in a user message — prevents context bloat and Ollama OOM
MAX_MESSAGE_LENGTH = 32_000

# Track active streams for cancellation.
# NOTE: in-memory per worker. For multi-worker deploys move to Redis pub/sub.
_active_streams: dict[str, bool] = {}


# ════════════════════════════════════════════════════════════
# Intent change detection — kept verbatim from the legacy router.
# Used to trim history before feeding it to the graph.
# ════════════════════════════════════════════════════════════


def _detect_intent_change(prev_user_msgs: list[str], new_msg: str) -> bool:
    if not prev_user_msgs:
        return False

    _VI_STOP = {
        "công", "ty", "doanh", "nghiệp", "tôi", "bạn", "của", "cho",
        "với", "như", "thế", "nào", "thế", "gì", "sao", "khi", "trong",
        "một", "các", "những", "nhất", "hiện", "được", "đang", "hãy",
        "cho", "biết", "giúp", "tôi", "mình", "đây", "này", "kia",
        "năm", "tháng", "quý", "ngày",
    }
    _EN_STOP = {
        "what", "how", "why", "when", "where", "which", "that", "this",
        "with", "from", "have", "been", "were", "will", "would", "could",
        "should", "please", "tell", "about", "some", "many", "much",
    }
    _STOP = _VI_STOP | _EN_STOP

    def tokenize(s: str) -> set[str]:
        words = re.findall(r"[a-zA-ZÀ-ỹ]+", s.lower())
        return {w for w in words if len(w) > 3 and w not in _STOP}

    new_tokens = tokenize(new_msg)
    if len(new_tokens) < 2:
        return False

    recent_tokens: set[str] = set()
    for m in prev_user_msgs[-3:]:
        recent_tokens.update(tokenize(m))
    if not recent_tokens:
        return False

    overlap = new_tokens & recent_tokens
    overlap_ratio = len(overlap) / max(1, len(new_tokens))
    return overlap_ratio < 0.25


# ════════════════════════════════════════════════════════════
# Pydantic shapes
# ════════════════════════════════════════════════════════════


class SendRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=MAX_MESSAGE_LENGTH)


class EventOut(BaseModel):
    message_id: UUID
    session_id: UUID
    num_order: int
    role: str
    text: str
    event_type: str
    create_at: datetime

    class Config:
        from_attributes = True


# ════════════════════════════════════════════════════════════
# DB helpers
# ════════════════════════════════════════════════════════════


async def _insert_event_atomic(
    db: AsyncSession,
    session_id: UUID,
    role: str,
    msg_text: str,
    event_type: str,
) -> ChatEvent:
    """Insert a chat_event row, computing num_order atomically."""
    capped_text = msg_text[:1_000_000]   # 1 MB hard ceiling

    stmt = text(
        """
        INSERT INTO chat_event (session_id, num_order, role, text, event_type)
        VALUES (
            :session_id,
            COALESCE(
                (SELECT MAX(num_order) + 1 FROM chat_event WHERE session_id = :session_id),
                1
            ),
            :role, :text, :event_type
        )
        RETURNING message_id, num_order
        """
    )
    result = await db.execute(
        stmt,
        {
            "session_id": str(session_id),
            "role": role,
            "text": capped_text,
            "event_type": event_type,
        },
    )
    row = result.fetchone()
    obj_result = await db.execute(
        select(ChatEvent).where(ChatEvent.message_id == row.message_id)
    )
    return obj_result.scalar_one()


async def _build_history_for_graph(
    session_id: UUID,
    db: AsyncSession,
    current_user_text: str,
) -> list[dict]:
    """Return chat-only (user+assistant) trimmed history for the graph state.

    System messages are NOT included — the graph builds its own system
    blocks (system prompt, RAG, agent summaries). Intent-change detection
    decides whether to include long-term context (checkpoints/summaries
    are still pulled here for the answering side as user/assistant turns
    only when relevant; pure summaries are dropped because the collector
    re-injects RAG/agent context anyway).
    """
    res = await db.execute(
        select(ChatEvent)
        .where(
            ChatEvent.session_id == session_id,
            ChatEvent.event_type == "message",
            ChatEvent.role == "user",
        )
        .order_by(ChatEvent.num_order.desc())
        .limit(4)
    )
    recent_user_events = list(res.scalars().all())
    prev_user_msgs = [
        e.text for e in recent_user_events if e.text != current_user_text
    ][:3]
    intent_changed = _detect_intent_change(prev_user_msgs, current_user_text)

    if intent_changed:
        log.info(f"[session={session_id}] intent change detected — trimming history")
        res = await db.execute(
            select(ChatEvent)
            .where(
                ChatEvent.session_id == session_id,
                ChatEvent.event_type == "message",
                ChatEvent.role.in_(("user", "assistant")),
            )
            .order_by(ChatEvent.num_order.desc())
            .limit(2)
        )
        msgs = list(reversed(res.scalars().all()))
        return [{"role": m.role, "content": m.text} for m in msgs]

    res = await db.execute(
        select(ChatEvent)
        .where(
            ChatEvent.session_id == session_id,
            ChatEvent.event_type == "message",
            ChatEvent.role.in_(("user", "assistant")),
        )
        .order_by(ChatEvent.num_order.desc())
        .limit(6)
    )
    msgs = list(reversed(res.scalars().all()))
    history = [{"role": m.role, "content": m.text} for m in msgs]
    # Drop the final user msg if it's the current text (we'll re-add it
    # via state.user_text inside the graph).
    if history and history[-1].get("content") == current_user_text:
        history = history[:-1]
    return history


async def _generate_title_background(
    session_id: UUID,
    model_used: str,
    user_text: str,
    assistant_text: str,
):
    """Fire-and-forget title generation."""
    try:
        from services.ollama import chat_sync
        title_resp = await chat_sync(
            model_used,
            [
                {"role": "system", "content": "Generate a very short title (max 6 words) for this conversation. Reply with ONLY the title, no quotes or punctuation."},
                {"role": "user", "content": user_text[:1000]},
                {"role": "assistant", "content": assistant_text[:200]},
            ],
        )
        title = title_resp.get("message", {}).get("content", "").strip()[:100]
        if not title:
            return
        async with async_session_factory() as db:
            result = await db.execute(
                select(ChatSession).where(ChatSession.session_id == session_id)
            )
            session = result.scalar_one_or_none()
            if session and not session.session_title:
                session.session_title = title
                await db.commit()
    except Exception:
        pass


# ════════════════════════════════════════════════════════════
# SSE translation
# ════════════════════════════════════════════════════════════


def _graph_event_to_sse(evt: GraphEvent) -> str:
    """Render a GraphEvent as one SSE `data:` line.

    The `type` field is preserved as-is so existing UI handlers
    (token, tool_start, tool_end, rag_sources, query_rewrite,
    clarification, …) keep working without changes.
    """
    body: dict = {"type": evt.type}
    body.update(evt.payload or {})
    return f"data: {json.dumps(body, ensure_ascii=False)}\n\n"


async def _persist_event(
    db: AsyncSession,
    session_id: UUID,
    spec: PersistSpec,
) -> None:
    try:
        await _insert_event_atomic(
            db, session_id, spec.role, spec.text, spec.event_type,
        )
        await db.commit()
    except Exception as e:
        log.warning("[session=%s] persist %s failed: %s",
                    session_id, spec.event_type, e)


# ════════════════════════════════════════════════════════════
# Endpoints
# ════════════════════════════════════════════════════════════


@router.post("/{session_id}/send")
async def send_message(
    session_id: UUID,
    body: SendRequest,
    user_id: int = Depends(get_current_user),
):
    """Send a message and stream the assistant response via SSE.

    The heavy lifting happens inside the LangGraph runtime:
        rewriter → (rag ⟂ orchestrator → tool agents) → collector
    """

    async with async_session_factory() as authz_db:
        session_obj = await authorize_session(authz_db, user_id, session_id)
        session_project_id = session_obj.project_id
        session_model = session_obj.model_used
        session_tools = session_obj.tools_used or []

    stream_key = str(session_id)
    _active_streams[stream_key] = True
    log.info(
        f"[session={session_id}] chat request user={user_id} "
        f"project={session_project_id} model={session_model} "
        f"tools={session_tools} text_len={len(body.text)}"
    )

    async def event_generator():
        t0 = time.perf_counter()
        async with async_session_factory() as db:
            try:
                # Persist user message + load session row
                result = await db.execute(
                    select(ChatSession).where(ChatSession.session_id == session_id)
                )
                session = result.scalar_one_or_none()
                if not session:
                    yield f"data: {json.dumps({'type': 'error', 'content': 'Session not found'})}\n\n"
                    return

                await _insert_event_atomic(
                    db, session_id, "user", body.text, "message"
                )
                await db.commit()

                history = await _build_history_for_graph(session_id, db, body.text)

                state = ChatState(
                    session_id=session_id,
                    user_id=user_id,
                    project_id=session_project_id,
                    user_text=body.text,
                    history=history,
                    enabled_tools=list(session.tools_used or []),
                    session_model=session.model_used,
                )

                queue: asyncio.Queue = asyncio.Queue()
                graph = get_graph()

                async def _run_graph():
                    try:
                        await graph.ainvoke(
                            state,
                            config={"configurable": {"sse_queue": queue}},
                        )
                    except Exception as e:
                        log.error("[session=%s] graph crashed: %s",
                                  session_id, e, exc_info=True)
                        await queue.put(GraphEvent(
                            type="error",
                            payload={"content": str(e)[:500]},
                        ))
                    finally:
                        await push_sentinel(queue)

                graph_task = asyncio.create_task(_run_graph())

                final_answer_text = ""

                # ── Drain queue → SSE + DB persistence ─────────
                while True:
                    if not _active_streams.get(stream_key, False):
                        log.info(f"[session={session_id}] cancelled by user")
                        graph_task.cancel()
                        try:
                            await graph_task
                        except (asyncio.CancelledError, Exception):
                            pass
                        break

                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=0.5)
                    except asyncio.TimeoutError:
                        if graph_task.done():
                            # graph finished but didn't push sentinel (race)
                            break
                        continue

                    if item is SENTINEL:
                        break
                    if not isinstance(item, GraphEvent):
                        continue

                    # Forward as SSE
                    yield _graph_event_to_sse(item)

                    # Capture final answer for title-gen
                    if item.type == "final_answer":
                        final_answer_text = item.payload.get("content", "")

                    # Persist if requested
                    if item.persist is not None:
                        await _persist_event(db, session_id, item.persist)

                # Wait for the graph task to finish if not already
                if not graph_task.done():
                    try:
                        await asyncio.wait_for(graph_task, timeout=2.0)
                    except (asyncio.TimeoutError, Exception):
                        graph_task.cancel()

                # Update session turn count + auto-title
                try:
                    session.turn_count += 1
                    session.update_at = datetime.now(timezone.utc)
                    await db.commit()
                except Exception as e:
                    log.warning("[session=%s] turn_count update failed: %s",
                                session_id, e)

                if (
                    session.turn_count == 1
                    and not session.session_title
                    and final_answer_text
                ):
                    asyncio.create_task(
                        _generate_title_background(
                            session_id, session.model_used,
                            body.text, final_answer_text,
                        )
                    )

                yield f"data: {json.dumps({'type': 'done'})}\n\n"
                log.info(
                    f"[session={session_id}] turn finished in "
                    f"{(time.perf_counter() - t0)*1000:.0f}ms"
                )

            except Exception as e:
                log.error("[session=%s] router crashed: %s",
                          session_id, e, exc_info=True)
                yield f"data: {json.dumps({'type': 'error', 'content': str(e)[:500]})}\n\n"
            finally:
                _active_streams.pop(stream_key, None)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/{session_id}/stop")
async def stop_stream(
    session_id: UUID,
    user_id: int = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Cancel an active stream."""
    await authorize_session(db, user_id, session_id)
    stream_key = str(session_id)
    if stream_key in _active_streams:
        _active_streams[stream_key] = False
        return {"status": "stopped"}
    return {"status": "no_active_stream"}


@router.get("/{session_id}/events", response_model=list[EventOut])
async def get_events(
    session_id: UUID,
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    user_id: int = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get paginated events for a session, ordered by num_order."""
    await authorize_session(db, user_id, session_id)
    result = await db.execute(
        select(ChatEvent)
        .where(ChatEvent.session_id == session_id)
        .order_by(ChatEvent.num_order.asc())
        .offset(offset)
        .limit(limit)
    )
    return result.scalars().all()
