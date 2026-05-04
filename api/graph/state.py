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

    @property
    def embed_query(self) -> str:
        if self.rewritten and not self.needs_clarification:
            return self.rewritten
        return self.original


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


class ToolCallTrace(BaseModel):
    """One tool invocation inside a ReAct agent loop."""

    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    ok: bool = True
    result: str = ""    # JSON-serialized tool output (capped)


class AgentResult(BaseModel):
    """Final output of a single tool ReAct agent run."""

    tool_type: ToolType
    goal: str
    answer: str = ""              # the agent's natural-language summary
    calls: list[ToolCallTrace] = Field(default_factory=list)
    error: str = ""


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
