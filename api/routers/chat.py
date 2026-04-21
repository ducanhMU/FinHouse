"""FinHouse — Chat Router (message send, stream, events)."""

import json
import asyncio
import html as html_module
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db, async_session_factory
from models import ChatSession, ChatEvent, File
from services.ollama import chat_stream, chat_sync, TOOL_CAPABLE_MODELS
from tools.web_search import web_search, WEB_SEARCH_TOOL_SCHEMA
from tools.database_query import (
    run_sql, is_enabled as db_enabled,
    DATABASE_QUERY_TOOL_SCHEMA,
)
from tools.visualize import build_chart, VISUALIZE_TOOL_SCHEMA
from routers.auth import get_current_user
from routers.sessions import authorize_session

router = APIRouter(prefix="/chat", tags=["chat"])

# Max chars in a user message — prevents context bloat and Ollama OOM
MAX_MESSAGE_LENGTH = 32_000

# Max chars to store from a tool result (web search HTML etc.)
MAX_TOOL_RESULT_LENGTH = 20_000

# Track active streams for cancellation.
# NOTE: this is in-memory per worker. Works fine for single-worker deploys.
# For multi-worker: move to Redis pub/sub.
_active_streams: dict[str, bool] = {}


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


def _sanitize_tool_result(raw: str, max_length: int = MAX_TOOL_RESULT_LENGTH) -> str:
    """
    Cap length and unescape HTML entities. Web search results often contain
    huge blobs with HTML; we strip nothing structurally but bound the size
    to avoid bloating chat context.
    """
    if not raw:
        return ""
    # Truncate first
    truncated = raw[:max_length]
    if len(raw) > max_length:
        truncated += f"\n\n[... truncated {len(raw) - max_length} more chars]"
    return truncated


async def _insert_event_atomic(
    db: AsyncSession,
    session_id: UUID,
    role: str,
    msg_text: str,
    event_type: str,
) -> ChatEvent:
    """
    Insert a chat event, computing num_order atomically server-side.
    Uses SQL subquery to avoid read-then-write race condition.
    """
    # Cap text size defensively
    capped_text = msg_text[:1_000_000]  # 1 MB hard ceiling

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

    # Reload via ORM so the caller gets a full ChatEvent object
    obj_result = await db.execute(
        select(ChatEvent).where(ChatEvent.message_id == row.message_id)
    )
    return obj_result.scalar_one()


async def _project_has_files(db: AsyncSession, project_id: int) -> bool:
    """Check if a project has any ready files — used to skip RAG when empty."""
    result = await db.execute(
        select(func.count(File.file_id)).where(
            File.project_id == project_id,
            File.process_status == "ready",
        )
    )
    return (result.scalar() or 0) > 0


async def _build_messages(session_id: UUID, db: AsyncSession) -> list[dict]:
    """Build Ollama message list from recent events."""
    messages = []

    # 1. Latest checkpoint
    result = await db.execute(
        select(ChatEvent)
        .where(
            ChatEvent.session_id == session_id,
            ChatEvent.event_type == "checkpoint",
        )
        .order_by(ChatEvent.num_order.desc())
        .limit(1)
    )
    checkpoint = result.scalar_one_or_none()
    if checkpoint:
        messages.append({
            "role": "system",
            "content": f"[Conversation checkpoint]\n{checkpoint.text}",
        })

    # 2. Recent summaries since last checkpoint (up to 3)
    checkpoint_order = checkpoint.num_order if checkpoint else 0
    result = await db.execute(
        select(ChatEvent)
        .where(
            ChatEvent.session_id == session_id,
            ChatEvent.event_type == "summary",
            ChatEvent.num_order > checkpoint_order,
        )
        .order_by(ChatEvent.num_order.desc())
        .limit(3)
    )
    summaries = result.scalars().all()
    for s in reversed(summaries):
        messages.append({
            "role": "system",
            "content": f"[Summary of earlier conversation]\n{s.text}",
        })

    # 3. Last 6 message events (3 turns of user+assistant)
    result = await db.execute(
        select(ChatEvent)
        .where(
            ChatEvent.session_id == session_id,
            ChatEvent.event_type == "message",
        )
        .order_by(ChatEvent.num_order.desc())
        .limit(6)
    )
    recent_msgs = list(reversed(result.scalars().all()))
    for msg in recent_msgs:
        messages.append({
            "role": msg.role,
            "content": msg.text,
        })

    return messages


async def _handle_tool_calls(
    session_id: UUID,
    tool_calls: list[dict],
    db: AsyncSession,
    enabled_tools: list[str],
) -> list[dict]:
    """Execute tool calls and return tool results as messages."""
    tool_messages = []

    for tc in tool_calls:
        func_info = tc.get("function", {})
        tool_name = func_info.get("name", "")
        tool_args = func_info.get("arguments", {})

        # Log tool call
        await _insert_event_atomic(
            db, session_id, "assistant",
            json.dumps({"tool": tool_name, "args": tool_args})[:MAX_TOOL_RESULT_LENGTH],
            "tool_call",
        )

        # Execute
        result_text = ""
        if tool_name == "web_search" and "web_search" in enabled_tools:
            query = tool_args.get("query", "")[:500]
            try:
                results = await web_search(query)
                result_text = json.dumps(results, ensure_ascii=False)
            except Exception as e:
                result_text = json.dumps({"error": f"web_search failed: {e}"})
        elif tool_name == "database_query" and "database_query" in enabled_tools:
            sql = tool_args.get("sql", "")
            try:
                result = await run_sql(sql)
                result_text = json.dumps(result, ensure_ascii=False, default=str)
            except Exception as e:
                result_text = json.dumps({"error": f"database_query failed: {e}"})
        elif tool_name == "visualize" and "visualize" in enabled_tools:
            try:
                result = await build_chart(
                    data_rows=tool_args.get("data_rows", []),
                    mark=tool_args.get("mark", "bar"),
                    x_field=tool_args.get("x_field", ""),
                    y_field=tool_args.get("y_field", ""),
                    color_field=tool_args.get("color_field"),
                    title=tool_args.get("title"),
                )
                result_text = json.dumps(result, ensure_ascii=False, default=str)
            except Exception as e:
                result_text = json.dumps({"error": f"visualize failed: {e}"})
        else:
            result_text = json.dumps({"error": f"Tool '{tool_name}' not available"})

        result_text = _sanitize_tool_result(result_text)

        # Log tool result
        await _insert_event_atomic(
            db, session_id, "system", result_text, "tool_result",
        )

        tool_messages.append({
            "role": "tool",
            "content": result_text,
        })

    return tool_messages


async def _generate_title_background(
    session_id: UUID,
    model_used: str,
    user_text: str,
    assistant_text: str,
):
    """Fire-and-forget title generation. Runs after response is done."""
    try:
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
        pass  # title is nice-to-have, never blocks


@router.post("/{session_id}/send")
async def send_message(
    session_id: UUID,
    body: SendRequest,
    user_id: int = Depends(get_current_user),
):
    """Send a message and stream the assistant response via SSE."""

    # Authorize upfront using a short-lived session.
    # We'll open longer-lived sessions inside the generator for streaming.
    async with async_session_factory() as authz_db:
        session_obj = await authorize_session(authz_db, user_id, session_id)
        session_project_id = session_obj.project_id
        session_model = session_obj.model_used
        session_tools = session_obj.tools_used or []

    stream_key = str(session_id)
    _active_streams[stream_key] = True

    async def event_generator():
        async with async_session_factory() as db:
            try:
                # Re-load session to work with it in this db session
                result = await db.execute(
                    select(ChatSession).where(ChatSession.session_id == session_id)
                )
                session = result.scalar_one_or_none()
                if not session:
                    yield f"data: {json.dumps({'type': 'error', 'content': 'Session not found'})}\n\n"
                    return

                enabled_tools = session.tools_used or []

                # Insert user message (atomic num_order)
                await _insert_event_atomic(
                    db, session_id, "user", body.text, "message"
                )
                await db.commit()

                # Build prompt
                messages = await _build_messages(session_id, db)

                # ── RAG Retrieval (only if project has files) ────
                rag_sources = []
                search_project = session_project_id if session_project_id >= 0 else 0
                if await _project_has_files(db, search_project):
                    try:
                        from services.ingest import retrieve_context
                        rag_chunks = await retrieve_context(
                            query=body.text,
                            project_id=search_project,
                            top_k=20,
                            top_n_rerank=5,
                        )
                        if rag_chunks:
                            source_lines = []
                            for i, chunk in enumerate(rag_chunks, 1):
                                source_lines.append(
                                    f"[{i}] (File: {chunk.get('file_name','unknown')}) "
                                    f"{chunk['text'][:800]}"
                                )
                                rag_sources.append({
                                    "index": i,
                                    "file_name": chunk.get("file_name", ""),
                                    "text": chunk["text"][:300],
                                    "score": chunk.get("rerank_score", chunk.get("score", 0)),
                                })
                            rag_text = (
                                "Use the following retrieved document excerpts to answer "
                                "the user's question. Cite sources using [1], [2], etc.\n\n"
                                + "\n\n".join(source_lines)
                            )
                            messages.insert(0, {"role": "system", "content": rag_text})

                            await _insert_event_atomic(
                                db, session_id, "system",
                                json.dumps(rag_sources, ensure_ascii=False)[:MAX_TOOL_RESULT_LENGTH],
                                "rag_context",
                            )
                            await db.commit()
                    except Exception as e:
                        import logging
                        logging.getLogger("finhouse.chat").warning(
                            f"RAG retrieval skipped: {e}"
                        )

                if rag_sources:
                    yield f"data: {json.dumps({'type': 'rag_sources', 'sources': rag_sources})}\n\n"

                if not messages or messages[-1].get("content") != body.text:
                    messages.append({"role": "user", "content": body.text})

                tools = []
                if "web_search" in enabled_tools:
                    tools.append(WEB_SEARCH_TOOL_SCHEMA)
                if "database_query" in enabled_tools and db_enabled():
                    tools.append(DATABASE_QUERY_TOOL_SCHEMA)
                if "visualize" in enabled_tools:
                    tools.append(VISUALIZE_TOOL_SCHEMA)

                # Tool-use loop
                max_tool_rounds = 3
                full_response = ""
                for round_idx in range(max_tool_rounds + 1):
                    if not _active_streams.get(stream_key, False):
                        break

                    if tools and round_idx < max_tool_rounds:
                        response = await chat_sync(
                            session.model_used, messages, tools=tools
                        )
                        msg = response.get("message", {})

                        if msg.get("tool_calls"):
                            for tc in msg["tool_calls"]:
                                fn = tc.get("function", {})
                                yield f"data: {json.dumps({'type': 'tool_start', 'tool': fn.get('name',''), 'args': fn.get('arguments',{})})}\n\n"

                            tool_results = await _handle_tool_calls(
                                session_id, msg["tool_calls"], db, enabled_tools,
                            )
                            await db.commit()

                            for tr in tool_results:
                                yield f"data: {json.dumps({'type': 'tool_end', 'content': tr['content'][:500]})}\n\n"

                            messages.append({
                                "role": "assistant",
                                "content": msg.get("content", ""),
                                "tool_calls": msg["tool_calls"],
                            })
                            messages.extend(tool_results)
                            continue

                    # Streaming final response
                    full_response = ""
                    async for chunk in chat_stream(
                        session.model_used, messages, tools=None
                    ):
                        if not _active_streams.get(stream_key, False):
                            full_response += " [cancelled]"
                            break
                        content = chunk.get("message", {}).get("content", "")
                        if content:
                            full_response += content
                            yield f"data: {json.dumps({'type': 'token', 'content': content})}\n\n"

                        if chunk.get("done"):
                            break

                    # Save assistant response
                    await _insert_event_atomic(
                        db, session_id, "assistant", full_response, "message"
                    )

                    session.turn_count += 1
                    session.update_at = datetime.now(timezone.utc)
                    await db.commit()

                    # Auto-title — FIRE AND FORGET (non-blocking)
                    if session.turn_count == 1 and not session.session_title:
                        asyncio.create_task(
                            _generate_title_background(
                                session_id,
                                session.model_used,
                                body.text,
                                full_response,
                            )
                        )

                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                    break

            except Exception as e:
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
    # Require auth to stop — prevents random UUID guessers from canceling streams
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
    # AUTHORIZE — this was the big hole previously
    await authorize_session(db, user_id, session_id)

    result = await db.execute(
        select(ChatEvent)
        .where(ChatEvent.session_id == session_id)
        .order_by(ChatEvent.num_order.asc())
        .offset(offset)
        .limit(limit)
    )
    return result.scalars().all()
