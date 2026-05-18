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

> **Lưu ý cơ sở chấm điểm**: metric `correctness` (Layer A) so câu trả
> lời hệ thống với `reference_answer` và đòi khớp **≥60% `key_facts`**;
> Layer B `context_recall` tách `reference_answer` thành claim. Không có
> metric nào đối chiếu số liệu tool DB/web với field riêng — toàn bộ
> ground truth DB + web dồn vào `reference_answer` + `key_facts`. Vì vậy
> stage 3 phải verify từng số và viết key_facts atomic, nếu không sẽ
> chấm ngược (phạt hệ thống trả lời đúng).

---

## Phân loại case sau stage 2

Quy tắc lọc input cho stage 3:

| Tình trạng sau stage 2 | Stage 3 làm gì |
|---|---|
| `unanswerable: true` AND `category` ∈ {F. Recent news, một phần E. Sector/macro} | **Sinh mới hoàn toàn** reference_answer từ web search |
| `unanswerable: false` AND `category` ∈ {B, C, D} (cần số liệu DB) | **Verify + bổ sung số liệu** chính xác (đối chiếu BCTC chính thức) |
| `unanswerable: false` AND `category` ∈ {A. Definition, H. Ambiguous} | **Bỏ qua** — không cần enrich (định nghĩa stable, clarification không cần data) |
| `category = G. Visualize` | **Bỏ qua** — đã tách sang `questions_visualize.json`, xem `04_visualize_handling.md` |

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
Bạn là expert tài chính Việt Nam có quyền truy cập web. Đang xây bộ benchmark GOLD cho hệ thống RAG-agent FinHouse. Hôm nay là 2026-05-18. Mỗi item JSON dưới đây đã qua stage 2 (NotebookLM) nhưng thiếu số liệu, hoặc unanswerable, hoặc có thể chứa số NotebookLM bịa nhẹ.

BỐI CẢNH QUAN TRỌNG: reference_answer và key_facts của bạn sẽ được dùng làm ĐÁP ÁN CHUẨN để chấm điểm hệ thống. Bộ chấm yêu cầu hệ thống phải khớp ÍT NHẤT 60% số key_facts. Vì vậy mọi con số trong key_facts phải CHÍNH XÁC, ATOMIC và ĐÃ VERIFY. Một key_fact sai = chấm ngược (phạt hệ thống trả lời đúng). Đây là gold set, không phải câu trả lời thường.

NHIỆM VỤ với mỗi item:

1. Đọc kỹ question + expected_entities + expected_timeframe (+ history nếu Bucket I).

2. VERIFY BẮT BUỘC — không bỏ qua: với MỖI con số đã có trong reference_answer/key_facts từ stage 2, phải tra lại từ nguồn uy tín (cafef.vn, vietstock.vn, fireant.vn, simplize.vn, hoặc BCTC chính thức trên web công ty). Ba trường hợp:
   - Số khớp nguồn -> giữ, gắn citation nguồn.
   - Số LỆCH so với nguồn -> SỬA theo nguồn uy tín, set corrected: true, và ghi giá trị cũ vào negative_facts dạng "KHÔNG dùng <số cũ sai> (giá trị NotebookLM bịa)".
   - Số KHÔNG verify được từ bất kỳ nguồn uy tín nào -> KHÔNG được giữ làm gold: bỏ con số đó khỏi reference_answer/key_facts, set web_verified: false. Nếu sau khi bỏ mà item không còn fact nào chắc chắn -> set unanswerable: true, reference_answer: null, sources: [], key_facts ghi "không verify được số liệu từ nguồn uy tín tính đến 2026-05-18".

3. BỔ SUNG TỐI ĐA số liệu/tin làm cơ sở chấm — càng nhiều fact verify được càng tốt:
   - Bucket B/C/D (cần DB): liệt kê ĐẦY ĐỦ mọi chỉ tiêu liên quan tới câu hỏi với GIÁ TRỊ CHÍNH XÁC: doanh thu, lợi nhuận gộp/trước thuế/sau thuế, ROE, ROA, EPS, biên lợi nhuận, tổng tài sản, vốn chủ sở hữu, nợ vay; với ngân hàng thêm NIM, nợ xấu (NPL), tỷ lệ bao phủ, tăng trưởng tín dụng, CASA. Mỗi chỉ tiêu kèm năm/quý + đơn vị + ghi rõ hợp nhất/riêng lẻ, đã kiểm toán hay chưa. Tối thiểu 5-10 key_facts atomic cho B/C/D.
   - Bucket F (tin gần đây): tìm tin trong vòng 30 ngày tính tới 2026-05-18. Mỗi tin là một key_fact kèm NGÀY công bố + nguồn. Tối thiểu 3-5 tin nếu có. Không có tin nào trong nguồn uy tín -> unanswerable: true, key_facts ghi "no recent news found in trusted sources as of 2026-05-18".
   - Bucket E (ngành/vĩ mô): bổ sung số liệu định lượng (tăng trưởng ngành %, GDP, lạm phát, lãi suất, giá hàng hoá...) kèm kỳ + nguồn.

4. ĐỊNH DẠNG key_facts — BẮT BUỘC atomic, mỗi phần tử một fact kiểm được, dạng:
   "<chỉ tiêu> <kỳ> = <giá trị> <đơn vị> [<nguồn>]"
   Ví dụ: "ROE 2024 của VNM = 22.48% [vietstock]", "LNST 2024 = 9.453 tỷ đồng (hợp nhất, đã kiểm toán) [cafef]", "FPT ký hợp tác NVIDIA về AI Factory ngày 12/05/2026 [cafef]".
   KHÔNG ghi key_fact mơ hồ kiểu "lợi nhuận tăng mạnh", "kết quả tích cực".

5. Cập nhật reference_answer:
   - Stage 2 đã có answer -> MERGE: giữ phần grounded từ NotebookLM ([1]) + thêm số liệu/tin verify từ web ([web:cafef], [web:vietstock]...). Phân biệt rõ nguồn bằng citation.
   - Stage 2 unanswerable -> viết mới từ web, set unanswerable: false (trừ khi web cũng không có).
   - Văn phong báo cáo tài chính tiếng Việt, KHÔNG CJK leak, KHÔNG markdown header.

6. Cập nhật fields:
   - web_sources: list URL/domain đã thực sự tra. VD ["cafef.vn/vnm-2024", "vietstock.vn/finance/VNM"].
   - web_enriched: true (luôn, khi đi qua stage 3).
   - web_verified: true nếu thực sự browse được URL; false nếu chỉ dựa kiến thức training (đánh dấu để review tay — và áp dụng quy tắc honesty ở mục 2).
   - corrected: true nếu đã sửa số sai của stage 2, kèm note ở negative_facts.
   - negative_facts: thêm fact dễ nhầm (VD "KHÔNG nhầm ROE với ROA", "KHÔNG dùng số chưa kiểm toán", "KHÔNG dùng <số cũ sai>"). Tối đa 3.

7. GIỮ NGUYÊN, KHÔNG sửa các field tag stage 1: id, question, history, category, expected_tools, expected_entities, expected_timeframe, scope (kể cả scope="general" của Bucket A/H), style, persona, complexity, needs_clarification, sources.

8. Bucket I (multi-turn): đọc cả history để hiểu câu follow-up trước khi enrich.

OUTPUT: chỉ một JSON array duy nhất, đúng số item như input, KHÔNG markdown wrap, KHÔNG text giải thích nào khác.

VÍ DỤ — merge + verify + bổ sung dày số liệu:

INPUT:
[
  {
    "id": "B-001",
    "question": "ROE VNM 2024 là bao nhiêu?",
    "expected_entities": ["VNM"],
    "expected_timeframe": "2024",
    "category": "B. Single-fact lookup",
    "reference_answer": "ROE năm 2024 của Vinamilk đạt 22.5% [1], tăng so với 20.1% năm 2023 [1].",
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
    "reference_answer": "ROE năm 2024 của Vinamilk (VNM) đạt 22.48% [1][web:vietstock], tăng nhẹ so với 20.1% năm 2023 [1]. Theo BCTC hợp nhất kiểm toán 2024: lợi nhuận sau thuế 9.453 tỷ đồng, vốn chủ sở hữu bình quân 42.045 tỷ đồng, doanh thu thuần 61.783 tỷ đồng (+2,2% YoY), biên lợi nhuận gộp 41,9% [web:cafef].",
    "sources": ["VNM_BCTC_2024.pdf — Báo cáo HĐKD trang 4"],
    "web_sources": ["cafef.vn/vnm-bao-cao-tai-chinh-2024", "vietstock.vn/finance/VNM"],
    "key_facts": [
      "ROE 2024 của VNM = 22.48% [vietstock]",
      "ROE 2023 của VNM = 20.1% [vietstock]",
      "LNST 2024 = 9.453 tỷ đồng (hợp nhất, đã kiểm toán) [cafef]",
      "Vốn chủ sở hữu bình quân 2024 = 42.045 tỷ đồng [cafef]",
      "Doanh thu thuần 2024 = 61.783 tỷ đồng, +2,2% YoY [cafef]",
      "Biên lợi nhuận gộp 2024 = 41,9% [cafef]"
    ],
    "negative_facts": [
      "KHÔNG được nói ROE > 30%",
      "KHÔNG nhầm ROE với ROA (ROA 2024 thấp hơn, ~15%)",
      "KHÔNG dùng ROE 22.5% làm con số chính xác (đó là số làm tròn của stage 2)"
    ],
    "unanswerable": false,
    "web_enriched": true,
    "web_verified": true,
    "corrected": true
  }
]

VÍ DỤ — Bucket F có tin trong 30 ngày:

INPUT:
[
  {
    "id": "F-002",
    "question": "FPT trong tháng này có thông báo hợp đồng lớn, thương vụ AI hay tin quan trọng nào không?",
    "category": "F. Recent news",
    "expected_entities": ["FPT"],
    "reference_answer": null,
    "sources": [],
    "key_facts": [],
    "unanswerable": true
  }
]

OUTPUT:
[
  {
    "id": "F-002",
    "question": "FPT trong tháng này có thông báo hợp đồng lớn, thương vụ AI hay tin quan trọng nào không?",
    "category": "F. Recent news",
    "expected_entities": ["FPT"],
    "reference_answer": "Trong khoảng 20/04–15/05/2026, FPT công bố ký hợp tác AI Factory với NVIDIA tại Việt Nam (07/05/2026) [web:cafef] và báo doanh thu mảng AI Q1/2026 tăng 67% YoY [web:vietstock].",
    "sources": [],
    "web_sources": ["cafef.vn/fpt-nvidia-ai-factory-2026", "vietstock.vn/news/FPT-Q1-2026"],
    "key_facts": [
      "FPT ký hợp tác AI Factory với NVIDIA ngày 07/05/2026 [cafef]",
      "Doanh thu mảng AI của FPT Q1/2026 tăng 67% YoY [vietstock]"
    ],
    "negative_facts": [
      "KHÔNG bịa tin — phải kiểm tra ngày publish trước khi đưa vào"
    ],
    "unanswerable": false,
    "web_enriched": true,
    "web_verified": true,
    "corrected": false
  }
]

VÍ DỤ — không verify được, áp dụng honesty:

INPUT:
[
  {
    "id": "F-006",
    "question": "BID có sự kiện đáng chú ý nào trong năm 2026 về phát hành cổ phiếu, trả cổ tức không?",
    "category": "F. Recent news",
    "expected_entities": ["BID"],
    "reference_answer": null,
    "sources": [],
    "key_facts": [],
    "unanswerable": true
  }
]

OUTPUT:
[
  {
    "id": "F-006",
    "question": "BID có sự kiện đáng chú ý nào trong năm 2026 về phát hành cổ phiếu, trả cổ tức không?",
    "category": "F. Recent news",
    "expected_entities": ["BID"],
    "reference_answer": null,
    "sources": [],
    "web_sources": ["cafef.vn/BID", "vietstock.vn/finance/BID"],
    "key_facts": ["no recent news found in trusted sources as of 2026-05-18"],
    "negative_facts": [],
    "unanswerable": true,
    "web_enriched": true,
    "web_verified": false,
    "corrected": false
  }
]

BÂY GIỜ XỬ LÝ BATCH SAU. Output JSON array đúng schema, đúng số item, không kèm text khác:

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

`questions_enriched.json` là file output cuối của pipeline test set
(Q&A buckets A–F, H, I). Bucket G đi riêng qua `questions_visualize.json`.
Tiếp theo (phase code) sẽ có script `split_testset.py` cắt thành 3
file JSONL cho 3 layer.

---

## Lưu ý chất lượng

1. **Spot-check 15-20 item** sau khi enrich. Đặc biệt với Bucket B/D:
   các con số tài chính phải đối chiếu với BCTC chính thức, không
   chỉ tin theo aggregator.

2. **Date sensitivity**: Bucket F dependency vào ngày chạy prompt
   (template chốt mốc **2026-05-18**). Ghi date vào file
   `questions_enriched.json` ở metadata top-level:
   ```json
   {
     "metadata": {
       "stage3_run_date": "2026-05-18",
       "stage3_model": "chatgpt-5 (web browsing)"
     },
     "items": [ ... ]
   }
   ```
   Khi chạy benchmark 6 tháng sau, các answer Bucket F sẽ outdated
   → cần re-run stage 3 cho bucket F trước mỗi major benchmark.

3. **`web_verified: false`** items: review tay vì có thể là LLM hallucination
   numbers. Nếu không verify được, set `unanswerable: true` cho honest
   (đã là bước bắt buộc trong mục 2 của template).
