"""
FinHouse — Minimal ReAct loop for tool agents.

Each tool agent (web / database / visualize) is an instance of
`ReactAgent`. The loop:

    1. Send messages to the agent's LLM with the agent's tool schemas.
    2. If the model returns tool_calls, execute them via the registered
       handler functions and feed results back as tool messages.
    3. Repeat until the model stops emitting tool_calls (or we hit
       AGENT_MAX_ROUNDS — at which point the model is told to wrap up
       with what it has).
    4. Return an `AgentResult` with the natural-language answer and a
       trace of every tool invocation.

The handlers are the existing functions in `api/tools/*.py` — we don't
rewrite them, we just adapt their signatures into a uniform
`async (args: dict) -> dict | list | str` shape.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from langchain_core.runnables import RunnableConfig

from config import get_settings
from graph.llm_router import LLMHandle
from graph.sse import emit_tool_end, emit_tool_start
from graph.state import AgentResult, LLMUsage, ToolCallTrace, ToolType

log = logging.getLogger("finhouse.graph.react")
settings = get_settings()


ToolHandler = Callable[[dict], Awaitable[Any]]


@dataclass
class AgentTool:
    name: str
    schema: dict          # OpenAI/Ollama function schema
    handler: ToolHandler  # async fn(args) -> JSON-serialisable


@dataclass
class ReactAgent:
    """
    A small ReAct-style loop bound to one LLMHandle, one prompt, and
    one set of tools. Stateless between invocations — call `run()` for
    each new task.

    `default_options` is forwarded to every `chat_sync` call (think
    `response_format`, `temperature`…). Use it to opt agents into JSON
    mode when their final answer must be machine-parseable (e.g. the
    rewriter, which emits a `RewriteOutput` JSON envelope).
    """

    name: str
    tool_type: ToolType
    llm: LLMHandle
    system_prompt: str
    tools: list[AgentTool]
    max_rounds: Optional[int] = None
    max_result_chars: int = 20_000
    default_options: Optional[dict] = None

    async def run(
        self,
        goal: str,
        args_hint: Optional[dict] = None,
        config: Optional[RunnableConfig] = None,
    ) -> AgentResult:
        """Run the agent until it stops calling tools (or hits the cap)."""
        rounds_cap = self.max_rounds or settings.AGENT_MAX_ROUNDS

        user_block = f"NHIỆM VỤ: {goal}"
        if args_hint:
            user_block += "\nGỢI Ý THAM SỐ: " + json.dumps(
                args_hint, ensure_ascii=False, default=str
            )
        user_block += (
            "\n\nGọi đúng các tool đã đăng ký để hoàn thành nhiệm vụ này. "
            "Khi đã đủ dữ liệu, dừng gọi tool và trả về tổng kết ngắn "
            "bằng tiếng Việt nêu rõ kết quả + nguồn (tên tool/bảng/URL "
            "khi có)."
        )
        messages: list[dict] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_block},
        ]
        tool_schemas = [t.schema for t in self.tools] if self.tools else None
        traces: list[ToolCallTrace] = []
        answer = ""
        usage = LLMUsage()
        hit_soft_ceiling = False

        for round_idx in range(rounds_cap + 1):
            # Soft ceiling: tell the model to stop calling tools
            if tool_schemas and round_idx == rounds_cap:
                messages.append({
                    "role": "system",
                    "content": (
                        f"Đã chạy {rounds_cap} vòng tool — DỪNG gọi thêm. "
                        "Tổng kết ngắn bằng tiếng Việt từ dữ liệu đã có. "
                        "Nếu thiếu, nói rõ thiếu gì để bước collector "
                        "biết không bịa."
                    ),
                })
                tool_schemas = None  # force final answer
                hit_soft_ceiling = True

            try:
                resp = await self.llm.chat_sync(
                    messages, tools=tool_schemas,
                    options=self.default_options,
                )
            except Exception as e:
                log.warning("ReAct[%s] LLM call failed: %s", self.name, e)
                return AgentResult(
                    tool_type=self.tool_type, goal=goal,
                    answer="", calls=traces,
                    error=f"LLM failure: {e}",
                    usage=usage,
                )

            # Accumulate token usage across rounds.
            u = resp.get("usage")
            if isinstance(u, dict):
                usage = usage.add(LLMUsage(**u))

            msg = resp.get("message") or {}
            tool_calls = msg.get("tool_calls") or []

            if tool_calls and tool_schemas:
                # Execute tools (sequentially — easier to attribute traces).
                messages.append({
                    "role": "assistant",
                    "content": msg.get("content", "") or "",
                    "tool_calls": tool_calls,
                })
                for tc in tool_calls:
                    fn = tc.get("function", {}) or {}
                    tname = fn.get("name") or ""
                    targs = fn.get("arguments") or {}
                    if isinstance(targs, str):
                        try:
                            targs = json.loads(targs)
                        except Exception:
                            targs = {"_raw": targs}

                    await emit_tool_start(config, tname, targs, agent=self.name)

                    handler = self._handler_for(tname)
                    if handler is None:
                        result_text = json.dumps(
                            {"error": f"unknown tool '{tname}' for agent {self.name}"},
                            ensure_ascii=False,
                        )
                        ok = False
                    else:
                        try:
                            result = await handler(targs)
                            result_text = json.dumps(result, ensure_ascii=False, default=str)
                            ok = not (isinstance(result, dict) and result.get("error"))
                        except Exception as e:
                            result_text = json.dumps(
                                {"error": f"{tname} crashed: {e}"},
                                ensure_ascii=False,
                            )
                            ok = False

                    if len(result_text) > self.max_result_chars:
                        result_text = (
                            result_text[: self.max_result_chars]
                            + f"\n[... truncated {len(result_text) - self.max_result_chars} chars]"
                        )

                    traces.append(ToolCallTrace(
                        tool=tname, args=targs, ok=ok, result=result_text,
                    ))
                    await emit_tool_end(
                        config, tname, result_text, error=not ok, agent=self.name,
                    )
                    messages.append({"role": "tool", "content": result_text})

                # Loop again — let the model react to tool results.
                continue

            # No tool calls → this is the final answer for the agent.
            answer = (msg.get("content") or "").strip()
            break

        needs_clar, clar_req = self._detect_clarification(
            goal, traces, answer, hit_soft_ceiling,
        )
        return AgentResult(
            tool_type=self.tool_type, goal=goal,
            answer=answer, calls=traces, error="",
            needs_clarification=needs_clar,
            clarification_request=clar_req,
            usage=usage,
        )

    @staticmethod
    def _detect_clarification(
        goal: str,
        traces: list[ToolCallTrace],
        answer: str,
        hit_soft_ceiling: bool,
    ) -> tuple[bool, str]:
        """Decide whether the agent needs the user to clarify.

        Surface a clarification suggestion to the collector when one of
        these heuristics fires — collector embeds the question in its
        final answer so the chat flow stays single-turn (no extra graph
        node, no second user prompt mid-run):

          1. Every tool call errored (or no tool was called) AND the
             agent produced no usable answer.
          2. We hit the soft ceiling (max rounds) yet the answer is
             still empty/very short — agent ran out of room without
             converging.
        """
        successful = [t for t in traces if t.ok]
        any_call = bool(traces)
        ans_short = len(answer) < 40
        all_failed = any_call and not successful

        if (not any_call and ans_short) or all_failed or (hit_soft_ceiling and ans_short):
            return True, (
                f"Mình chưa thu thập đủ dữ liệu cho yêu cầu \"{goal[:120]}\". "
                "Bạn có thể cung cấp thêm thông tin (mã chứng khoán/ticker, "
                "mốc thời gian cụ thể, tên bảng/chỉ số) để mình truy xuất "
                "chính xác hơn không?"
            )
        return False, ""

    def _handler_for(self, tool_name: str) -> Optional[ToolHandler]:
        for t in self.tools:
            if t.name == tool_name:
                return t.handler
        return None


# ── Helper: run multiple agents in parallel ─────────────────


async def run_agents_parallel(
    runs: list[tuple[ReactAgent, str, Optional[dict]]],
    config: Optional[RunnableConfig] = None,
) -> list[AgentResult]:
    """Fire all agent runs concurrently. Order matches input."""
    if not runs:
        return []
    coros = [agent.run(goal, args, config) for (agent, goal, args) in runs]
    return await asyncio.gather(*coros)
