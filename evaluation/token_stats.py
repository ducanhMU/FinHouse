"""
FinHouse — token-consumption aggregator.

Reads the per-component JSONL files a benchmark run dropped in
`<run_dir>/` ({rewriter,orchestrator,rag,db,web,visualize,collector}.jsonl)
and rolls their `usage` blocks up into `<run_dir>/tokens.json`:

    • by_component      — min/max/avg/sum per component (over the records
                          that actually issued an LLM call)
    • overall_per_turn  — whole-turn cost: sum every component for a
                          `test_id`, then min/max/avg/sum over turns
    • by_category       — the same per-turn roll-up, grouped by the
                          `category` stamped on each record

Double-count safety
-------------------
Each component record's envelope `usage` is one non-overlapping LLM
call: rewriter / orchestrator / rag / db / web / visualize / collector
(the collector logs its OWN synthesis tokens; the tool-agent roll-up
lives in `output.structured.agent_usage`, which we deliberately ignore).
So summing every component for a turn is exact, no subtraction needed.

Missing usage (item D)
----------------------
A record with no `usage` means that component made no LLM call that
turn (e.g. RAG retrieval-only, orchestrator skipped). Such records are
EXCLUDED from per-component min/avg (they'd pin min to 0) but still
counted in `count`; for the per-turn roll-up they contribute 0.

Usage:

    # standalone, re-aggregate any existing run
    python -m evaluation.token_stats evaluation/results/2026-05-14_10-30_default

The benchmark runner also calls `aggregate_tokens()` automatically at
the end of every run, so `tokens.json` is always fresh.
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("finhouse.eval.tokens")

# Components whose envelope `usage` is a distinct, non-overlapping LLM
# call. Order is the rough pipeline order (used for stable chart axes).
COMPONENTS = [
    "rewriter",
    "orchestrator",
    "rag",
    "db",
    "web",
    "visualize",
    "collector",
]


# ── stats helpers ─────────────────────────────────────────────


def _stats(values: list[float]) -> dict:
    """min/max/avg/sum/n over a list. Empty → all zeros."""
    if not values:
        return {"min": 0.0, "max": 0.0, "avg": 0.0, "sum": 0.0, "n": 0}
    return {
        "min": round(float(min(values)), 2),
        "max": round(float(max(values)), 2),
        "avg": round(sum(values) / len(values), 2),
        "sum": round(float(sum(values)), 2),
        "n":   len(values),
    }


def _usage_triple(rec: dict) -> tuple[int, int, int] | None:
    """Return (input, output, total) if the record carries a real usage
    block, else None (no LLM call that turn)."""
    u = rec.get("usage")
    if not isinstance(u, dict):
        return None
    tot = int(u.get("total_tokens") or 0)
    if tot <= 0:
        return None
    return (
        int(u.get("input_tokens") or 0),
        int(u.get("output_tokens") or 0),
        tot,
    )


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            log.warning("skipping malformed line in %s", path.name)
    return out


# ── core aggregation ──────────────────────────────────────────


def aggregate_tokens(run_dir: Path) -> dict:
    """Build the token-stats dict for one run directory and write
    `<run_dir>/tokens.json`. Returns the dict."""
    run_dir = Path(run_dir)

    # component -> list[(test_id, category, input, output, total)]
    rows: list[tuple[str, str, str, int, int, int]] = []
    for comp in COMPONENTS:
        for rec in _load_jsonl(run_dir / f"{comp}.jsonl"):
            trip = _usage_triple(rec)
            if trip is None:
                # still record the turn so `count` is honest
                rows.append((
                    comp,
                    rec.get("test_id") or "?",
                    rec.get("category") or "(uncategorised)",
                    0, 0, -1,            # total = -1 marks "no LLM call"
                ))
                continue
            rows.append((
                comp,
                rec.get("test_id") or "?",
                rec.get("category") or "(uncategorised)",
                *trip,
            ))

    # ── by_component ──────────────────────────────────────────
    by_component: dict[str, dict] = {}
    for comp in COMPONENTS:
        crows = [r for r in rows if r[0] == comp]
        if not crows:
            continue
        used = [r for r in crows if r[5] >= 0]
        by_component[comp] = {
            "count":      len(crows),
            "with_usage": len(used),
            "input":  _stats([r[3] for r in used]),
            "output": _stats([r[4] for r in used]),
            "total":  _stats([r[5] for r in used]),
        }

    # ── per-turn roll-up (whole) ──────────────────────────────
    # turn key = test_id; sum every component's total (no-call → 0).
    turn_total: dict[str, int] = {}
    turn_input: dict[str, int] = {}
    turn_output: dict[str, int] = {}
    turn_cat: dict[str, str] = {}
    turn_comp: dict[str, dict[str, int]] = {}
    for comp, tid, cat, i, o, t in rows:
        turn_cat.setdefault(tid, cat)
        if t < 0:
            continue
        turn_total[tid] = turn_total.get(tid, 0) + t
        turn_input[tid] = turn_input.get(tid, 0) + i
        turn_output[tid] = turn_output.get(tid, 0) + o
        turn_comp.setdefault(tid, {})
        turn_comp[tid][comp] = turn_comp[tid].get(comp, 0) + t

    turn_ids = sorted(turn_cat.keys())

    def _comp_avg(ids: list[str]) -> dict[str, float]:
        """Average per-turn contribution of each component over `ids`."""
        out: dict[str, float] = {}
        if not ids:
            return out
        for comp in COMPONENTS:
            vals = [turn_comp.get(tid, {}).get(comp, 0) for tid in ids]
            out[comp] = round(sum(vals) / len(vals), 2)
        return out

    overall = {
        "turns":  len(turn_ids),
        "input":  _stats([turn_input.get(t, 0) for t in turn_ids]),
        "output": _stats([turn_output.get(t, 0) for t in turn_ids]),
        "total":  _stats([turn_total.get(t, 0) for t in turn_ids]),
        "by_component_avg": _comp_avg(turn_ids),
    }

    # ── by_category ───────────────────────────────────────────
    cats: dict[str, list[str]] = {}
    for tid in turn_ids:
        cats.setdefault(turn_cat[tid], []).append(tid)
    by_category = {
        cat: {
            "turns":  len(ids),
            "input":  _stats([turn_input.get(t, 0) for t in ids]),
            "output": _stats([turn_output.get(t, 0) for t in ids]),
            "total":  _stats([turn_total.get(t, 0) for t in ids]),
            "by_component_avg": _comp_avg(ids),
        }
        for cat, ids in sorted(cats.items())
    }

    result = {
        "run_id":       run_dir.name,
        "generated_at": datetime.now(timezone.utc)
        .isoformat(timespec="seconds").replace("+00:00", "Z"),
        "by_component":     by_component,
        "overall_per_turn": overall,
        "by_category":      by_category,
    }

    out_path = run_dir / "tokens.json"
    out_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    log.info("saved %s (%d turns)", out_path, overall["turns"])
    return result


# ── CLI ───────────────────────────────────────────────────────


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("run_dir", type=Path,
                   help="A benchmark run directory (contains *.jsonl)")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    res = aggregate_tokens(args.run_dir)
    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
