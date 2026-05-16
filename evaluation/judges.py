"""
LLM-as-judge helpers — structured JSON output.

We reuse the same `get_llm` / `LLMHandle` plumbing the production graph
uses (DashScope qwen3-max by default, with fallback chain). Every judge
returns a normalised dict:

    {
        "score":  float in [0, 1],
        "reason": "<short explanation in Vietnamese>",
        "raw":    <full JSON the LLM emitted, for debugging>,
    }

Patterns:

    BinaryScore  — pass/fail, score ∈ {0.0, 1.0}
    LikertScore  — 0-5 integer, score = value / 5
    OverlapScore — float ∈ [0.0, 1.0]
    ClaimList    — split text into claim strings

`response_format={"type": "json_object"}` is enough for DashScope
qwen-series + Gemini + OpenAI — no need to go to `json_schema` strict
mode. Schema is described in the system prompt; we validate after.

Add `from evaluation.judges import *` and just call `binary_judge(...)`
in metric modules.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

# Path bootstrap so `python -m evaluation.<x>` from FinHouse repo root
# can import api/ modules.
import sys
import os
_API_DIR = os.path.join(os.path.dirname(__file__), "..", "api")
if _API_DIR not in sys.path:
    sys.path.insert(0, _API_DIR)

from graph.llm_router import get_llm   # noqa: E402

log = logging.getLogger("finhouse.eval.judges")


# Reuse the rag agent's LLM slot for judges — same DashScope model,
# already configured in env. Override with FINHOUSE_JUDGE_MODEL if needed.
JUDGE_AGENT_KEY = "rag"
FALLBACK_SESSION_MODEL = os.environ.get("FINHOUSE_JUDGE_MODEL", "qwen2.5:14b")


# ── core call ─────────────────────────────────────────────────


async def _call_judge_json(
    system_prompt: str,
    user_input: str,
    options: Optional[dict] = None,
) -> dict:
    """Single LLM call returning a JSON object (parsed). Best-effort
    repair on partial / fenced output; raises ValueError if unparseable."""
    llm = get_llm(JUDGE_AGENT_KEY, FALLBACK_SESSION_MODEL)
    opts = {"response_format": {"type": "json_object"}, "temperature": 0.0}
    if options:
        opts.update(options)
    resp = await llm.chat_sync(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_input},
        ],
        options=opts,
    )
    content = ((resp.get("message") or {}).get("content") or "").strip()
    return _parse_json_loose(content)


def _parse_json_loose(raw: str) -> dict:
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("empty judge response")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # strip ```json fences
    for fence in ("```json", "```JSON", "```"):
        if fence in raw:
            for part in raw.split(fence):
                p = part.strip().rstrip("`").strip()
                if p.startswith("{"):
                    try:
                        return json.loads(p)
                    except json.JSONDecodeError:
                        continue
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        return json.loads(m.group(0))
    raise ValueError(f"unparseable judge JSON: {raw[:200]}")


# ── binary judge ──────────────────────────────────────────────


_BINARY_OUTPUT_SPEC = (
    "\n\nOUTPUT — JSON DUY NHẤT, không markdown wrap, không text khác:\n"
    "{\n"
    '  "GiaiThich": "<1-2 câu giải thích bằng tiếng Việt>",\n'
    '  "DiemSo":    true | false\n'
    "}"
)


async def binary_judge(criteria: str, payload: str) -> dict:
    """`criteria` is the system message describing the pass condition.
    `payload` is the user message containing inputs to judge.

    Returns {"score": 0.0 | 1.0, "reason": str, "raw": dict}.
    On LLM/parse failure returns {"score": 0.0, "reason": "judge-error: ...", "raw": {}}.
    """
    try:
        data = await _call_judge_json(criteria + _BINARY_OUTPUT_SPEC, payload)
    except Exception as e:
        log.warning("binary judge failed: %s", e)
        return {"score": 0.0, "reason": f"judge-error: {e}", "raw": {}}
    val = data.get("DiemSo")
    if isinstance(val, str):
        val = val.strip().lower() in {"true", "1", "yes", "có"}
    return {
        "score":  1.0 if bool(val) else 0.0,
        "reason": str(data.get("GiaiThich", ""))[:300],
        "raw":    data,
    }


# ── likert (0-5) judge ────────────────────────────────────────


_LIKERT_OUTPUT_SPEC = (
    "\n\nOUTPUT — JSON DUY NHẤT:\n"
    "{\n"
    '  "GiaiThich": "<giải thích ngắn>",\n'
    '  "DiemSo":    <integer 0-5>\n'
    "}"
)


async def likert_judge(criteria: str, payload: str) -> dict:
    """Returns score normalised to [0, 1] by dividing the 0-5 value by 5."""
    try:
        data = await _call_judge_json(criteria + _LIKERT_OUTPUT_SPEC, payload)
    except Exception as e:
        return {"score": 0.0, "reason": f"judge-error: {e}", "raw": {}}
    raw = data.get("DiemSo")
    try:
        v = max(0, min(5, int(raw)))
    except (TypeError, ValueError):
        v = 0
    return {
        "score":  v / 5.0,
        "reason": str(data.get("GiaiThich", ""))[:300],
        "raw":    data,
    }


# ── claim splitter ────────────────────────────────────────────


_CLAIM_SPLIT_PROMPT = (
    "Bạn là grader RAG. Tách CÂU TRẢ LỜI thành danh sách các CLAIM "
    "(mỗi claim = 1 mệnh đề độc lập có thể verify đúng/sai).\n\n"
    "Quy tắc:\n"
    "- Mỗi claim 1 câu ngắn, tự chứa thông tin.\n"
    "- Bỏ câu nối/giới thiệu/tâm tình.\n"
    "- Mỗi số liệu cụ thể (ROE = 22%) là 1 claim.\n"
    "- Tối đa 10 claim. Nếu answer rất dài → chọn 10 claim quan trọng nhất.\n\n"
    "OUTPUT — JSON: {\"claims\": [\"claim1\", \"claim2\", ...]}"
)


async def split_into_claims(text: str, cap: int = 10) -> list[str]:
    """Split a natural-language answer into atomic claims.
    Returns at most `cap` claims; empty list when LLM fails or text is empty."""
    text = (text or "").strip()
    if not text:
        return []
    try:
        data = await _call_judge_json(_CLAIM_SPLIT_PROMPT, f"CÂU TRẢ LỜI:\n{text}")
    except Exception as e:
        log.warning("claim-split failed: %s", e)
        return []
    raw = data.get("claims") or []
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for c in raw:
        if isinstance(c, str) and c.strip():
            out.append(c.strip()[:500])
            if len(out) >= cap:
                break
    return out


# ── helpers shared across metric modules ─────────────────────


def jaccard(a: list[str] | set[str], b: list[str] | set[str]) -> float:
    sa = {x.strip().lower() for x in a if isinstance(x, str) and x.strip()}
    sb = {x.strip().lower() for x in b if isinstance(x, str) and x.strip()}
    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def safe_mean(values: list[float]) -> float:
    values = [v for v in values if v is not None]
    if not values:
        return 0.0
    return sum(values) / len(values)


__all__ = [
    "binary_judge",
    "likert_judge",
    "split_into_claims",
    "jaccard",
    "safe_mean",
]
