"""
Pick a small, bucket-spread subset for manual UI testing.

Reads the layer testsets and selects ~N questions spread evenly across
all 9 buckets (A..I) so a short manual session still exercises every
graph path (definition / lookup / analyze / compare / sector / news /
chart / ambiguous / multi-turn).

Output: `<testset>/manual_picks.txt` — copy-paste ready, one line per
question:

    [B-001] ROE VNM 2024 là bao nhiêu?

Paste a line into the UI (with TEST_MODE=True) to run + log that case.
For multi-turn Bucket I, paste the token ONLY (e.g. `[I-001]`) so the
curated conversation history from the testset is replayed.

Usage:

    python -m evaluation.pick_manual_set \\
        --testset evaluation/testset --total 30
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

log = logging.getLogger("finhouse.eval.pick")

# Bucket letter → which layer file holds it. G only lives in agent.jsonl
# (it has no reference_answer); everything else is in e2e.jsonl.
_BUCKET_SOURCE = {
    "A": "e2e.jsonl", "B": "e2e.jsonl", "C": "e2e.jsonl",
    "D": "e2e.jsonl", "E": "e2e.jsonl", "F": "e2e.jsonl",
    "G": "agent.jsonl",
    "H": "e2e.jsonl", "I": "e2e.jsonl",
}
_BUCKETS = list(_BUCKET_SOURCE.keys())   # A..I, stable order


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _bucket_of(case: dict) -> str:
    """'B. Single-fact lookup' → 'B'."""
    cat = (case.get("category") or "").strip()
    return cat[0].upper() if cat else "?"


def _even_indices(n: int, k: int) -> list[int]:
    """k indices spread evenly across range(n)."""
    if k >= n:
        return list(range(n))
    if k <= 0:
        return []
    return [round(i * (n - 1) / (k - 1)) if k > 1 else 0 for i in range(k)]


def _quota(total: int) -> dict[str, int]:
    """Spread `total` across 9 buckets as evenly as possible."""
    base, extra = divmod(total, len(_BUCKETS))
    return {
        b: base + (1 if i < extra else 0)
        for i, b in enumerate(_BUCKETS)
    }


def pick(testset_dir: Path, total: int) -> list[tuple[str, str, str]]:
    """Return [(bucket, id, question), ...] spread across buckets."""
    # Group every case by bucket, from whichever file holds it.
    by_bucket: dict[str, list[dict]] = {b: [] for b in _BUCKETS}
    cache: dict[str, list[dict]] = {}
    for b in _BUCKETS:
        fname = _BUCKET_SOURCE[b]
        if fname not in cache:
            cache[fname] = _load_jsonl(testset_dir / fname)
        for case in cache[fname]:
            if _bucket_of(case) == b:
                by_bucket[b].append(case)

    quota = _quota(total)
    picks: list[tuple[str, str, str]] = []
    for b in _BUCKETS:
        cases = sorted(by_bucket[b], key=lambda c: c.get("id", ""))
        want = quota[b]
        for idx in _even_indices(len(cases), want):
            c = cases[idx]
            picks.append((b, c["id"], (c.get("question") or "").strip()))
        if not cases:
            log.warning("bucket %s: no cases found", b)
    return picks


def write_file(picks: list[tuple[str, str, str]], out_path: Path) -> None:
    lines = [
        "# FinHouse — manual benchmark picks",
        "# Set TEST_MODE=True, then paste ONE line at a time into the UI.",
        "# The leading [ID] token tags the turn for offline scoring;",
        "# the rest is sent to the bot as a normal question.",
        "# For multi-turn (I-xxx) paste just the token, e.g. [I-001],",
        "# so the testset's curated history is replayed.",
        "#",
        f"# {len(picks)} questions across {len(set(b for b,_,_ in picks))} buckets.",
        "",
    ]
    last_b = None
    for b, cid, q in picks:
        if b != last_b:
            lines.append(f"\n# ── Bucket {b} ───────────────────────────")
            last_b = b
        lines.append(f"[{cid}] {q}")
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--testset", type=Path, default=Path("evaluation/testset"))
    p.add_argument("--total", type=int, default=30,
                   help="Approx number of questions (spread across A..I)")
    p.add_argument("--out", type=Path, default=None,
                   help="Output txt (default <testset>/manual_picks.txt)")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    picks = pick(args.testset, args.total)
    out = args.out or (args.testset / "manual_picks.txt")
    write_file(picks, out)
    by_b: dict[str, int] = {}
    for b, _, _ in picks:
        by_b[b] = by_b.get(b, 0) + 1
    log.info("wrote %d picks → %s", len(picks), out)
    log.info("per bucket: %s", json.dumps(by_b, ensure_ascii=False))


if __name__ == "__main__":
    main()
