"""
Rewriter node — wraps `services.rewriter.rewrite_query`.

Responsibilities:
    1. Call rewrite_query with the agent's chosen LLM.
    2. If scope=company, verify entities against ClickHouse. If none
       resolve, flip to clarification mode.
    3. Emit a `query_rewrite` (or `clarification`) SSE event with the
       structured fields the UI uses to display the rewritten query.

This node is NOT a ReAct agent — the rewriter prompt is engineered to
produce a single JSON object, not iterative tool use. The verification
step is a deterministic post-process. We still route the LLM call
through `llm_router.get_llm("rewriter")` so the brain is configurable.
"""

from __future__ import annotations

import logging
import time

from langchain_core.runnables import RunnableConfig, RunnableLambda

from graph.llm_router import get_llm
from graph.sse import emit
from graph.state import ChatState, RewriteOutput

log = logging.getLogger("finhouse.graph.rewriter")


async def _rewriter_node(state: ChatState, config: RunnableConfig) -> dict:
    from services.rewriter import (
        rewrite_query as _rewrite_query,
        verify_company_entities,
    )

    llm = get_llm("rewriter", state.session_model)

    # Re-implement the rewriter call against an LLMHandle so it talks
    # to whichever provider the rewriter agent is configured for. The
    # original `rewrite_query` calls services.ollama.chat_sync directly;
    # here we construct the same messages but route through the handle.
    from services.rewriter import (
        _now_context_block,
        _build_history_block,
        _extract_json,
        _coerce_str_list,
        _passthrough,
        REWRITE_TIMEOUT_SEC,
        _VALID_SCOPE_TYPES,
    )
    from prompts import get_query_rewriter_prompt

    history_block = (
        _build_history_block(state.history) if state.history else "(chưa có)"
    )
    user_content = (
        f"{_now_context_block()}\n"
        f"LỊCH SỬ HỘI THOẠI:\n{history_block}\n\n"
        f"CÂU HỎI MỚI NHẤT CẦN PHÂN TÍCH & REWRITE:\n{state.user_text}\n\n"
        "Output DUY NHẤT một JSON object đúng schema (không markdown fence, "
        "không giải thích). Nhớ: chỉ set needs_clarification=true khi không "
        "xác định được scope; nếu chỉ thiếu thời gian → áp default = NĂM "
        "TÀI CHÍNH GẦN NHẤT HOÀN CHỈNH ở khối bối cảnh phía trên."
    )
    messages = [
        {"role": "system", "content": get_query_rewriter_prompt()},
        {"role": "user", "content": user_content},
    ]

    t0 = time.perf_counter()
    parsed: dict | None = None
    try:
        resp = await llm.chat_sync(
            messages,
            tools=None,
            timeout=REWRITE_TIMEOUT_SEC,
            options={"temperature": 0.1, "num_predict": 800},
        )
        raw = (resp.get("message") or {}).get("content") or ""
        parsed = _extract_json(raw)
    except Exception as e:
        log.warning("rewriter (%s) failed: %s — passthrough", llm.label, e)

    if not parsed:
        result = _passthrough(state.user_text)
        out = RewriteOutput(**result.__dict__)
        await emit(config, "query_rewrite", {
            "original": state.user_text,
            "rewritten": out.rewritten,
            "scope_type": out.scope_type,
            "entities": out.preserved_entities,
            "timeframe": out.preserved_timeframe,
            "metrics": out.preserved_metrics,
            "applied_defaults": out.applied_defaults,
        })
        return {"rewrite": out, "resolved_companies": []}

    scope_type = str(parsed.get("scope_type", "") or "").strip().lower()
    if scope_type not in _VALID_SCOPE_TYPES:
        scope_type = ""

    out = RewriteOutput(
        rewritten=str(parsed.get("rewritten", "") or "").strip(),
        needs_clarification=bool(parsed.get("needs_clarification", False)),
        clarification=str(parsed.get("clarification", "") or "").strip(),
        scope_type=scope_type,
        preserved_entities=_coerce_str_list(parsed.get("preserved_entities")),
        preserved_timeframe=str(parsed.get("preserved_timeframe", "") or "").strip(),
        preserved_metrics=_coerce_str_list(parsed.get("preserved_metrics")),
        applied_defaults=_coerce_str_list(parsed.get("applied_defaults")),
        original=state.user_text,
    )

    # Sanity repairs (mirrors services.rewriter)
    if out.needs_clarification and not out.clarification:
        out.clarification = (
            "Bạn có thể nói rõ hơn về đối tượng (công ty, ngành hay vĩ mô) "
            "mà bạn đang muốn hỏi không ạ?"
        )
    if not out.needs_clarification and not out.rewritten:
        out.rewritten = state.user_text
    if not out.needs_clarification and out.rewritten and not out.scope_type:
        out.scope_type = "company" if out.preserved_entities else "general"
    if len(out.rewritten) > 2000:
        out.rewritten = out.rewritten[:2000]
    if len(out.clarification) > 600:
        out.clarification = out.clarification[:600]

    log.info(
        "[rewriter %s] orig=%r → rewrite=%r clarify=%s scope=%s in %.0fms",
        llm.label, state.user_text[:60], out.rewritten[:60],
        out.needs_clarification, out.scope_type,
        (time.perf_counter() - t0) * 1000,
    )

    # ── Company verification ─────────────────────────────────
    resolved: list[dict] = []
    if (
        not out.needs_clarification
        and out.scope_type == "company"
        and out.preserved_entities
    ):
        try:
            resolved, unresolved, ch_avail = await verify_company_entities(
                out.preserved_entities
            )
            if ch_avail and not resolved:
                missing = ", ".join(unresolved or out.preserved_entities)
                out.needs_clarification = True
                out.clarification = (
                    f"Mình chưa tìm thấy công ty/mã chứng khoán khớp với "
                    f"'{missing}' trong dữ liệu nội bộ. Bạn có thể xác nhận "
                    "lại tên hoặc mã ticker (ví dụ: VNM, FPT, HPG, MWG...) "
                    "không ạ?"
                )
        except Exception as e:
            log.warning("company verify error (treated as unavailable): %s", e)

    # ── Emit SSE ─────────────────────────────────────────────
    if out.needs_clarification:
        await emit(config, "clarification", {"content": out.clarification})
    else:
        await emit(config, "query_rewrite", {
            "original": state.user_text,
            "rewritten": out.rewritten,
            "scope_type": out.scope_type,
            "entities": out.preserved_entities,
            "timeframe": out.preserved_timeframe,
            "metrics": out.preserved_metrics,
            "applied_defaults": out.applied_defaults,
        })

    return {"rewrite": out, "resolved_companies": resolved}


rewriter_runnable = RunnableLambda(_rewriter_node).with_config(
    run_name="rewriter",
)
