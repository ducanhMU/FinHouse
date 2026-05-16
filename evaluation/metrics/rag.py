"""
Layer B — RAG (RAGAS) metrics.

Scores the RAG agent's behaviour in isolation, using:
    case   — dict from evaluation/testset/rag.jsonl
    actual — dict from runner with RAG-specific fields:
        {
          "rag_answer":     str,        # state.rag_answer (generator output)
          "rag_sources":    list[dict], # state.rag_sources
          "rag_structured": dict,       # evaluator_decision, useful_idx, ...
          "rewrite":        dict,       # state.rewrite (for HyDE quality)
        }
"""

from __future__ import annotations

from evaluation.judges import binary_judge, split_into_claims, safe_mean


# ── 1. context_precision @ k ──────────────────────────────────


_PRECISION_SYS = (
    "Bạn là grader RAG. Đánh giá ĐOẠN TRÍCH có chứa thông tin liên "
    "quan để trả lời CÂU HỎI không. True nếu có info trực tiếp/gián "
    "tiếp giúp trả lời (paraphrase / suy ra OK). False nếu trùng từ "
    "khoá nhưng nội dung không trả lời được."
)


async def context_precision_at_k(question: str, chunks: list[dict], k: int = 5) -> float:
    chunks = chunks[:k]
    if not chunks:
        return 0.0
    scores: list[float] = []
    for ch in chunks:
        text = (ch.get("text") or "")[:1500]
        r = await binary_judge(
            _PRECISION_SYS,
            f"CÂU HỎI: {question}\n\nĐOẠN TRÍCH:\n{text}",
        )
        scores.append(r["score"])
    return safe_mean(scores)


# ── 2. context_recall ─────────────────────────────────────────


_RECALL_CLAIM_SUPPORTED_SYS = (
    "Bạn là grader RAG. CONTEXT có support cho CLAIM không? "
    "True nếu CLAIM có thể suy ra từ CONTEXT (paraphrase OK). "
    "False nếu CONTEXT không cover claim."
)


async def context_recall(reference_answer: str | None, chunks: list[dict]) -> float:
    """% claim trong reference_answer được cover bởi retrieved chunks."""
    if not reference_answer:
        return 1.0   # unanswerable case — vacuously full recall
    claims = await split_into_claims(reference_answer)
    if not claims:
        return 1.0
    if not chunks:
        return 0.0
    context = "\n\n".join((c.get("text") or "")[:1000] for c in chunks)
    scores: list[float] = []
    for claim in claims:
        r = await binary_judge(
            _RECALL_CLAIM_SUPPORTED_SYS,
            f"CONTEXT:\n{context[:8000]}\n\nCLAIM:\n{claim}",
        )
        scores.append(r["score"])
    return safe_mean(scores)


# ── 3. answer_faithfulness ────────────────────────────────────


_FAITHFUL_CLAIM_SYS = (
    "Bạn là grader. Đánh giá CLAIM trong câu trả lời có thể suy ra "
    "từ CONTEXT không. True nếu có. False nếu CLAIM là số liệu / fact "
    "không xuất hiện trong CONTEXT (= hallucination)."
)


async def answer_faithfulness(rag_answer: str, chunks: list[dict]) -> float:
    if not rag_answer.strip():
        return 1.0 if not chunks else 0.0
    claims = await split_into_claims(rag_answer)
    if not claims:
        return 1.0
    context = "\n\n".join((c.get("text") or "")[:1000] for c in chunks)
    if not context:
        return 0.0
    scores: list[float] = []
    for claim in claims:
        r = await binary_judge(
            _FAITHFUL_CLAIM_SYS,
            f"CONTEXT:\n{context[:8000]}\n\nCLAIM:\n{claim}",
        )
        scores.append(r["score"])
    return safe_mean(scores)


# ── 4. answer_relevancy (RAG-scoped) ──────────────────────────


_RELEVANCY_SYS = (
    "Bạn là grader. CÂU TRẢ LỜI có thực sự trả lời CÂU HỎI không? "
    "True nếu tập trung, có info đúng chủ đề. False nếu lạc đề, mơ hồ, "
    "hoặc không trả lời."
)


async def answer_relevancy(question: str, rag_answer: str) -> float:
    if not rag_answer.strip():
        return 0.0
    r = await binary_judge(
        _RELEVANCY_SYS,
        f"CÂU HỎI:\n{question}\n\nCÂU TRẢ LỜI:\n{rag_answer}",
    )
    return r["score"]


# ── 5. context_diversity (bonus) ──────────────────────────────


def context_diversity(chunks: list[dict]) -> float:
    """Ratio of unique source files in retrieved chunks.

    1.0 — every chunk from a different file (max diversity)
    0.x — duplicates from same file
    0.0 — no chunks
    """
    if not chunks:
        return 0.0
    files = [(c.get("file_name") or "").strip() for c in chunks]
    files = [f for f in files if f]
    if not files:
        return 0.0
    return len(set(files)) / len(files)


# ── 6. hyde_quality (bonus, only when HyDE enabled) ───────────


_HYDE_SYS = (
    "Bạn là grader chấm chất lượng HyDE passage cho RAG. Đánh giá "
    "PASSAGE có đạt 3 tiêu chí không:\n"
    "  (a) Văn phong khẳng định, kiểu trích từ báo cáo tài chính "
    "(KHÔNG phải câu hỏi, không phải mô tả meta).\n"
    "  (b) Có nhắc đúng entity/ticker + timeframe của câu rewritten.\n"
    "  (c) Cung cấp thông tin/góc nhìn cụ thể (số liệu, sự kiện, lý do).\n"
    "Trả True nếu đạt cả 3, False nếu thiếu 1 trong 3."
)


async def hyde_quality(rewrite: dict | None) -> float:
    if not rewrite:
        return 1.0  # nothing to judge — neutral
    passages = rewrite.get("hypothetical_passages") or []
    if not passages:
        return 1.0
    rewritten = rewrite.get("rewritten") or rewrite.get("original") or ""
    scores: list[float] = []
    for p in passages:
        if not (isinstance(p, str) and p.strip()):
            continue
        r = await binary_judge(
            _HYDE_SYS,
            f"REWRITTEN: {rewritten}\n\nPASSAGE:\n{p}",
        )
        scores.append(r["score"])
    return safe_mean(scores) if scores else 1.0


# ── orchestrator ──────────────────────────────────────────────


async def score_rag(case: dict, actual: dict) -> dict:
    chunks = actual.get("rag_sources") or []
    rag_answer = actual.get("rag_answer") or ""
    rewrite = actual.get("rewrite") or {}

    return {
        "context_precision":   await context_precision_at_k(case["question"], chunks, k=5),
        "context_recall":      await context_recall(case.get("reference_answer"), chunks),
        "answer_faithfulness": await answer_faithfulness(rag_answer, chunks),
        "answer_relevancy":    await answer_relevancy(case["question"], rag_answer),
        "context_diversity":   context_diversity(chunks),
        "hyde_quality":        await hyde_quality(rewrite),
    }


__all__ = ["score_rag"]
