"""FinHouse — Chat Router (message send, stream, events)."""

import json
import asyncio
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db, async_session_factory
from models import ChatSession, ChatEvent
from services.ollama import chat_stream, chat_sync, TOOL_CAPABLE_MODELS
from tools.web_search import web_search, WEB_SEARCH_TOOL_SCHEMA
from routers.auth import get_current_user

router = APIRouter(prefix="/chat", tags=["chat"])

# Track active streams for cancellation
_active_streams: dict[str, bool] = {}


class SendRequest(BaseModel):
    text: str


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


async def _get_next_order(session_id: UUID, db: AsyncSession) -> int:
    """Get the next num_order for a session."""
    result = await db.execute(
        select(func.coalesce(func.max(ChatEvent.num_order), 0))
        .where(ChatEvent.session_id == session_id)
    )
    return result.scalar() + 1


async def _insert_event(
    db: AsyncSession,
    session_id: UUID,
    num_order: int,
    role: str,
    text: str,
    event_type: str,
) -> ChatEvent:
    """Insert a chat event."""
    event = ChatEvent(
        session_id=session_id,
        num_order=num_order,
        role=role,
        text=text,
        event_type=event_type,
    )
    db.add(event)
    await db.flush()
    return event


async def _build_messages(session_id: UUID, db: AsyncSession) -> list[dict]:
    """
    Build Ollama message list from recent events.
    Uses: latest checkpoint + recent summaries + last 6 messages.
    """
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
        order = await _get_next_order(session_id, db)
        await _insert_event(
            db, session_id, order, "assistant", 
            json.dumps({"tool": tool_name, "args": tool_args}),
            "tool_call",
        )

        # Execute
        result_text = ""
        if tool_name == "web_search" and "web_search" in enabled_tools:
            query = tool_args.get("query", "")
            results = await web_search(query)
            result_text = json.dumps(results, ensure_ascii=False)
        else:
            result_text = json.dumps({"error": f"Tool '{tool_name}' not available"})

        # Log tool result
        order = await _get_next_order(session_id, db)
        await _insert_event(
            db, session_id, order, "system", result_text, "tool_result",
        )

        tool_messages.append({
            "role": "tool",
            "content": result_text,
        })

    return tool_messages


@router.post("/{session_id}/send")
async def send_message(
    session_id: UUID,
    body: SendRequest,
    request: Request,
    user_id: int = Depends(get_current_user),
):
    """Send a message and stream the assistant response via SSE."""

    stream_key = str(session_id)
    _active_streams[stream_key] = True

    async def event_generator():
        async with async_session_factory() as db:
            try:
                # Fetch session
                result = await db.execute(
                    select(ChatSession).where(ChatSession.session_id == session_id)
                )
                session = result.scalar_one_or_none()
                if not session:
                    yield f"data: {json.dumps({'type': 'error', 'content': 'Session not found'})}\n\n"
                    return

                enabled_tools = session.tools_used or []

                # Insert user message
                order = await _get_next_order(session_id, db)
                await _insert_event(
                    db, session_id, order, "user", body.text, "message"
                )
                await db.commit()

                # Build prompt
                messages = await _build_messages(session_id, db)

                # ── RAG Retrieval ────────────────────────────────
                rag_sources = []
                try:
                    from services.ingest import retrieve_context
                    project_id = session.project_id
                    # For incognito (negative project_id), also search default (0)
                    search_project = project_id if project_id >= 0 else 0
                    rag_chunks = await retrieve_context(
                        query=body.text,
                        project_id=search_project,
                        top_k=20,
                        top_n_rerank=5,
                    )
                    if rag_chunks:
                        # Format as numbered sources
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

                        # Log rag_context event
                        order = await _get_next_order(session_id, db)
                        await _insert_event(
                            db, session_id, order, "system",
                            json.dumps(rag_sources, ensure_ascii=False),
                            "rag_context",
                        )
                        await db.commit()
                except Exception as e:
                    import logging
                    logging.getLogger("finhouse.chat").warning(f"RAG retrieval skipped: {e}")

                # Notify UI about RAG sources
                if rag_sources:
                    yield f"data: {json.dumps({'type': 'rag_sources', 'sources': rag_sources})}\n\n"

                # Ensure the user message we just inserted is in the list
                if not messages or messages[-1].get("content") != body.text:
                    messages.append({"role": "user", "content": body.text})

                # Prepare tools if enabled
                tools = []
                if "web_search" in enabled_tools:
                    tools.append(WEB_SEARCH_TOOL_SCHEMA)

                # Call Ollama with possible tool usage loop
                max_tool_rounds = 3
                for round_idx in range(max_tool_rounds + 1):
                    if not _active_streams.get(stream_key, False):
                        break

                    if tools and round_idx < max_tool_rounds:
                        # Non-streaming call to check for tool calls
                        response = await chat_sync(
                            session.model_used, messages, tools=tools
                        )
                        msg = response.get("message", {})

                        if msg.get("tool_calls"):
                            # Notify UI about tool usage
                            for tc in msg["tool_calls"]:
                                fn = tc.get("function", {})
                                yield f"data: {json.dumps({'type': 'tool_start', 'tool': fn.get('name',''), 'args': fn.get('arguments',{})})}\n\n"

                            tool_results = await _handle_tool_calls(
                                session_id, msg["tool_calls"], db, enabled_tools,
                            )
                            await db.commit()

                            for tr in tool_results:
                                yield f"data: {json.dumps({'type': 'tool_end', 'content': tr['content'][:500]})}\n\n"

                            # Add assistant tool call + tool results to messages
                            messages.append({
                                "role": "assistant",
                                "content": msg.get("content", ""),
                                "tool_calls": msg["tool_calls"],
                            })
                            messages.extend(tool_results)
                            continue
                        else:
                            # No tool calls — model wants to respond directly
                            # Re-do as streaming for the final answer
                            pass

                    # Streaming final response (no tools)
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
                    order = await _get_next_order(session_id, db)
                    await _insert_event(
                        db, session_id, order, "assistant", full_response, "message"
                    )

                    # Update session
                    session.turn_count += 1
                    session.update_at = datetime.now(timezone.utc)
                    await db.commit()

                    # Auto-title after first turn if no title
                    if session.turn_count == 1 and not session.session_title:
                        try:
                            title_resp = await chat_sync(
                                session.model_used,
                                [
                                    {"role": "system", "content": "Generate a very short title (max 6 words) for this conversation. Reply with ONLY the title, no quotes or punctuation."},
                                    {"role": "user", "content": body.text},
                                    {"role": "assistant", "content": full_response[:200]},
                                ],
                            )
                            title = title_resp.get("message", {}).get("content", "").strip()[:100]
                            if title:
                                session.session_title = title
                                await db.commit()
                                yield f"data: {json.dumps({'type': 'title', 'content': title})}\n\n"
                        except Exception:
                            pass

                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                    break

            except Exception as e:
                yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"
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
async def stop_stream(session_id: UUID):
    """Cancel an active stream."""
    stream_key = str(session_id)
    if stream_key in _active_streams:
        _active_streams[stream_key] = False
        return {"status": "stopped"}
    return {"status": "no_active_stream"}


@router.get("/{session_id}/events", response_model=list[EventOut])
async def get_events(
    session_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Get all events for a session, ordered by num_order."""
    result = await db.execute(
        select(ChatEvent)
        .where(ChatEvent.session_id == session_id)
        .order_by(ChatEvent.num_order.asc())
    )
    return result.scalars().all()
