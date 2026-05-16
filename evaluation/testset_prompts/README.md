# Test Set Generation — 4-stage pipeline

Sinh bộ test 270 case cho benchmark FinHouse. Mỗi stage làm 1 việc rõ
ràng; output của stage trước là input của stage sau (trừ Bucket G đi
đường riêng vì visualize không có "reference text").

```
┌──────────────────────────────────────────────────────────────────┐
│ STAGE 1 — Generate questions  (ChatGPT / Claude / Gemini)        │
│   Input:  criteria của 9 bucket A..I                             │
│   Output: questions_raw.json (270 item, question + tags)         │
│   File:   01_question_generation.md                              │
└──────────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────────┐
│ STAGE 2 — RAG-grounded answer  (NotebookLM với data/ uploaded)   │
│   Input:  questions_raw.json                                     │
│   Output: questions_with_refs.json                               │
│   ✓ Cover Bucket A (def), C (analyze), một phần B/D/E            │
│   ✗ Bucket F (news), nhiều case B/D/E unanswerable               │
│   File:   02_reference_answer.md                                 │
└──────────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────────┐
│ STAGE 3 — Enrich với web + DB ground truth                       │
│   (ChatGPT browsing / Gemini / Claude with web tool)             │
│   Input:  questions_with_refs.json (filter case cần enrich)      │
│   Output: questions_enriched.json (FINAL test set)               │
│   ✓ Fill unanswerable cho F (news)                               │
│   ✓ Bổ sung số liệu verified cho B/C/D                           │
│   ✓ Cover E (sector + macro)                                     │
│   File:   03_enrich_answer.md                                    │
└──────────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────────┐
│ STAGE 4 (next phase) — Split thành 3 testset JSONL theo layer    │
│   Script: evaluation/split_testset.py (sẽ viết phase code)       │
│   Output: evaluation/testset/{e2e,rag,agent}.jsonl               │
└──────────────────────────────────────────────────────────────────┘

══════════════════════════════════════════════════════════════════════
BUCKET G (Visualize, 20 case) — ĐƯỜNG RIÊNG (không qua stage 2+3):
══════════════════════════════════════════════════════════════════════

┌──────────────────────────────────────────────────────────────────┐
│ Visualize handling — schema cấu trúc, không có reference text    │
│   - Viết tay 20 case theo matrix (chart_type × entity × metric)  │
│   - Schema có `expected_chart` thay vì `reference_answer`        │
│   - Eval bằng metric structural + data_correctness (SQL grounded)│
│   File:  04_visualize_handling.md                                │
└──────────────────────────────────────────────────────────────────┘
```

## Yếu tố thiết kế bộ test

| Yếu tố | Giá trị | Ghi chú |
|---|---|---|
| **Scope** | company / sector / macro / multi_company | Quyết định route trong graph |
| **Style** | lookup / analyze / compare / synthesis / definition | Quyết định độ sâu answer |
| **Expected tool** | rag / database / web_search / visualize / mixed | Eval Layer C |
| **Persona** | retail / analyst / student / journalist | Quyết định văn phong + độ chi tiết |
| **Timeframe** | historical / current_year / recent_news | Quyết định tool selection (recent_news → web) |
| **Complexity** | factual / multi_fact / reasoning | Phân tầng khó |

## 9 Buckets

| Bucket | N | Tool | Persona |
|---|---|---|---|
| A. Definition | 30 | rag | student |
| B. Single-fact lookup | 60 | db+rag | retail |
| C. Multi-fact analyze | 40 | db+rag | analyst |
| D. Compare | 40 | db | analyst |
| E. Sector/macro | 30 | rag+web | analyst |
| F. Recent news | 30 | web | journalist |
| G. Chart request | 20 | visualize | analyst |
| H. Ambiguous | 10 | (clarify) | mixed |
| I. Multi-turn | 10 | mixed | mixed |
| **Total** | **270** | | |

## Schema cuối — `questions_enriched.json` (sau stage 3)

```jsonc
{
  "id": "B-001",
  "question": "ROE VNM 2024 là bao nhiêu?",
  "history": [],                                  // [I] only

  // === tagging (từ stage 1) ===
  "category": "B. Single-fact lookup",
  "expected_tools": ["database", "rag"],
  "expected_entities": ["VNM"],
  "expected_timeframe": "2024",
  "scope": "company",
  "style": "lookup",
  "persona": "retail",
  "complexity": "factual",
  "needs_clarification": false,

  // === reference từ stage 2 (NotebookLM, grounded ở private docs) ===
  "reference_answer": "ROE năm 2024 của Vinamilk đạt 22.5% [1][web:cafef], tăng so với 20.1% năm 2023 [1]. Theo BCTC kiểm toán...",
  "sources": ["VNM_BCTC_2024.pdf — Báo cáo HĐKD trang 4"],
  "key_facts": ["ROE 2024 = 22.5%", "ROE 2023 = 20.1%", "LNST 2024 = 9.453 tỷ"],
  "negative_facts": ["KHÔNG được nói ROE > 30%"],
  "unanswerable": false,

  // === enrichment từ stage 3 (web + DB ground truth) ===
  "web_sources": ["cafef.vn/vnm-bao-cao-2024", "vietstock.vn/finance/VNM"],
  "web_enriched": true,
  "web_verified": true,
  "corrected": false
}
```

## Schema riêng cho Bucket G

```jsonc
{
  "id": "G-001",
  "question": "Vẽ biểu đồ doanh thu VNM 5 năm gần nhất",
  "category": "G. Chart request",
  "expected_tools": ["visualize"],
  // ... các tag chung ...

  // Không có reference_answer / sources / key_facts.
  // Thay vào đó:
  "expected_chart": {
    "chart_type": ["line", "bar"],
    "table": "income_statement",
    "x_column": "year",
    "y_columns": ["revenue"],
    "filters": {"symbol": "VNM", "quarter": 0},
    "order_by": [["year", "asc"]],
    "expected_n_points": 5
  },
  "expected_data_facts":    ["doanh thu 2024 ≈ 60-64 ngàn tỷ", "trend tăng đều"],
  "expected_caption_facts": ["đề cập 5 năm", "có nhận xét xu hướng"]
}
```
