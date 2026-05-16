# Prompt 2 — Sinh Reference Answer bằng NotebookLM

Mục tiêu: cho NotebookLM (đã được nạp toàn bộ tài liệu FinHouse trong
notebook) đọc từng câu hỏi và viết câu trả lời tham chiếu **chỉ dựa
vào tài liệu** + ghi nguồn.

NotebookLM ưu việt ở bước này vì:
1. Truy cập trực tiếp PDF/báo cáo private (không bịa).
2. Tự ghi citation `[N]` trỏ tới source.
3. Hiểu tiếng Việt tài chính rất tốt.

---

## Workflow

1. Upload toàn bộ file trong [data/](../../data/) vào 1 NotebookLM
   notebook (~50 source slot tối đa free tier; nếu vượt thì chia 2
   notebook theo ngành hoặc theo thời gian).
2. Chia file `questions_raw.json` thành các batch **30 câu mỗi prompt**.
   NotebookLM context window ~25k tokens output → 30 câu × ~500 token
   answer ≈ 15k token, an toàn.
3. Với mỗi batch, paste TEMPLATE bên dưới + paste batch JSON.
4. NotebookLM trả về JSON array với `reference_answer` + `sources` +
   `key_facts` đã được fill.
5. Ghép tất cả batch output → `evaluation/testset_prompts/questions_with_refs.json`.
6. Script (em sẽ viết ở phase sau) sẽ split file này thành 3 testset
   JSONL theo layer (e2e / rag / agent).

---

## TEMPLATE — copy từ `===BEGIN===` đến `===END===`, paste cùng batch JSON

```
===BEGIN===
Bạn là expert tài chính Việt Nam. Bạn đang giúp xây dựng bộ
benchmark cho hệ thống RAG FinHouse. Với mỗi câu hỏi trong JSON array
phía dưới, hãy viết REFERENCE ANSWER — câu trả lời "vàng" mà hệ thống
LÝ TƯỞNG nên đưa ra.

QUY TẮC TUYỆT ĐỐI:

1. **Chỉ dùng thông tin có trong các nguồn (sources)** đã upload vào
   notebook này. KHÔNG dùng kiến thức ngoài. KHÔNG bịa số liệu. Nếu
   nguồn không có data trả lời câu hỏi → set `reference_answer = null`
   và `sources = []`, đánh dấu `unanswerable: true`.

2. **Ngôn ngữ**: tiếng Việt sạch, văn phong báo cáo tài chính. KHÔNG
   CJK leak, KHÔNG markdown header. Câu trả lời 2-6 câu (tuỳ độ phức
   tạp của câu hỏi).

3. **Citation**: ghi `[1]`, `[2]` sau mỗi câu lấy fact từ nguồn. Map
   `[N]` → tên file/section trong field `sources`.

4. **key_facts**: liệt kê 3-7 fact chính trong reference_answer (dạng
   array string ngắn). Đây là input để benchmark chấm faithfulness +
   correctness. Ví dụ: ["ROE 2024 = 22.5%", "tăng so với 2023 (20.1%)"].

5. **negative_facts** (chỉ ghi khi có): fact SAI mà hệ thống dễ nói
   nhầm. Ví dụ "KHÔNG được nói ROE > 50%". Tối đa 3 entry. Không bắt
   buộc — bỏ trống nếu không nghĩ ra.

6. **Persona-aware**: tone của reference_answer phải hợp với persona:
   - retail → ngắn, có số liệu cụ thể, không jargon nặng
   - analyst → có context, multi-period nếu liên quan, technical term OK
   - student → có định nghĩa + công thức nếu hỏi về khái niệm
   - journalist → có nguồn cụ thể, ngày tháng

7. **Bucket H (Ambiguous)**: câu hỏi sẽ có `needs_clarification: true`.
   Với những câu này, `reference_answer` là CÂU CLARIFY mà hệ thống
   nên hỏi lại, KHÔNG phải answer. Ví dụ "Bạn muốn hỏi công ty nào ạ?
   (VNM / FPT / HPG / ...)".

8. **Bucket I (Multi-turn)**: phải tính cả history khi đọc câu hỏi.
   Câu "Còn lợi nhuận thì sao?" phải hiểu là "lợi nhuận của entity +
   timeframe trong câu user trước".

9. **Bucket F (Recent news)**: nguồn của bạn KHÔNG có news realtime.
   → đa số case Bucket F sẽ `unanswerable: true` (đó là OK — benchmark
   sẽ dùng để chấm web_search agent thay vì RAG). Nếu nguồn có news
   gần thời điểm hỏi thì vẫn trả lời được.

OUTPUT — JSON array duy nhất, KHÔNG markdown wrap, KHÔNG text khác.
Mỗi item KEEP NGUYÊN các field input + ADD 4 field mới:
`reference_answer`, `sources`, `key_facts`, `negative_facts`,
`unanswerable`.

VÍ DỤ FEW-SHOT (đầu vào + đầu ra):

INPUT:
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
  }
]

OUTPUT:
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
    "needs_clarification": false,

    "reference_answer": "ROE năm 2024 của Vinamilk (VNM) đạt 22.5% [1], tăng nhẹ so với mức 20.1% của năm 2023 [1]. Tỷ suất này phản ánh hiệu quả sinh lời cao nhờ biên lợi nhuận gộp duy trì trên 40% và doanh thu thuần tăng 5.1% [1].",
    "sources": ["VNM_BCTC_2024.pdf — Báo cáo HĐKD trang 4"],
    "key_facts": [
      "ROE 2024 của VNM = 22.5%",
      "ROE 2023 = 20.1%",
      "biên lợi nhuận gộp >40%",
      "doanh thu thuần tăng 5.1% YoY"
    ],
    "negative_facts": [
      "KHÔNG được nói ROE > 30%",
      "KHÔNG được claim số liệu cho năm khác mà bảo là 2024"
    ],
    "unanswerable": false
  }
]

VÍ DỤ unanswerable:

INPUT:
[
  {
    "id": "F-005",
    "question": "FPT tuần này có tin gì mới về AI không?",
    "category": "F. Recent news",
    ...
  }
]

OUTPUT:
[
  {
    "id": "F-005",
    ...
    "reference_answer": null,
    "sources": [],
    "key_facts": [],
    "negative_facts": [],
    "unanswerable": true
  }
]

VÍ DỤ Bucket H (Ambiguous):

INPUT:
[
  {
    "id": "H-001",
    "question": "Nó lãi sao?",
    "needs_clarification": true,
    ...
  }
]

OUTPUT:
[
  {
    "id": "H-001",
    ...
    "reference_answer": "Bạn có thể nói rõ hơn 'nó' là công ty/mã chứng khoán nào và mốc thời gian nào (năm, quý) không ạ?",
    "sources": [],
    "key_facts": ["clarification request: hỏi lại entity + timeframe"],
    "negative_facts": ["KHÔNG được trả lời bịa số liệu khi chưa biết công ty"],
    "unanswerable": false
  }
]

---

BÂY GIỜ XỬ LÝ BATCH SAU. Output đúng schema, đúng số lượng item như
input, JSON array duy nhất:

INPUT:
{{PASTE_BATCH_JSON_HERE}}
===END===
```

---

## Workflow chia batch

```bash
# Sau khi sinh xong questions_raw.json (~270 item):
cd evaluation/testset_prompts/

# Chia thành 9 batch 30 câu (em sẽ viết script nhỏ ở phase sau,
# tạm thời có thể chia tay bằng jq):
jq -c '.[0:30]'    questions_raw.json > batch_01.json
jq -c '.[30:60]'   questions_raw.json > batch_02.json
jq -c '.[60:90]'   questions_raw.json > batch_03.json
# ... đến batch_09.json
```

Sau đó với mỗi batch:
1. Mở NotebookLM notebook đã nạp data
2. Paste template + paste nội dung batch
3. Lưu output vào `batch_NN_done.json`

Cuối cùng:
```bash
jq -s 'add' batch_*_done.json > questions_with_refs.json
```

---

## Tips chạy NotebookLM

- **Verify ngẫu nhiên 10-15 case** sau khi NotebookLM trả về. NotebookLM
  có thể bịa nhẹ khi nguồn không đủ → đọc citation, đối chiếu lại.
- Nếu nhiều case `unanswerable: true` không như kỳ vọng → nguồn upload
  chưa đủ phong phú → thêm BCTC, báo cáo phân tích vào notebook.
- Với Bucket F (Recent news): chấp nhận `unanswerable: true` chiếm
  ~80%. Đây là tín hiệu để eval web_search agent đảm nhiệm.
- NotebookLM context limit khá rộng (~25k input + ~12k output), nhưng
  batch 30 là sweet spot — đủ to để output JSON gọn, đủ nhỏ để không
  truncate giữa item.
