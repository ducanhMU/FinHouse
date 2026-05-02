# FinHouse — Database Query Tool Guide
# Hướng dẫn cho LLM khi gọi tool `database_query` (ClickHouse OLAP).
# Chỉ inject vào messages khi tool `database_query` được bật.
# Nội dung sau dòng `---` được đưa vào LLM. Restart API sau khi sửa.
---
Bạn đang được trang bị tool **`database_query(sql)`** để chạy SQL READ-ONLY trên ClickHouse OLAP (database tên là `olap`). Hãy đọc kỹ phần mô tả schema dưới đây TRƯỚC KHI viết SQL — không cần `SHOW TABLES` / `DESCRIBE TABLE` lại trừ khi bạn nghi ngờ schema đã thay đổi.

## NGUYÊN TẮC CHUNG

1. **Chỉ SELECT / WITH / SHOW / DESCRIBE / EXPLAIN.** Mọi DDL/DML đều bị reject.
2. **Một câu lệnh duy nhất**, không dùng `;` ở giữa.
3. **LIMIT tự động** được thêm vào nếu thiếu — nhưng nên tự đặt LIMIT phù hợp (10–100 thường là đủ cho bảng tổng hợp; 5–10 cho top-N).
4. **ReplacingMergeTree** là engine chính: mỗi row có thể có nhiều version. Để lấy bản mới nhất, thêm `FINAL` sau tên bảng:
   ```sql
   SELECT * FROM olap.balance_sheet FINAL WHERE symbol='VNM' AND year=2024 AND quarter=0
   ```
   Không dùng `FINAL` thì có thể trả về row cũ. Với bảng append-only (`stock_intraday`, `stock_price_history`, `news`, `events`, `_ingestion_log`, `update_log`) thì không cần `FINAL`.
5. **Tên cột nhạy chữ hoa/thường** — luôn snake_case như trong schema.
6. **Tiền tệ**: tất cả số liệu tài chính trong các bảng `balance_sheet`, `cash_flow_statement`, `income_statement` đều là **VND nguyên (đồng)** — chia 1e9 để ra "tỷ đồng" khi trình bày. `market_cap_billions`, `ebitda_billions`, `ebit_billions` đã ở đơn vị **tỷ đồng**.
7. **Phần trăm**: các trường `*_percent`, `*_margin`, `roe`, `roa`, `roic`, `revenue_growth`, `profit_growth` thường ở dạng số thập phân (ví dụ 0.1523 = 15.23%). Khi hiển thị, nhân 100 và làm tròn 2 chữ số.
8. **Quý vs năm**: cột `quarter`:
   - `quarter = 0` → dữ liệu **cả năm** (annual). Đây là default khi user chỉ hỏi "năm 2024".
   - `quarter ∈ {1,2,3,4}` → dữ liệu **quý** đó. Cộng 4 quý KHÔNG bằng năm vì có chỉnh hợp nhất.
   - Cột `period` thường là chuỗi hệ thống VCI (`'Y'`, `'Q'`); luôn lọc kèm `year` và `quarter` cho chắc.
9. **Symbol vs ticker**: bảng `stocks` dùng cột `ticker`; mọi bảng tài chính (`balance_sheet`, `income_statement`, …) và bảng cổ đông/sự kiện dùng cột `symbol`. Hai cột này tham chiếu cùng mã chứng khoán nhưng tên khác.

## DANH SÁCH BẢNG (database `olap`)

### Tham chiếu (master data)

| Bảng | Mục đích | Cột chính |
|---|---|---|
| `stocks` | Danh mục mã chứng khoán | `ticker` (PK), `organ_name`, `en_organ_name`, `organ_short_name`, `com_type_code`, `status` (`'listed'`/`'delisted'`), `listed_date` (String), `delisted_date` (String), `company_id`, `tax_code`, `isin` |
| `exchanges` | HOSE, HNX, UPCOM... | `exchange` (PK), `exchange_name`, `exchange_code` |
| `indices` | VN30, VNINDEX, HNX30... | `index_code` (PK), `index_name`, `description`, `group_name`, `index_id`, `sector_id` |
| `industries` | Ngành ICB | `icb_code` (PK), `icb_name`, `en_icb_name`, `level` (1–4) |
| `stock_exchange` | Junction ticker ↔ exchange | `ticker`, `exchange`, `type` |
| `stock_index` | Junction ticker ↔ index | `ticker`, `index_code` |
| `stock_industry` | Ngành ICB chi tiết của từng ticker | `ticker`, `icb_code`, `icb_name2..4` (3 cấp), `en_icb_name2..4`, `icb_code1..4` |

### Hồ sơ doanh nghiệp

| Bảng | Mục đích | Cột chính |
|---|---|---|
| `company_overview` | Tổng quan công ty | `symbol` (PK), `issue_share` (cổ phiếu phát hành), `history` (lịch sử dạng text), `company_profile` (mô tả dạng text), `icb_name2/3/4` (ngành 3 cấp), `charter_capital` (vốn điều lệ, VND) |
| `officers` | Ban lãnh đạo | `id`, `symbol`, `officer_name`, `officer_position`, `officer_own_percent` (0–100, %), `quantity` (số CP nắm giữ), `status` (`'working'`/`'left'`) |
| `shareholders` | Cổ đông lớn | `id`, `symbol`, `share_holder` (tên cổ đông), `quantity`, `share_own_percent` (0–100, %), `update_date` (String) |
| `subsidiaries` | Công ty con / liên kết | `id`, `symbol`, `sub_organ_code`, `ownership_percent` (0–100, %), `organ_name`, `type` |

### Báo cáo tài chính (FINAL khuyên dùng)

Tất cả 4 bảng dưới đây có cùng PK logic `(symbol, period, year, quarter)`. Đơn vị: **VND nguyên đồng** trừ khi ghi chú khác.

| Bảng | Mục đích | Cột tiêu biểu |
|---|---|---|
| `balance_sheet` | Bảng cân đối kế toán | `total_assets`, `asset_current`, `cash_and_equivalents`, `inventory`, `accounts_receivable`, `fixed_assets`, `liabilities_total`, `liabilities_current`, `equity_total`, `share_capital`, `retained_earnings`, `total_equity_and_liabilities`, `data_json` (raw VCI JSON) |
| `income_statement` | Kết quả kinh doanh | `revenue`, `revenue_growth` (decimal), `net_revenue`, `cost_of_goods_sold`, `gross_profit`, `financial_income`, `financial_expense`, `operating_expenses`, `operating_profit`, `profit_before_tax`, `corporate_income_tax`, `net_profit`, `net_profit_parent_company`, `profit_growth` (decimal), `eps` (VND/cp) |
| `cash_flow_statement` | Lưu chuyển tiền tệ (rất nhiều cột — xem schema) | `net_cash_from_operating_activities`, `net_cash_from_investing_activities`, `net_cash_from_financing_activities`, `net_cash_flow_period`, `cash_and_cash_equivalents_beginning/ending`, `depreciation_fixed_assets`, `dividends_paid`, … |
| `financial_ratios` | Chỉ số tài chính tổng hợp | `price_to_book`, `price_to_earnings`, `price_to_sales`, `eps_vnd`, `bvps_vnd`, `market_cap_billions` (tỷ đồng), `shares_outstanding_millions`, `ev_to_ebitda`, `debt_to_equity`, `roe`, `roa`, `roic`, `gross_margin`, `net_profit_margin`, `ebit_margin`, `ebitda_billions`, `ebit_billions`, `current_ratio`, `quick_ratio`, `cash_ratio`, `interest_coverage_ratio`, `dividend_payout_ratio`, `beta`, `inventory_turnover`, `days_*_outstanding`, `cash_conversion_cycle` |
| `financial_reports` | Wrapper raw (3 statement gộp dạng JSON) | `report_type` (`'balance_sheet'`/`'income_statement'`/`'cash_flow'`), `data_json` |

### Sự kiện & tin tức

| Bảng | Mục đích | Cột chính |
|---|---|---|
| `events` | Sự kiện doanh nghiệp (chia tách, cổ tức, ĐHCĐ...) | `id`, `symbol`, `event_title`, `event_list_code`, `event_list_name`, `ratio`, `value`, `public_date`, `record_date`, `exright_date`, `issue_date` (tất cả các *_date là **String**, không phải Date) |
| `news` | Tin tức | `id`, `symbol`, `news_title`, `news_short_content`, `news_full_content`, `news_source_link`, `public_date` (**Int64 epoch ms** — chuyển bằng `toDateTime(public_date / 1000)`), `close_price`, `price_change_pct` (decimal) |
| `stock_dividend` | Cổ tức bằng cổ phiếu | `symbol`, `record_date` (Date), `payment_date` (Date), `exercise_rate` (tỷ lệ), `plan_volume`, `issue_volume`, `dividend_year`, `duration` |
| `cash_dividend` | Cổ tức tiền mặt | `symbol`, `record_date`, `payment_date`, `exercise_rate`, `dps` (Dividend Per Share, VND), `currency`, `dividend_year`, `duration` |

### Giá cổ phiếu

| Bảng | Mục đích | Cột chính |
|---|---|---|
| `stock_intraday` | Tick data trong phiên (append-only) | `symbol`, `time` (DateTime), `price`, `volume`, `accumulated_val`, `accumulated_vol`, `match_type`. Partition theo `toYYYYMM(time)`. |
| `stock_price_history` | OHLCV theo ngày | `symbol`, `time` (Date), `open`, `high`, `low`, `close`, `volume`. Partition theo `toYYYYMM(time)`. |

### Logs (ít dùng cho câu hỏi user)

- `_ingestion_log` — log Spark ingest. TTL 90 ngày.
- `update_log` — bảo trì.

## PATTERN PHỔ BIẾN

### Lấy báo cáo cả năm gần nhất của 1 mã

```sql
SELECT symbol, year, revenue, net_profit, eps
FROM olap.income_statement FINAL
WHERE symbol = 'VNM' AND quarter = 0
ORDER BY year DESC
LIMIT 5
```

### Lấy báo cáo theo quý liên tục

```sql
SELECT symbol, year, quarter, revenue, net_profit
FROM olap.income_statement FINAL
WHERE symbol = 'FPT' AND quarter > 0
ORDER BY year DESC, quarter DESC
LIMIT 8
```

### Cơ cấu cổ đông (PIE-friendly)

```sql
SELECT share_holder, share_own_percent
FROM olap.shareholders FINAL
WHERE symbol = 'HPG'
ORDER BY share_own_percent DESC
LIMIT 10
```

### So sánh ROE giữa nhiều công ty cùng năm (BAR-friendly)

```sql
SELECT symbol, roe * 100 AS roe_pct
FROM olap.financial_ratios FINAL
WHERE symbol IN ('VNM','FPT','HPG','MWG','VCB') AND year = 2024 AND quarter = 0
ORDER BY roe_pct DESC
```

### Trend giá đóng cửa theo tháng (LINE-friendly)

```sql
SELECT toStartOfMonth(time) AS month, avg(close) AS avg_close
FROM olap.stock_price_history
WHERE symbol = 'VNM' AND time >= '2024-01-01'
GROUP BY month
ORDER BY month
```

### Top mã theo vốn hoá ngành ngân hàng

```sql
SELECT s.ticker, s.organ_name, fr.market_cap_billions
FROM olap.stocks AS s
INNER JOIN olap.stock_industry AS si ON s.ticker = si.ticker
INNER JOIN olap.financial_ratios FINAL AS fr ON fr.symbol = s.ticker
WHERE si.icb_name2 = 'Ngân hàng' AND fr.year = 2024 AND fr.quarter = 0
ORDER BY fr.market_cap_billions DESC
LIMIT 10
```

### Tin tức gần nhất của 1 mã

```sql
SELECT news_title, toDateTime(public_date / 1000) AS published_at, news_source_link
FROM olap.news
WHERE symbol = 'VIC'
ORDER BY public_date DESC
LIMIT 10
```

## SAI LẦM HAY GẶP — TRÁNH

- ❌ Quên `FINAL` → dữ liệu trùng/cũ (đặc biệt khi join nhiều bảng tài chính).
- ❌ Hỏi "doanh thu năm 2024" mà filter `quarter > 0` → ra số quý, cộng lại sai.
- ❌ Filter `period = '2024'` — `period` là enum nội bộ (`'Y'`/`'Q'`), không phải năm. Luôn dùng `year = 2024`.
- ❌ Coi `roe` đã ở dạng %  — thực tế là decimal, cần `* 100`.
- ❌ JOIN bảng tài chính với bảng tài chính khác mà không match cả `(symbol, year, quarter)` → bùng số dòng.
- ❌ Hỏi "ngành ngân hàng" rồi filter `icb_name = '...'` — bảng `stock_industry` có 3 cấp (`icb_name2`, `icb_name3`, `icb_name4`); chọn cấp phù hợp với câu hỏi (cấp 2 thường = ngành lớn).
- ❌ Format `news.public_date` thành Date trực tiếp — nó là epoch ms, dùng `toDateTime(public_date / 1000)`.

## KHI NÀO KHÔNG DÙNG TOOL NÀY

- Câu hỏi định nghĩa khái niệm thuần ("EBITDA là gì?") — trả lời từ kiến thức, không cần SQL.
- Tin tức / sự kiện sau ngày cutoff training và không có trong bảng `news` / `events` — dùng `web_search` thay thế.
- Câu hỏi về tài liệu nội bộ đã có trong RAG context — ưu tiên trích dẫn [1], [2] từ context, không SQL lại.
