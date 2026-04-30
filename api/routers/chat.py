"""FinHouse — Chat Router (message send, stream, events)."""

import json
import asyncio
import html as html_module
import logging
import re
import time
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
from prompts import get_system_prompt
from routers.auth import get_current_user
from routers.sessions import authorize_session

router = APIRouter(prefix="/chat", tags=["chat"])
log = logging.getLogger("finhouse.chat")

# Max chars in a user message — prevents context bloat and Ollama OOM
MAX_MESSAGE_LENGTH = 32_000

# Max chars to store from a tool result (web search HTML etc.)
MAX_TOOL_RESULT_LENGTH = 20_000

# Track active streams for cancellation.
# NOTE: this is in-memory per worker. Works fine for single-worker deploys.
# For multi-worker: move to Redis pub/sub.
_active_streams: dict[str, bool] = {}


# ════════════════════════════════════════════════════════════
# System prompt is loaded from api/prompts/system.md
# Edit that file to change the AI persona. Restart API to reload
# (or call reload_system_prompt() programmatically).
# ════════════════════════════════════════════════════════════


def _detect_intent_change(prev_user_msgs: list[str], new_msg: str) -> bool:
    """
    Phát hiện user đổi chủ đề.

    Heuristic: so sánh set content words (>3 chars, bỏ stopwords) giữa
    tin nhắn mới và 3 tin user gần nhất. Nếu overlap < 25% → intent changed.

    Tuned cho chat tiếng Việt về tài chính — thêm stopwords Vietnamese
    để tránh false negative (common words như "công ty", "có", "là"
    che mất proper noun thực sự là chủ đề).
    """
    if not prev_user_msgs:
        return False

    # Stopwords — những từ xuất hiện nhiều không phải chủ đề
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
        # Lấy từ có ít nhất 3 ký tự, lowercase, bỏ stopwords
        words = re.findall(r"[a-zA-Z\u00C0-\u1EF9]+", s.lower())
        return {w for w in words if len(w) > 3 and w not in _STOP}

    new_tokens = tokenize(new_msg)
    if len(new_tokens) < 2:
        # Câu quá ngắn/ít thông tin → không reliable, giữ context cũ
        return False

    recent_tokens: set[str] = set()
    for m in prev_user_msgs[-3:]:
        recent_tokens.update(tokenize(m))

    if not recent_tokens:
        return False

    overlap = new_tokens & recent_tokens
    overlap_ratio = len(overlap) / max(1, len(new_tokens))
    return overlap_ratio < 0.25


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
    """
    Check if RAG retrieval has anything to work with.

    Rule:
      - Incognito (project_id < 0): only check that project.
      - Any positive project_id: check base (0) OR that project.
    """
    if project_id < 0:
        result = await db.execute(
            select(func.count(File.file_id)).where(
                File.project_id == project_id,
                File.process_status == "ready",
            )
        )
        return (result.scalar() or 0) > 0

    # Normal or base search — check base OR the specific project
    result = await db.execute(
        select(func.count(File.file_id)).where(
            File.project_id.in_([0, project_id]),
            File.process_status == "ready",
        )
    )
    return (result.scalar() or 0) > 0


async def _build_messages(
    session_id: UUID,
    db: AsyncSession,
    current_user_text: str,
) -> list[dict]:
    """
    Build Ollama message list. Rules:

    1. ALWAYS start with the Vietnamese finance system prompt.
    2. If user's new message changes topic sharply (intent drift),
       drop summaries/checkpoints — they bias the model to the old topic.
    3. Otherwise: checkpoint + recent summaries + last 6 messages.
    """
    messages: list[dict] = [{"role": "system", "content": get_system_prompt()}]

    # ── Collect recent user messages to detect intent change ──
    res = await db.execute(
        select(ChatEvent)
        .where(
            ChatEvent.session_id == session_id,
            ChatEvent.event_type == "message",
            ChatEvent.role == "user",
        )
        .order_by(ChatEvent.num_order.desc())
        .limit(4)       # last 4 user turns (current + 3 prev)
    )
    recent_user_events = list(res.scalars().all())
    # Exclude the one we just wrote (current text) if present
    prev_user_msgs = [
        e.text for e in recent_user_events if e.text != current_user_text
    ][:3]

    intent_changed = _detect_intent_change(prev_user_msgs, current_user_text)

    if intent_changed and prev_user_msgs:
        log.info(
            f"[session={session_id}] intent change detected — "
            f"trimming context to recent messages only"
        )
        # Skip checkpoints + summaries. Just include the last 2 messages
        # as minimal continuity.
        res = await db.execute(
            select(ChatEvent)
            .where(
                ChatEvent.session_id == session_id,
                ChatEvent.event_type == "message",
            )
            .order_by(ChatEvent.num_order.desc())
            .limit(2)
        )
        for msg in reversed(res.scalars().all()):
            messages.append({"role": msg.role, "content": msg.text})
        return messages

    # ── Normal path — include compaction + full recent history ──

    # 1. Latest checkpoint
    res = await db.execute(
        select(ChatEvent)
        .where(
            ChatEvent.session_id == session_id,
            ChatEvent.event_type == "checkpoint",
        )
        .order_by(ChatEvent.num_order.desc())
        .limit(1)
    )
    checkpoint = res.scalar_one_or_none()
    if checkpoint:
        messages.append({
            "role": "system",
            "content": f"[Tóm tắt dài — chỉ dùng làm bối cảnh, ưu tiên câu hỏi hiện tại]\n{checkpoint.text}",
        })

    # 2. Recent summaries since last checkpoint (up to 3)
    checkpoint_order = checkpoint.num_order if checkpoint else 0
    res = await db.execute(
        select(ChatEvent)
        .where(
            ChatEvent.session_id == session_id,
            ChatEvent.event_type == "summary",
            ChatEvent.num_order > checkpoint_order,
        )
        .order_by(ChatEvent.num_order.desc())
        .limit(3)
    )
    summaries = res.scalars().all()
    for s in reversed(summaries):
        messages.append({
            "role": "system",
            "content": f"[Tóm tắt đoạn chat trước]\n{s.text}",
        })

    # 3. Last 6 message events (3 turns of user+assistant)
    res = await db.execute(
        select(ChatEvent)
        .where(
            ChatEvent.session_id == session_id,
            ChatEvent.event_type == "message",
        )
        .order_by(ChatEvent.num_order.desc())
        .limit(6)
    )
    recent_msgs = list(reversed(res.scalars().all()))
    for msg in recent_msgs:
        messages.append({"role": msg.role, "content": msg.text})

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

        # Detect tool failure for UI indicator
        has_error = False
        try:
            parsed = json.loads(result_text)
            if isinstance(parsed, dict) and "error" in parsed:
                has_error = True
        except Exception:
            pass

        # Log tool result
        await _insert_event_atomic(
            db, session_id, "system", result_text, "tool_result",
        )

        tool_messages.append({
            "role": "tool",
            "content": result_text,
            # Extra metadata — consumed by the SSE yield below, not by the LLM.
            # The LLM message format only includes role + content; these extra
            # keys are stripped before forwarding to Ollama.
            "_tool_name": tool_name,
            "_error": has_error,
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
    log.info(
        f"[session={session_id}] chat request user={user_id} "
        f"project={session_project_id} model={session_model} "
        f"tools={session_tools} text_len={len(body.text)}"
    )

    async def event_generator():
        t0 = time.perf_counter()
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

                # Build prompt (passes current text so intent detection works)
                t_build = time.perf_counter()
                messages = await _build_messages(session_id, db, body.text)
                log.info(
                    f"[session={session_id}] built {len(messages)} messages "
                    f"in {(time.perf_counter() - t_build)*1000:.0f}ms"
                )

                # ── Query Rewriter (resolve references, expand context) ──
                # Goal: turn "Nó lãi bao nhiêu?" into a self-contained question
                # based on prior turns, so RAG retrieval gets a clean query.
                from config import get_settings as _gs
                _settings = _gs()

                # Build chat-only history for rewriter (strip system/tool events)
                rewriter_history = [
                    {"role": m["role"], "content": m.get("content") or ""}
                    for m in messages
                    if m.get("role") in ("user", "assistant") and m.get("content")
                ]
                # Exclude the current user message itself (we don't have it
                # in `messages` at position 0 — it'll be appended later — but
                # _build_messages may have included it as the last turn).
                if rewriter_history and rewriter_history[-1].get("content") == body.text:
                    rewriter_history = rewriter_history[:-1]

                rewrite_result = None
                embed_query_text = body.text   # default: embed original
                # Skip rewriter on the very first turn (no history) — there's
                # nothing to resolve and the user is waiting on a synchronous
                # LLM call before RAG even begins. Saves ~rewriter_timeout_sec
                # on cold turns.
                if _settings.REWRITER_ENABLED and rewriter_history:
                    t_rw = time.perf_counter()
                    try:
                        from services.rewriter import rewrite_query
                        rewriter_model = _settings.REWRITER_MODEL or session.model_used
                        rewrite_result = await rewrite_query(
                            user_message=body.text,
                            history=rewriter_history,
                            model=rewriter_model,
                        )
                        log.info(
                            f"[session={session_id}] rewriter done in "
                            f"{(time.perf_counter() - t_rw)*1000:.0f}ms "
                            f"needs_clarification={rewrite_result.needs_clarification}"
                        )
                    except Exception as e:
                        log.warning(
                            f"[session={session_id}] rewriter error (ignoring): {e}",
                            exc_info=True,
                        )
                        rewrite_result = None

                # If rewriter asked for clarification, short-circuit:
                # send clarification as assistant reply, save it, and exit.
                # No RAG, no main LLM call.
                if rewrite_result and rewrite_result.needs_clarification:
                    clarif = rewrite_result.clarification
                    log.info(
                        f"[session={session_id}] clarification requested: {clarif!r}"
                    )
                    yield f"data: {json.dumps({'type': 'clarification', 'content': clarif}, ensure_ascii=False)}\n\n"
                    # Stream the text so UI renders it in-place
                    yield f"data: {json.dumps({'type': 'token', 'content': clarif}, ensure_ascii=False)}\n\n"

                    await _insert_event_atomic(
                        db, session_id, "assistant", clarif, "message"
                    )
                    await db.commit()

                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                    _active_streams.pop(stream_key, None)
                    return

                # Otherwise, use rewritten query for RAG embedding if available
                if rewrite_result and rewrite_result.rewritten:
                    embed_query_text = rewrite_result.embed_query
                    # Emit event so UI can show "searching as: <rewritten>"
                    if embed_query_text != body.text:
                        yield (
                            "data: "
                            + json.dumps({
                                "type": "query_rewrite",
                                "original": body.text,
                                "rewritten": rewrite_result.rewritten,
                                "entities": rewrite_result.preserved_entities,
                                "timeframe": rewrite_result.preserved_timeframe,
                            }, ensure_ascii=False)
                            + "\n\n"
                        )

                # ── RAG Retrieval (only if project has files) ────
                rag_sources = []
                search_project = session_project_id if session_project_id >= 0 else 0
                t_rag = time.perf_counter()
                if await _project_has_files(db, search_project):
                    try:
                        from services.ingest import retrieve_context
                        rag_chunks = await retrieve_context(
                            query=embed_query_text,
                            project_id=search_project,
                            top_k=20,
                            top_n_rerank=5,
                        )
                        log.info(
                            f"[session={session_id}] RAG retrieved "
                            f"{len(rag_chunks) if rag_chunks else 0} chunks "
                            f"for query={embed_query_text[:80]!r} "
                            f"in {(time.perf_counter() - t_rag)*1000:.0f}ms"
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
                                "Các đoạn trích từ tài liệu hệ thống "
                                "(dùng để trả lời câu hỏi; trích dẫn [1], [2]...):\n\n"
                                + "\n\n".join(source_lines)
                            )
                            # Insert RAG context right after the system prompt (position 1)
                            insert_at = 1 if messages and messages[0].get("role") == "system" else 0
                            messages.insert(insert_at, {"role": "system", "content": rag_text})

                            await _insert_event_atomic(
                                db, session_id, "system",
                                json.dumps(rag_sources, ensure_ascii=False)[:MAX_TOOL_RESULT_LENGTH],
                                "rag_context",
                            )
                            await db.commit()
                    except Exception as e:
                        log.warning(
                            f"[session={session_id}] RAG retrieval skipped: {e}",
                            exc_info=True,
                        )
                else:
                    log.info(f"[session={session_id}] no files in project, skip RAG")

                if rag_sources:
                    yield f"data: {json.dumps({'type': 'rag_sources', 'sources': rag_sources})}\n\n"

                # Give the main LLM a hint about the resolved question so it
                # doesn't have to re-infer references. Small but meaningful.
                if (
                    rewrite_result
                    and rewrite_result.rewritten
                    and rewrite_result.rewritten != body.text
                ):
                    hint_parts = [
                        f"Ý định đã resolve của user: {rewrite_result.rewritten}"
                    ]
                    if rewrite_result.preserved_entities:
                        hint_parts.append(
                            "Các thực thể cần tập trung: "
                            + ", ".join(rewrite_result.preserved_entities)
                        )
                    if rewrite_result.preserved_timeframe:
                        hint_parts.append(
                            f"Mốc thời gian: {rewrite_result.preserved_timeframe}"
                        )
                    hint_text = " | ".join(hint_parts)
                    insert_at = 1 if messages and messages[0].get("role") == "system" else 0
                    messages.insert(insert_at, {"role": "system", "content": hint_text})

                if not messages or messages[-1].get("content") != body.text:
                    messages.append({"role": "user", "content": body.text})

                tools = []
                if "web_search" in enabled_tools:
                    tools.append(WEB_SEARCH_TOOL_SCHEMA)
                if "database_query" in enabled_tools and db_enabled():
                    tools.append(DATABASE_QUERY_TOOL_SCHEMA)
                if "visualize" in enabled_tools:
                    tools.append(VISUALIZE_TOOL_SCHEMA)

                log.info(
                    f"[session={session_id}] tools enabled: "
                    f"{[t['function']['name'] for t in tools] if tools else []}"
                )

                # Tool-use loop
                max_tool_rounds = _settings.MAX_TOOL_ROUNDS
                full_response = ""
                for round_idx in range(max_tool_rounds + 1):
                    if not _active_streams.get(stream_key, False):
                        log.info(f"[session={session_id}] cancelled at round {round_idx}")
                        break

                    # Final round: out of tool budget. Tell the model to stop
                    # calling tools and synthesize an answer from what it
                    # already gathered. Without this hint, models that were
                    # mid-plan (e.g. schema discovery → still need a SELECT)
                    # tend to emit an unfulfilled tool_call and stream empty
                    # content — leaving the user with a blank reply.
                    if tools and round_idx == max_tool_rounds:
                        messages.append({
                            "role": "system",
                            "content": (
                                "Bạn đã hết lượt gọi tool. KHÔNG được gọi "
                                "thêm tool nào nữa. Hãy dựa trên dữ liệu đã "
                                "thu thập từ các tool trước để trả lời "
                                "trực tiếp cho user bằng tiếng Việt. Nếu dữ "
                                "liệu còn thiếu, nói rõ phần nào thiếu và "
                                "đề xuất câu hỏi tiếp theo."
                            ),
                        })

                    if tools and round_idx < max_tool_rounds:
                        t_llm = time.perf_counter()
                        response = await chat_sync(
                            session.model_used, messages, tools=tools
                        )
                        log.info(
                            f"[session={session_id}] LLM round {round_idx} took "
                            f"{(time.perf_counter() - t_llm)*1000:.0f}ms"
                        )
                        msg = response.get("message", {})

                        if msg.get("tool_calls"):
                            for tc in msg["tool_calls"]:
                                fn = tc.get("function", {})
                                log.info(
                                    f"[session={session_id}] tool call: "
                                    f"{fn.get('name')} args={str(fn.get('arguments'))[:200]}"
                                )
                                yield f"data: {json.dumps({'type': 'tool_start', 'tool': fn.get('name',''), 'args': fn.get('arguments',{})})}\n\n"

                            t_tool = time.perf_counter()
                            tool_results = await _handle_tool_calls(
                                session_id, msg["tool_calls"], db, enabled_tools,
                            )
                            log.info(
                                f"[session={session_id}] tools executed in "
                                f"{(time.perf_counter() - t_tool)*1000:.0f}ms"
                            )
                            await db.commit()

                            for tr in tool_results:
                                yield (
                                    f"data: "
                                    + json.dumps({
                                        "type": "tool_end",
                                        "tool": tr.get("_tool_name", ""),
                                        "error": tr.get("_error", False),
                                        "content": tr["content"][:500],
                                    }, ensure_ascii=False)
                                    + "\n\n"
                                )

                            messages.append({
                                "role": "assistant",
                                "content": msg.get("content", ""),
                                "tool_calls": msg["tool_calls"],
                            })
                            # Strip UI-only metadata before forwarding to LLM
                            messages.extend(
                                {"role": tr["role"], "content": tr["content"]}
                                for tr in tool_results
                            )
                            continue

                    # Streaming final response
                    full_response = ""
                    t_stream = time.perf_counter()
                    saw_tool_call_attempt = False
                    chunk_count = 0
                    async for chunk in chat_stream(
                        session.model_used, messages, tools=None
                    ):
                        chunk_count += 1
                        if not _active_streams.get(stream_key, False):
                            full_response += " [cancelled]"
                            break
                        chunk_msg = chunk.get("message", {}) or {}
                        content = chunk_msg.get("content", "") or ""
                        # Some models (gpt-oss family) put intermediate text
                        # in `thinking` and leave `content` empty. Surface
                        # that to the user instead of streaming nothing.
                        if not content:
                            content = chunk_msg.get("thinking", "") or ""
                        if chunk_msg.get("tool_calls"):
                            saw_tool_call_attempt = True
                        if content:
                            full_response += content
                            yield f"data: {json.dumps({'type': 'token', 'content': content})}\n\n"

                        if chunk.get("done"):
                            break

                    log.info(
                        f"[session={session_id}] stream done in "
                        f"{(time.perf_counter() - t_stream)*1000:.0f}ms "
                        f"chunks={chunk_count} content_len={len(full_response)} "
                        f"tool_attempt_in_final_round={saw_tool_call_attempt}"
                    )

                    # Empty-response guardrail. Most common cause: model
                    # wanted to call another tool but tools were disabled
                    # for the final round, so it emitted only tool_calls
                    # with empty content. Tell the user something useful
                    # rather than letting the UI render a blank message.
                    if not full_response.strip():
                        if saw_tool_call_attempt:
                            full_response = (
                                "Model còn muốn gọi thêm tool để hoàn tất "
                                "câu trả lời nhưng đã hết lượt tool "
                                f"({max_tool_rounds} lượt). Hãy hỏi lại "
                                "với câu hỏi cụ thể hơn, hoặc tăng "
                                "MAX_TOOL_ROUNDS trong cấu hình."
                            )
                        else:
                            full_response = (
                                "Model trả về phản hồi rỗng. Có thể model "
                                "này không hỗ trợ tool-use tốt cho prompt "
                                "vừa rồi — thử model khác hoặc tắt bớt "
                                "tool xem sao."
                            )
                        yield (
                            f"data: "
                            + json.dumps(
                                {"type": "token", "content": full_response},
                                ensure_ascii=False,
                            )
                            + "\n\n"
                        )

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