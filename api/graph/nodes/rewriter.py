"""
Rewriter ReAct agent.

This is a ReAct agent (not a single LLM call) so it can:
    1. Call `lookup_company(query)` to verify a ticker / name fragment
       exists in `stocks` + `company_overview` BEFORE deciding scope.
    2. Iterate if the first guess didn't match (try a different spelling
       or fall back to clarification).
    3. Optionally call `list_tables` / `describe_table` to ground itself
       on what the OLAP database actually contains.

The agent's terminal response (no tool_calls) MUST be a single JSON
object matching the `RewriteOutput` schema. We parse it deterministically
and emit `query_rewrite` / `clarification` SSE events. If parsing fails
or the LLM crashes, we degrade to passthrough (use the original message
as embed query) so chat never blocks on rewriter quirks.

Cap rounds tight: the rewriter sits in the critical latency path before
RAG + tools, so REWRITER_MAX_ROUNDS=3 (config) is enough — typical run
is 1 lookup + 1 JSON emit.
"""

from __future__ import annotations

import json
import logging
import time

from langchain_core.runnables import RunnableConfig, RunnableLambda

from config import get_settings
from graph.llm_router import get_llm
from graph.react_agent import AgentTool, ReactAgent
from graph.sse import emit
from graph.state import ChatState, RewriteOutput
from prompts import get_query_rewriter_prompt
from tools.database_query import (
    DESCRIBE_TABLE_TOOL_SCHEMA,
    LIST_TABLES_TOOL_SCHEMA,
    LOOKUP_COMPANY_TOOL_SCHEMA,
    describe_table as db_describe_table,
    list_tables as db_list_tables,
    lookup_company as db_lookup_company,
    verify_company_entities,
)

log = logging.getLogger("finhouse.graph.rewriter")
settings = get_settings()

_VALID_SCOPE_TYPES = {"company", "sector", "macro", "general", ""}


# ── Tool handlers (adapt to ReactAgent's args:dict contract) ──


async def _h_lookup_company(args: dict):
    return await db_lookup_company(args.get("query", ""))


async def _h_list_tables(args: dict):  # noqa: ARG001
    return await db_list_tables()


async def _h_describe_table(args: dict):
    return await db_describe_table(args.get("table", ""))


def _make_rewriter_agent(session_model: str) -> ReactAgent:
    return ReactAgent(
        name="rewriter_agent",
        tool_type="database",   # tool_type is informational here
        llm=get_llm("rewriter", session_model),
        system_prompt=get_query_rewriter_prompt(),
        tools=[
            AgentTool(
                name="lookup_company",
                schema=LOOKUP_COMPANY_TOOL_SCHEMA,
                handler=_h_lookup_company,
            ),
            AgentTool(
                name="list_tables",
                schema=LIST_TABLES_TOOL_SCHEMA,
                handler=_h_list_tables,
            ),
            AgentTool(
                name="describe_table",
                schema=DESCRIBE_TABLE_TOOL_SCHEMA,
                handler=_h_describe_table,
            ),
        ],
        max_rounds=settings.REWRITER_MAX_ROUNDS,
    )


# ── JSON extraction (kept identical to legacy rewriter for parity) ──

def _extract_json(raw: str) -> dict | None:
    if not raw:
        return None
    s = raw.strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    import re
    for fence in ("```json", "```JSON", "```"):
        if fence in s:
            for part in s.split(fence):
                p = part.strip().rstrip("`").strip()
                if p.startswith("{"):
                    try:
                        return json.loads(p)
                    except Exception:
                        continue
    m = re.search(r"\{.*\}", s, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


def _coerce_str_list(raw, cap: int = 10) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, str):
        items = [p.strip() for p in raw.split(",")]
    elif isinstance(raw, list):
        items = [str(x).strip() for x in raw]
    else:
        return []
    return [x for x in items if x][:cap]


def _passthrough(original: str) -> RewriteOutput:
    return RewriteOutput(rewritten=original, original=original)


# ── Build the user message that drives the agent ────────────


def _build_user_block(state: ChatState) -> str:
    from services.rewriter import _now_context_block, _build_history_block

    history_block = (
        _build_history_block(state.history) if state.history else "(chưa có)"
    )
    return (
        f"{_now_context_block()}\n"
        f"LỊCH SỬ HỘI THOẠI:\n{history_block}\n\n"
        f"CÂU HỎI MỚI NHẤT CẦN PHÂN TÍCH & REWRITE:\n{state.user_text}\n\n"
        "Quy trình:\n"
        "  1. Phân tích câu hỏi để rút scope/time/metrics.\n"
        "  2. Nếu nghĩ scope là 'company' và có entity (ticker hoặc tên) → "
        "GỌI tool `lookup_company(query=...)` để verify trước. Có thể gọi "
        "nhiều lần với các biến thể tên (Vinamilk, VNM, Vinamilk Việt Nam) "
        "nếu lần đầu không match.\n"
        "  3. (Tùy chọn) `list_tables()` / `describe_table(table)` nếu cần "
        "biết DB có gì để phán đoán scope='sector'/'macro' chính xác hơn.\n"
        "  4. Sau khi đủ thông tin → DỪNG GỌI TOOL và emit DUY NHẤT 1 JSON "
        "object đúng schema (không markdown fence, không text khác).\n\n"
        "Schema JSON output:\n"
        "{\n"
        '  "rewritten": "<câu hỏi self-contained>",\n'
        '  "needs_clarification": false,\n'
        '  "clarification": "",\n'
        '  "scope_type": "company|sector|macro|general",\n'
        '  "preserved_entities": ["VNM"],\n'
        '  "preserved_timeframe": "2025",\n'
        '  "preserved_metrics": ["doanh thu"],\n'
        '  "applied_defaults": ["timeframe=2025"]\n'
        "}\n\n"
        "Nếu sau khi gọi `lookup_company` mà không entity nào match → "
        'set `needs_clarification=true`, viết `clarification` ngắn hỏi '
        'user xác nhận lại tên/ticker. Còn lại để mặc định.\n'
        "Nếu chỉ thiếu thời gian → áp default = NĂM TÀI CHÍNH GẦN NHẤT "
        "HOÀN CHỈNH ở khối bối cảnh phía trên, ghi vào `applied_defaults`."
    )


# ── Main node ───────────────────────────────────────────────


async def _rewriter_node(state: ChatState, config: RunnableConfig) -> dict:
    agent = _make_rewriter_agent(state.session_model)
    user_block = _build_user_block(state)

    t0 = time.perf_counter()
    try:
        result = await agent.run(
            goal=user_block,
            args_hint=None,
            config=config,
        )
    except Exception as e:
        log.warning("rewriter agent crashed: %s — passthrough", e)
        out = _passthrough(state.user_text)
        await emit(config, "query_rewrite", _rewrite_payload(state.user_text, out))
        return {"rewrite": out, "resolved_companies": []}

    raw = (result.answer or "").strip()
    parsed = _extract_json(raw)
    if not parsed:
        log.warning("rewriter output not parseable: %r — passthrough", raw[:200])
        out = _passthrough(state.user_text)
        await emit(config, "query_rewrite", _rewrite_payload(state.user_text, out))
        return {"rewrite": out, "resolved_companies": []}

    # Build RewriteOutput
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

    # Sanity repairs
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

    # Pull canonical company info from the lookup_company tool traces
    resolved: list[dict] = []
    for call in result.calls:
        if call.tool != "lookup_company" or not call.ok:
            continue
        try:
            payload = json.loads(call.result)
        except Exception:
            continue
        for m in payload.get("matches") or []:
            if isinstance(m, dict) and m.get("symbol"):
                if not any(r.get("symbol") == m.get("symbol") for r in resolved):
                    resolved.append(m)

    # Belt-and-suspenders: if the agent claimed company scope but didn't
    # call lookup_company (or matches were empty), do a final verify.
    if (
        not out.needs_clarification
        and out.scope_type == "company"
        and out.preserved_entities
        and not resolved
    ):
        try:
            r2, unresolved, ch_avail = await verify_company_entities(
                out.preserved_entities
            )
            resolved = r2
            if ch_avail and not r2:
                missing = ", ".join(unresolved or out.preserved_entities)
                out.needs_clarification = True
                out.clarification = (
                    f"Mình chưa tìm thấy công ty/mã chứng khoán khớp với "
                    f"'{missing}' trong dữ liệu nội bộ. Bạn có thể xác nhận "
                    "lại tên hoặc mã ticker (ví dụ: VNM, FPT, HPG, MWG...) "
                    "không ạ?"
                )
        except Exception as e:
            log.warning("post-rewrite verify failed: %s", e)

    log.info(
        "[rewriter %s] orig=%r → rewrite=%r clarify=%s scope=%s "
        "tool_calls=%d resolved=%d in %.0fms",
        agent.llm.label, state.user_text[:60], out.rewritten[:60],
        out.needs_clarification, out.scope_type,
        len(result.calls), len(resolved),
        (time.perf_counter() - t0) * 1000,
    )

    if out.needs_clarification:
        await emit(config, "clarification", {"content": out.clarification})
    else:
        await emit(config, "query_rewrite", _rewrite_payload(state.user_text, out))

    return {"rewrite": out, "resolved_companies": resolved}


def _rewrite_payload(original: str, out: RewriteOutput) -> dict:
    return {
        "original": original,
        "rewritten": out.rewritten,
        "scope_type": out.scope_type,
        "entities": out.preserved_entities,
        "timeframe": out.preserved_timeframe,
        "metrics": out.preserved_metrics,
        "applied_defaults": out.applied_defaults,
    }


rewriter_runnable = RunnableLambda(_rewriter_node).with_config(run_name="rewriter")
