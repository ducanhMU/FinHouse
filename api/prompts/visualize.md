# FinHouse — Visualize Tool Guide
# Hướng dẫn chọn loại biểu đồ và set tham số đúng cho tool `visualize`.
# Chỉ inject vào messages khi tool `visualize` được bật.
# Nội dung sau dòng `---` được đưa vào LLM. Restart API sau khi sửa.
---
Bạn có tool **`visualize(data_rows, mark, x_field, y_field, color_field, title)`** để render biểu đồ PNG từ kết quả `database_query`. Tool trả về một URL — luôn nhúng URL vào câu trả lời bằng cú pháp markdown `![<title>](URL)`.

## QUY TẮC GỌI TOOL

1. **Luôn gọi SAU `database_query`**. `data_rows` là list các dict — chuyển từ `{columns, rows}` của `database_query` thành `[{col1: val, col2: val, ...}, ...]`.
2. **`x_field`, `y_field` phải khớp đúng tên cột** đã trả về từ SQL (snake_case, đúng chữ hoa/thường).
3. **`y_field` PHẢI là số** (Float/Int). Tool sẽ ép `float()`; nếu cột chuỗi sẽ lỗi.
4. **Chỉ vẽ khi có ≥ 2 row**. 1 row → không có gì để so sánh, hãy trả lời bằng văn bản.
5. **Title bằng tiếng Việt** (hoặc tiếng Anh nếu user đang nói tiếng Anh) — ngắn gọn, mô tả rõ chỉ số + đối tượng + mốc thời gian. Ví dụ: `"ROE 2024 — Top 5 ngân hàng"`.
6. **KHÔNG vẽ chart cho số đơn lẻ** (1 doanh thu của 1 năm) — nói thẳng bằng số.

## CHỌN LOẠI BIỂU ĐỒ (`mark`)

### `pie` — Cơ cấu / share-of-whole

**Chỉ dùng khi**: tổng của `y_field` thực sự đại diện cho **100%** (hoặc xấp xỉ) — tức cột là **tỷ trọng** / **phần trăm** / **thành phần của một tổng**.

**Use case điển hình**:
- Cơ cấu cổ đông (`shareholders.share_own_percent`).
- Cơ cấu vốn (vốn chủ vs nợ — 2 lát).
- Cơ cấu tài sản (current vs non-current).
- Tỷ trọng doanh thu theo phân khúc / công ty con.
- Tỷ lệ sở hữu của ban lãnh đạo.

**Không dùng pie khi**:
- So sánh các đại lượng tuyệt đối không cộng lại thành 100% (doanh thu của 5 công ty khác nhau → BAR).
- Trend theo thời gian (luôn LINE/BAR).
- Có quá nhiều slice (>7) — pie thành rối; chuyển sang BAR ngang hoặc gộp các slice nhỏ vào "Khác".
- Các giá trị âm (pie không biểu diễn được số âm).

### `bar` — So sánh đại lượng giữa các nhóm

**Dùng khi**:
- So sánh chỉ số (doanh thu, lợi nhuận, ROE…) giữa **nhiều công ty** trong cùng kỳ.
- So sánh chỉ số giữa **nhiều ngành**.
- Top-N rankings (top 10 vốn hoá, top 5 ROE…).
- So sánh **một công ty qua các quý** khi số kỳ ≤ 8 (>8 thì LINE đẹp hơn).

`x_field` = nhãn nhóm (ticker, tên ngành, quý), `y_field` = giá trị số.

### `line` — Xu hướng theo thời gian

**Dùng khi**:
- 1 chỉ số theo **chuỗi thời gian dài** (giá đóng cửa theo ngày, doanh thu theo quý qua nhiều năm).
- Trend của 1 đại lượng — dùng line để nhấn mạnh chuyển động lên/xuống.

`x_field` PHẢI là cột thời gian đã sắp xếp (year, quarter_label, month, date). SẮP `ORDER BY` trong SQL trước khi vẽ — tool không tự sort.

### `area` — Trend kèm cảm giác khối lượng

Như `line` nhưng tô vùng dưới đường — dùng khi muốn nhấn cumulative/khối lượng (volume giao dịch theo thời gian, EBITDA tích lũy).

### `scatter` — Quan hệ giữa 2 chỉ số định lượng

**Dùng khi** cả `x_field` và `y_field` đều là số và bạn muốn xem **tương quan** — ví dụ:
- ROE vs P/E của các công ty trong ngành.
- Vốn hoá vs Tăng trưởng doanh thu.

### `hist` — Phân phối của 1 đại lượng

**Dùng khi** muốn xem distribution của một chỉ số trên một tập mã — ví dụ phân phối P/E của các mã VN30. `y_field` chính là cột số cần phân phối.

## CHEAT SHEET — TỪ CÂU HỎI USER → CHỌN MARK

| Cụm từ trong câu hỏi | Mark phù hợp |
|---|---|
| "cơ cấu", "tỷ trọng", "share", "phần trăm chiếm", "ai sở hữu" | `pie` |
| "so sánh A vs B", "top 5", "xếp hạng", "nhóm nào cao nhất" | `bar` |
| "qua các năm", "theo thời gian", "trend", "diễn biến", "lịch sử giá" | `line` |
| "tăng trưởng tích lũy", "cumulative", "volume theo ngày" | `area` hoặc `bar` |
| "quan hệ giữa X và Y", "có tương quan không" | `scatter` |
| "phân phối", "distribution của" | `hist` |

## VÍ DỤ ĐẦY ĐỦ

### Cơ cấu cổ đông HPG → PIE

SQL trước:
```sql
SELECT share_holder, share_own_percent
FROM olap.shareholders FINAL
WHERE symbol = 'HPG' AND share_own_percent IS NOT NULL
ORDER BY share_own_percent DESC LIMIT 6
```
Gọi tool:
```json
{
  "data_rows": [
    {"share_holder": "Trần Đình Long", "share_own_percent": 25.81},
    {"share_holder": "Vũ Thị Hiền", "share_own_percent": 7.34},
    ...
  ],
  "mark": "pie",
  "x_field": "share_holder",
  "y_field": "share_own_percent",
  "title": "Cơ cấu cổ đông Hoà Phát (HPG)"
}
```

### So sánh ROE 2024 giữa 5 ngân hàng → BAR

```json
{
  "data_rows": [
    {"symbol": "VCB", "roe_pct": 19.2},
    {"symbol": "TCB", "roe_pct": 15.8},
    {"symbol": "MBB", "roe_pct": 21.4},
    {"symbol": "ACB", "roe_pct": 22.1},
    {"symbol": "BID", "roe_pct": 16.7}
  ],
  "mark": "bar",
  "x_field": "symbol",
  "y_field": "roe_pct",
  "title": "ROE 2024 — 5 ngân hàng lớn (%)"
}
```

### Doanh thu VNM theo quý → LINE

```json
{
  "data_rows": [
    {"period_label": "2023Q1", "revenue_bn": 13954.2},
    {"period_label": "2023Q2", "revenue_bn": 15197.1},
    ... (8–12 quý)
  ],
  "mark": "line",
  "x_field": "period_label",
  "y_field": "revenue_bn",
  "title": "Doanh thu thuần Vinamilk theo quý (tỷ VND)"
}
```

## SAU KHI TOOL TRẢ VỀ URL

- Nhúng vào câu trả lời: `![Cơ cấu cổ đông HPG](https://...)` ngay TRƯỚC hoặc SAU đoạn diễn giải số liệu, KHÔNG cuối cùng riêng lẻ.
- Vẫn diễn giải bằng văn bản — chart minh hoạ, text mới là nội dung chính.
- Không bịa URL; chỉ dùng URL tool vừa trả.
- Nếu tool trả `error` → giải thích ngắn cho user (lý do thường là cột non-numeric, data rỗng, hoặc tên cột sai) và **không** cố nhúng URL không có.
