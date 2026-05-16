# Bucket G — Visualize Test Set (đường riêng)

Visualize agent khác bản chất với các agent khác: output là **biểu đồ
PNG**, không phải text. NotebookLM/LLM không thể "sinh reference
chart" — chỉ có thể sinh ngữ cảnh xung quanh chart.

→ Bucket G **bỏ qua stage 2 + 3**. Thay vào đó:
1. Em viết tay schema test case (template bên dưới).
2. Đánh giá bằng metric **structural**, không phải answer-text.

---

## Tại sao không dùng reference_answer text

Câu hỏi "Vẽ doanh thu VNM 5 năm gần nhất" → output đúng là 1 chart
PNG có 5 bar/line cho 5 năm. So sánh "câu trả lời" với reference text
sẽ luôn fail (text ≠ ảnh).

Cái thực sự cần chấm:
1. Agent có chọn đúng **loại chart** (line cho trend, bar cho peer
   comparison, pie cho share)?
2. Args truyền vào tool có đúng **table + column + filter + order**?
3. Tool có trả về **URL PNG hợp lệ** không (vs error/404)?
4. (Optional) Caption text accompanying chart có hợp lý không?

---

## Schema cho Bucket G

Mỗi item viết tay, KHÔNG cần ground truth answer text:

```jsonc
{
  "id": "G-001",
  "question": "Vẽ biểu đồ doanh thu VNM 5 năm gần nhất",

  // Tagging giống các bucket khác
  "category": "G. Chart request",
  "expected_tools": ["visualize"],
  "expected_entities": ["VNM"],
  "expected_timeframe": "2020-2024",
  "scope": "company",
  "style": "analyze",
  "persona": "analyst",
  "complexity": "multi_fact",
  "needs_clarification": false,

  // ── PHẦN ĐẶC THÙ G ──
  "expected_chart": {
    "chart_type": ["line", "bar"],   // chấp nhận 1 trong 2 (trend → ưu tiên line)
    "table":      "income_statement",
    "x_column":   "year",
    "y_columns":  ["revenue"],
    "filters":    {"symbol": "VNM", "quarter": 0},
    "order_by":   [["year", "asc"]],
    "expected_n_points": 5
  },
  "expected_data_facts": [
    // Các số liệu mà chart NÊN show — verify được bằng SQL ground truth
    "doanh thu 2024 ≈ 60.000-64.000 tỷ (đối chiếu BCTC)",
    "trend tăng đều 2020-2024",
    "có data cho mỗi năm trong [2020, 2021, 2022, 2023, 2024]"
  ],
  "expected_caption_facts": [
    // Caption text agent NÊN nói kèm chart
    "đề cập đến chuỗi 5 năm",
    "có nhận xét xu hướng tăng/giảm",
    "đề cập đơn vị (tỷ đồng)"
  ],

  // Không cần các field này cho Bucket G:
  "reference_answer": null,
  "sources": [],
  "key_facts": []
}
```

---

## Metric đánh giá Bucket G (Layer C — Agent)

| Metric | Cách đo | Range |
|---|---|---|
| **chart_type_acc** | `chart_type` agent dùng ∈ `expected_chart.chart_type` | {0, 1} |
| **table_acc** | `table` arg khớp `expected_chart.table` | {0, 1} |
| **column_acc** | Jaccard(actual y_columns, expected y_columns) | [0, 1] |
| **filter_acc** | Filter có chứa expected entity + timeframe không (LLM judge) | {0, 1} |
| **tool_result_ok** | Tool trả về URL hợp lệ (HTTP 200, content-type image) | {0, 1} |
| **data_correctness** | Query SQL ground truth → so với chart data series | [0, 1] |
| **caption_completeness** | Bao nhiêu `expected_caption_facts` xuất hiện trong agent answer (LLM judge) | [0, 1] |

`data_correctness` là metric mạnh nhất nhưng cần code đặc biệt: parse
chart URL → fetch underlying data từ DB (vì visualize tool đọc trực
tiếp từ ClickHouse) → so series. Em sẽ implement trong phase code.

---

## Cách tạo bộ 20 case Bucket G

Vì là viết tay nên dùng matrix-driven thay vì LLM-generated:

| Chart type | Entity | Timeframe | Metric | N |
|---|---|---|---|---|
| **line trend** (1 entity, 1 metric, multi-year) | VN30 (10 mã) | 2020-2024 | doanh thu / LNST / ROE | 8 |
| **bar peer** (multi entity, 1 metric, 1 year) | ngành (3 ngành × 3-5 mã) | 2024 | doanh thu / LNST | 6 |
| **pie share** (1 entity, share-of-whole) | VNM, FPT, VIC, HPG | 2024 | shareholders / segment mix | 4 |
| **line multi-metric** (1 entity, ≥2 metric trên cùng axis) | VN30 | 2020-2024 | doanh thu + LNST | 2 |

Em sẽ viết script `generate_g_template.py` ở phase code để sinh
skeleton 20 case này, anh chỉ cần fill `expected_data_facts` (~5 phút/case).

---

## (Optional) Visual quality check với multimodal LLM

Nếu muốn cover "chart có dễ đọc không / có label đúng không" thay vì
chỉ structural, có thể thêm metric **C4. Visual quality**:

```
Multimodal LLM (Gemini 1.5 Pro, Claude 3.5 Sonnet vision) nhận:
  - Câu hỏi user
  - PNG chart từ visualize agent

Trả về:
  {
    "GiaiThich": "...",
    "co_title": bool,
    "co_axis_label": bool,
    "data_matching_question": bool,    // chart có thực sự trả lời câu hỏi không
    "DiemTong": 0-3
  }
```

Cost: ~$0.01/case với Gemini Flash vision → 20 case × 5 lần chạy
benchmark = $1. Hợp lý cho release-time, không cần cho smoke test.

Em đề xuất:
- **Phase đầu**: skip C4, chỉ chấm C1-C3 + data_correctness. Đủ phân
  biệt được agent tốt/tệ.
- **Phase sau** (nếu cần precision cao hơn cho release sign-off):
  thêm C4 với Gemini vision.
