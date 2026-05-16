# Prompt 1 — Generate Question Lists

Paste prompt này vào **ChatGPT / Claude / Gemini** (KHÔNG cần NotebookLM
ở bước này — bước này không cần truy cập tài liệu private). Mục tiêu:
sinh 30 câu hỏi cho 1 bucket, phong cách + persona xác định.

Chạy lại 9 lần (mỗi bucket A..I) để có đủ 270 case. Nếu muốn nhiều
biến thể, chạy 2-3 lần cho bucket lớn (B, C, D).

---

## TEMPLATE — copy từ dòng `===BEGIN===` đến `===END===`

```
===BEGIN===
Bạn đang giúp thiết kế bộ test cho hệ thống RAG-agent tài chính Việt
Nam tên FinHouse. Hệ thống có 4 tool: web_search, database (OLAP có
sẵn 11 bảng tài chính VN: stocks, company_overview, balance_sheet,
income_statement, cash_flow_statement, financial_ratios, shareholders,
officers, news, events, stock_price_history), visualize, và RAG trên
tài liệu nội bộ (báo cáo phân tích, báo cáo tài chính, tin tức).

NHIỆM VỤ: Sinh 30 câu hỏi tiếng Việt theo CHÍNH XÁC các thông số sau:

- **Bucket**: {{BUCKET_NAME}}                  (ví dụ: "B. Single-fact lookup")
- **Mô tả bucket**: {{BUCKET_DESC}}            (ví dụ: "Câu hỏi tra cứu 1 chỉ số tài chính của 1 công ty trong 1 timeframe rõ ràng")
- **Expected tool**: {{EXPECTED_TOOLS}}        (ví dụ: ["database", "rag"])
- **Persona**: {{PERSONA}}                     (ví dụ: "nhà đầu tư cá nhân — hỏi ngắn, thẳng, quan tâm số liệu cụ thể")
- **Scope**: {{SCOPE}}                         ("company" | "sector" | "macro" | "multi_company")
- **Style**: {{STYLE}}                         ("lookup" | "analyze" | "compare" | "synthesis" | "definition")
- **Timeframe mix**: {{TIMEFRAME_MIX}}         (ví dụ: "70% năm 2024, 20% 2023, 10% Q3/2024")
- **Complexity mix**: {{COMPLEXITY_MIX}}       (ví dụ: "60% factual, 30% multi_fact, 10% reasoning")

YÊU CẦU:

1. **Đa dạng entity**: trải đều qua các mã CK VN30 + một số mid-cap.
   Mỗi mã không xuất hiện quá 3 lần. Ưu tiên: VNM, FPT, HPG, MWG, VCB,
   BID, VHM, VIC, MSN, TCB, ACB, MBB, SAB, GAS, PLX, POW, GVR, KDH,
   PNJ, REE, SSI, VND, HCM, DGC, DCM, DPM.

2. **Đa dạng ngôn ngữ**: pha nhiều cách hỏi:
   - Cụt: "ROE VNM 2024?"
   - Đầy đủ: "Tỷ suất sinh lời trên vốn chủ sở hữu (ROE) của Vinamilk
     năm 2024 là bao nhiêu?"
   - Đời thường: "VNM năm ngoái lãi sao?"

3. **Persona-aware**: câu hỏi phải hợp lý cho persona đã cho. Ví dụ:
   - retail → hỏi ngắn, tập trung "có nên mua không", ROE, cổ tức, P/E
   - analyst → hỏi sâu, multi-quarter trend, segment, DuPont
   - student → định nghĩa, công thức, ví dụ
   - journalist → tin tức, sự kiện, surprise, controversy

4. **Tránh trùng lặp ngữ nghĩa**: 2 câu hỏi không được hỏi cùng 1 thứ
   với chỉ khác ticker. Mỗi câu phải có 1 góc khác nhau.

5. **KHÔNG bịa data**: chỉ ra câu hỏi, không cần đưa đáp án. Đáp án
   sẽ được sinh ở bước sau bằng NotebookLM với tài liệu thực.

OUTPUT: JSON array duy nhất, không markdown wrap, không text khác.
Schema mỗi item:

{
  "id":              "<bucket_letter>-<3-digit-seq>",  // VD: "B-001"
  "question":        "<câu hỏi tiếng Việt>",
  "category":        "{{BUCKET_NAME}}",
  "expected_tools":  {{EXPECTED_TOOLS}},
  "expected_entities": ["<ticker>" | "<sector>" | "<concept>"],
  "expected_timeframe": "<text or empty>",
  "scope":           "{{SCOPE}}",
  "style":           "{{STYLE}}",
  "persona":         "{{PERSONA}}",
  "complexity":      "factual" | "multi_fact" | "reasoning",
  "needs_clarification": false  // true CHỈ khi bucket = H. Ambiguous
}

VÍ DỤ FEW-SHOT (bucket B, persona retail):

[
  {
    "id": "B-001",
    "question": "ROE VNM 2024 là bao nhiêu?",
    "category": "B. Single-fact lookup",
    "expected_tools": ["database", "rag"],
    "expected_entities": ["VNM"],
    "expected_timeframe": "2024",
    "scope": "company",
    "style": "lookup",
    "persona": "retail",
    "complexity": "factual",
    "needs_clarification": false
  },
  {
    "id": "B-002",
    "question": "Vinamilk năm ngoái chia cổ tức bao nhiêu?",
    "category": "B. Single-fact lookup",
    "expected_tools": ["database", "rag"],
    "expected_entities": ["VNM"],
    "expected_timeframe": "2024",
    "scope": "company",
    "style": "lookup",
    "persona": "retail",
    "complexity": "factual",
    "needs_clarification": false
  },
  {
    "id": "B-003",
    "question": "Doanh thu Q3/2024 của HPG bao nhiêu?",
    "category": "B. Single-fact lookup",
    "expected_tools": ["database", "rag"],
    "expected_entities": ["HPG"],
    "expected_timeframe": "Q3/2024",
    "scope": "company",
    "style": "lookup",
    "persona": "retail",
    "complexity": "factual",
    "needs_clarification": false
  }
]

Bây giờ sinh **30 câu** tiếp theo cho thông số đã cho. CHỈ output
JSON array, không text khác.
===END===
```

---

## Cấu hình từng bucket — copy thay vào `{{...}}` của template

### Bucket A — Definition (30 câu)
- BUCKET_NAME: `A. Definition`
- BUCKET_DESC: `Câu hỏi định nghĩa khái niệm tài chính, không cần entity/timeframe cụ thể`
- EXPECTED_TOOLS: `["rag"]`
- PERSONA: `student — hỏi định nghĩa, công thức, ví dụ cụ thể; giọng tò mò, học hỏi`
- SCOPE: `general`
- STYLE: `definition`
- TIMEFRAME_MIX: `không có timeframe`
- COMPLEXITY_MIX: `80% factual, 20% multi_fact`

### Bucket B — Single-fact lookup (60 câu, chạy 2 lần × 30)
- BUCKET_NAME: `B. Single-fact lookup`
- BUCKET_DESC: `Tra cứu 1 chỉ số tài chính của 1 công ty trong 1 timeframe rõ ràng`
- EXPECTED_TOOLS: `["database", "rag"]`
- PERSONA: `retail — nhà đầu tư cá nhân, hỏi ngắn, thẳng, quan tâm ROE/EPS/cổ tức/P/E`
- SCOPE: `company`
- STYLE: `lookup`
- TIMEFRAME_MIX: `60% năm 2024, 20% 2023, 20% Q1-Q4/2024`
- COMPLEXITY_MIX: `100% factual`

### Bucket C — Multi-fact analyze (40 câu)
- BUCKET_NAME: `C. Multi-fact analyze`
- BUCKET_DESC: `Phân tích toàn diện 1 công ty trên nhiều khía cạnh (doanh thu, lợi nhuận, biên, nợ)`
- EXPECTED_TOOLS: `["database", "rag"]`
- PERSONA: `analyst — chuyên viên phân tích, hỏi sâu, muốn xu hướng multi-period + segment breakdown`
- SCOPE: `company`
- STYLE: `analyze`
- TIMEFRAME_MIX: `50% năm 2024, 30% giai đoạn 2022-2024, 20% Q1-Q4/2024`
- COMPLEXITY_MIX: `20% multi_fact, 80% reasoning`

### Bucket D — Compare (40 câu)
- BUCKET_NAME: `D. Compare`
- BUCKET_DESC: `So sánh ≥2 công ty trên 1 hoặc nhiều chỉ số`
- EXPECTED_TOOLS: `["database"]`
- PERSONA: `analyst — muốn ranking, peer comparison`
- SCOPE: `multi_company`
- STYLE: `compare`
- TIMEFRAME_MIX: `70% năm 2024, 30% multi-year`
- COMPLEXITY_MIX: `40% multi_fact, 60% reasoning`

### Bucket E — Sector/macro (30 câu)
- BUCKET_NAME: `E. Sector/macro`
- BUCKET_DESC: `Phân tích ngành hoặc vĩ mô (lãi suất, tỷ giá, GDP), không gắn với 1 công ty cụ thể`
- EXPECTED_TOOLS: `["rag", "web_search"]`
- PERSONA: `analyst — muốn outlook ngành, driver chính, rủi ro`
- SCOPE: `sector` (60%) hoặc `macro` (40%)
- STYLE: `analyze`
- TIMEFRAME_MIX: `40% 2024, 40% outlook 2025, 20% giai đoạn`
- COMPLEXITY_MIX: `30% multi_fact, 70% reasoning`

### Bucket F — Recent news (30 câu)
- BUCKET_NAME: `F. Recent news`
- BUCKET_DESC: `Tin tức gần đây, sự kiện, ngoài cutoff của training data`
- EXPECTED_TOOLS: `["web_search"]`
- PERSONA: `journalist — hỏi tin tuần này / tháng này, sự kiện bất ngờ, scandal`
- SCOPE: `company` (70%) hoặc `sector` (30%)
- STYLE: `lookup`
- TIMEFRAME_MIX: `100% recent_news (tuần/tháng/2026)`
- COMPLEXITY_MIX: `70% factual, 30% multi_fact`

### Bucket G — Chart request (20 câu)
- BUCKET_NAME: `G. Chart request`
- BUCKET_DESC: `User yêu cầu vẽ biểu đồ (bar/line/pie) cho data tài chính`
- EXPECTED_TOOLS: `["visualize"]`
- PERSONA: `analyst — muốn visualize trend / share / comparison`
- SCOPE: hỗn hợp company + multi_company
- STYLE: `analyze` (line/bar trend) hoặc `compare` (bar peer)
- TIMEFRAME_MIX: `80% multi-year, 20% năm 2024 cross-section`
- COMPLEXITY_MIX: `100% multi_fact`

### Bucket H — Ambiguous (10 câu)
- BUCKET_NAME: `H. Ambiguous`
- BUCKET_DESC: `Câu hỏi thiếu entity / timeframe / chỉ số → buộc rewriter ra clarification`
- EXPECTED_TOOLS: `[]` (vì rewriter sẽ chặn ở clarification)
- PERSONA: hỗn hợp
- SCOPE: `general`
- STYLE: `lookup`
- TIMEFRAME_MIX: `không có timeframe rõ`
- COMPLEXITY_MIX: `100% factual`
- LƯU Ý ĐẶC BIỆT: set `needs_clarification: true` cho tất cả 10 case.
  Ví dụ: "Nó lãi sao?", "Doanh thu?", "Năm ngoái thế nào?"

### Bucket I — Multi-turn (10 câu)
- BUCKET_NAME: `I. Multi-turn`
- BUCKET_DESC: `Câu hỏi follow-up cần context từ history`
- EXPECTED_TOOLS: hỗn hợp
- PERSONA: hỗn hợp
- LƯU Ý: mỗi item phải có field `history` thay vì chỉ `question`. Sửa
  schema thành:
  ```
  {
    "id": "I-001",
    "history": [
      {"role": "user", "content": "Doanh thu VNM Q2/2024?"},
      {"role": "assistant", "content": "Doanh thu VNM Q2/2024 đạt 15.700 tỷ đồng..."}
    ],
    "question": "Còn lợi nhuận thì sao?",
    ...
  }
  ```

---

## Output → Lưu ở đâu

Ghép kết quả 9 bucket vào `evaluation/testset_prompts/questions_raw.json`
(array tổng). File này là input cho prompt 2 (NotebookLM).
