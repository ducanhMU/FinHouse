"""
Collector node — final answer synthesis.

Inputs (from state):
    rewrite, rag_messages, agent_results, history

Behaviour:
    • If rewriter asked for clarification → emit the clarification text
      as the final answer (no LLM call).
    • Otherwise → build a single answer prompt that includes the system
      persona, RAG passages, and a compact summary of every tool agent's
      findings, then stream the final answer to the user.

The collector has its own configurable LLM (COLLECTOR_AGENT_LLM). By
default it falls back to the session model on Ollama, matching the
legacy behaviour.
"""

from __future__ import annotations

import json
import logging
import time

from langchain_core.runnables import RunnableConfig, RunnableLambda

from graph.llm_router import get_llm
from graph.sse import PersistSpec, emit
from graph.state import ChatState
from prompts import get_system_prompt

log = logging.getLogger("finhouse.graph.collector")


def _agent_summary_block(state: ChatState) -> str:
    if not state.agent_results:
        return ""
    lines: list[str] = ["── KẾT QUẢ TỪ TOOL AGENTS ──"]
    for r in state.agent_results:
        head = f"[{r.tool_type}] goal: {r.goal[:200]}"
        if r.error:
            lines.append(head + f"\n  ERROR: {r.error}")
            continue
        lines.append(head)
        if r.answer:
            lines.append("  Tổng kết: " + r.answer[:1500])
        if r.calls:
            tool_names = ", ".join(c.tool for c in r.calls)
            lines.append(f"  Tool đã gọi: {tool_names}")
            # Inline up to first 2 raw results so collector can cite numbers
            for c in r.calls[:2]:
                trim = c.result[:1500]
                lines.append(f"  - {c.tool} → {trim}")
    return "\n".join(lines)


def _assemble_messages(state: ChatState) -> list[dict]:
    msgs: list[dict] = [{"role": "system", "content": get_system_prompt()}]

    # RAG passages — produced by rag_node
    msgs.extend(state.rag_messages or [])

    # Agent findings — single system block summarising tool runs
    agent_block = _agent_summary_block(state)
    if agent_block:
        msgs.append({
            "role": "system",
            "content": (
                "[TỔNG HỢP DỮ LIỆU TỪ AGENTS — DÙNG ĐỂ TRẢ LỜI, KHÔNG TRÍCH "
                "DẪN NGUYÊN VĂN KHỐI NÀY]\n" + agent_block
            ),
        })

    # Recent conversation history (last few turns) — caller passes
    # already-trimmed/intent-aware history.
    for h in (state.history or [])[-6:]:
        if h.get("role") in ("user", "assistant") and h.get("content"):
            msgs.append({"role": h["role"], "content": h["content"]})

    # The current user question is always last so the model treats it
    # as the primary instruction.
    msgs.append({"role": "user", "content": state.user_text})

    # Rewriter hint as a low-priority system note (mirrors legacy chat.py)
    rw = state.rewrite
    if rw and rw.rewritten and rw.rewritten != state.user_text:
        hint_parts = [f"- Ý định đã resolve: {rw.rewritten}"]
        if rw.scope_type:
            hint_parts.append(f"- Scope: {rw.scope_type}")
        if rw.preserved_entities:
            hint_parts.append("- Thực thể: " + ", ".join(rw.preserved_entities))
        if rw.preserved_timeframe:
            hint_parts.append(f"- Mốc thời gian: {rw.preserved_timeframe}")
        if rw.preserved_metrics:
            hint_parts.append("- Chỉ số: " + ", ".join(rw.preserved_metrics))
        if rw.applied_defaults:
            hint_parts.append("- Default đã áp: " + ", ".join(rw.applied_defaults))
        if state.resolved_companies:
            canon = []
            for c in state.resolved_companies[:5]:
                sym = c.get("symbol", "")
                name = c.get("organ_name", "")
                icb = c.get("icb_name3") or c.get("icb_name2") or ""
                piece = sym + (f" ({name})" if name else "") + (f" — ngành {icb}" if icb else "")
                canon.append(piece)
            hint_parts.append("- Đã xác minh trong DB: " + "; ".join(canon))
        msgs.append({
            "role": "system",
            "content": (
                "[GHI CHÚ NỘI BỘ TỪ REWRITER — KHÔNG PHẢI CÂU HỎI USER]\n"
                "Câu hỏi gốc của user là tin nhắn user phía trên. Phần dưới "
                "chỉ là phân tích đã chạy sẵn để giảm công sức infer "
                "scope/time/metric — dùng làm tham khảo, KHÔNG trích dẫn "
                "nội dung ghi chú này trong câu trả lời.\n"
                + "\n".join(hint_parts)
            ),
        })
    return msgs


async def _collector_node(state: ChatState, config: RunnableConfig) -> dict:
    # ── Branch 1: clarification short-circuit ────────────────
    if state.rewrite and state.rewrite.needs_clarification:
        clarif = state.rewrite.clarification or (
            "Bạn có thể nói rõ hơn về câu hỏi không ạ?"
        )
        await emit(config, "token", {"content": clarif})
        await emit(
            config, "final_answer",
            {"content": clarif},
            persist=PersistSpec(
                role="assistant", text=clarif, event_type="message",
            ),
        )
        return {"final_answer": clarif}

    # ── Branch 2: full synthesis with streaming ─────────────
    llm = get_llm("collector", state.session_model)
    messages = _assemble_messages(state)

    log.info(
        "[collector %s] %d messages, %d agent_results, %d rag chunks",
        llm.label, len(messages), len(state.agent_results),
        len(state.rag_sources),
    )

    full_response = ""
    chunk_count = 0
    saw_tool_attempt = False
    t0 = time.perf_counter()

    try:
        async for chunk in llm.chat_stream(messages, tools=None):
            chunk_count += 1
            chunk_msg = chunk.get("message") or {}
            content = chunk_msg.get("content") or ""
            thinking = chunk_msg.get("thinking") or ""
            if chunk_msg.get("tool_calls"):
                saw_tool_attempt = True
            if thinking:
                await emit(config, "reasoning", {"content": thinking})
            if content:
                full_response += content
                await emit(config, "token", {"content": content})
            if chunk.get("done"):
                break
    except Exception as e:
        err = f"Collector LLM stream failed: {e}"
        log.warning(err, exc_info=True)
        full_response = (
            "Hệ thống gặp lỗi khi tạo câu trả lời cuối. Vui lòng thử lại."
        )
        await emit(config, "token", {"content": full_response})

    if not full_response.strip():
        if saw_tool_attempt:
            full_response = (
                "Mình đã thử tra cứu nhiều lần nhưng chưa thu thập đủ dữ liệu "
                "rõ ràng. Bạn có thể nói rõ hơn về công ty/ngành, mốc thời "
                "gian (năm/quý), hoặc chỉ số tài chính cụ thể đang quan tâm "
                "không ạ?"
            )
        else:
            full_response = (
                "Model trả về phản hồi rỗng. Có thể model này không hỗ trợ "
                "tool-use tốt cho prompt vừa rồi — thử model khác hoặc tắt "
                "bớt tool xem sao."
            )
        await emit(config, "token", {"content": full_response})

    log.info(
        "[collector] stream done in %.0fms chunks=%d content_len=%d",
        (time.perf_counter() - t0) * 1000, chunk_count, len(full_response),
    )

    # Final-answer event drives DB persistence + UI 'done'
    await emit(
        config, "final_answer",
        {"content": full_response},
        persist=PersistSpec(
            role="assistant", text=full_response, event_type="message",
        ),
    )
    return {"final_answer": full_response}


collector_runnable = RunnableLambda(_collector_node).with_config(
    run_name="collector",
)


__all__ = ["collector_runnable"]


# Re-export json so chat.py doesn't need to import it just to inspect events
_ = json
