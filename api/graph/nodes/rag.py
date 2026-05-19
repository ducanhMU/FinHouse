"""
RAG agent — retriever → evaluator → (generator | web-fallback → generator).

This used to be a pure retrieval node (chunks only); we now wrap it in
a tiny three-stage agent so RAG produces its OWN natural-language
synthesis (`state.rag_answer`). That makes the RAG layer evaluable in
isolation by the benchmark (Layer B: RAGAS-style metrics) without
having to attribute mistakes to the downstream collector.

Stages
======

1. **Retriever** — unchanged: embed queries via HyDE, hybrid search +
   rerank against the rewritten question, return top-K chunks.

2. **Evaluator** — single JSON LLM call. Returns one of:
       sufficient   — chunks alone are good enough → straight to generator
       partial      — relevant info but missing pieces → web fallback first
       insufficient — chunks not useful → web fallback first
   Plus `useful_idx`: which chunks the generator should actually keep.

3. **Web fallback** — only when evaluator said partial/insufficient.
   Single SearXNG `web_search` call (NOT the full web ReAct agent).
   The fallback's role is to *supplement* RAG, not replace it. Snippets
   are passed to the generator as a second context block.

4. **Generator** — synthesises a focused Vietnamese answer using only
   the kept chunks + optional web snippets. Cites chunks as `[n]`.
   Output is written to `state.rag_answer`; the collector picks it up.

State writes
============

`rag_sources` / `rag_messages` — kept for backward compatibility (the
UI still shows source chips; the collector still gets the raw chunks
as a backup context block in case it disagrees with the RAG answer).

`rag_answer` — the generator's synthesis. Empty when retrieval was
skipped entirely (no files / clarification).

`rag_structured` — machine-readable summary of the run:
    {evaluator_decision, useful_idx, web_fallback_used,
     n_chunks_total, n_chunks_kept, latency_ms}

`component_logs` — one record per turn when benchmarking.
"""

from __future__ import annotations

import json
import logging
import time
from uuid import UUID

from langchain_core.runnables import RunnableConfig, RunnableLambda
from sqlalchemy import select, func

from database import async_session_factory
from graph.llm_router import get_llm
from graph.logging_helper import make_log_record
from graph.sse import emit, PersistSpec
from graph.state import ChatState, RagChunk
from models import File
from prompts import get_rag_evaluator_prompt, get_rag_generator_prompt
from tools.web_search import web_search

log = logging.getLogger("finhouse.graph.rag")


# Mirror the cap used by the legacy retrieval-only node, so anything
# downstream that already truncates against this constant keeps working.
_MAX_TOOL_RESULT_LENGTH = 20_000

# Hard caps for the supplementary web fallback. We're not running a full
# web agent here — just one search to enrich the generator prompt.
_WEB_FALLBACK_TOPN = 5
_WEB_FALLBACK_SNIPPET_CHARS = 600

# LLM call ceilings. RAG sits on the critical latency path: evaluator +
# generator are two SEQUENTIAL chat_sync calls (plus an optional web
# search) that the collector waits on. Without a timeout a hung/slow
# model stalls the whole turn until the UI's 300s HTTP timeout. Bounding
# them means a stuck call degrades gracefully instead — evaluator → a
# permissive `partial`, generator → empty `rag_answer` (the collector
# still has the raw chunks + tool-agent results to answer from). Mirrors
# the orchestrator's explicit `timeout=12.0` pattern.
_EVAL_TIMEOUT_S = 15.0
_GEN_TIMEOUT_S = 60.0
# Benchmark mode (state.bench set): the tight production bounds above
# force evaluator/generator onto a weaker fallback on the slow remote↔
# cloud hop, so the benchmark would score the wrong brain. Use generous
# bounds so the configured primary actually answers. Still bounded — the
# detached bench finalizer caps the whole turn anyway.
_EVAL_TIMEOUT_BENCH_S = 60.0
_GEN_TIMEOUT_BENCH_S = 120.0


# ── DB precondition (unchanged from the previous retrieval-only node) ─

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


# ── Stage 1: retriever ────────────────────────────────────────


async def _retrieve(state: ChatState) -> tuple[list[dict], list[RagChunk], str]:
    """Run hybrid search + rerank. Returns (raw_chunks, rag_sources, rerank_query)."""
    if state.rewrite:
        embed_queries = state.rewrite.embed_queries or [state.user_text]
        rerank_query = state.rewrite.embed_query or state.user_text
    else:
        embed_queries = [state.user_text]
        rerank_query = state.user_text

    search_project = state.project_id if state.project_id >= 0 else 0

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

    async with async_session_factory() as db:
        if not await _project_has_files(db, search_project):
            log.info("[rag] no files in project=%s, skipping retrieval", search_project)
            return [], [], rerank_query

    from services.ingest import retrieve_context
    chunks = await retrieve_context(
        query=embed_queries,
        project_id=search_project,
        top_k=20,
        top_n_rerank=5,
        file_name_prefixes=ticker_prefixes or None,
        rerank_query=rerank_query,
    )

    rag_sources: list[RagChunk] = []
    for i, ch in enumerate(chunks or [], 1):
        rag_sources.append(RagChunk(
            index=i,
            file_name=ch.get("file_name", ""),
            text=ch["text"][:300],
            score=float(ch.get("rerank_score", ch.get("score", 0)) or 0),
        ))

    return chunks or [], rag_sources, rerank_query


# ── Stage 2: evaluator ────────────────────────────────────────


def _format_chunks_for_eval(chunks: list[dict]) -> str:
    blocks: list[str] = []
    for i, ch in enumerate(chunks, 1):
        # Slightly bigger preview than what we expose downstream — the
        # evaluator needs enough context to judge relevance, not just
        # the rerank summary the UI shows.
        text = (ch.get("text") or "")[:600]
        fn = ch.get("file_name", "unknown")
        blocks.append(f"[{i}] (File: {fn}) {text}")
    return "\n\n".join(blocks)


async def _evaluate(
    question: str,
    chunks: list[dict],
    session_model: str,
    bench: bool = False,
) -> dict:
    """Ask the evaluator LLM whether retrieval is sufficient.

    Returns a normalised dict:
        {"decision": "sufficient"|"partial"|"insufficient",
         "useful_idx": [int], "explanation": str, "usage": {...}}

    Degrades gracefully on LLM / JSON failure: defaults to a permissive
    `partial` so the generator still runs and the chat doesn't stall.
    """
    if not chunks:
        return {
            "decision": "insufficient",
            "useful_idx": [],
            "explanation": "Retriever returned no chunks.",
            "usage": None,
        }

    llm = get_llm("rag", session_model)
    sys_prompt = get_rag_evaluator_prompt()
    user_block = (
        f"CÂU HỎI: {question}\n\n"
        f"CÁC ĐOẠN TRÍCH (1..{len(chunks)}):\n\n"
        + _format_chunks_for_eval(chunks)
    )

    try:
        resp = await llm.chat_sync(
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user",   "content": user_block},
            ],
            timeout=(_EVAL_TIMEOUT_BENCH_S if bench else _EVAL_TIMEOUT_S),
            options={"response_format": {"type": "json_object"}},
        )
    except Exception as e:
        log.warning("[rag] evaluator LLM failed: %s — defaulting to partial", e)
        return {
            "decision": "partial",
            "useful_idx": list(range(1, len(chunks) + 1)),
            "explanation": f"evaluator-error: {e}",
            "usage": None,
        }

    content = ((resp.get("message") or {}).get("content") or "").strip()
    usage = resp.get("usage")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        log.warning("[rag] evaluator returned non-JSON: %r — defaulting to partial",
                    content[:200])
        return {
            "decision": "partial",
            "useful_idx": list(range(1, len(chunks) + 1)),
            "explanation": "evaluator-non-json",
            "usage": usage,
        }

    decision = parsed.get("decision") or "partial"
    if decision not in {"sufficient", "partial", "insufficient"}:
        decision = "partial"

    raw_idx = parsed.get("useful_idx") or []
    if not isinstance(raw_idx, list):
        raw_idx = []
    useful_idx: list[int] = []
    seen: set[int] = set()
    for v in raw_idx:
        try:
            iv = int(v)
        except (TypeError, ValueError):
            continue
        if 1 <= iv <= len(chunks) and iv not in seen:
            useful_idx.append(iv)
            seen.add(iv)

    # If the evaluator said sufficient but listed no useful chunks, treat
    # as partial — sufficient with empty support is a contradiction and
    # usually a model slip.
    if decision == "sufficient" and not useful_idx:
        decision = "partial"

    return {
        "decision": decision,
        "useful_idx": useful_idx,
        "explanation": parsed.get("GiaiThich") or "",
        "usage": usage,
    }


# ── Stage 3: web fallback (lightweight) ───────────────────────


async def _web_fallback(question: str) -> list[dict]:
    """One SearXNG call. Returns a normalised list of snippet dicts.

    Errors are swallowed — the generator can still write an answer from
    the chunks alone, just with a warning that supplementation failed.
    """
    try:
        results = await web_search(question[:500])
    except Exception as e:
        log.warning("[rag] web fallback failed: %s", e)
        return []

    snippets: list[dict] = []
    if isinstance(results, dict):
        results = results.get("results") or results.get("items") or []
    if not isinstance(results, list):
        return []

    for r in results[:_WEB_FALLBACK_TOPN]:
        if not isinstance(r, dict):
            continue
        title = (r.get("title") or "")[:200]
        url = r.get("url") or r.get("link") or ""
        body = (r.get("content") or r.get("snippet") or r.get("body") or "")
        # web_search() returns this exact sentinel on internal failure
        # (SearXNG down / bad response). Drop it so the raw exception
        # text never leaks into the generator prompt as a "snippet".
        if title == "Search Error" and not url:
            continue
        if not body and not title:
            continue
        snippets.append({
            "title": title,
            "url": url,
            "snippet": body[:_WEB_FALLBACK_SNIPPET_CHARS],
        })
    return snippets


# ── Stage 4: generator ────────────────────────────────────────


def _format_chunks_for_gen(chunks: list[dict], keep_idx: list[int]) -> str:
    """Build the chunk block for the generator prompt.

    Indices in `keep_idx` are 1-based and match the original retrieved
    order so citations the generator produces line up with what we
    persist into `rag_sources` (the UI source list).
    """
    keep = set(keep_idx) if keep_idx else set(range(1, len(chunks) + 1))
    blocks: list[str] = []
    for i, ch in enumerate(chunks, 1):
        if i not in keep:
            continue
        text = (ch.get("text") or "")[:900]
        fn = ch.get("file_name", "unknown")
        blocks.append(f"[{i}] (File: {fn}) {text}")
    return "\n\n".join(blocks)


def _format_web_for_gen(snippets: list[dict]) -> str:
    if not snippets:
        return ""
    lines = ["KẾT QUẢ WEB BỔ SUNG (chỉ dùng khi tài liệu hệ thống thiếu, "
             "trích dẫn bằng [web] kèm domain):"]
    for s in snippets:
        host = ""
        url = s.get("url") or ""
        if "://" in url:
            host = url.split("://", 1)[1].split("/", 1)[0]
        lines.append(f"- ({host or 'web'}) {s.get('title', '')}\n  {s.get('snippet','')}")
    return "\n".join(lines)


async def _generate(
    question: str,
    chunks: list[dict],
    keep_idx: list[int],
    web_snippets: list[dict],
    session_model: str,
    bench: bool = False,
) -> tuple[str, dict | None]:
    """Produce the natural-language RAG answer. Sync (non-streamed)."""
    llm = get_llm("rag", session_model)
    sys_prompt = get_rag_generator_prompt()

    chunk_block = _format_chunks_for_gen(chunks, keep_idx)
    web_block = _format_web_for_gen(web_snippets)

    user_parts = [f"CÂU HỎI: {question}"]
    if chunk_block:
        user_parts.append("CÁC ĐOẠN TRÍCH (đánh số = index để trích dẫn [n]):\n\n" + chunk_block)
    else:
        user_parts.append("CÁC ĐOẠN TRÍCH: (không có chunk hữu ích)")
    if web_block:
        user_parts.append(web_block)
    user_parts.append(
        "Viết câu trả lời ngắn, đúng fact, có [n] trỏ tới chunk đã dùng. "
        "Nếu thiếu dữ kiện → nói rõ thiếu gì, không bịa."
    )

    try:
        resp = await llm.chat_sync(
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user",   "content": "\n\n".join(user_parts)},
            ],
            timeout=(_GEN_TIMEOUT_BENCH_S if bench else _GEN_TIMEOUT_S),
        )
    except Exception as e:
        log.warning("[rag] generator LLM failed: %s", e)
        return "", None

    content = ((resp.get("message") or {}).get("content") or "").strip()
    usage = resp.get("usage")
    return content, usage


# ── Node entrypoint ───────────────────────────────────────────


async def _rag_node(state: ChatState, config: RunnableConfig) -> dict:
    t0 = time.perf_counter()

    # Clarification short-circuit — no retrieval, no answer.
    if state.rewrite and state.rewrite.needs_clarification:
        return {
            "rag_sources": [],
            "rag_messages": [],
            "rag_answer": "",
            "rag_structured": {"skipped": "clarification"},
            "component_logs": make_log_record(
                state, "rag",
                input={"question": state.user_text, "skipped": "clarification"},
                output={"answer": "", "structured": {"skipped": "clarification"}},
                latency_ms=int((time.perf_counter() - t0) * 1000),
            ),
        }

    # ── 1. Retrieve ─────────────────────────────────────────
    try:
        chunks, rag_sources, rerank_query = await _retrieve(state)
    except Exception as e:
        log.warning("[rag] retrieval failed: %s", e, exc_info=True)
        return {
            "rag_sources": [],
            "rag_messages": [],
            "rag_answer": "",
            "rag_structured": {"error": f"retrieval: {e}"},
            "component_logs": make_log_record(
                state, "rag",
                input={"question": state.user_text},
                output={"answer": "", "structured": {"error": str(e)}},
                latency_ms=int((time.perf_counter() - t0) * 1000),
                error=f"retrieval: {e}",
            ),
        }

    log.info(
        "[rag] retrieved %d chunks for %r in %.0fms",
        len(chunks), rerank_query[:80], (time.perf_counter() - t0) * 1000,
    )

    # No chunks at all → emit empty + log; collector can still proceed
    # via the orchestrator branch (tool agents may rescue the turn).
    if not chunks:
        return {
            "rag_sources": [],
            "rag_messages": [],
            "rag_answer": "",
            "rag_structured": {
                "evaluator_decision": "insufficient",
                "n_chunks_total": 0,
                "n_chunks_kept": 0,
                "web_fallback_used": False,
            },
            "component_logs": make_log_record(
                state, "rag",
                input={"question": rerank_query},
                output={
                    "answer": "",
                    "structured": {"evaluator_decision": "insufficient",
                                   "n_chunks_total": 0},
                },
                traces=[{"step": "retriever",
                         "data": {"chunks": 0, "rerank_query": rerank_query}}],
                latency_ms=int((time.perf_counter() - t0) * 1000),
            ),
        }

    # Build the legacy rag_messages block now (the collector still reads
    # it as a backup context — we don't want the generator answer to be
    # the only thing collector sees).
    source_lines = [
        f"[{s.index}] (File: {s.file_name}) {ch['text'][:800]}"
        for s, ch in zip(rag_sources, chunks)
    ]
    rag_text = (
        "Các đoạn trích từ tài liệu hệ thống "
        "(dùng để trả lời câu hỏi; trích dẫn [1], [2]...):\n\n"
        + "\n\n".join(source_lines)
    )
    rag_message = {"role": "system", "content": rag_text}

    # ── 2. Evaluate ────────────────────────────────────────
    t_eval = time.perf_counter()
    verdict = await _evaluate(
        rerank_query, chunks, state.session_model, bench=bool(state.bench),
    )
    eval_ms = int((time.perf_counter() - t_eval) * 1000)
    log.info("[rag] evaluator → %s (useful=%s) in %dms",
             verdict["decision"], verdict["useful_idx"], eval_ms)

    # ── 3. Web fallback (when partial/insufficient) ────────
    web_snippets: list[dict] = []
    web_used = False
    if verdict["decision"] in {"partial", "insufficient"}:
        t_web = time.perf_counter()
        web_snippets = await _web_fallback(rerank_query)
        web_used = bool(web_snippets)
        log.info("[rag] web fallback → %d snippets in %dms",
                 len(web_snippets), int((time.perf_counter() - t_web) * 1000))

    # ── 4. Generate ────────────────────────────────────────
    keep_idx = verdict["useful_idx"]
    # When evaluator said insufficient but we recovered via web, still
    # pass the (poor) chunks through so the generator can reference
    # whatever shred of relevance survives.
    if verdict["decision"] == "insufficient" and not keep_idx:
        keep_idx = list(range(1, len(chunks) + 1))

    t_gen = time.perf_counter()
    rag_answer, gen_usage = await _generate(
        rerank_query, chunks, keep_idx, web_snippets, state.session_model,
        bench=bool(state.bench),
    )
    gen_ms = int((time.perf_counter() - t_gen) * 1000)
    log.info("[rag] generator len=%d in %dms", len(rag_answer), gen_ms)

    # Aggregate token usage
    total_usage: dict | None = None
    eval_usage = verdict.get("usage")
    if eval_usage or gen_usage:
        total_usage = {
            "input_tokens":  (eval_usage or {}).get("input_tokens", 0)  + (gen_usage or {}).get("input_tokens", 0),
            "output_tokens": (eval_usage or {}).get("output_tokens", 0) + (gen_usage or {}).get("output_tokens", 0),
            "total_tokens":  (eval_usage or {}).get("total_tokens", 0)  + (gen_usage or {}).get("total_tokens", 0),
            "calls":         (eval_usage or {}).get("calls", 0)         + (gen_usage or {}).get("calls", 0),
        }

    structured = {
        "evaluator_decision": verdict["decision"],
        "evaluator_reason":   verdict["explanation"][:300],
        "useful_idx":         keep_idx,
        "web_fallback_used":  web_used,
        "n_web_snippets":     len(web_snippets),
        "n_chunks_total":     len(chunks),
        "n_chunks_kept":      len(keep_idx),
        "rerank_query":       rerank_query,
    }

    # ── SSE: source chips + run meta ───────────────────────
    # `sources` keeps the legacy contract. `meta` is additive: it lets
    # the live UI surface that the new 4-stage RAG agent ran (evaluator
    # verdict + whether a web fallback kicked in) without a new event
    # type. The PERSISTED text stays sources-only so the reload path
    # (load_session_events → json.loads expecting a list) is unchanged.
    sources_payload = [s.model_dump() for s in rag_sources]
    persist_text = json.dumps(sources_payload, ensure_ascii=False)[:_MAX_TOOL_RESULT_LENGTH]
    await emit(
        config, "rag_sources",
        {
            "sources": sources_payload,
            "meta": {
                "evaluator_decision": verdict["decision"],
                "web_fallback_used":  web_used,
                "n_chunks_kept":      len(keep_idx),
                "n_chunks_total":     len(chunks),
            },
        },
        persist=PersistSpec(
            role="system", text=persist_text, event_type="rag_context",
        ),
    )

    total_ms = int((time.perf_counter() - t0) * 1000)

    # ── Structured log record (no-op outside benchmark) ────
    log_record = make_log_record(
        state, "rag",
        input={
            "question":     rerank_query,
            "user_text":    state.user_text,
            "embed_queries": state.rewrite.embed_queries if state.rewrite else [state.user_text],
            "ticker_prefixes": [
                (c.get("symbol") or "").upper()
                for c in state.resolved_companies if c.get("symbol")
            ],
        },
        output={"answer": rag_answer, "structured": structured},
        traces=[
            {"step": "retriever", "data": {
                "n_chunks": len(chunks),
                "chunks_preview": [
                    {"index": s.index, "file_name": s.file_name, "score": s.score,
                     "text": s.text[:160]}
                    for s in rag_sources
                ],
            }},
            {"step": "evaluator", "data": {
                "decision": verdict["decision"],
                "useful_idx": verdict["useful_idx"],
                "reason": verdict["explanation"][:300],
                "latency_ms": eval_ms,
            }},
            {"step": "web_fallback", "data": {
                "used": web_used,
                "n_snippets": len(web_snippets),
                "snippets": [{"title": s["title"], "url": s["url"]} for s in web_snippets],
            }},
            {"step": "generator", "data": {
                "kept_idx": keep_idx,
                "latency_ms": gen_ms,
                "answer_len": len(rag_answer),
            }},
        ],
        usage=total_usage,
        latency_ms=total_ms,
    )

    return {
        "rag_sources":    rag_sources,
        "rag_messages":   [rag_message],
        "rag_answer":     rag_answer,
        "rag_structured": structured,
        "component_logs": log_record,
    }


rag_runnable = RunnableLambda(_rag_node).with_config(run_name="rag")


__all__ = ["rag_runnable"]


# ── Helper for callers that need it without graph state ─────


async def has_files(project_id: int, session_id: UUID | None = None) -> bool:  # noqa: ARG001
    async with async_session_factory() as db:
        return await _project_has_files(db, project_id)
