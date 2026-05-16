"""
Layer A — End-to-End metrics.

Each function returns a float ∈ [0, 1]. The top-level orchestrator
`score_e2e(case, actual)` runs them all and returns a flat dict.

Inputs:
    case   — dict from evaluation/testset/e2e.jsonl
    actual — dict from the runner, containing:
        {
          "final_answer":  str,        # collector output (state.final_answer)
          "rag_sources":   list[dict], # state.rag_sources (chunks)
          "agent_calls":   list[dict], # flattened tool calls from agent_results
        }
"""

from __future__ import annotations

import re

from evaluation.judges import binary_judge, split_into_claims, safe_mean


# ── 1. correctness ────────────────────────────────────────────


_CORRECTNESS_SYS = (
    "Bạn là grader benchmark RAG. So sánh CÂU TRẢ LỜI DỰ ĐOÁN với CÂU "
    "TRẢ LỜI THAM CHIẾU.\n\n"
    "Trả True (pass) nếu predicted khớp đủ các fact chính của reference "
    "(cho phép diễn đạt khác, paraphrasing, citation khác). Trả False "
    "nếu thiếu fact quan trọng, sai số liệu, hoặc lệch ý nghĩa.\n"
    "Khi reference_answer = null hoặc rỗng → trả True nếu predicted "
    "thừa nhận thiếu data / đề nghị clarification; trả False nếu "
    "predicted bịa câu trả lời."
)


async def correctness(predicted: str, reference: str | None, key_facts: list[str]) -> float:
    if not (predicted and predicted.strip()):
        return 0.0
    if reference is None:
        # Unanswerable case — pass if predicted admits ignorance
        if re.search(r"không tìm thấy|chưa thu thập|chưa có dữ|không có thông tin", predicted, re.IGNORECASE):
            return 1.0
        return 0.0
    facts_block = ""
    if key_facts:
        facts_block = "\nKEY FACTS BẮT BUỘC PHẢI CÓ (ít nhất 60%):\n- " + "\n- ".join(key_facts)
    payload = (
        f"DỰ ĐOÁN:\n{predicted}\n\n"
        f"THAM CHIẾU:\n{reference}"
        f"{facts_block}"
    )
    r = await binary_judge(_CORRECTNESS_SYS, payload)
    return r["score"]


# ── 2. faithfulness ───────────────────────────────────────────


_FAITHFULNESS_SYS = (
    "Bạn là grader. Đánh giá CLAIM sau có support trong CONTEXT không. "
    "Trả True nếu CONTEXT chứa thông tin để suy ra claim (không cần "
    "exact match, paraphrase OK). Trả False nếu CONTEXT KHÔNG cover "
    "claim — đây là dấu hiệu hallucination."
)


async def faithfulness(answer: str, rag_chunks: list[dict], agent_summaries: list[str]) -> float:
    """% claim in `answer` supported by retrieved context + agent results."""
    claims = await split_into_claims(answer)
    if not claims:
        return 1.0 if not answer.strip() else 0.0

    ctx_parts = []
    for c in rag_chunks:
        t = c.get("text") or ""
        if t:
            ctx_parts.append(f"[RAG] {t[:800]}")
    for s in agent_summaries:
        if s:
            ctx_parts.append(f"[AGENT] {s[:1500]}")
    if not ctx_parts:
        return 0.0
    context = "\n\n".join(ctx_parts)

    scores: list[float] = []
    for claim in claims:
        payload = f"CONTEXT:\n{context[:8000]}\n\nCLAIM:\n{claim}"
        r = await binary_judge(_FAITHFULNESS_SYS, payload)
        scores.append(r["score"])
    return safe_mean(scores)


# ── 3. answer_relevancy ───────────────────────────────────────


_RELEVANCY_SYS = (
    "Bạn là grader. Đánh giá CÂU TRẢ LỜI có thực sự trả lời CÂU HỎI "
    "không. True nếu answer tập trung vào câu hỏi (cho phép thêm "
    "context có ích). False nếu lạc đề, lan man, hoặc né tránh."
)


async def answer_relevancy(question: str, answer: str) -> float:
    if not answer.strip():
        return 0.0
    r = await binary_judge(
        _RELEVANCY_SYS,
        f"CÂU HỎI:\n{question}\n\nCÂU TRẢ LỜI:\n{answer}",
    )
    return r["score"]


# ── 4. no_hallucination ───────────────────────────────────────


_NEGATIVE_FACT_SYS = (
    "Bạn là grader phát hiện hallucination. Cho ANSWER và 1 FACT SAI. "
    "Trả True nếu ANSWER KHẲNG ĐỊNH fact sai đó (hoặc tương đương). "
    "Trả False nếu ANSWER KHÔNG nhắc tới hoặc phủ định fact sai. "
    "Lưu ý: True ở đây = phát hiện ra hallucination (xấu cho hệ thống)."
)


async def no_hallucination(answer: str, negative_facts: list[str]) -> float:
    """Returns 1.0 if NO negative fact is endorsed by the answer."""
    if not negative_facts:
        return 1.0
    if not answer.strip():
        return 1.0   # empty answer can't hallucinate
    for neg in negative_facts:
        r = await binary_judge(
            _NEGATIVE_FACT_SYS,
            f"ANSWER:\n{answer}\n\nFACT SAI CẦN PHÁT HIỆN:\n{neg}",
        )
        if r["score"] >= 0.5:
            return 0.0   # hallucination detected
    return 1.0


# ── 5. citation_validity ──────────────────────────────────────


def citation_validity(answer: str, rag_chunks: list[dict]) -> float:
    """Every [n] cited in answer must point to an existing chunk index.

    Returns:
        1.0 — all citations valid (or no citations at all)
        ratio — when some valid, some invalid
        0.0 — citations present but none valid (or chunks empty)
    """
    cites = re.findall(r"\[(\d+)\]", answer)
    if not cites:
        return 1.0
    valid_idx = {c.get("index") for c in rag_chunks}
    if not valid_idx:
        return 0.0
    hits = sum(1 for c in cites if int(c) in valid_idx)
    return hits / len(cites)


# ── 6. language_clean ─────────────────────────────────────────


def language_clean(answer: str) -> float:
    """Cheap heuristic: penalise CJK leakage and stray markdown headers.

    Score breakdown (max 1.0):
      - 0.6 — no CJK / Cyrillic characters
      - 0.4 — no leading '# Header' lines (collector prompt forbids these)
    """
    if not answer:
        return 0.0
    score = 0.0
    cjk_re = re.compile(r"[぀-ヿㇰ-ㇿ㐀-䶿一-鿿Ѐ-ӿ]")
    if not cjk_re.search(answer):
        score += 0.6
    head_re = re.compile(r"^\s*#{1,3}\s+\S", re.MULTILINE)
    if not head_re.search(answer):
        score += 0.4
    return round(score, 2)


# ── orchestrator ──────────────────────────────────────────────


async def score_e2e(case: dict, actual: dict) -> dict:
    """Run every Layer-A metric for one case. Returns flat dict."""
    answer = (actual.get("final_answer") or "").strip()
    chunks = actual.get("rag_sources") or []
    agent_summaries = [
        s.get("answer", "") for s in (actual.get("agent_summaries") or [])
    ]

    return {
        "correctness":       await correctness(
            answer, case.get("reference_answer"), case.get("key_facts", []),
        ),
        "faithfulness":      await faithfulness(answer, chunks, agent_summaries),
        "answer_relevancy":  await answer_relevancy(case["question"], answer),
        "no_hallucination":  await no_hallucination(answer, case.get("negative_facts", [])),
        "citation_validity": citation_validity(answer, chunks),
        "language_clean":    language_clean(answer),
    }


__all__ = ["score_e2e"]
