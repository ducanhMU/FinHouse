"""
RAG node — wraps `services.ingest.retrieve_context`.

This node has no LLM of its own; it is pure retrieval. It runs in
parallel with the orchestrator branch from the rewriter fan-out.

Output:
    rag_sources    — list[RagChunk] for the UI / downstream collector
    rag_messages   — list of system messages to splice into the
                     collector's prompt (so the collector can answer
                     citing [1], [2], …)
"""

from __future__ import annotations

import json
import logging
import time
from uuid import UUID

from langchain_core.runnables import RunnableConfig, RunnableLambda
from sqlalchemy import select, func

from database import async_session_factory
from graph.sse import emit, PersistSpec
from graph.state import ChatState, RagChunk
from models import File

log = logging.getLogger("finhouse.graph.rag")


_MAX_TOOL_RESULT_LENGTH = 20_000


async def _project_has_files(db, project_id: int) -> bool:
    if project_id < 0:
        result = await db.execute(
            select(func.count(File.file_id)).where(
                File.project_id == project_id,
                File.process_status == "ready",
            )
        )
        return (result.scalar() or 0) > 0
    result = await db.execute(
        select(func.count(File.file_id)).where(
            File.project_id.in_([0, project_id]),
            File.process_status == "ready",
        )
    )
    return (result.scalar() or 0) > 0


async def _rag_node(state: ChatState, config: RunnableConfig) -> dict:
    # Short-circuit: clarification → no RAG needed
    if state.rewrite and state.rewrite.needs_clarification:
        return {"rag_sources": [], "rag_messages": []}

    embed_query = (
        state.rewrite.embed_query
        if state.rewrite else state.user_text
    )
    search_project = state.project_id if state.project_id >= 0 else 0

    # Ticker prefix nudge — same convention as the legacy chat router
    ticker_prefixes: list[str] = []
    if (
        state.rewrite
        and state.rewrite.scope_type == "company"
        and state.resolved_companies
    ):
        ticker_prefixes = [
            (c.get("symbol") or "").upper()
            for c in state.resolved_companies
            if c.get("symbol")
        ]

    t0 = time.perf_counter()
    chunks: list[dict] = []
    try:
        async with async_session_factory() as db:
            if not await _project_has_files(db, search_project):
                log.info("[rag] no files in project=%s, skipping", search_project)
                return {"rag_sources": [], "rag_messages": []}

        from services.ingest import retrieve_context
        chunks = await retrieve_context(
            query=embed_query,
            project_id=search_project,
            top_k=20,
            top_n_rerank=5,
            file_name_prefixes=ticker_prefixes or None,
        )
    except Exception as e:
        log.warning("[rag] retrieval skipped: %s", e, exc_info=True)
        return {"rag_sources": [], "rag_messages": []}

    log.info(
        "[rag] retrieved %d chunks for query=%r prefixes=%s in %.0fms",
        len(chunks or []), embed_query[:80], ticker_prefixes or "-",
        (time.perf_counter() - t0) * 1000,
    )
    if not chunks:
        return {"rag_sources": [], "rag_messages": []}

    rag_sources: list[RagChunk] = []
    source_lines: list[str] = []
    for i, ch in enumerate(chunks, 1):
        rag_sources.append(RagChunk(
            index=i,
            file_name=ch.get("file_name", ""),
            text=ch["text"][:300],
            score=float(ch.get("rerank_score", ch.get("score", 0)) or 0),
        ))
        source_lines.append(
            f"[{i}] (File: {ch.get('file_name','unknown')}) {ch['text'][:800]}"
        )

    rag_text = (
        "Các đoạn trích từ tài liệu hệ thống "
        "(dùng để trả lời câu hỏi; trích dẫn [1], [2]...):\n\n"
        + "\n\n".join(source_lines)
    )
    rag_message = {"role": "system", "content": rag_text}

    # ── SSE: tell UI which sources we used ─────────────────
    sources_payload = [s.model_dump() for s in rag_sources]
    persist_text = json.dumps(sources_payload, ensure_ascii=False)[:_MAX_TOOL_RESULT_LENGTH]
    await emit(
        config, "rag_sources",
        {"sources": sources_payload},
        persist=PersistSpec(
            role="system", text=persist_text, event_type="rag_context",
        ),
    )

    return {"rag_sources": rag_sources, "rag_messages": [rag_message]}


rag_runnable = RunnableLambda(_rag_node).with_config(run_name="rag")


# Re-exported for chat router (used by SessionId-bound DB queries upstream)
__all__ = ["rag_runnable"]


# ── Helper for callers that need it without graph state ─────


async def has_files(project_id: int, session_id: UUID | None = None) -> bool:  # noqa: ARG001
    async with async_session_factory() as db:
        return await _project_has_files(db, project_id)
