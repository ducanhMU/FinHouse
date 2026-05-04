"""
FinHouse — Query Rewriter

Takes the latest user message + recent chat history, asks an LLM to
produce a self-contained rewritten query that can be embedded for RAG
retrieval. The rewriter extracts the three pillars of any finance Q&A:

    • SCOPE   — company / sector / macro / general
    • TIME    — point (Q1/2026, năm 2025) or range (2023–2025, Q1/2024–Q3/2025)
    • METRICS — doanh thu, ROE, GDP, …

Decision policy (mirrors the prompt):
    • If SCOPE cannot be resolved → set needs_clarification=true and
      ask the user a short, specific question.
    • Otherwise → rewrite the query as self-contained, applying
      defaults for missing pieces (especially TIME → "năm 2025") and
      reporting them in `applied_defaults`.

The rewriter uses the Ollama model the user picked for the session
(see `model` argument). Settings.REWRITER_MODEL is only an explicit
override; if blank, the caller should pass session.model_used.

Result:

    RewriteResult(
        rewritten="<self-contained question>",
        needs_clarification=False,
        clarification="",
        scope_type="company",
        preserved_entities=["VNM", "Vinamilk"],
        preserved_timeframe="Q2/2024",
        preserved_metrics=["biên lợi nhuận gộp"],
        applied_defaults=[],
        original="<original user text>",
    )

If the LLM call fails or returns malformed JSON, we fall back to
returning the original message unchanged — chat still works, just
without rewrite benefit.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from config import get_settings
from prompts import get_query_rewriter_prompt
from services.ollama import chat_sync

settings = get_settings()
log = logging.getLogger("finhouse.rewriter")

# How many previous turns to include as context (user+assistant pairs).
# Keep small to control rewriter latency + tokens.
REWRITE_CONTEXT_TURNS = 4

# Max chars per message when building history context
REWRITE_HISTORY_MSG_CAP = 1500

# Timeout for the rewriter LLM call (seconds).
# Kept tight: rewriter runs synchronously before RAG, so any wait here is
# pure latency in front of the user. If the local model can't return in
# this window, fall back to passthrough — RAG with the original message
# is far better than blocking the whole turn.
REWRITE_TIMEOUT_SEC = 12

_VALID_SCOPE_TYPES = {"company", "sector", "macro", "general", ""}


@dataclass
class RewriteResult:
    rewritten: str
    needs_clarification: bool = False
    clarification: str = ""
    scope_type: str = ""
    preserved_entities: list[str] = field(default_factory=list)
    preserved_timeframe: str = ""
    preserved_metrics: list[str] = field(default_factory=list)
    applied_defaults: list[str] = field(default_factory=list)
    original: str = ""

    @property
    def embed_query(self) -> str:
        """The text we actually feed to the embedder for RAG search."""
        if self.rewritten and not self.needs_clarification:
            return self.rewritten
        return self.original


def _passthrough(original: str) -> RewriteResult:
    """Fallback result — use original message as-is on any error."""
    return RewriteResult(rewritten=original, original=original)


def _now_context_block() -> str:
    """
    Build a fresh date/time anchor for the rewriter user message.

    The prompt body itself is static (loaded from disk + cached), so we
    inject the moving "today" reference here at call time. This keeps
    "năm hiện tại / năm tài chính gần nhất hoàn chỉnh" accurate without
    requiring the prompt file to be edited every year.

    "Năm tài chính gần nhất hoàn chỉnh" = current_year - 1, on the
    assumption that annual filings for year N-1 are available by mid-N.
    Most VN-listed companies file Q4/full-year reports by end of Q1.
    """
    today = date.today()
    year = today.year
    month = today.month
    quarter = (month - 1) // 3 + 1

    if quarter == 1:
        last_full_quarter = 4
        last_full_quarter_year = year - 1
    else:
        last_full_quarter = quarter - 1
        last_full_quarter_year = year

    # In Q1 of a calendar year, year-2 reports may still be the "latest
    # full year" for some filers. Past Q1 we trust year-1 is complete.
    last_full_year = year - 1 if month >= 4 else year - 2

    return (
        "── BỐI CẢNH THỜI GIAN HIỆN TẠI ──\n"
        f"NGÀY HIỆN TẠI: {today.isoformat()}\n"
        f"NĂM HIỆN TẠI: {year}\n"
        f"QUÝ HIỆN TẠI: Q{quarter}/{year}\n"
        f"QUÝ GẦN NHẤT HOÀN CHỈNH: Q{last_full_quarter}/{last_full_quarter_year}\n"
        f"NĂM TÀI CHÍNH GẦN NHẤT HOÀN CHỈNH: {last_full_year}\n"
        "── HẾT BỐI CẢNH THỜI GIAN ──\n"
    )


def _build_history_block(history: list[dict]) -> str:
    """Format recent turns as a plain-text transcript for the rewriter."""
    msgs = history[-(REWRITE_CONTEXT_TURNS * 2):]
    lines = []
    for m in msgs:
        role = m.get("role", "user")
        content = (m.get("content") or "")[:REWRITE_HISTORY_MSG_CAP]
        if not content:
            continue
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


def _extract_json(raw: str) -> Optional[dict]:
    """
    Extract first valid JSON object from LLM output. Handles:
      • Pure JSON
      • JSON wrapped in ```json ... ``` fences
      • JSON with preamble/postamble text
    """
    if not raw:
        return None

    stripped = raw.strip()
    try:
        return json.loads(stripped)
    except Exception:
        pass

    for fence in ("```json", "```JSON", "```"):
        if fence in stripped:
            parts = stripped.split(fence)
            for part in parts:
                part = part.strip().rstrip("`").strip()
                if part.startswith("{"):
                    try:
                        return json.loads(part)
                    except Exception:
                        continue

    m = re.search(r"\{.*\}", stripped, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None

    return None


def _coerce_str_list(raw, cap: int = 10) -> list[str]:
    """Best-effort convert LLM output to a list[str], dropping empties."""
    if not raw:
        return []
    if isinstance(raw, str):
        # Some models emit a comma-separated string instead of a list
        items = [p.strip() for p in raw.split(",")]
    elif isinstance(raw, list):
        items = [str(x).strip() for x in raw]
    else:
        return []
    return [x for x in items if x][:cap]


async def rewrite_query(
    user_message: str,
    history: list[dict],
    model: str,
) -> RewriteResult:
    """
    Rewrite a user query using conversation history. Never throws —
    on any error or malformed output, returns passthrough.

    Per design decision: we rewrite EVERY user message including the
    first one. The rewriter is the place where we decide whether the
    main agent should answer immediately or ask the user a clarifying
    question first. This avoids the previous behaviour of jumping into
    an answer with under-specified context.

    Args:
        user_message: the raw latest user message
        history: list of prior messages in the conversation
                 (each dict has {"role": "user"|"assistant", "content": str})
        model: Ollama model name to use for rewriting. Pass the session
               model so the rewriter speaks the same dialect / tokenizer
               family as the main answer agent.

    Returns:
        RewriteResult with resolved query or clarification request.
    """
    system_prompt = get_query_rewriter_prompt()
    history_block = _build_history_block(history) if history else "(chưa có)"

    user_content = (
        f"{_now_context_block()}\n"
        f"LỊCH SỬ HỘI THOẠI:\n{history_block}\n\n"
        f"CÂU HỎI MỚI NHẤT CẦN PHÂN TÍCH & REWRITE:\n{user_message}\n\n"
        "Output DUY NHẤT một JSON object đúng schema (không markdown fence, "
        "không giải thích). Nhớ: chỉ set needs_clarification=true khi "
        "không xác định được scope; nếu chỉ thiếu thời gian → áp default "
        "= NĂM TÀI CHÍNH GẦN NHẤT HOÀN CHỈNH ở khối bối cảnh phía trên."
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    try:
        response = await chat_sync(
            model=model,
            messages=messages,
            tools=None,
            timeout=REWRITE_TIMEOUT_SEC,
            # Lower temperature for more deterministic rewriting.
            # num_predict bumped to fit the larger JSON schema (scope,
            # metrics, applied_defaults).
            options={"temperature": 0.1, "num_predict": 800},
        )
    except Exception as e:
        log.warning(f"rewriter LLM call failed: {e}; using passthrough")
        return _passthrough(user_message)

    raw = response.get("message", {}).get("content", "")
    log.debug(f"rewriter raw output: {raw[:500]}")

    parsed = _extract_json(raw)
    if not parsed:
        log.warning(f"rewriter output not parseable as JSON: {raw[:200]!r}; passthrough")
        return _passthrough(user_message)

    try:
        scope_type = str(parsed.get("scope_type", "") or "").strip().lower()
        if scope_type not in _VALID_SCOPE_TYPES:
            scope_type = ""

        result = RewriteResult(
            rewritten=str(parsed.get("rewritten", "") or "").strip(),
            needs_clarification=bool(parsed.get("needs_clarification", False)),
            clarification=str(parsed.get("clarification", "") or "").strip(),
            scope_type=scope_type,
            preserved_entities=_coerce_str_list(parsed.get("preserved_entities")),
            preserved_timeframe=str(parsed.get("preserved_timeframe", "") or "").strip(),
            preserved_metrics=_coerce_str_list(parsed.get("preserved_metrics")),
            applied_defaults=_coerce_str_list(parsed.get("applied_defaults")),
            original=user_message,
        )
    except Exception as e:
        log.warning(f"rewriter result construction failed: {e}; passthrough")
        return _passthrough(user_message)

    # ── Sanity / consistency repairs ─────────────────────────
    if result.needs_clarification and not result.clarification:
        # Rewriter flagged ambiguous but didn't provide a clarification.
        # Use a generic but still actionable fallback.
        result.clarification = (
            "Bạn có thể nói rõ hơn về đối tượng (công ty, ngành hay vĩ mô) "
            "mà bạn đang muốn hỏi không ạ?"
        )

    if not result.needs_clarification and not result.rewritten:
        # Rewriter returned empty without flagging clarification — use original
        log.info("rewriter returned empty rewritten text, using original")
        result.rewritten = user_message

    # If rewriter forgot scope_type but produced a rewrite, infer a
    # conservative fallback so downstream code can branch on it.
    if not result.needs_clarification and result.rewritten and not result.scope_type:
        if result.preserved_entities:
            result.scope_type = "company"
        else:
            result.scope_type = "general"

    # Truncate overly long rewritten queries (defense)
    if len(result.rewritten) > 2000:
        result.rewritten = result.rewritten[:2000]
    if len(result.clarification) > 600:
        result.clarification = result.clarification[:600]

    log.info(
        f"rewrite: orig={user_message[:80]!r} → "
        f"rewritten={result.rewritten[:80]!r} "
        f"clarify={result.needs_clarification} "
        f"scope={result.scope_type} "
        f"entities={result.preserved_entities} "
        f"timeframe={result.preserved_timeframe!r} "
        f"metrics={result.preserved_metrics} "
        f"defaults={result.applied_defaults}"
    )
    return result


# ── Company scope verification ──────────────────────────────
#
# The implementation moved to tools.database_query so it can be exposed
# as a ReAct tool (lookup_company). Re-export here for back-compat with
# any caller that still imports from services.rewriter.
from tools.database_query import (   # noqa: E402,F401
    verify_company_entities,
)
