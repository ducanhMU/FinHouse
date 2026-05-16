"""
Layer C — Agent / tool metrics.

Two flavours:

1. Generic tool agent metrics (B, C, D, E, F):
     tool_selection      — Jaccard(expected_tools, agents that ran)
     tool_args_valid     — LLM judge: agent's tool args used right entity/timeframe
     tool_result_ok      — LLM judge: tool result actually addresses the goal

2. Bucket G (visualize) — structural only, no reference_answer:
     chart_type_acc      — agent picked bar/line/pie from expected set
     table_acc           — agent passed correct OLAP table
     column_acc          — Jaccard(expected y_columns, actual y_columns)
     filter_acc          — filters cover expected entity + timeframe (LLM judge)
     tool_result_ok      — visualize tool returned a URL (not error)

`score_agent(case, actual)` dispatches based on category and returns a
flat dict.
"""

from __future__ import annotations

from evaluation.judges import binary_judge, jaccard, safe_mean


# Map state.agent_results[*].tool_type → testset expected_tools naming.
# Both vocabs use the same strings in practice, but be lenient.
_TOOL_ALIASES = {
    "database":   {"database", "db"},
    "web_search": {"web_search", "web"},
    "visualize":  {"visualize", "viz", "chart"},
}


def _normalise_tools(names: list[str]) -> set[str]:
    out: set[str] = set()
    for n in names or []:
        nl = (n or "").strip().lower()
        if not nl:
            continue
        # Map to canonical key
        for canon, aliases in _TOOL_ALIASES.items():
            if nl in aliases:
                out.add(canon)
                break
        else:
            out.add(nl)
    return out


# ── 1. tool_selection ─────────────────────────────────────────


def tool_selection(expected_tools: list[str], agent_results: list[dict]) -> float:
    exp = _normalise_tools(expected_tools)
    actual = _normalise_tools([r.get("tool_type") for r in agent_results])
    if not exp and not actual:
        return 1.0
    if not exp or not actual:
        return 0.0
    return len(exp & actual) / len(exp | actual)


# ── 2. tool_args_valid (judge) ────────────────────────────────


_ARGS_VALID_SYS = (
    "Bạn là grader đánh giá agent gọi tool đúng ngữ cảnh không. Cho "
    "EXPECTED (entity, timeframe kỳ vọng) và ACTUAL_CALLS (args agent "
    "thực tế truyền). Trả True nếu CÁC tool call quan trọng có ÍT NHẤT "
    "filter / argument liên quan tới expected entity + timeframe "
    "(symbol, year, quarter, từ khoá tìm kiếm…). False nếu các call "
    "không hề chứa entity/timeframe (= agent gọi tool sai mục đích)."
)


async def tool_args_valid(case: dict, agent_results: list[dict]) -> float:
    if not agent_results:
        return 0.0
    expected = {
        "entities":  case.get("expected_entities", []),
        "timeframe": case.get("expected_timeframe", ""),
    }
    if not expected["entities"] and not expected["timeframe"]:
        return 1.0   # nothing to check
    calls_compact = []
    for r in agent_results:
        for c in r.get("calls", []):
            calls_compact.append({
                "tool": c.get("tool"),
                "args": c.get("args"),
                "ok":   c.get("ok"),
            })
    if not calls_compact:
        return 0.0
    payload = (
        f"EXPECTED:\n  entities: {expected['entities']}\n"
        f"  timeframe: {expected['timeframe']!r}\n\n"
        f"ACTUAL_CALLS (tool, args):\n{calls_compact[:20]}"
    )
    r = await binary_judge(_ARGS_VALID_SYS, payload)
    return r["score"]


# ── 3. tool_result_ok (judge) ─────────────────────────────────


_RESULT_OK_SYS = (
    "Bạn là grader đánh giá kết quả tool có đáp ứng goal được giao "
    "không. True nếu kết quả chứa thông tin đủ để trả lời goal "
    "(số liệu, snippet, schema, URL hợp lệ). False nếu sai schema, "
    "empty, error, hoặc dữ liệu không match entity/timeframe."
)


async def tool_result_ok(agent_results: list[dict]) -> float:
    """Average over agents — each agent's `answer` field is its synthesis
    of the tool results. We judge whether that synthesis looks right
    given the goal it was given."""
    if not agent_results:
        return 0.0
    scores: list[float] = []
    for r in agent_results:
        goal = (r.get("goal") or "")[:300]
        answer = (r.get("answer") or "")[:2000]
        if r.get("error"):
            scores.append(0.0)
            continue
        if not answer.strip():
            scores.append(0.0)
            continue
        payload = (
            f"GOAL: {goal}\nTOOL_TYPE: {r.get('tool_type')}\n\n"
            f"AGENT_ANSWER:\n{answer}"
        )
        j = await binary_judge(_RESULT_OK_SYS, payload)
        scores.append(j["score"])
    return safe_mean(scores)


# ── Bucket G metrics ──────────────────────────────────────────


def _find_viz_call(agent_results: list[dict]) -> dict | None:
    """Return the bar/line/pie/chart_from_data call (last one wins)."""
    last: dict | None = None
    for r in agent_results:
        if r.get("tool_type") != "visualize":
            continue
        for c in r.get("calls", []):
            if c.get("tool") in {"bar", "line", "pie", "chart_from_data"}:
                last = c
    return last


def chart_type_acc(case: dict, agent_results: list[dict]) -> float:
    expected = (case.get("expected_chart") or {}).get("chart_type") or []
    if isinstance(expected, str):
        expected = [expected]
    call = _find_viz_call(agent_results)
    if not call:
        return 0.0
    return 1.0 if call.get("tool") in expected else 0.0


def table_acc(case: dict, agent_results: list[dict]) -> float:
    expected = (case.get("expected_chart") or {}).get("table")
    if not expected:
        return 1.0
    call = _find_viz_call(agent_results)
    if not call:
        return 0.0
    actual = (call.get("args") or {}).get("table")
    return 1.0 if actual == expected else 0.0


def column_acc(case: dict, agent_results: list[dict]) -> float:
    expected = (case.get("expected_chart") or {}).get("y_columns") or []
    call = _find_viz_call(agent_results)
    if not call:
        return 0.0
    actual = (call.get("args") or {}).get("y_columns") or []
    return jaccard(expected, actual)


_FILTER_SYS = (
    "Đánh giá FILTERS agent truyền vào visualize tool có CHỨA expected "
    "entity + timeframe không. True nếu có ít nhất 1 filter cho mỗi cái "
    "(symbol=VNM, year=2024…). False nếu filters rỗng hoặc không liên quan."
)


async def filter_acc(case: dict, agent_results: list[dict]) -> float:
    expected = (case.get("expected_chart") or {}).get("filters") or {}
    if not expected:
        return 1.0
    call = _find_viz_call(agent_results)
    if not call:
        return 0.0
    actual = (call.get("args") or {}).get("filters") or {}
    payload = (
        f"EXPECTED filters: {expected}\n"
        f"ACTUAL filters:   {actual}\n"
        f"Expected entities: {case.get('expected_entities', [])}\n"
        f"Expected timeframe: {case.get('expected_timeframe', '')!r}"
    )
    r = await binary_judge(_FILTER_SYS, payload)
    return r["score"]


def viz_tool_result_ok(agent_results: list[dict]) -> float:
    """Did the visualize agent get a non-error result back?"""
    viz_agents = [r for r in agent_results if r.get("tool_type") == "visualize"]
    if not viz_agents:
        return 0.0
    ok_count = 0
    for r in viz_agents:
        if r.get("error"):
            continue
        # Look for any successful call result containing a URL
        for c in r.get("calls", []):
            if c.get("ok") and ("http" in (c.get("result") or "").lower() or "minio" in (c.get("result") or "").lower()):
                ok_count += 1
                break
    return ok_count / len(viz_agents)


# ── orchestrator ──────────────────────────────────────────────


async def score_agent(case: dict, actual: dict) -> dict:
    """Dispatch to generic vs Bucket G metrics based on category."""
    agent_results = actual.get("agent_results") or []
    cat = (case.get("category") or "").strip()
    is_g = cat.startswith("G.")

    if is_g:
        return {
            "chart_type_acc": chart_type_acc(case, agent_results),
            "table_acc":      table_acc(case, agent_results),
            "column_acc":     column_acc(case, agent_results),
            "filter_acc":     await filter_acc(case, agent_results),
            "tool_result_ok": viz_tool_result_ok(agent_results),
        }

    return {
        "tool_selection":  tool_selection(case.get("expected_tools", []), agent_results),
        "tool_args_valid": await tool_args_valid(case, agent_results),
        "tool_result_ok":  await tool_result_ok(agent_results),
    }


__all__ = ["score_agent"]
