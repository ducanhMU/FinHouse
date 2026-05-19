"""
Manual / interactive benchmark bridge.

Lets you exercise the *real* UI → API → graph path one question at a
time, while still capturing everything the automated `runner.py` would,
so the SAME metric code can score it afterwards.

How it plugs in
----------------
1. `.env` has `TEST_MODE=True`.
2. In the UI you type a message whose text starts with a test-id token:

       [B-001] ROE VNM 2024 là bao nhiêu?
       [B-001]                              ← token only → canonical Q

3. `routers/chat.py` (when TEST_MODE) calls `prepare_test_turn()`:
     - strips the token,
     - if the remaining text is empty, substitutes the canonical
       question from the testset,
     - seeds `state.bench` so every graph node emits structured logs
       (exactly like the automated runner),
     - seeds the curated multi-turn `history` from the testset so
       Bucket-I cases reproduce faithfully (same as runner.py).
   The turn is then processed *completely normally* — the user sees the
   real streamed answer.

4. After the graph finishes, the router calls `persist_turn()` which
   flushes the per-component logs and appends the final state to
   `<run_dir>/actuals.jsonl`.

5. Offline, `python -m evaluation.score_manual` reads `actuals.jsonl`,
   matches each id back to the layer testsets and runs the unchanged
   `score_e2e / score_rag / score_agent` metrics.

This module is intentionally dependency-light (stdlib only) because it
is imported inside the live chat request path. The heavy metric/judge
code is only touched by `score_manual.py`, which runs offline.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# A test-id token: "[B-001]", "[G-020]", "[I-010]" — case-insensitive,
# optional surrounding whitespace. Everything after the closing bracket
# is the (optional) free-text question.
_TOKEN_RE = re.compile(r"^\s*\[\s*([A-Za-z]+-\d+)\s*\]\s*(.*)$", re.DOTALL)

# Layer files searched (in order) to resolve a test-id → canonical case.
# e2e.jsonl covers A,B,C,D,E,F,H,I; agent.jsonl additionally covers G.
_LAYER_FILES = ("e2e.jsonl", "agent.jsonl", "rag.jsonl")


# ── IO helpers (mirrors runner.py, kept local to stay stdlib-only) ──


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
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


# ── token parsing ─────────────────────────────────────────────


def parse_test_token(text: str) -> tuple[Optional[str], str]:
    """Split a raw message into (test_id, remaining_text).

    No token → (None, original_text). The test_id is upper-cased so
    "[b-001]" and "[B-001]" both resolve.
    """
    m = _TOKEN_RE.match(text or "")
    if not m:
        return None, text or ""
    return m.group(1).upper(), m.group(2).strip()


# ── testset lookup ────────────────────────────────────────────


def load_case(test_id: str, testset_dir: str | Path) -> Optional[dict]:
    """Return the canonical case dict for `test_id` (question, history,
    category), searching the layer files in `_LAYER_FILES` order.

    Returns None when the id is unknown."""
    testset_dir = Path(testset_dir)
    for fname in _LAYER_FILES:
        for case in _load_jsonl(testset_dir / fname):
            if case.get("id") == test_id:
                return case
    return None


# ── turn preparation (called by the chat router) ──────────────


def prepare_test_turn(
    raw_text: str,
    testset_dir: str | Path,
    run_dir: str | Path,
) -> Optional[dict]:
    """Decide whether this message is a benchmarked turn.

    Returns None when there is no test-id token (→ caller proceeds with
    the normal flow, untouched).

    When a token is present returns a plan dict:

        {
          "test_id":  "B-001",
          "known":    True/False,      # id found in testset?
          "question": "<text to feed the graph>",
          "history":  [...],           # curated history (Bucket I) or []
          "bench":    {run_id, test_id, category, log_dir},
        }

    `question` = the user's own text if they typed any after the token,
    otherwise the canonical testset question. `bench` is what gets
    assigned to `ChatState.bench`.
    """
    test_id, remaining = parse_test_token(raw_text)
    if test_id is None:
        return None

    run_dir = Path(run_dir)
    case = load_case(test_id, testset_dir)
    known = case is not None
    category = (case or {}).get("category", "")

    # Free-text wins; fall back to canonical question when token-only.
    if remaining:
        question = remaining
    elif known:
        question = case.get("question", "")
    else:
        # Unknown id AND no text — nothing to ask. Treat as normal msg.
        return None

    return {
        "test_id":  test_id,
        "known":    known,
        "question": question,
        # Curated multi-turn history mirrors runner.py semantics so
        # Bucket-I cases reproduce identically. Empty for single-turn.
        "history":  (case or {}).get("history", []) if known else [],
        "bench": {
            "run_id":   "manual",
            "test_id":  test_id,
            "category": category,
            "log_dir":  str(run_dir),
        },
    }


# ── persistence after the graph finishes ──────────────────────


def _as_dict(final_state: Any) -> dict:
    """Normalise a LangGraph result (ChatState | dict) to a plain dict."""
    if hasattr(final_state, "model_dump"):
        return final_state.model_dump()
    if isinstance(final_state, dict):
        return dict(final_state)
    return {}


def _build_actual(final_state: dict) -> dict:
    """Identical shape to runner._build_actual_for_metrics so the
    unchanged metric modules consume it as-is."""
    rag_sources = final_state.get("rag_sources") or []
    if rag_sources and hasattr(rag_sources[0], "model_dump"):
        rag_sources = [s.model_dump() for s in rag_sources]
    elif rag_sources and isinstance(rag_sources[0], dict):
        pass
    else:
        rag_sources = []

    norm_agents: list[dict] = []
    for r in final_state.get("agent_results") or []:
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


def _flush_component_logs(log_dir: Path, records: list[dict]) -> None:
    """Group by `component` → append to <log_dir>/<component>.jsonl.
    Same on-disk layout the automated runner produces, so token_stats
    and any per-component inspection work unchanged."""
    by_comp: dict[str, list[dict]] = {}
    for r in records:
        by_comp.setdefault(r.get("component") or "unknown", []).append(r)
    for comp, recs in by_comp.items():
        _append_jsonl(log_dir / f"{comp}.jsonl", recs)


def persist_turn(
    run_dir: str | Path,
    final_state: Any,
    *,
    latency_ms: Optional[int] = None,
) -> Optional[str]:
    """Persist one finished benchmarked turn under `run_dir`.

    Writes:
      <run_dir>/<component>.jsonl   — appended per-component logs
      <run_dir>/actuals.jsonl      — one {id, category, actual} record

    Returns the test_id on success, None if the state carried no bench
    marker (defensive — should not happen when called by the router).
    Never raises on disk issues the caller can't fix; it logs upstream.
    """
    state = _as_dict(final_state)
    bench = state.get("bench") or {}
    test_id = bench.get("test_id")
    if not test_id:
        return None

    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    _flush_component_logs(run_dir, state.get("component_logs") or [])

    _append_jsonl(run_dir / "actuals.jsonl", [{
        "id":         test_id,
        "category":   bench.get("category", ""),
        "ts":         _now_iso(),
        "latency_ms": latency_ms,
        "actual":     _build_actual(state),
    }])
    return test_id


__all__ = [
    "parse_test_token",
    "load_case",
    "prepare_test_turn",
    "persist_turn",
]
