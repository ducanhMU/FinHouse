"""
Split the master `questions_enriched.json` (+ optional `bucket_g.json`)
into 3 layer-specific JSONL testset files.

Layer A (e2e):   ALL non-G cases — each tested through the full graph
Layer B (rag):   cases where RAG retrieval is expected to contribute
                 (categories A, B, C, E, and B/F items that ended up
                 grounded in private docs)
Layer C (agent): cases where specific tool agents are expected
                 (categories B, C, D, E, F, G)

Bucket G has a separate file `bucket_g.json` (or array inside main
file with `category` starting with "G."). It's written into `agent.jsonl`
only — the structural metrics defined in metrics/agent.py handle it.

Usage:

    python -m evaluation.split_testset \\
        --master  evaluation/testset_prompts/questions_enriched.json \\
        --bucket-g evaluation/testset_prompts/bucket_g.json \\
        --output  evaluation/testset/
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

log = logging.getLogger("finhouse.eval.split")


# Categories that need RAG retrieval to contribute meaningfully.
RAG_CATEGORIES = {"A.", "B.", "C.", "E."}

# Categories where specific tool agents should fire.
AGENT_CATEGORIES = {"B.", "C.", "D.", "E.", "F.", "G."}


def _cat_prefix(case: dict) -> str:
    """'B. Single-fact lookup' → 'B.'"""
    cat = (case.get("category") or "").strip()
    if not cat:
        return ""
    head = cat.split(" ", 1)[0]
    return head if head.endswith(".") else head + "."


def _load_master(path: Path) -> list[dict]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and "items" in raw:
        # Optional metadata wrapper from stage-3 output
        return raw["items"]
    if isinstance(raw, list):
        return raw
    raise ValueError(f"{path} must be a JSON array or {{items: [...]}}")


def _drop_for_e2e(case: dict) -> dict:
    """e2e layer needs question + reference_answer + tags + history."""
    return {
        "id":                case["id"],
        "question":          case["question"],
        "history":           case.get("history", []),
        "category":          case.get("category", ""),
        "persona":           case.get("persona", ""),
        "scope":             case.get("scope", ""),
        "style":             case.get("style", ""),
        "complexity":        case.get("complexity", ""),
        "expected_tools":    case.get("expected_tools", []),
        "expected_entities": case.get("expected_entities", []),
        "expected_timeframe": case.get("expected_timeframe", ""),
        "needs_clarification": case.get("needs_clarification", False),
        "reference_answer":  case.get("reference_answer"),
        "key_facts":         case.get("key_facts", []),
        "negative_facts":    case.get("negative_facts", []),
        "unanswerable":      case.get("unanswerable", False),
    }


def _drop_for_rag(case: dict) -> dict:
    """rag layer is the RAGAS-style 4-field schema + extras for our metrics."""
    return {
        "id":                case["id"],
        "question":          case["question"],
        "history":           case.get("history", []),
        "category":          case.get("category", ""),
        "reference_answer":  case.get("reference_answer"),
        "sources":           case.get("sources", []),
        "key_facts":         case.get("key_facts", []),
        "expected_entities": case.get("expected_entities", []),
        "expected_timeframe": case.get("expected_timeframe", ""),
        "unanswerable":      case.get("unanswerable", False),
    }


def _drop_for_agent(case: dict) -> dict:
    """agent layer needs expected_tools + (for G) expected_chart + facts."""
    out = {
        "id":                case["id"],
        "question":          case["question"],
        "history":           case.get("history", []),
        "category":          case.get("category", ""),
        "expected_tools":    case.get("expected_tools", []),
        "expected_entities": case.get("expected_entities", []),
        "expected_timeframe": case.get("expected_timeframe", ""),
        "key_facts":         case.get("key_facts", []),
        "reference_answer":  case.get("reference_answer"),
    }
    if _cat_prefix(case) == "G.":
        # Bucket G — visualize-specific structural fields
        if "expected_chart" in case:
            out["expected_chart"] = case["expected_chart"]
        if "expected_data_facts" in case:
            out["expected_data_facts"] = case["expected_data_facts"]
        if "expected_caption_facts" in case:
            out["expected_caption_facts"] = case["expected_caption_facts"]
    return out


def split(
    master_path: Path,
    bucket_g_path: Path | None,
    output_dir: Path,
) -> dict:
    cases = _load_master(master_path)
    if bucket_g_path and bucket_g_path.exists():
        g_cases = _load_master(bucket_g_path)
        # Bucket G items shouldn't already be in master; warn if so.
        seen = {c["id"] for c in cases}
        for g in g_cases:
            if g["id"] in seen:
                log.warning("bucket-g case %s already in master — keeping master version", g["id"])
                continue
            cases.append(g)

    output_dir.mkdir(parents=True, exist_ok=True)

    e2e: list[dict] = []
    rag: list[dict] = []
    agent: list[dict] = []

    for c in cases:
        cat = _cat_prefix(c)
        # e2e — everything except pure-G (G is best evaluated structurally,
        # not via end-to-end reference_answer comparison)
        if cat != "G.":
            e2e.append(_drop_for_e2e(c))
        # rag — only categories where retrieval contributes
        if cat in RAG_CATEGORIES and c.get("reference_answer") is not None:
            rag.append(_drop_for_rag(c))
        # agent — categories where tool agents should fire
        if cat in AGENT_CATEGORIES:
            agent.append(_drop_for_agent(c))

    def _dump_jsonl(path: Path, items: list[dict]):
        with path.open("w", encoding="utf-8") as f:
            for it in items:
                f.write(json.dumps(it, ensure_ascii=False) + "\n")

    _dump_jsonl(output_dir / "e2e.jsonl",   e2e)
    _dump_jsonl(output_dir / "rag.jsonl",   rag)
    _dump_jsonl(output_dir / "agent.jsonl", agent)

    counts = {"e2e": len(e2e), "rag": len(rag), "agent": len(agent)}
    log.info("split done: %s", counts)
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--master", type=Path, required=True,
                        help="questions_enriched.json from stage 3")
    parser.add_argument("--bucket-g", type=Path, default=None,
                        help="Optional bucket_g.json (visualize-specific cases)")
    parser.add_argument("--output", type=Path, default=Path("evaluation/testset"),
                        help="Output directory for {e2e,rag,agent}.jsonl")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    counts = split(args.master, args.bucket_g, args.output)
    print(json.dumps(counts, indent=2))


if __name__ == "__main__":
    main()
