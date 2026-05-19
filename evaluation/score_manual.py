"""
Score a manual / interactive benchmark run.

Counterpart to `runner.py`: the runner *drives* the graph itself, this
command does NOT — the graph already ran live through the UI (see
`evaluation/manual.py`) and dropped `<run_dir>/actuals.jsonl` plus the
per-component `*.jsonl` logs. Here we just replay those captured final
states through the **unchanged** metric modules.

Everything below the graph invocation is reused verbatim from the
automated pipeline:

    • metrics.{e2e,rag,agent}.score_*   — identical scoring
    • runner._aggregate                 — identical summary roll-up
    • token_stats.aggregate_tokens      — identical token report

so a manual run and an automated run produce the same `summary.json`
shape and are directly comparable.

Usage (same env as runner.py — inside the api Docker container):

    python -m evaluation.score_manual \\
        --run-dir evaluation/results/manual \\
        --testset evaluation/testset \\
        --layers  e2e,rag,agent
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

# Same api/ path bootstrap as runner.py / judges.py.
_API_DIR = os.path.join(os.path.dirname(__file__), "..", "api")
if _API_DIR not in sys.path:
    sys.path.insert(0, _API_DIR)

from evaluation.metrics.e2e   import score_e2e    # noqa: E402
from evaluation.metrics.rag   import score_rag    # noqa: E402
from evaluation.metrics.agent import score_agent  # noqa: E402

# Reuse the runner's IO + aggregation helpers — no reimplementation.
from evaluation.runner import (                    # noqa: E402
    _aggregate,
    _append_jsonl,
    _load_jsonl,
    _now_iso,
)

log = logging.getLogger("finhouse.eval.score_manual")

_SCORERS = {"e2e": score_e2e, "rag": score_rag, "agent": score_agent}


def _load_actuals(run_dir: Path) -> dict[str, dict]:
    """Read actuals.jsonl. A test-id typed several times keeps the LAST
    attempt (newest answer wins)."""
    path = run_dir / "actuals.jsonl"
    if not path.exists():
        raise SystemExit(
            f"no actuals.jsonl at {path} — type some "
            f"'[ID] question' messages in the UI first (TEST_MODE=True)."
        )
    by_id: dict[str, dict] = {}
    for rec in _load_jsonl(path):
        rid = rec.get("id")
        if rid:
            by_id[rid] = rec   # last occurrence wins
    return by_id


async def _score_layer(
    layer: str,
    actuals: dict[str, dict],
    testset_dir: Path,
) -> list[dict]:
    """Score every captured actual whose id exists in this layer's
    testset file. Ids not in the layer file are simply skipped (e.g. a
    Bucket-G id won't be in e2e.jsonl)."""
    cases = {c["id"]: c for c in _load_jsonl(testset_dir / f"{layer}.jsonl")}
    scorer = _SCORERS[layer]
    scored: list[dict] = []
    for rid, rec in actuals.items():
        case = cases.get(rid)
        if case is None:
            continue
        try:
            metrics = await scorer(case, rec.get("actual") or {})
        except Exception as e:                       # pragma: no cover
            log.warning("[%s] %s scoring crashed: %s", layer, rid, e)
            scored.append({"id": rid, "metrics": {}, "error": str(e)})
            continue
        scored.append({
            "id":         rid,
            "category":   rec.get("category", ""),
            "latency_ms": rec.get("latency_ms"),
            "metrics":    metrics,
        })
    log.info("[%s] scored %d / %d captured turns",
             layer, len(scored), len(actuals))
    return scored


async def run(run_dir: Path, testset_dir: Path, layers: list[str]) -> dict:
    run_dir = Path(run_dir)
    actuals = _load_actuals(run_dir)
    log.info("loaded %d captured turn(s) from %s",
             len(actuals), run_dir / "actuals.jsonl")

    scored_by_layer: dict[str, list[dict]] = {}
    for layer in layers:
        scored = await _score_layer(layer, actuals, testset_dir)
        if not scored:
            log.warning("[%s] no captured turns match this layer's "
                        "testset — skipping", layer)
            continue
        # Overwrite (this is a fresh recompute over all captured turns).
        out = run_dir / f"scores_{layer}.jsonl"
        out.unlink(missing_ok=True)
        _append_jsonl(out, scored)
        scored_by_layer[layer] = scored

    summary = _aggregate(scored_by_layer)

    (run_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    (run_dir / "meta.json").write_text(
        json.dumps({
            "run_id":       run_dir.name,
            "mode":         "manual",
            "scored_at":    _now_iso(),
            "layers":       list(scored_by_layer.keys()),
            "case_counts":  {k: len(v) for k, v in scored_by_layer.items()},
            "captured_turns": len(actuals),
            "testset_dir":  str(testset_dir),
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    try:
        from evaluation.token_stats import aggregate_tokens
        aggregate_tokens(run_dir)
    except Exception as e:                            # pragma: no cover
        log.warning("token aggregation failed: %s", e)

    log.info("DONE — summary at %s", run_dir / "summary.json")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-dir", type=Path,
                   default=Path("evaluation/results/manual"),
                   help="Manual run dir (contains actuals.jsonl)")
    p.add_argument("--testset", type=Path,
                   default=Path("evaluation/testset"),
                   help="Dir with {e2e,rag,agent}.jsonl")
    p.add_argument("--layers", default="e2e,rag,agent",
                   help="Comma-separated subset of layers to score")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    layers = [s.strip() for s in args.layers.split(",") if s.strip()]
    asyncio.run(run(args.run_dir, args.testset, layers))


if __name__ == "__main__":
    main()
