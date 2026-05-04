"""
Orchestrator node — converts the rewriter's structured intent into an
`OrchestratorPlan` (a list of OrchestratorTask). The plan is consumed
by the `dispatcher` node, which runs the matching tool agent for each
task.

The orchestrator is a single LLM call returning JSON — no tool calls
of its own. Its only choice is *which* downstream agents to spawn.

If the LLM call fails or the output is malformed, we degrade gracefully
to "no tasks" — RAG context alone often answers the question and the
collector will say so.
"""

from __future__ import annotations

import json
import logging
import re
import time

from langchain_core.runnables import RunnableConfig, RunnableLambda

from graph.llm_router import get_llm
from graph.sse import emit
from graph.state import ChatState, OrchestratorPlan, OrchestratorTask, ToolType
from prompts import get_orchestrator_prompt

log = logging.getLogger("finhouse.graph.orchestrator")


_VALID_TOOL_TYPES: set[ToolType] = {"web_search", "database", "visualize"}


def _extract_json(raw: str) -> dict | None:
    if not raw:
        return None
    s = raw.strip()
    try:
        return json.loads(s)
    except Exception:
        pass
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


def _build_user_block(state: ChatState) -> str:
    rw = state.rewrite
    parts: list[str] = []
    parts.append(f"CÂU HỎI USER: {state.user_text}")
    if rw and rw.rewritten and rw.rewritten != state.user_text:
        parts.append(f"REWRITTEN: {rw.rewritten}")
    if rw and rw.scope_type:
        parts.append(f"SCOPE: {rw.scope_type}")
    if rw and rw.preserved_entities:
        parts.append("ENTITIES: " + ", ".join(rw.preserved_entities))
    if rw and rw.preserved_timeframe:
        parts.append(f"TIMEFRAME: {rw.preserved_timeframe}")
    if rw and rw.preserved_metrics:
        parts.append("METRICS: " + ", ".join(rw.preserved_metrics))
    if rw and rw.applied_defaults:
        parts.append("APPLIED_DEFAULTS: " + ", ".join(rw.applied_defaults))
    if state.resolved_companies:
        canon = []
        for c in state.resolved_companies[:5]:
            sym = c.get("symbol", "")
            name = c.get("organ_name", "")
            piece = sym + (f" ({name})" if name else "")
            canon.append(piece)
        parts.append("VERIFIED_COMPANIES: " + "; ".join(canon))
    parts.append("TOOLS_ENABLED: " + ", ".join(state.enabled_tools or ["(none)"]))
    parts.append(
        "\nHãy lên kế hoạch task cho các agent. Output JSON đúng schema."
    )
    return "\n".join(parts)


async def _orchestrator_node(state: ChatState, config: RunnableConfig) -> dict:
    # Skip if rewriter asked for clarification — the run will short-circuit.
    if state.rewrite and state.rewrite.needs_clarification:
        return {"plan": OrchestratorPlan(tasks=[], reasoning="(skipped: clarification requested)")}

    # If no tools are enabled at all, the collector can answer using only
    # RAG + system prompt. No need to ping the orchestrator LLM.
    if not state.enabled_tools:
        plan = OrchestratorPlan(tasks=[], reasoning="(skipped: no tools enabled)")
        await emit(config, "orchestrator_plan", {
            "tasks": [],
            "reasoning": plan.reasoning,
        })
        return {"plan": plan}

    llm = get_llm("orchestrator", state.session_model)
    messages = [
        {"role": "system", "content": get_orchestrator_prompt()},
        {"role": "user", "content": _build_user_block(state)},
    ]

    t0 = time.perf_counter()
    parsed = None
    try:
        resp = await llm.chat_sync(
            messages, tools=None,
            timeout=12.0,
            options={"temperature": 0.2, "num_predict": 600},
        )
        raw = (resp.get("message") or {}).get("content") or ""
        parsed = _extract_json(raw)
    except Exception as e:
        log.warning("[orchestrator] LLM call failed: %s", e)

    plan = OrchestratorPlan(tasks=[], reasoning="")
    if parsed:
        try:
            tasks: list[OrchestratorTask] = []
            for raw_task in (parsed.get("tasks") or []):
                if not isinstance(raw_task, dict):
                    continue
                tt = str(raw_task.get("tool_type", "") or "").strip().lower()
                if tt not in _VALID_TOOL_TYPES:
                    continue
                # filter by enabled tools — orchestrator may be optimistic
                enabled_map = {
                    "web_search": "web_search" in state.enabled_tools,
                    "database":   "database_query" in state.enabled_tools,
                    "visualize":  "visualize" in state.enabled_tools,
                }
                if not enabled_map.get(tt, False):
                    continue
                tasks.append(OrchestratorTask(
                    goal=str(raw_task.get("goal", "") or "").strip(),
                    tool_type=tt,  # type: ignore[arg-type]
                    args=raw_task.get("args") or {},
                ))
            plan = OrchestratorPlan(
                tasks=tasks,
                reasoning=str(parsed.get("reasoning", "") or "").strip(),
            )
        except Exception as e:
            log.warning("[orchestrator] plan construction failed: %s", e)

    log.info(
        "[orchestrator %s] %d tasks reasoning=%r in %.0fms",
        llm.label, len(plan.tasks), plan.reasoning[:80],
        (time.perf_counter() - t0) * 1000,
    )

    await emit(config, "orchestrator_plan", {
        "tasks": [t.model_dump() for t in plan.tasks],
        "reasoning": plan.reasoning,
    })
    return {"plan": plan}


orchestrator_runnable = RunnableLambda(_orchestrator_node).with_config(
    run_name="orchestrator",
)
