# Prompt 3 — Enrich Reference Answer với Web + DB Ground Truth

NotebookLM (stage 2) chỉ ground vào private docs. Sau stage 2 sẽ có
nhiều case `unanswerable: true` hoặc reference_answer thiếu số liệu
cụ thể. Stage 3 dùng **LLM có web search** (ChatGPT Plus với browsing,
Gemini, hoặc Claude với web tool) để bổ sung từ nguồn công khai
(cafef.vn, vietstock.vn, BCTC, web).

Mục tiêu: làm cho reference_answer phản ánh **câu trả lời lý tưởng đa
nguồn**, không chỉ riêng RAG. Vì:
- Hệ thống FinHouse cũng có tool web_search + database → reference
  phải cover những gì các tool đó nên đem về.
- Benchmark cần `key_facts` đa dạng để chấm faithfulness/correctness
  của câu trả lời cuối (collector tổng hợp từ tất cả tool).

---

## Phân loại case sau stage 2

Quy tắc lọc input cho stage 3:

| Tình trạng sau stage 2 | Stage 3 làm gì |
|---|---|
| `unanswerable: true` AND `category` ∈ {F. Recent news, một phần E. Sector/macro} | **Sinh mới hoàn toàn** reference_answer từ web search |
| `unanswerable: false` AND `category` ∈ {B, C, D} (cần số liệu DB) | **Verify + bổ sung số liệu** chính xác (đối chiếu BCTC chính thức) |
| `unanswerable: false` AND `category` ∈ {A. Definition, H. Ambiguous} | **Bỏ qua** — không cần enrich (định nghĩa stable, clarification không cần data) |
| `category = G. Visualize` | **Bỏ qua** — xem `04_visualize_handling.md` |

Tip: pre-filter trước khi gửi cho LLM:
```bash
jq '[.[] | select(
    .unanswerable == true
    or .category | startswith("B.") or startswith("C.") or startswith("D.") or startswith("F.")
)]' questions_with_refs.json > questions_to_enrich.json
```

---

## TEMPLATE — Stage 3 prompt cho LLM-with-web

Paste vào ChatGPT (browsing on) / Gemini / Claude (web tool on). Batch
**20 câu mỗi prompt** (nhỏ hơn stage 2 vì web search tốn token).

```
===BEGIN===
Bạn là expert tài chính Việt Nam có quyền truy cập web. Đang giúp xây
dựng bộ benchmark cho hệ thống RAG-agent FinHouse. Mỗi item trong JSON
array dưới đây đã được NotebookLM xử lý nhưng còn thiếu — câu trả lời
chưa đủ số liệu cụ thể, hoặc unanswerable vì nguồn private không có.

NHIỆM VỤ: Với mỗi item:

1. **Đọc kỹ `question` + `expected_entities` + `expected_timeframe`**.

2. **Search web** để lấy:
   - Số liệu tài chính chính xác (ROE, doanh thu, lợi nhuận, …) từ
     **nguồn uy tín**: cafef.vn, vietstock.vn, fireant.vn, simplize.vn,
     hoặc BCTC chính thức trên web công ty.
   - Tin tức gần đây nếu là Bucket F.
   - Nếu LLM của bạn không thể browse real-time → vẫn dùng kiến thức
     huấn luyện mới nhất + báo cao tài chính public, nhưng đánh dấu
     `web_verified: false` cho item đó.

3. **Cập nhật `reference_answer`**:
   - Nếu item cũ đã có answer từ NotebookLM → MERGE: giữ phần grounded
     từ NotebookLM + thêm số liệu/context từ web. Phân biệt rõ bằng
     citation: `[1]` cho NotebookLM source, `[web:cafef]` cho web.
   - Nếu item cũ `unanswerable: true` → viết MỚI từ web, set
     `unanswerable: false` (trừ khi web cũng không có).

4. **Cập nhật fields**:
   - `web_sources`: list URL hoặc tên domain đã reference.
     VD: `["cafef.vn/VNM-2024", "vietstock.vn/finance/VNM"]`.
   - `key_facts`: bổ sung số liệu mới tìm được (VD: `"ROE 2024 = 22.5% theo BCTC kiểm toán"`).
   - `web_verified`: `true` nếu thực sự đã browse được URL đó;
     `false` nếu chỉ dựa kiến thức training (mark để anh review tay sau).
   - `web_enriched`: `true` (luôn set khi đi qua stage 3).
   - Nếu phát hiện reference_answer cũ SAI (NotebookLM bịa nhẹ) →
     SỬA + ghi `corrected: true`. Đây là check quan trọng.

5. **negative_facts**: thêm nếu thấy fact dễ bị nhầm. VD: "KHÔNG được
   nhầm với ROA", "KHÔNG được dùng số liệu chưa kiểm toán".

6. **Ngôn ngữ**: tiếng Việt, văn phong báo cáo. Citation rõ ràng.

7. **Bucket F (Recent news)**: ưu tiên tìm tin trong vòng 30 ngày gần
   nhất (so với ngày bạn chạy prompt). Nếu không có tin nào → giữ
   `unanswerable: true`, nhưng `key_facts` ghi "no recent news found
   in trusted sources as of <date>".

OUTPUT: JSON array, giữ nguyên tất cả field cũ + cập nhật/bổ sung như
trên. KHÔNG markdown wrap.

VÍ DỤ FEW-SHOT — input đã qua stage 2, output sau stage 3:

INPUT (item B-001 đã có NotebookLM answer):
[
  {
    "id": "B-001",
    "question": "ROE VNM 2024 là bao nhiêu?",
    "expected_entities": ["VNM"],
    "expected_timeframe": "2024",
    "category": "B. Single-fact lookup",
    "reference_answer": "ROE năm 2024 của Vinamilk (VNM) đạt 22.5% [1], tăng nhẹ so với mức 20.1% của năm 2023 [1].",
    "sources": ["VNM_BCTC_2024.pdf — Báo cáo HĐKD trang 4"],
    "key_facts": ["ROE 2024 của VNM = 22.5%", "ROE 2023 = 20.1%"],
    "negative_facts": ["KHÔNG được nói ROE > 30%"],
    "unanswerable": false
  }
]

OUTPUT:
[
  {
    "id": "B-001",
    "question": "ROE VNM 2024 là bao nhiêu?",
    "expected_entities": ["VNM"],
    "expected_timeframe": "2024",
    "category": "B. Single-fact lookup",
    "reference_answer": "ROE năm 2024 của Vinamilk (VNM) đạt 22.5% [1][web:cafef], tăng so với 20.1% năm 2023 [1]. Theo BCTC kiểm toán 2024, lợi nhuận sau thuế đạt 9.453 tỷ đồng, vốn chủ sở hữu bình quân 42.045 tỷ → ROE = 22.48% [web:vietstock].",
    "sources": ["VNM_BCTC_2024.pdf — Báo cáo HĐKD trang 4"],
    "web_sources": ["cafef.vn/vnm-bao-cao-2024", "vietstock.vn/finance/VNM"],
    "key_facts": [
      "ROE 2024 của VNM = 22.5% (làm tròn) hoặc 22.48% (chính xác)",
      "ROE 2023 = 20.1%",
      "LNST 2024 = 9.453 tỷ đồng",
      "VCSH bình quân 2024 = 42.045 tỷ"
    ],
    "negative_facts": [
      "KHÔNG được nói ROE > 30%",
      "KHÔNG được nhầm với ROA (ROA thường thấp hơn)"
    ],
    "unanswerable": false,
    "web_enriched": true,
    "web_verified": true,
    "corrected": false
  }
]

INPUT (item F-005, NotebookLM đã trả unanswerable):
[
  {
    "id": "F-005",
    "question": "FPT tuần này có tin gì mới về AI không?",
    "category": "F. Recent news",
    "expected_entities": ["FPT"],
    "reference_answer": null,
    "sources": [],
    "key_facts": [],
    "unanswerable": true
  }
]

OUTPUT (giả sử ngày chạy là 2026-05-14):
[
  {
    "id": "F-005",
    "question": "FPT tuần này có tin gì mới về AI không?",
    "category": "F. Recent news",
    "expected_entities": ["FPT"],
    "reference_answer": "Trong tuần 12-14/05/2026, FPT công bố ký hợp tác với NVIDIA về AI Factory tại Việt Nam [web:cafef], đồng thời cập nhật doanh thu mảng AI Q1/2026 tăng 67% YoY [web:vietstock].",
    "sources": [],
    "web_sources": ["cafef.vn/fpt-nvidia-2026", "vietstock.vn/news/FPT-Q1-2026"],
    "key_facts": [
      "FPT ký hợp tác NVIDIA về AI Factory (tuần 12-14/05/2026)",
      "Doanh thu mảng AI Q1/2026 tăng 67% YoY"
    ],
    "negative_facts": [
      "KHÔNG được bịa tin (kiểm tra ngày publish trước khi quote)"
    ],
    "unanswerable": false,
    "web_enriched": true,
    "web_verified": true,
    "corrected": false
  }
]

---

BÂY GIỜ XỬ LÝ BATCH SAU. Output JSON array đúng schema, đúng số item:

INPUT:
{{PASTE_BATCH_JSON_HERE}}
===END===
```

---

## Workflow chia batch + merge

```bash
# Pre-filter từ stage 2 output
jq '[.[] | select(
    .unanswerable == true
    or (.category | startswith("B.") or startswith("C.") or startswith("D.") or startswith("F."))
)]' questions_with_refs.json > to_enrich.json

# Chia 20 câu/batch (web search tốn token hơn stage 2):
jq -c '.[0:20]'     to_enrich.json > stage3_batch_01.json
jq -c '.[20:40]'    to_enrich.json > stage3_batch_02.json
# ...

# Chạy từng batch trên ChatGPT/Gemini/Claude với web search ON,
# lưu output vào stage3_batch_NN_done.json

# Ghép tất cả batch + merge ngược vào file gốc (item nào không qua
# stage 3 thì giữ nguyên từ stage 2):
jq -s '
  (.[0]) as $base |
  (.[1] | map({(.id): .}) | add) as $enriched |
  $base | map(. + ($enriched[.id] // {}))
' questions_with_refs.json <(jq -s 'add' stage3_batch_*_done.json) \
  > questions_enriched.json
```

`questions_enriched.json` là file output cuối của pipeline test set.
Tiếp theo (phase code) sẽ có script `split_testset.py` cắt thành 3
file JSONL cho 3 layer.

---

## Lưu ý chất lượng

1. **Spot-check 15-20 item** sau khi enrich. Đặc biệt với Bucket B/D:
   các con số tài chính phải đối chiếu với BCTC chính thức, không
   chỉ tin theo aggregator.

2. **Date sensitivity**: Bucket F dependency vào ngày chạy prompt.
   Ghi date vào file `questions_enriched.json` ở metadata top-level:
   ```json
   {
     "metadata": {
       "stage3_run_date": "2026-05-14",
       "stage3_model": "chatgpt-5 (web browsing)"
     },
     "items": [ ... ]
   }
   ```
   Khi chạy benchmark 6 tháng sau, các answer Bucket F sẽ outdated
   → cần re-run stage 3 cho bucket F trước mỗi major benchmark.

3. **`web_verified: false`** items: review tay vì có thể là LLM hallucination
   numbers. Nếu không verify được, set `unanswerable: true` cho honest.
