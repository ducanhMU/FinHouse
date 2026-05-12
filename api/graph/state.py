"""
FinHouse — Graph state models.

All node I/O is routed through Pydantic models so each step has a
guaranteed shape. The big shared object is `ChatState`, which lives
across nodes inside the LangGraph runtime; smaller models are used as
the contract for individual ReAct agents.

Field reducers:
    * `rag_sources`, `tool_results`, `agent_traces` use Annotated +
      operator.add so parallel branches can append without overwriting.
    * Scalar fields (rewrite, plan, final_answer) have a single writer.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field


# ── Rewriter ─────────────────────────────────────────────────

ScopeType = Literal["company", "sector", "macro", "general", ""]


class RewriteOutput(BaseModel):
    """Pydantic mirror of services.rewriter.RewriteResult."""

    rewritten: str = ""
    needs_clarification: bool = False
    clarification: str = ""
    scope_type: ScopeType = ""
    preserved_entities: list[str] = Field(default_factory=list)
    preserved_timeframe: str = ""
    preserved_metrics: list[str] = Field(default_factory=list)
    applied_defaults: list[str] = Field(default_factory=list)
    original: str = ""

    # ── HyDE additions (backward-compatible; default empty) ──
    # Hypothetical answer passages (HyDE). Written in document style as
    # if extracted from a financial report, NOT as questions. Used as
    # additional embed queries for retrieval. Empty list → no HyDE
    # boost, retrieval falls back to single-query (rewritten only).
    hypothetical_passages: list[str] = Field(default_factory=list)
    # Paraphrased clarification options shown to the user as clickable
    # chips when needs_clarification=True. Each item is a self-contained
    # question with a different scope assumption. Empty → UI shows only
    # the plain `clarification` text.
    clarification_suggestions: list[str] = Field(default_factory=list)

    @property
    def embed_query(self) -> str:
        if self.rewritten and not self.needs_clarification:
            return self.rewritten
        return self.original

    @property
    def embed_queries(self) -> list[str]:
        """All queries to embed for retrieval: rewritten + HyDE passages."""
        if self.needs_clarification:
            return [self.original] if self.original else []
        base = self.rewritten or self.original
        if not base:
            return []
        out = [base]
        for p in self.hypothetical_passages:
            p = (p or "").strip()
            if p and p not in out:
                out.append(p)
        return out


# ── Orchestrator ─────────────────────────────────────────────

ToolType = Literal["web_search", "database", "visualize"]


class OrchestratorTask(BaseModel):
    """One task assignment from orchestrator → tool agent."""

    goal: str
    tool_type: ToolType
    args: dict[str, Any] = Field(default_factory=dict)


class OrchestratorPlan(BaseModel):
    tasks: list[OrchestratorTask] = Field(default_factory=list)
    reasoning: str = ""


# ── RAG ──────────────────────────────────────────────────────


class RagChunk(BaseModel):
    index: int
    file_name: str
    text: str
    score: float = 0.0


# ── Tool agent results ───────────────────────────────────────


class LLMUsage(BaseModel):
    """Token accounting for one or more LLM calls.

    Aggregated across rounds within a single ReAct agent run, and across
    the orchestrator / rewriter / collector calls in the surrounding
    chat turn. Default zero so summing is straightforward.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    calls: int = 0   # number of LLM calls aggregated into this counter

    def add(self, other: "LLMUsage") -> "LLMUsage":
        return LLMUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            calls=self.calls + other.calls,
        )


class ToolCallTrace(BaseModel):
    """One tool invocation inside a ReAct agent loop."""

    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    ok: bool = True
    result: str = ""    # JSON-serialized tool output (capped)


class AgentResult(BaseModel):
    """Final output of a single tool ReAct agent run.

    `needs_clarification` + `clarification_request` form the ask-back
    contract: tool agents never block the chat to ask the user — they
    flag the gap here and the collector node weaves a suggestion into
    its final answer (so the existing graph flow is unchanged).
    """

    tool_type: ToolType
    goal: str
    answer: str = ""              # the agent's natural-language summary
    calls: list[ToolCallTrace] = Field(default_factory=list)
    error: str = ""
    needs_clarification: bool = False
    clarification_request: str = ""
    usage: LLMUsage = Field(default_factory=LLMUsage)


# ── Top-level graph state ────────────────────────────────────


class ChatState(BaseModel):
    """
    State carried across all graph nodes for one user turn.

    Inputs (set before invoke):
        session_id, user_text, history, enabled_tools,
        session_model, project_id, user_id

    Outputs (filled by nodes):
        rewrite, resolved_companies, rag_sources, plan, agent_results,
        final_answer
    """

    # ── inputs (immutable across the run) ──
    session_id: UUID
    user_id: int
    project_id: int
    user_text: str
    history: list[dict[str, str]] = Field(default_factory=list)
    enabled_tools: list[str] = Field(default_factory=list)
    session_model: str

    # ── computed by nodes ──
    rewrite: Optional[RewriteOutput] = None
    resolved_companies: list[dict[str, Any]] = Field(default_factory=list)

    # parallel writers — RAG branch + dispatch branch.
    rag_sources: Annotated[list[RagChunk], operator.add] = Field(default_factory=list)
    rag_messages: Annotated[list[dict], operator.add] = Field(default_factory=list)

    plan: Optional[OrchestratorPlan] = None
    agent_results: Annotated[list[AgentResult], operator.add] = Field(default_factory=list)

    final_answer: str = ""

    # Class-level config: allow non-validated assignment for runtime queues.
    model_config = {
        "arbitrary_types_allowed": True,
    }
