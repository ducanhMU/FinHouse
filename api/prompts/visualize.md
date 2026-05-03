# FinHouse — Visualize Tool Guide
# Hướng dẫn cho LLM khi gọi tools `bar` / `line` / `pie`.
# Chỉ inject vào messages khi `visualize` được bật trong session.
# Nội dung sau dòng `---` được đưa vào LLM. Restart API sau khi sửa.
---
Bạn có 3 tool vẽ biểu đồ. Mỗi tool TỰ ĐỌC dữ liệu từ MỘT bảng OLAP (giống `select_rows`) rồi render PNG, upload, trả về một URL. **Bạn KHÔNG truyền `data_rows`** — tool tự lấy. Khi nhận URL, nhúng vào câu trả lời bằng cú pháp markdown `![<title>](URL)`.

## 🛠️ TOOLS

### `bar(table, x_column, y_columns, filters?, order_by?, limit?, use_final?, title?)`
So sánh giữa các nhóm. `x_column` là nhãn (ticker, năm, quý, ngành); `y_columns` là list cột số. Truyền nhiều `y_columns` → nhiều thanh cùng nhóm (grouped bar).

### `line(table, x_column, y_columns, filters?, order_by?, limit?, use_final?, title?)`
Trend theo thời gian. **Luôn** truyền `order_by` cột thời gian theo `asc` — tool không tự sort. Nhiều `y_columns` → nhiều đường trên cùng trục.

### `pie(table, label_column, value_column, filters?, order_by?, limit?, use_final?, title?)`
Cơ cấu / share-of-whole. `label_column` là nhãn lát, `value_column` là cột số (1 cột duy nhất). Tool tự bỏ giá trị âm/null.

Tham số `filters`, `order_by`, `use_final`, `limit` có cùng ý nghĩa và schema như `select_rows`.

## 🎯 CHỌN LOẠI BIỂU ĐỒ

| Câu hỏi user | Tool |
|---|---|
| "cơ cấu", "tỷ trọng", "ai sở hữu", "share of …" | `pie` |
| "so sánh A vs B", "top N", "xếp hạng" | `bar` |
| "qua các năm", "theo thời gian", "trend", "diễn biến" | `line` |

**KHÔNG dùng `pie` khi**: các giá trị không cộng lại thành 100% (so doanh thu của 5 công ty khác nhau → bar). Quá nhiều slice (>7) → bar ngang đẹp hơn.

**KHÔNG vẽ chart** cho 1 con số đơn lẻ (1 doanh thu của 1 năm) — trả lời bằng văn bản.

## ⛔ QUY TẮC

- Title bằng tiếng Việt (nếu user nói tiếng Việt) — ngắn, mô tả đối tượng + chỉ số + mốc thời gian. Ví dụ: `"ROE 2024 — Top 5 ngân hàng"`.
- Khi tool trả `error` (cột non-numeric, 0 row, …): nói lý do ngắn cho user, KHÔNG bịa URL.
- Khi tool trả `url`: nhúng `![<title>](url)` ngay cạnh đoạn diễn giải số liệu, KHÔNG đặt riêng cuối cùng.
- Vẫn diễn giải bằng văn bản — chart minh hoạ, text mới là nội dung chính.

## ✅ VÍ DỤ

### Cơ cấu cổ đông HPG → `pie`

```jsonc
pie({
  "table": "shareholders",
  "label_column": "share_holder",
  "value_column": "share_own_percent",
  "filters": [{"column": "symbol", "op": "=", "value": "HPG"}],
  "order_by": [{"column": "share_own_percent", "dir": "desc"}],
  "limit": 6,
  "title": "Cơ cấu cổ đông Hoà Phát (HPG)"
})
```

### So sánh ROE 2024 giữa 5 ngân hàng → `bar`

```jsonc
bar({
  "table": "financial_ratios",
  "x_column": "symbol",
  "y_columns": ["roe"],
  "filters": [
    {"column": "symbol",  "op": "IN", "value": ["VCB","TCB","MBB","ACB","BID"]},
    {"column": "year",    "op": "=",  "value": 2024},
    {"column": "quarter", "op": "=",  "value": 0}
  ],
  "order_by": [{"column": "roe", "dir": "desc"}],
  "title": "ROE 2024 — 5 ngân hàng lớn"
})
// Khi diễn giải: nhân roe * 100 và làm tròn 2 chữ số.
```

### Doanh thu + LN ròng VNM theo năm → `line` (multi-series)

```jsonc
line({
  "table": "income_statement",
  "x_column": "year",
  "y_columns": ["revenue", "net_profit"],
  "filters": [
    {"column": "symbol",  "op": "=", "value": "VNM"},
    {"column": "quarter", "op": "=", "value": 0}
  ],
  "order_by": [{"column": "year", "dir": "asc"}],
  "limit": 8,
  "title": "Vinamilk — Doanh thu & LN ròng theo năm (VND)"
})
```

### Yêu cầu nhiều biểu đồ trong 1 lượt user → gọi nhiều tool song song

User: *"Cho mình tổng quan HPG: cơ cấu cổ đông và xu hướng doanh thu 5 năm gần nhất."*

Gọi 2 tool **song song** trong CÙNG một lượt assistant:

```jsonc
pie({
  "table": "shareholders",
  "label_column": "share_holder",
  "value_column": "share_own_percent",
  "filters": [{"column": "symbol", "op": "=", "value": "HPG"}],
  "order_by": [{"column": "share_own_percent", "dir": "desc"}],
  "limit": 6,
  "title": "Cơ cấu cổ đông HPG"
})

line({
  "table": "income_statement",
  "x_column": "year",
  "y_columns": ["revenue"],
  "filters": [
    {"column": "symbol",  "op": "=", "value": "HPG"},
    {"column": "quarter", "op": "=", "value": 0}
  ],
  "order_by": [{"column": "year", "dir": "asc"}],
  "limit": 5,
  "title": "HPG — Doanh thu 5 năm gần nhất (VND)"
})
```

## ⚠️ CHƯA HỖ TRỢ (NÓI THẲNG VỚI USER)

Các tool hiện tại **không** làm được:

- Aggregate (SUM/AVG/GROUP BY) bên trong chart. Nếu cần, lấy data thô bằng `select_rows`/`aggregate` rồi tóm tắt bằng văn bản — đừng cố vẽ.
- Time bucketing (gộp ngày → tháng/quý). Chart `line` vẽ giá trị thẳng từ cột thời gian có sẵn (`year`, `quarter`, `time`).
- Kết hợp dữ liệu từ nhiều bảng vào 1 chart.
- Scatter / area / histogram.

Khi user yêu cầu một trong các tình huống này, làm 1 trong 2:
1. Vẽ phần đơn giản nhất bạn có thể vẽ + nói rõ phần còn lại chưa hỗ trợ.
2. Giải thích bằng văn bản (kèm số liệu lấy từ `select_rows`/`aggregate`) và đề nghị user chia nhỏ yêu cầu.
