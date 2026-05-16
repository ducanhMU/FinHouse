"""
FinHouse benchmark runner.

Invokes the production graph for each test case with `state.bench` set,
captures the per-component structured logs, computes metrics, and
writes everything to `<output>/<run_id>/`.

Output structure:

    <output>/<run_id>/
        meta.json              — run metadata (timestamps, config, counts)
        rewriter.jsonl         — per-case rewriter records
        rag.jsonl              — per-case rag agent records
        db.jsonl / web.jsonl / visualize.jsonl
        collector.jsonl        — per-case end-to-end records
        scores_e2e.jsonl       — per-case metric dicts (Layer A)
        scores_rag.jsonl       — per-case metric dicts (Layer B)
        scores_agent.jsonl     — per-case metric dicts (Layer C)
        summary.json           — flat aggregated metrics for visualisation

Run from FinHouse repo root (inside the api Docker env, or with all
api/ deps installed locally):

    python -m evaluation.runner \\
        --testset evaluation/testset/ \\
        --output  evaluation/results \\
        --layers  e2e,rag,agent
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

# Bootstrap api/ on path
_API_DIR = os.path.join(os.path.dirname(__file__), "..", "api")
if _API_DIR not in sys.path:
    sys.path.insert(0, _API_DIR)

from graph import ChatState, get_graph  # noqa: E402

from evaluation.metrics.e2e   import score_e2e    # noqa: E402
from evaluation.metrics.rag   import score_rag    # noqa: E402
from evaluation.metrics.agent import score_agent  # noqa: E402

log = logging.getLogger("finhouse.eval.runner")


# ── IO helpers ────────────────────────────────────────────────


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _append_jsonl(path: Path, records: list[dict]) -> None:
    if not records:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _safe_mean(values: list[float]) -> float:
    values = [v for v in values if v is not None]
    if not values:
        return 0.0
    return sum(values) / len(values)


# ── Graph invocation ──────────────────────────────────────────


async def _invoke_graph_for_case(
    case: dict,
    *,
    run_id: str,
    log_dir: Path,
    project_id: int,
    user_id: int,
    session_model: str,
    enabled_tools: list[str],
) -> dict:
    """Build a ChatState, invoke the compiled graph, return the merged
    final state as a dict.

    `state.bench` is set so every node emits structured logs to
    `state.component_logs`.
    """
    graph = get_graph()
    state = ChatState(
        session_id=uuid4(),
        user_id=user_id,
        project_id=project_id,
        user_text=case["question"],
        history=case.get("history", []),
        enabled_tools=enabled_tools,
        session_model=session_model,
        bench={
            "run_id":   run_id,
            "test_id":  case["id"],
            "category": case.get("category", ""),
            "log_dir":  str(log_dir),
        },
    )
    t0 = time.perf_counter()
    final = await graph.ainvoke(state)
    latency = int((time.perf_counter() - t0) * 1000)
    # LangGraph returns either a ChatState or a dict — normalise.
    if isinstance(final, ChatState):
        out = final.model_dump()
    else:
        out = dict(final)
    out["_latency_ms"] = latency
    return out


# ── Per-case workflow ─────────────────────────────────────────


def _flush_component_logs(log_dir: Path, records: list[dict]) -> None:
    """Group records by `component` and append to <log_dir>/<component>.jsonl."""
    by_comp: dict[str, list[dict]] = {}
    for r in records:
        comp = r.get("component") or "unknown"
        by_comp.setdefault(comp, []).append(r)
    for comp, recs in by_comp.items():
        _append_jsonl(log_dir / f"{comp}.jsonl", recs)


def _build_actual_for_metrics(final_state: dict) -> dict:
    """Translate graph output into the shape the metric modules expect."""
    rag_sources = final_state.get("rag_sources") or []
    if rag_sources and hasattr(rag_sources[0], "model_dump"):
        rag_sources = [s.model_dump() for s in rag_sources]
    elif rag_sources and isinstance(rag_sources[0], dict):
        pass
    else:
        rag_sources = []

    agent_results = final_state.get("agent_results") or []
    norm_agents: list[dict] = []
    for r in agent_results:
        if hasattr(r, "model_dump"):
            r = r.model_dump()
        norm_agents.append(r)

    rewrite_obj = final_state.get("rewrite")
    if hasattr(rewrite_obj, "model_dump"):
        rewrite_obj = rewrite_obj.model_dump()
    elif rewrite_obj is None:
        rewrite_obj = {}

    return {
        "final_answer":   final_state.get("final_answer") or "",
        "rag_answer":     final_state.get("rag_answer") or "",
        "rag_sources":    rag_sources,
        "rag_structured": final_state.get("rag_structured") or {},
        "agent_results":  norm_agents,
        "agent_summaries": norm_agents,
        "rewrite":        rewrite_obj,
    }


async def _run_one_case(
    case: dict,
    layer: str,
    *,
    run_id: str,
    log_dir: Path,
    project_id: int,
    user_id: int,
    session_model: str,
    enabled_tools: list[str],
) -> dict:
    """Invoke the graph + score one case for a single layer.
    Returns {"id": ..., "metrics": {...}} ready for scores_<layer>.jsonl."""
    try:
        final = await _invoke_graph_for_case(
            case,
            run_id=run_id, log_dir=log_dir,
            project_id=project_id, user_id=user_id,
            session_model=session_model, enabled_tools=enabled_tools,
        )
    except Exception as e:
        log.warning("[%s] case %s graph crashed: %s", layer, case["id"], e)
        return {"id": case["id"], "metrics": {}, "error": str(e)}

    # Flush per-component logs the graph nodes emitted onto this state.
    comp_logs = final.get("component_logs") or []
    _flush_component_logs(log_dir, comp_logs)

    actual = _build_actual_for_metrics(final)

    try:
        if layer == "e2e":
            metrics = await score_e2e(case, actual)
        elif layer == "rag":
            metrics = await score_rag(case, actual)
        elif layer == "agent":
            metrics = await score_agent(case, actual)
        else:
            raise ValueError(f"unknown layer {layer!r}")
    except Exception as e:
        log.warning("[%s] case %s scoring crashed: %s", layer, case["id"], e)
        return {"id": case["id"], "metrics": {}, "error": f"scoring: {e}"}

    return {
        "id": case["id"],
        "category": case.get("category", ""),
        "latency_ms": final.get("_latency_ms"),
        "metrics": metrics,
    }


async def _run_layer(
    layer: str,
    cases: list[dict],
    *,
    run_id: str,
    log_dir: Path,
    project_id: int,
    user_id: int,
    session_model: str,
    enabled_tools: list[str],
    concurrency: int,
) -> list[dict]:
    """Run all cases for a layer with bounded concurrency.

    NOTE: graph nodes hit the same external APIs (DashScope, Milvus,
    SearXNG) so high concurrency triggers rate-limits. Default 4 is
    safe; tune via --concurrency."""
    sem = asyncio.Semaphore(concurrency)
    results: list[dict] = []

    async def _gated(case):
        async with sem:
            return await _run_one_case(
                case, layer,
                run_id=run_id, log_dir=log_dir,
                project_id=project_id, user_id=user_id,
                session_model=session_model, enabled_tools=enabled_tools,
            )

    coros = [_gated(c) for c in cases]
    for i, fut in enumerate(asyncio.as_completed(coros), 1):
        r = await fut
        results.append(r)
        if i % 5 == 0 or i == len(cases):
            log.info("[%s] %d/%d done", layer, i, len(cases))
    # Preserve case-list order for stable output.
    by_id = {r["id"]: r for r in results}
    return [by_id[c["id"]] for c in cases if c["id"] in by_id]


# ── Aggregation ───────────────────────────────────────────────


def _aggregate(scored_by_layer: dict[str, list[dict]]) -> dict[str, float]:
    summary: dict[str, float] = {}
    for layer, scored in scored_by_layer.items():
        metric_names: set[str] = set()
        for r in scored:
            metric_names.update((r.get("metrics") or {}).keys())
        for m in sorted(metric_names):
            vals = [
                r["metrics"][m] for r in scored
                if isinstance(r.get("metrics", {}).get(m), (int, float))
            ]
            summary[f"{layer}.{m}"] = round(_safe_mean(vals), 4)
    return summary


# ── Main ──────────────────────────────────────────────────────


async def run(
    testset_dir: Path,
    output_root: Path,
    layers: list[str],
    config_name: str,
    project_id: int,
    user_id: int,
    session_model: str,
    enabled_tools: list[str],
    concurrency: int,
    limit: int | None,
):
    run_id = datetime.now().strftime("%Y-%m-%d_%H-%M") + "_" + config_name
    log_dir = output_root / run_id
    log_dir.mkdir(parents=True, exist_ok=True)
    log.info("run_id=%s output=%s", run_id, log_dir)

    meta = {
        "run_id":        run_id,
        "config_name":   config_name,
        "started_at":    _now_iso(),
        "layers":        layers,
        "project_id":    project_id,
        "user_id":       user_id,
        "session_model": session_model,
        "enabled_tools": enabled_tools,
        "concurrency":   concurrency,
        "testset_dir":   str(testset_dir),
        "limit":         limit,
    }

    scored_by_layer: dict[str, list[dict]] = {}
    for layer in layers:
        path = testset_dir / f"{layer}.jsonl"
        cases = _load_jsonl(path)
        if limit:
            cases = cases[:limit]
        if not cases:
            log.warning("[%s] no cases at %s — skipping", layer, path)
            continue
        log.info("[%s] running %d cases", layer, len(cases))
        scored = await _run_layer(
            layer, cases,
            run_id=run_id, log_dir=log_dir,
            project_id=project_id, user_id=user_id,
            session_model=session_model, enabled_tools=enabled_tools,
            concurrency=concurrency,
        )
        _append_jsonl(log_dir / f"scores_{layer}.jsonl", scored)
        scored_by_layer[layer] = scored

    summary = _aggregate(scored_by_layer)
    meta["finished_at"] = _now_iso()
    meta["case_counts"] = {k: len(v) for k, v in scored_by_layer.items()}

    (log_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (log_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Token roll-up — never let an aggregation hiccup fail the whole run.
    try:
        from evaluation.token_stats import aggregate_tokens
        aggregate_tokens(log_dir)
    except Exception as e:  # pragma: no cover - defensive
        log.warning("token aggregation failed: %s", e)

    log.info("DONE — summary saved at %s", log_dir / "summary.json")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--testset", type=Path, default=Path("evaluation/testset"),
                   help="Dir containing {e2e,rag,agent}.jsonl")
    p.add_argument("--output",  type=Path, default=Path("evaluation/results"),
                   help="Parent directory for run folders")
    p.add_argument("--layers",  default="e2e,rag,agent",
                   help="Comma-separated subset of layers to run")
    p.add_argument("--config-name", default="default",
                   help="Suffix appended to run_id (e.g. 'v2_hybrid' for A/B)")
    p.add_argument("--project-id", type=int, default=int(os.environ.get("FINHOUSE_BENCH_PROJECT", "0")))
    p.add_argument("--user-id",    type=int, default=int(os.environ.get("FINHOUSE_BENCH_USER", "0")))
    p.add_argument("--session-model", default=os.environ.get("FINHOUSE_BENCH_MODEL", "qwen2.5:14b"))
    p.add_argument("--enabled-tools", default="web_search,database,visualize")
    p.add_argument("--concurrency", type=int, default=4,
                   help="Bounded parallel cases per layer")
    p.add_argument("--limit", type=int, default=None,
                   help="Smoke-test cap on N cases per layer")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    layers = [s.strip() for s in args.layers.split(",") if s.strip()]
    enabled_tools = [s.strip() for s in args.enabled_tools.split(",") if s.strip()]

    asyncio.run(run(
        testset_dir=args.testset,
        output_root=args.output,
        layers=layers,
        config_name=args.config_name,
        project_id=args.project_id,
        user_id=args.user_id,
        session_model=args.session_model,
        enabled_tools=enabled_tools,
        concurrency=args.concurrency,
        limit=args.limit,
    ))


if __name__ == "__main__":
    main()
