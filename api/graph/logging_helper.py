"""
FinHouse — Structured per-component logging for benchmarks.

Each pipeline node (rewriter, rag, db, web, visualize, collector) emits
one record per turn via `make_log_record()`. The records are accumulated
on `ChatState.component_logs` and flushed to disk by the benchmark
runner — production traffic leaves `state.bench = None` so the helper
becomes a no-op.

Schema (all components share this envelope):

    {
        "run_id":   "2026-05-14_10-30_default" | null,
        "test_id":  "rag-001"                  | null,
        "session_id": "uuid",
        "component": "rag" | "rewriter" | "collector" | "db" | "web" | "visualize",
        "ts_end":   "2026-05-14T10:30:03.456Z",
        "latency_ms": 1234,
        "input":    { ... },        # what the node received
        "output":   {
            "answer": "...",        # natural-language synthesis — benchmarked
            "structured": { ... },  # machine-readable result
        },
        "traces":   [ ... ],        # optional intermediate steps
        "usage":    { ... } | None,
        "error":    null | "...",
    }

`ChatState.component_logs` uses `operator.add` so parallel branches
(RAG ↔ dispatcher) can each emit without overwriting; LangGraph merges
the lists when branches converge at the collector.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional


def now_iso() -> str:
    """UTC timestamp with explicit 'Z' suffix (matches log spec)."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z",
    )


def make_log_record(
    state,
    component: str,
    *,
    input: dict[str, Any],
    output: dict[str, Any],
    traces: Optional[list[dict[str, Any]]] = None,
    usage: Optional[dict[str, Any]] = None,
    latency_ms: Optional[int] = None,
    error: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Build one log record. Returns a 1-element list ready to be merged
    into `ChatState.component_logs` via the reducer.

    Returns `[]` when `state.bench` is None — i.e. production / non-bench
    invocations — so the helper is free at runtime.

    Returning a list (rather than mutating state) keeps node functions
    side-effect free; nodes assemble their return dict with
    `"component_logs": make_log_record(...)` and the LangGraph reducer
    handles the merge.
    """
    if getattr(state, "bench", None) is None:
        return []

    bench = state.bench or {}
    record: dict[str, Any] = {
        "run_id":     bench.get("run_id"),
        "test_id":    bench.get("test_id"),
        "session_id": str(getattr(state, "session_id", "")) or None,
        "component":  component,
        "ts_end":     now_iso(),
        "input":      input,
        "output":     output,
    }
    if traces:
        record["traces"] = traces
    if usage is not None:
        record["usage"] = usage
    if latency_ms is not None:
        record["latency_ms"] = int(latency_ms)
    if error:
        record["error"] = error
    return [record]


__all__ = ["make_log_record", "now_iso"]
