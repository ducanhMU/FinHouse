"""
FinHouse — LangGraph topology.

Flow:

    START
      │
      ▼
    rewriter ────────► (clarification?) ──yes──► collector ──► END
      │                  no
      ├──────────► rag ─────────────┐
      │                             ▼
      └──► orchestrator ──► dispatcher ──► collector ──► END

Branches `rag` and `orchestrator → dispatcher` execute in parallel from
`rewriter`; LangGraph waits for both to converge before running
`collector`.

Convergence works because:
  • RAG writes only to `rag_sources` / `rag_messages`.
  • Dispatcher writes only to `agent_results`.
  • These are Annotated[..., operator.add] in `state.py` so even if
    LangGraph schedules updates concurrently, the merge is well-defined.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from langgraph.graph import END, START, StateGraph

from graph.nodes.collector import collector_runnable
from graph.nodes.orchestrator import orchestrator_runnable
from graph.nodes.rag import rag_runnable
from graph.nodes.rewriter import rewriter_runnable
from graph.nodes.tool_agents import dispatcher_runnable
from graph.state import ChatState

log = logging.getLogger("finhouse.graph.runtime")


def _route_after_rewriter(state: ChatState) -> list[str]:
    """Conditional fan-out from the rewriter.

    Returns a LIST so LangGraph schedules every branch in parallel.
    """
    if state.rewrite and state.rewrite.needs_clarification:
        return ["collector"]
    # Fan-out: RAG and orchestrator run concurrently
    return ["rag", "orchestrator"]


def _build_graph():
    g = StateGraph(ChatState)

    g.add_node("rewriter",     rewriter_runnable)
    g.add_node("rag",          rag_runnable)
    g.add_node("orchestrator", orchestrator_runnable)
    g.add_node("dispatcher",   dispatcher_runnable)
    g.add_node("collector",    collector_runnable)

    g.add_edge(START, "rewriter")

    # rewriter → {collector | (rag + orchestrator)}
    g.add_conditional_edges(
        "rewriter",
        _route_after_rewriter,
        {
            "collector": "collector",
            "rag": "rag",
            "orchestrator": "orchestrator",
        },
    )

    # orchestrator → dispatcher → collector
    g.add_edge("orchestrator", "dispatcher")
    g.add_edge("dispatcher",   "collector")

    # rag → collector  (parallel branch)
    g.add_edge("rag",          "collector")

    g.add_edge("collector",    END)

    return g.compile()


@lru_cache(maxsize=1)
def get_graph():
    """Compile the graph once per process."""
    log.info("Compiling FinHouse chat graph (multi-ReAct topology)")
    return _build_graph()


__all__ = ["get_graph"]
