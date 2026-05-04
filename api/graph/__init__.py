"""FinHouse — Multi-ReAct chat graph (LangChain + LangGraph wrapper).

Public surface:
    get_graph()    — compiled LangGraph runtime
    ChatState      — pydantic state model carried across nodes
    GraphEvent     — SSE event model emitted by nodes
    SENTINEL       — queue terminator pushed when graph completes
    push_sentinel  — helper to put SENTINEL onto a queue
"""

from graph.runtime import get_graph
from graph.sse import SENTINEL, GraphEvent, PersistSpec, push_sentinel
from graph.state import (
    AgentResult,
    ChatState,
    OrchestratorPlan,
    OrchestratorTask,
    RagChunk,
    RewriteOutput,
    ToolCallTrace,
)

__all__ = [
    "get_graph",
    "ChatState",
    "GraphEvent",
    "PersistSpec",
    "SENTINEL",
    "push_sentinel",
    "RewriteOutput",
    "OrchestratorPlan",
    "OrchestratorTask",
    "RagChunk",
    "AgentResult",
    "ToolCallTrace",
]
