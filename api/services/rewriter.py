"""
FinHouse — Query Rewriter

Takes the latest user message + recent chat history, asks an LLM to
produce a self-contained rewritten query that can be embedded for RAG
retrieval. Handles:

  • Pronoun/reference resolution ("nó", "công ty đó" → specific entity)
  • Topic inheritance ("Còn lợi nhuận?" → "Lợi nhuận của <prev_entity> thì sao?")
  • Topic switch ("Còn FPT thì sao?" → keep metric, swap entity)
  • Preserving critical details (timeframes, tickers, numbers)
  • Flagging ambiguous queries for clarification

The rewriter returns structured output:

    RewriteResult(
        rewritten="<self-contained question>",
        needs_clarification=False,
        clarification="",
        preserved_entities=["VNM", "Vinamilk"],
        preserved_timeframe="Q2 2024",
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
REWRITE_TIMEOUT_SEC = 8


@dataclass
class RewriteResult:
    rewritten: str
    needs_clarification: bool = False
    clarification: str = ""
    preserved_entities: list[str] = field(default_factory=list)
    preserved_timeframe: str = ""
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


def _build_history_block(history: list[dict]) -> str:
    """Format recent turns as a plain-text transcript for the rewriter."""
    # Take the last N turns (N user + N assistant pairs = 2N messages)
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

    # Try raw parse first (if LLM obeyed instruction perfectly)
    stripped = raw.strip()
    try:
        return json.loads(stripped)
    except Exception:
        pass

    # Strip common code fences
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

    # Regex for first {...} block (greedy to match nested)
    m = re.search(r"\{.*\}", stripped, re.DOTALL)
    if m:
        candidate = m.group(0)
        try:
            return json.loads(candidate)
        except Exception:
            return None

    return None


async def rewrite_query(
    user_message: str,
    history: list[dict],
    model: str,
) -> RewriteResult:
    """
    Rewrite a user query using conversation history. Never throws —
    on any error or malformed output, returns passthrough.

    Per design decision: we rewrite EVERY user message, including the
    first one in a session. This catches ambiguous first messages like
    "Nó lãi bao nhiêu?" (no prior entity → clarification) and also
    normalizes self-contained messages like "ROE của HPG 2024?" by
    expanding to canonical form ("ROE của Hoà Phát (HPG) năm 2024").

    The rewriter prompt is explicit about handling empty history via
    the `needs_clarification` output field.

    Args:
        user_message: the raw latest user message
        history: list of prior messages in the conversation
                 (each dict has {"role": "user"|"assistant", "content": str})
        model: Ollama model name to use for rewriting

    Returns:
        RewriteResult with resolved query or clarification request.
    """
    system_prompt = get_query_rewriter_prompt()
    history_block = _build_history_block(history) if history else "(chưa có)"

    user_content = (
        f"LỊCH SỬ HỘI THOẠI:\n{history_block}\n\n"
        f"CÂU HỎI MỚI NHẤT CẦN REWRITE:\n{user_message}\n\n"
        "Output JSON theo format đã quy định."
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
            # Lower temperature for more deterministic rewriting
            options={"temperature": 0.1, "num_predict": 500},
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
        result = RewriteResult(
            rewritten=str(parsed.get("rewritten", "") or "").strip(),
            needs_clarification=bool(parsed.get("needs_clarification", False)),
            clarification=str(parsed.get("clarification", "") or "").strip(),
            preserved_entities=[
                str(x) for x in (parsed.get("preserved_entities") or [])
                if x
            ][:10],
            preserved_timeframe=str(parsed.get("preserved_timeframe", "") or "").strip(),
            original=user_message,
        )
    except Exception as e:
        log.warning(f"rewriter result construction failed: {e}; passthrough")
        return _passthrough(user_message)

    # Sanity checks
    if result.needs_clarification and not result.clarification:
        # Rewriter flagged ambiguous but didn't provide a clarification.
        # Fallback to a generic question.
        result.clarification = (
            "Bạn có thể nói rõ hơn bạn đang hỏi về đối tượng/công ty/chỉ số nào không?"
        )

    if not result.needs_clarification and not result.rewritten:
        # Rewriter returned empty without flagging clarification — use original
        log.info("rewriter returned empty rewritten text, using original")
        result.rewritten = user_message

    # Truncate overly long rewritten queries (defense)
    if len(result.rewritten) > 2000:
        result.rewritten = result.rewritten[:2000]

    log.info(
        f"rewrite: orig={user_message[:80]!r} → "
        f"rewritten={result.rewritten[:80]!r} "
        f"clarify={result.needs_clarification} "
        f"entities={result.preserved_entities} "
        f"timeframe={result.preserved_timeframe!r}"
    )
    return result
