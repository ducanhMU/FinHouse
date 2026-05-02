# FinHouse — Database Query Tool Guide
# Hướng dẫn cho LLM khi gọi tool `database_query` (ClickHouse OLAP).
# Chỉ inject vào messages khi tool `database_query` được bật.
# Nội dung sau dòng `---` được đưa vào LLM. Restart API sau khi sửa.
---
Bạn đang được trang bị tool **`database_query(sql)`** để chạy SQL READ-ONLY trên ClickHouse OLAP (database tên là `olap`). Toàn bộ schema bạn cần đã có ngay trong file này — đọc một lượt rồi đi thẳng vào `SELECT`.

## ✅ WHITELIST BẢNG (chỉ những bảng dưới đây tồn tại — không có ngoại lệ)

```
olap.stocks                olap.exchanges             olap.indices
olap.industries            olap.stock_exchange        olap.stock_index
olap.stock_industry        olap.company_overview      olap.officers
olap.shareholders          olap.subsidiaries          olap.balance_sheet
olap.income_statement      olap.cash_flow_statement   olap.financial_ratios
olap.financial_reports     olap.events                olap.news
olap.stock_dividend        olap.cash_dividend         olap.stock_intraday
olap.stock_price_history   olap._ingestion_log        olap.update_log
```

**Mọi tên bảng khác đều KHÔNG TỒN TẠI.** Trước khi viết `FROM ...`, đối chiếu với danh sách trên. Nếu định gõ một tên bảng không có trong whitelist này — DỪNG, đọc lại bản đồ câu hỏi → bảng ở dưới, hoặc chuyển sang `web_search`.

## 🎯 FIRST-CALL PATTERN (đọc kỹ — đây là chỗ hay sai nhất)

Khi user hỏi về 1 công ty (system hint có `Thực thể: <TICKER>` hoặc tên công ty), **lượt SQL đầu tiên** của bạn phải đi thẳng vào bảng dữ liệu mục tiêu, KHÔNG dò bảng:

- **Không bao giờ** chạy `SHOW TABLES LIKE '%<TICKER>%'`. **Ticker là GIÁ TRỊ trong cột `symbol`/`ticker`, KHÔNG phải tên bảng.** `'HPG'`, `'VNM'`, `'FPT'` không xuất hiện trong tên bảng — chúng nằm trong dữ liệu.
- **Không bao giờ** chạy `SHOW TABLES`, `DESCRIBE TABLE`, `EXISTS TABLE`. Schema đã có sẵn ở dưới.
- **Không bao giờ** chạy `SELECT * FROM <bảng>` không kèm `WHERE symbol = '<TICKER>'` — bảng tài chính có hàng triệu row, sẽ timeout / vô nghĩa.

Mẫu chuẩn theo loại câu hỏi (1 câu SQL = 1 kết quả dùng được):

```sql
-- (A) Hỏi tổng quan công ty / xác nhận tồn tại + lấy hồ sơ
SELECT symbol, issue_share, charter_capital, icb_name2, icb_name3, icb_name4,
       company_profile, history
FROM olap.company_overview FINAL
WHERE symbol = '<TICKER>' LIMIT 1

-- (B) Hỏi báo cáo tài chính cả năm
SELECT year, revenue, gross_profit, operating_profit, net_profit, eps
FROM olap.income_statement FINAL
WHERE symbol = '<TICKER>' AND quarter = 0
ORDER BY year DESC LIMIT 5

-- (C) Hỏi chỉ số tài chính (ROE, P/E, market cap...)
SELECT year, roe, roa, price_to_earnings, price_to_book,
       market_cap_billions, debt_to_equity
FROM olap.financial_ratios FINAL
WHERE symbol = '<TICKER>' AND quarter = 0
ORDER BY year DESC LIMIT 5
```

Khi câu hỏi user là "tổng quan", chạy **song song nhiều `database_query`** trong cùng 1 lượt (mẫu A + B + C cùng lúc), không tuần tự.

## ⛔ BẮT BUỘC ĐỌC TRƯỚC KHI GỌI TOOL

**KHÔNG được làm:**
- ❌ **TUYỆT ĐỐI KHÔNG DÙNG `JOIN`** (INNER / LEFT / RIGHT / FULL / CROSS / ASOF / bất kỳ biến thể nào). Lý do: nhiều bảng có cột trùng tên (`symbol`, `ticker`, `year`, `quarter`, `icb_name2/3/4`, `organ_name`, `update_date`, `data_json`, `source`…) — ClickHouse sẽ báo lỗi `AMBIGUOUS_COLUMN_NAME` hoặc trả kết quả sai do nhân row. **Cách đúng:** `SELECT` từng bảng riêng (mỗi bảng một câu `database_query`, gọi **song song** trong cùng 1 lượt assistant), rồi tự tổng hợp kết quả khi viết câu trả lời. Nếu cần lọc bảng B theo kết quả bảng A, dùng subquery `WHERE col IN (SELECT … FROM A)` thay vì JOIN.
- ❌ Gọi `SHOW TABLES`, `DESCRIBE TABLE`, `EXISTS TABLE` để "dò" schema. Schema đầy đủ đã liệt kê dưới — coi đây là source of truth. Mỗi lượt tool gọi là một roundtrip tốn thời gian, đừng lãng phí vào việc đã biết.
- ❌ **`SHOW TABLES LIKE '%<TICKER>%'` là sai 2 lớp**: (1) đang đi dò bảng (đã cấm ở trên), (2) ticker là GIÁ TRỊ trong cột `symbol`/`ticker`, KHÔNG bao giờ xuất hiện trong tên bảng. Câu này luôn trả 0 row và là dấu hiệu bạn không hiểu data model. Thay bằng `SELECT ... FROM olap.company_overview FINAL WHERE symbol = '<TICKER>'`.
- ❌ Bịa tên bảng/cột ngoài danh sách. Sai lầm thường gặp:
  - `finance_data`, `financial_data`, `finance_annual`, `company_financials`, `company_info`, `stock_data` → **KHÔNG TỒN TẠI**. Báo cáo tài chính nằm ở `income_statement` / `balance_sheet` / `cash_flow_statement` / `financial_ratios`. Hồ sơ công ty ở `company_overview`.
  - `company = 'HDB'` → **sai cột**. Bảng tài chính dùng `symbol`, bảng `stocks` dùng `ticker`.
  - `name`, `company_name` → không có. Tên công ty ở `stocks.organ_name` hoặc `company_overview` (qua `symbol`).
- ❌ Khi 1 query lỗi vì sai bảng, **TUYỆT ĐỐI KHÔNG** gợi ý cho user "tôi sẽ thử bảng X / bảng Y" với tên bảng do bạn tự bịa. Chỉ được nêu tên bảng nằm trong WHITELIST ở trên. Nếu không có bảng phù hợp → nói thẳng "DB không có dữ liệu này" và chuyển `web_search`.
- ❌ Tự đổi mốc thời gian sang năm khác với system hint. Nếu hint nói `Mốc thời gian: 2025` thì WHERE phải là `year = 2025` — không lùi về 2021–2023 "cho an toàn".
- ❌ Quên `FINAL` trên bảng ReplacingMergeTree (mọi bảng tài chính + master data trừ append-only) → trả về row cũ.

**PHẢI làm:**
- ✅ Đọc system hint (`Mốc thời gian: ...`, `Thực thể: ...`, `Đã xác minh trong DB: ...`) và ÁP THẲNG vào WHERE clause.
- ✅ Một lượt tool = một câu `SELECT` có kết quả dùng được, không phải một bước thăm dò.
- ✅ Khi cần nhiều mặt dữ liệu (ví dụ "tổng quan công ty"), gọi song song nhiều `database_query` trong CÙNG một lượt assistant, mỗi câu nhắm 1 bảng cụ thể.

## 🗺️ BẢN ĐỒ CÂU HỎI → BẢNG (đọc kỹ — đây là phần hay sai)

| Loại thông tin user hỏi | Bảng cần query | Cột định danh | Ghi chú |
|---|---|---|---|
| Hồ sơ / mô tả công ty, vốn điều lệ, ngành ICB | `company_overview` FINAL | `symbol` | có `company_profile`, `history`, `icb_name2/3/4`, `charter_capital` |
| Ban lãnh đạo (CEO, Chủ tịch…) | `officers` FINAL | `symbol` | lọc `status='working'` để lấy người đương nhiệm |
| Cổ đông lớn, cơ cấu sở hữu | `shareholders` FINAL | `symbol` | `share_own_percent` đã ở dạng % (0–100) |
| Công ty con / liên kết | `subsidiaries` FINAL | `symbol` | |
| Doanh thu, lợi nhuận, EPS, biên lợi nhuận thô | `income_statement` FINAL | `symbol`, `year`, `quarter` | đơn vị **VND nguyên** |
| Tài sản, nợ, vốn chủ sở hữu | `balance_sheet` FINAL | `symbol`, `year`, `quarter` | đơn vị **VND nguyên** |
| Dòng tiền (CFO/CFI/CFF) | `cash_flow_statement` FINAL | `symbol`, `year`, `quarter` | đơn vị **VND nguyên** |
| ROE, ROA, P/E, P/B, D/E, market cap, EBITDA, biên lợi nhuận | `financial_ratios` FINAL | `symbol`, `year`, `quarter` | tỷ số ở dạng decimal (×100 để ra %); `market_cap_billions` đã là tỷ đồng |
| Cổ tức tiền mặt | `cash_dividend` FINAL | `symbol` | |
| Cổ tức bằng cổ phiếu, chia tách | `stock_dividend` FINAL | `symbol` | |
| Sự kiện DN (ĐHCĐ, phát hành, chia tách…) | `events` FINAL | `symbol` | các cột `*_date` là String, không phải Date |
| Tin tức | `news` | `symbol` | append-only, KHÔNG `FINAL`; `public_date` là epoch ms |
| Giá đóng cửa lịch sử (OHLCV theo ngày) | `stock_price_history` | `symbol`, `time` (Date) | append-only, KHÔNG `FINAL` |
| Tick trong phiên | `stock_intraday` | `symbol`, `time` (DateTime) | append-only |
| Danh mục mã, tên đầy đủ, sàn niêm yết | `stocks` FINAL | `ticker` (KHÔNG `symbol`) | KHÔNG JOIN với `company_overview`. Cần cả hai → SELECT 2 câu riêng (1 từ `stocks` filter `ticker='X'`, 1 từ `company_overview` filter `symbol='X'`) rồi tự ghép. |
| Ngành ICB chi tiết của 1 mã | `stock_industry` FINAL | `ticker` | có `icb_name2/3/4` (3 cấp) |

> **Quy tắc nhanh về `quarter`**: hỏi "năm 2025" → `quarter = 0` (cả năm). Hỏi "Q3/2025" → `quarter = 3`. Đừng cộng 4 quý ra năm.

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

## DANH SÁCH BẢNG & GIẢI NGHĨA TỪNG CỘT (database `olap`)

> Đọc kỹ phần này — mọi cột bạn cần đã có ngữ nghĩa ở đây. Đừng đoán cột.

### 1. `stocks` — Danh mục mã chứng khoán (FINAL)

Master list của mọi ticker đang/đã niêm yết. **Cột định danh là `ticker`, KHÔNG phải `symbol`.**

| Cột | Kiểu | Ý nghĩa |
|---|---|---|
| `ticker` | String (PK) | Mã chứng khoán, ví dụ `'HPG'`, `'VNM'`, `'FPT'`. |
| `organ_name` | String | Tên công ty đầy đủ tiếng Việt, ví dụ `'Công ty Cổ phần Tập đoàn Hoà Phát'`. |
| `en_organ_name` | String | Tên tiếng Anh. |
| `organ_short_name` / `en_organ_short_name` | String | Tên rút gọn (Hoà Phát, Hoa Phat Group). |
| `com_type_code` | String | Loại doanh nghiệp (`'CT'` = công ty đại chúng…). |
| `status` | String | `'listed'` = đang niêm yết; `'delisted'` = đã hủy niêm yết. |
| `listed_date` / `delisted_date` | String | Ngày niêm yết / hủy niêm yết (string `'YYYY-MM-DD'`, KHÔNG phải Date). |
| `company_id`, `tax_code`, `isin` | String | ID nội bộ VCI, mã số thuế, ISIN quốc tế. |

**Khi dùng:**
- User hỏi "tên đầy đủ của X là gì", "X niêm yết khi nào", "X có còn niêm yết không".
- Tra ticker từ tên: "Vinamilk có mã gì?" → search `organ_name`.
- Lọc theo trạng thái niêm yết (`status = 'listed'`).

**Mẫu:**
```sql
-- Tra mã từ tên
SELECT ticker, organ_name, listed_date, status
FROM olap.stocks FINAL
WHERE positionCaseInsensitive(coalesce(organ_name,''), 'Vinamilk') > 0
   OR positionCaseInsensitive(coalesce(organ_short_name,''), 'Vinamilk') > 0
LIMIT 5

-- Toàn bộ thông tin định danh 1 mã
SELECT ticker, organ_name, en_organ_name, status, listed_date, isin, tax_code
FROM olap.stocks FINAL
WHERE ticker = 'HPG' LIMIT 1
```

### 2. `company_overview` — Hồ sơ công ty (FINAL) ⭐

**Bảng "first-call" để xác nhận entity và lấy ngữ cảnh chung.**

| Cột | Kiểu | Ý nghĩa |
|---|---|---|
| `symbol` | String (PK) | Ticker (cùng giá trị với `stocks.ticker`). |
| `issue_share` | Int64 | Số cổ phiếu đang lưu hành (đơn vị: cổ phiếu). |
| `financial_ratio_issue_share` | Int64 | Số CP lưu hành theo nguồn financial_ratio (đôi khi khác `issue_share`). |
| `charter_capital` | Int64 | Vốn điều lệ, đơn vị **VND nguyên** — chia 1e9 để ra "tỷ đồng". |
| `history` | String | Đoạn text dài về lịch sử công ty. |
| `company_profile` | String | Đoạn text dài mô tả ngành nghề, sản phẩm, vị thế. |
| `icb_name2` | String | Ngành ICB cấp 2 (lớn) — VD `'Ngân hàng'`, `'Bất động sản'`, `'Tài nguyên cơ bản'`. |
| `icb_name3` | String | Ngành ICB cấp 3 (chi tiết hơn) — VD `'Sắt và Thép'`. |
| `icb_name4` | String | Ngành ICB cấp 4 (chi tiết nhất). |

**Khi dùng:**
- User hỏi "công ty X làm gì", "lịch sử / mô tả / hồ sơ X", "X thuộc ngành nào", "vốn điều lệ X".
- **Bảng "first-call" mặc định** khi câu hỏi tổng quan không nói rõ chỉ số → lấy hồ sơ + ngành làm ngữ cảnh.
- Cần ngành ICB nhanh (cấp 2/3/4) mà không phải query thêm `stock_industry`.

**Mẫu:**
```sql
-- Tổng quan công ty
SELECT symbol, charter_capital, issue_share,
       icb_name2, icb_name3, icb_name4,
       company_profile, history
FROM olap.company_overview FINAL
WHERE symbol = 'HPG' LIMIT 1

-- Lọc các công ty cùng ngành ICB cấp 2
SELECT symbol, icb_name3, charter_capital
FROM olap.company_overview FINAL
WHERE icb_name2 = 'Ngân hàng'
ORDER BY charter_capital DESC LIMIT 20
```

### 3. `income_statement` — Báo cáo KQKD (FINAL) ⭐

PK logic: `(symbol, year, quarter)`. **Đơn vị: VND nguyên đồng** (chia 1e9 → tỷ đồng).

| Cột | Kiểu | Ý nghĩa |
|---|---|---|
| `symbol` | String | Ticker. |
| `period` | String | `'Y'` (cả năm) hoặc `'Q'` (quý). Đừng filter bằng cột này — luôn dùng `year` + `quarter`. |
| `year` | Int32 | Năm tài chính (2020, 2021, …). |
| `quarter` | Int32 | `0` = cả năm; `1`–`4` = quý. **Hỏi "năm 2024" → `quarter = 0`.** |
| `revenue` | Float64 | Tổng doanh thu (gross). |
| `revenue_growth` | Float64 | Tăng trưởng doanh thu YoY, **decimal** (0.15 = 15%). |
| `net_revenue` | Float64 | Doanh thu thuần (sau giảm trừ). Thường nhỏ hơn `revenue` chút. |
| `cost_of_goods_sold` | Float64 | Giá vốn hàng bán (COGS). |
| `gross_profit` | Float64 | Lợi nhuận gộp = `net_revenue` − `cost_of_goods_sold`. |
| `financial_income` | Float64 | Doanh thu hoạt động tài chính (lãi tiền gửi, cổ tức nhận, lãi tỷ giá). |
| `financial_expense` | Float64 | Chi phí tài chính (lãi vay, lỗ tỷ giá). |
| `net_financial_income` | Float64 | Chênh lệch tài chính ròng. |
| `operating_expenses` | Float64 | Chi phí bán hàng + chi phí quản lý DN. |
| `operating_profit` | Float64 | Lợi nhuận thuần từ HĐKD (EBIT cốt lõi). |
| `other_income` | Float64 | Thu nhập khác (thanh lý TS, tiền phạt thu được…). |
| `profit_before_tax` | Float64 | LNTT. |
| `corporate_income_tax` | Float64 | Thuế TNDN hiện hành. |
| `deferred_income_tax` | Float64 | Thuế TNDN hoãn lại. |
| `net_profit` | Float64 | Lợi nhuận sau thuế (LNST tổng). |
| `minority_interest` | Float64 | Phần LN của cổ đông không kiểm soát (consolidated). |
| `net_profit_parent_company` | Float64 | LNST thuộc cổ đông công ty mẹ — **đây là số dùng để tính EPS, ROE**. |
| `net_profit_parent_company_post` | Float64 | LNST thuộc cổ đông mẹ sau điều chỉnh. |
| `profit_growth` | Float64 | Tăng trưởng LNST YoY, **decimal**. |
| `eps` | Float64 | EPS, đơn vị **VND/cổ phiếu**. |
| `data_json` | String | Raw JSON từ VCI (chứa cột chi tiết hơn nếu cần). |
| `source` | String | Nguồn (`'VCI'` mặc định). |

**Khi dùng:**
- User hỏi về **doanh thu, lợi nhuận, EPS, biên LN gộp/ròng, COGS, chi phí, LNTT/LNST, tăng trưởng** của một công ty.
- "X năm 2024 lãi bao nhiêu?", "Doanh thu Q3/2025 của Y", "EPS của Z", "biên gộp của ngành".
- Cần xu hướng nhiều năm/quý để vẽ biểu đồ.

**Mẫu:**
```sql
-- KQKD 5 năm gần nhất (cả năm)
SELECT year, revenue, gross_profit, operating_profit,
       net_profit, net_profit_parent_company, eps
FROM olap.income_statement FINAL
WHERE symbol = 'VNM' AND quarter = 0
ORDER BY year DESC LIMIT 5

-- 8 quý liên tiếp gần nhất
SELECT year, quarter, revenue, net_profit, profit_growth
FROM olap.income_statement FINAL
WHERE symbol = 'FPT' AND quarter > 0
ORDER BY year DESC, quarter DESC LIMIT 8

-- Biên LN gộp 1 năm cụ thể
SELECT symbol, year,
       round(gross_profit / nullIf(net_revenue, 0) * 100, 2) AS gross_margin_pct,
       round(net_profit / nullIf(net_revenue, 0) * 100, 2) AS net_margin_pct
FROM olap.income_statement FINAL
WHERE symbol = 'HPG' AND year = 2024 AND quarter = 0
```

### 4. `balance_sheet` — Bảng cân đối kế toán (FINAL) ⭐

PK: `(symbol, year, quarter)`. **Đơn vị: VND nguyên đồng.** Quy ước `quarter` giống `income_statement`.

| Cột | Ý nghĩa |
|---|---|
| `asset_current` | Tài sản ngắn hạn (tổng). |
| `cash_and_equivalents` | Tiền và tương đương tiền. |
| `short_term_investments` | Đầu tư tài chính ngắn hạn. |
| `accounts_receivable` | Phải thu ngắn hạn. |
| `inventory` | Hàng tồn kho. |
| `current_assets_other` | Tài sản ngắn hạn khác. |
| `asset_non_current` | Tài sản dài hạn (tổng). |
| `long_term_receivables` | Phải thu dài hạn. |
| `fixed_assets` | Tài sản cố định (giá trị còn lại). |
| `long_term_investments` | Đầu tư tài chính dài hạn. |
| `non_current_assets_other` | Tài sản dài hạn khác. |
| `total_assets` | **Tổng tài sản** = ngắn hạn + dài hạn. |
| `liabilities_current` | Nợ ngắn hạn. |
| `liabilities_non_current` | Nợ dài hạn. |
| `liabilities_total` | **Tổng nợ phải trả**. |
| `share_capital` | Vốn điều lệ đã phát hành (vốn cổ phần). |
| `retained_earnings` | Lợi nhuận sau thuế chưa phân phối (lũy kế). |
| `equity_other` | Quỹ khác thuộc vốn chủ sở hữu (thặng dư, quỹ đầu tư phát triển…). |
| `equity_total` | **Vốn chủ sở hữu**. |
| `total_equity_and_liabilities` | Tổng nguồn vốn = `liabilities_total` + `equity_total` ≈ `total_assets`. |
| `data_json` | Raw VCI JSON. |

**Khi dùng:**
- User hỏi về **tài sản, nợ, vốn chủ sở hữu, hàng tồn kho, tiền mặt, TSCĐ, cấu trúc vốn**.
- "Tổng tài sản X", "X có bao nhiêu nợ", "lợi nhuận giữ lại của Y", "X đang có bao nhiêu tiền mặt".
- Tính D/E thủ công khi `financial_ratios.debt_to_equity` thiếu.

**Mẫu:**
```sql
-- Snapshot bảng cân đối cuối năm gần nhất
SELECT year, total_assets, asset_current, cash_and_equivalents,
       inventory, fixed_assets,
       liabilities_total, liabilities_current,
       equity_total, share_capital, retained_earnings
FROM olap.balance_sheet FINAL
WHERE symbol = 'HPG' AND quarter = 0
ORDER BY year DESC LIMIT 1

-- D/E tính thủ công (nếu cần khớp với báo cáo)
SELECT year,
       round(liabilities_total / nullIf(equity_total, 0), 2) AS de_ratio,
       round(asset_current / nullIf(liabilities_current, 0), 2) AS current_ratio
FROM olap.balance_sheet FINAL
WHERE symbol = 'VNM' AND quarter = 0
ORDER BY year DESC LIMIT 5
```

### 5. `cash_flow_statement` — Báo cáo lưu chuyển tiền tệ (FINAL) ⭐

PK: `(symbol, year, quarter)`. Đơn vị VND nguyên. Cấu trúc 3 nhóm: HĐKD (operating) / Đầu tư (investing) / Tài chính (financing).

| Cột | Ý nghĩa |
|---|---|
| `profit_before_tax` | LNTT — điểm xuất phát của bảng gián tiếp. |
| `depreciation_fixed_assets` | Khấu hao TSCĐ. |
| `provision_credit_loss_real_estate` | Trích lập dự phòng (BĐS / tín dụng). |
| `profit_loss_from_disposal_fixed_assets` | Lãi/lỗ thanh lý TSCĐ. |
| `profit_loss_investment_activities` | Lãi/lỗ từ HĐ đầu tư. |
| `interest_income` / `interest_and_dividend_income` | Lãi tiền gửi & cổ tức nhận được. |
| `net_cash_flow_from_operating_activities_before_working_capital` | LCT từ HĐKD trước thay đổi vốn lưu động. |
| `increase_decrease_receivables` / `_inventory` / `_payables` / `_prepaid_expenses` | Biến động vốn lưu động. |
| `interest_expense_paid` | Lãi vay đã trả. |
| `corporate_income_tax_paid` | Thuế TNDN đã nộp. |
| `other_cash_from_operating_activities` / `other_cash_paid_for_operating_activities` | Thu/chi khác từ HĐKD. |
| `net_cash_from_operating_activities` | **CFO — Dòng tiền thuần từ HĐKD**. |
| `purchase_purchase_fixed_assets` | Chi mua sắm TSCĐ (CAPEX). |
| `proceeds_from_disposal_fixed_assets` | Thu thanh lý TSCĐ. |
| `loans_other_collections` | Thu hồi cho vay. |
| `investments_other_companies` | Chi đầu tư vào DN khác. |
| `proceeds_from_sale_investments_other_companies` | Thu từ thoái vốn DN khác. |
| `dividends_and_profits_received` | Cổ tức & lợi nhuận được chia đã thu. |
| `net_cash_from_investing_activities` | **CFI — Dòng tiền thuần từ đầu tư** (thường âm với DN tăng trưởng). |
| `increase_share_capital_contribution_equity` | Thu từ phát hành cổ phiếu / góp vốn. |
| `payment_for_capital_contribution_buyback_shares` | Chi mua lại cổ phiếu quỹ. |
| `proceeds_from_borrowings` | Tiền vay nhận được. |
| `repayments_of_borrowings` | Trả nợ gốc vay. |
| `lease_principal_payments` | Trả gốc thuê tài chính. |
| `dividends_paid` | Cổ tức đã trả cho cổ đông. |
| `other_cash_from_financing_activities` | Thu/chi tài chính khác. |
| `net_cash_from_financing_activities` | **CFF — Dòng tiền thuần từ HĐ tài chính**. |
| `net_cash_flow_period` | Dòng tiền thuần kỳ = CFO + CFI + CFF. |
| `cash_and_cash_equivalents_beginning` / `_ending` | Tiền đầu kỳ / cuối kỳ. |

**Khi dùng:**
- User hỏi về **dòng tiền, CFO/CFI/CFF, CAPEX, FCF, cổ tức đã trả, vay/trả nợ, thanh lý TSCĐ**.
- "X có dòng tiền HĐKD bao nhiêu", "CAPEX của Y", "Z đã trả bao nhiêu cổ tức".
- Đánh giá chất lượng LN (so CFO với LNST), khả năng tự tài trợ (FCF = CFO − CAPEX).

**Mẫu:**
```sql
-- Tổng quan 3 dòng tiền
SELECT year,
       net_cash_from_operating_activities AS cfo,
       net_cash_from_investing_activities AS cfi,
       net_cash_from_financing_activities AS cff,
       net_cash_flow_period AS net_cf,
       cash_and_cash_equivalents_ending AS cash_end
FROM olap.cash_flow_statement FINAL
WHERE symbol = 'HPG' AND quarter = 0
ORDER BY year DESC LIMIT 5

-- Free Cash Flow xấp xỉ (CFO − CAPEX)
SELECT year,
       net_cash_from_operating_activities AS cfo,
       purchase_purchase_fixed_assets AS capex,
       net_cash_from_operating_activities + purchase_purchase_fixed_assets AS fcf_approx
FROM olap.cash_flow_statement FINAL
WHERE symbol = 'VNM' AND quarter = 0
ORDER BY year DESC LIMIT 5
-- (purchase_purchase_fixed_assets thường là số âm, nên cộng = trừ giá trị tuyệt đối)
```

### 6. `financial_ratios` — Chỉ số tài chính tổng hợp (FINAL) ⭐

PK: `(symbol, year, quarter)`. Hỗn hợp đơn vị — đọc kỹ ghi chú.

| Cột | Đơn vị | Ý nghĩa |
|---|---|---|
| `price_to_book` | lần | P/B. |
| `price_to_earnings` | lần | P/E. |
| `price_to_sales` | lần | P/S. |
| `price_to_cash_flow` | lần | P/CF. |
| `eps_vnd` | VND/cp | EPS (cùng ý nghĩa `income_statement.eps`). |
| `bvps_vnd` | VND/cp | Book Value Per Share. |
| `market_cap_billions` | **tỷ đồng** | Vốn hoá thị trường. |
| `shares_outstanding_millions` | **triệu cp** | Số CP lưu hành. |
| `ev_to_ebitda` / `ev_to_ebit` | lần | EV/EBITDA, EV/EBIT. |
| `debt_to_equity` | lần (decimal) | Nợ vay / VCSH. |
| `debt_to_equity_adjusted` | lần | D/E sau điều chỉnh. |
| `fixed_assets_to_equity` | lần | TSCĐ / VCSH. |
| `equity_to_charter_capital` | lần | VCSH / Vốn điều lệ. |
| `asset_turnover` | lần/năm | Vòng quay tổng tài sản. |
| `fixed_asset_turnover` | lần/năm | Vòng quay TSCĐ. |
| `days_sales_outstanding` (DSO) | ngày | Số ngày phải thu. |
| `days_inventory_outstanding` (DIO) | ngày | Số ngày tồn kho. |
| `days_payable_outstanding` (DPO) | ngày | Số ngày phải trả. |
| `cash_conversion_cycle` | ngày | DSO + DIO − DPO. |
| `inventory_turnover` | lần/năm | Vòng quay hàng tồn kho. |
| `gross_margin` | **decimal** | Biên LN gộp (×100 → %). |
| `net_profit_margin` | **decimal** | Biên LN ròng. |
| `ebit_margin` | **decimal** | Biên EBIT. |
| `roe` | **decimal** | ROE — **luôn ×100 khi hiển thị**. |
| `roa` | **decimal** | ROA. |
| `roic` | **decimal** | Return on Invested Capital. |
| `ebitda_billions` / `ebit_billions` | **tỷ đồng** | EBITDA / EBIT tuyệt đối. |
| `dividend_payout_ratio` | decimal | Tỷ lệ chi trả cổ tức. |
| `current_ratio` / `quick_ratio` / `cash_ratio` | lần | Khả năng thanh toán. |
| `interest_coverage_ratio` | lần | EBIT / Lãi vay. |
| `financial_leverage` | lần | Tổng tài sản / VCSH. |
| `beta` | lần | Beta thị trường. |

**Khi dùng:**
- User hỏi **ROE, ROA, ROIC, P/E, P/B, P/S, EV/EBITDA, vốn hoá, EBITDA, biên LN, D/E, beta, current/quick/cash ratio, vòng quay HTK**, các chỉ số đếm ngày (DSO/DIO/DPO/CCC).
- So sánh giữa nhiều công ty / nhiều năm.
- Lấy `market_cap_billions` và `shares_outstanding_millions` (đơn vị đã ở tỷ/triệu, không phải VND nguyên).

**Mẫu:**
```sql
-- Bộ chỉ số sinh lời + định giá 1 mã
SELECT year,
       round(roe * 100, 2) AS roe_pct,
       round(roa * 100, 2) AS roa_pct,
       round(gross_margin * 100, 2) AS gross_margin_pct,
       round(net_profit_margin * 100, 2) AS net_margin_pct,
       price_to_earnings AS pe,
       price_to_book AS pb,
       market_cap_billions
FROM olap.financial_ratios FINAL
WHERE symbol = 'FPT' AND quarter = 0
ORDER BY year DESC LIMIT 5

-- So sánh ROE giữa nhiều công ty cùng năm
SELECT symbol, round(roe * 100, 2) AS roe_pct, market_cap_billions
FROM olap.financial_ratios FINAL
WHERE symbol IN ('VNM','MSN','MWG','SAB') AND year = 2024 AND quarter = 0
ORDER BY roe_pct DESC

-- Top vốn hoá ngành — KHÔNG JOIN. Dùng subquery IN.
-- Bước 1: lấy ticker theo ngành + ROE/market cap
SELECT symbol, round(roe * 100, 2) AS roe_pct, market_cap_billions
FROM olap.financial_ratios FINAL
WHERE symbol IN (
    SELECT ticker FROM olap.stock_industry FINAL
    WHERE icb_name2 = 'Ngân hàng'
)
  AND year = 2024 AND quarter = 0
ORDER BY market_cap_billions DESC LIMIT 10
-- Bước 2 (nếu cần tên công ty): SELECT riêng từ olap.stocks WHERE ticker IN (...)
-- rồi tự ghép `organ_name` vào kết quả khi viết câu trả lời.
```

### 7. `financial_reports` — Wrapper raw 3 BCTC (FINAL)

| Cột | Ý nghĩa |
|---|---|
| `report_type` | `'balance_sheet'` / `'income_statement'` / `'cash_flow'`. |
| `data_json` | Toàn bộ BCTC raw dưới dạng JSON từ VCI. Dùng khi bảng đã chuẩn hoá thiếu cột. |

**Khi dùng:** chỉ khi 4 bảng chuẩn hoá ở trên thiếu cột user cần, và bạn sẵn sàng parse JSON. Hiếm dùng.

### 8. `officers` — Ban lãnh đạo (FINAL)

| Cột | Ý nghĩa |
|---|---|
| `symbol` | Ticker. |
| `officer_name` | Họ tên. |
| `officer_position` | Chức vụ đầy đủ (`'Chủ tịch HĐQT'`, `'Tổng Giám đốc'`…). |
| `position_short_name` | Chức vụ rút gọn (`'CEO'`, `'CFO'`…). |
| `officer_own_percent` | % sở hữu cá nhân (**0–100**, không phải decimal). |
| `quantity` | Số CP cá nhân nắm giữ. |
| `update_date` | Ngày cập nhật (String). |
| `status` | `'working'` (đương nhiệm) / `'left'` (đã rời). Lọc `status='working'` để lấy ban lãnh đạo hiện tại. |

**Khi dùng:** "Ai là CEO/Chủ tịch của X", "ban lãnh đạo X", "X có CFO không", người nội bộ nắm giữ bao nhiêu CP.

**Mẫu:**
```sql
SELECT officer_name, officer_position, position_short_name,
       officer_own_percent, quantity
FROM olap.officers FINAL
WHERE symbol = 'HPG' AND status = 'working'
ORDER BY officer_own_percent DESC LIMIT 20
```

### 9. `shareholders` — Cổ đông lớn (FINAL)

| Cột | Ý nghĩa |
|---|---|
| `symbol` | Ticker. |
| `share_holder` | Tên cổ đông (cá nhân hoặc tổ chức). |
| `quantity` | Số CP nắm giữ. |
| `share_own_percent` | % sở hữu (**0–100**). Tổng các dòng có thể không đến 100% vì chỉ liệt kê cổ đông lớn. |
| `update_date` | String. |

**Khi dùng:** "Cơ cấu cổ đông X", "ai sở hữu nhiều nhất ở X", "X có cổ đông nhà nước/nước ngoài không", input cho biểu đồ pie.

**Mẫu:**
```sql
SELECT share_holder, share_own_percent, quantity, update_date
FROM olap.shareholders FINAL
WHERE symbol = 'VNM'
ORDER BY share_own_percent DESC LIMIT 10
```

### 10. `subsidiaries` — Công ty con / liên kết (FINAL)

| Cột | Ý nghĩa |
|---|---|
| `symbol` | Ticker công ty mẹ. |
| `sub_organ_code` | Mã công ty con (nếu có niêm yết). |
| `organ_name` | Tên công ty con. |
| `ownership_percent` | % sở hữu (**0–100**). |
| `type` | `'subsidiary'` / `'associate'` (công ty liên kết). |

**Khi dùng:** "X có những công ty con nào", "danh sách thành viên tập đoàn X", "X sở hữu bao nhiêu % công ty Y".

**Mẫu:**
```sql
SELECT organ_name, sub_organ_code, ownership_percent, type
FROM olap.subsidiaries FINAL
WHERE symbol = 'VIC'
ORDER BY ownership_percent DESC LIMIT 30
```

### 11. `events` — Sự kiện DN (FINAL, append-style)

| Cột | Ý nghĩa |
|---|---|
| `symbol` | Ticker. |
| `event_title` / `en_event_title` | Tiêu đề sự kiện. |
| `event_list_code` / `event_list_name` | Mã & nhóm sự kiện (chia tách, ĐHCĐ, cổ tức, phát hành thêm…). |
| `ratio` / `value` | Tỷ lệ / giá trị (tuỳ loại sự kiện). |
| `public_date`, `issue_date`, `record_date`, `exright_date` | **String `'YYYY-MM-DD'`**, KHÔNG phải Date. Dùng `parseDateTimeBestEffortOrNull(...)` nếu cần so sánh. |
| `source_url` | Link công bố. |

**Khi dùng:** "Sự kiện gần đây của X", ngày chốt quyền cổ tức, ngày phát hành thêm, ngày họp ĐHCĐ, lịch sử chia tách.

**Mẫu:**
```sql
-- Sự kiện gần đây của 1 mã
SELECT event_title, event_list_name, ratio, value,
       public_date, record_date, exright_date
FROM olap.events FINAL
WHERE symbol = 'VNM'
ORDER BY parseDateTimeBestEffortOrNull(public_date) DESC
LIMIT 10

-- Sự kiện theo loại trong 1 năm
SELECT symbol, event_title, public_date, record_date
FROM olap.events FINAL
WHERE event_list_name LIKE '%cổ tức%'
  AND parseDateTimeBestEffortOrNull(public_date) >= '2024-01-01'
ORDER BY public_date DESC LIMIT 50
```

### 12. `news` — Tin tức (append-only, KHÔNG `FINAL`)

| Cột | Ý nghĩa |
|---|---|
| `symbol` | Ticker liên quan (có thể NULL nếu là tin chung). |
| `news_title` / `news_sub_title` / `friendly_sub_title` | Tiêu đề + phụ đề. |
| `news_short_content` / `news_full_content` | Nội dung. |
| `news_source_link` | Link bài gốc. |
| `news_image_url` | Ảnh đại diện. |
| `public_date` | **Int64 epoch ms** — chuyển bằng `toDateTime(public_date / 1000)`. |
| `close_price`, `ref_price`, `floor`, `ceiling` | Giá trong ngày tin (Int64). |
| `price_change_pct` | % thay đổi giá ngày tin (decimal). |

**Khi dùng:** "Tin tức gần đây về X", tin theo ngày, tin liên quan đến biến động giá. Lưu ý: bảng có thể không có tin sau cutoff training — nếu user hỏi tin "hôm nay", chuyển sang `web_search`.

**Mẫu:**
```sql
-- Tin mới nhất của 1 mã
SELECT news_title, news_short_content,
       toDateTime(public_date / 1000) AS published_at,
       news_source_link, price_change_pct
FROM olap.news
WHERE symbol = 'FPT'
ORDER BY public_date DESC LIMIT 10

-- Tin trong khoảng thời gian
SELECT symbol, news_title, toDateTime(public_date / 1000) AS published_at
FROM olap.news
WHERE public_date >= toUnixTimestamp(toDateTime('2024-10-01')) * 1000
  AND symbol IN ('VNM','HPG','FPT')
ORDER BY public_date DESC LIMIT 30
```

### 13. `cash_dividend` — Cổ tức tiền mặt

| Cột | Ý nghĩa |
|---|---|
| `symbol` | Ticker. |
| `record_date` / `payment_date` | Ngày chốt / ngày thanh toán (Date). |
| `exercise_rate` | Tỷ lệ thực hiện (decimal, ví dụ 0.1 = 10%). |
| `dps` | **Dividend Per Share** — VND/cp. |
| `currency` | Tiền tệ (`'VND'`/`'USD'`). |
| `dividend_year` | Năm cổ tức được phân phối. |
| `duration` | Mô tả kỳ (`'cả năm 2024'`, `'tạm ứng 2025'`…). |

**Khi dùng:** "X chia cổ tức tiền mặt bao nhiêu", "lịch sử cổ tức X", "DPS năm 2024 của Y".

**Mẫu:**
```sql
SELECT dividend_year, duration, exercise_rate, dps,
       record_date, payment_date
FROM olap.cash_dividend FINAL
WHERE symbol = 'VNM'
ORDER BY dividend_year DESC, record_date DESC LIMIT 10
```

### 14. `stock_dividend` — Cổ tức cổ phiếu / chia tách

| Cột | Ý nghĩa |
|---|---|
| `symbol`, `record_date`, `payment_date`, `dividend_year`, `duration` | Tương tự `cash_dividend`. |
| `exercise_rate` | Tỷ lệ phát hành thêm (1:1 = 1.0). |
| `plan_volume` | Khối lượng dự kiến phát hành (cp). |
| `issue_volume` | Khối lượng thực phát (cp). |

**Khi dùng:** "X chia cổ phiếu thưởng tỷ lệ bao nhiêu", "X có chia tách không", "X phát hành thêm khi nào".

**Mẫu:**
```sql
SELECT dividend_year, duration, exercise_rate,
       plan_volume, issue_volume, record_date, payment_date
FROM olap.stock_dividend FINAL
WHERE symbol = 'HPG'
ORDER BY dividend_year DESC LIMIT 10
```

### 15. `stock_price_history` — OHLCV theo ngày (append-only, KHÔNG `FINAL`)

| Cột | Ý nghĩa |
|---|---|
| `symbol` | Ticker. |
| `time` | Date (1 row / ngày giao dịch). |
| `open`, `high`, `low`, `close` | Giá VND. |
| `volume` | Khối lượng giao dịch. |
| Partition: `toYYYYMM(time)` — luôn filter `time >= '...'` để dùng được partition prune. |

**Khi dùng:** "Giá đóng cửa X ngày Y", "biểu đồ giá X 1 năm qua", "X cao nhất / thấp nhất bao nhiêu", trend giá theo tháng/quý.

**Mẫu:**
```sql
-- Trend giá đóng cửa theo tháng
SELECT toStartOfMonth(time) AS month,
       avg(close) AS avg_close,
       max(high) AS month_high,
       min(low) AS month_low
FROM olap.stock_price_history
WHERE symbol = 'VNM' AND time >= '2024-01-01'
GROUP BY month
ORDER BY month

-- Giá cuối ngày 1 ngày cụ thể
SELECT time, open, high, low, close, volume
FROM olap.stock_price_history
WHERE symbol = 'HPG' AND time = '2024-12-30'
LIMIT 1

-- Hiệu suất 1 năm (so giá hiện tại với 1 năm trước)
SELECT symbol,
       any(close) FILTER (WHERE time = (SELECT max(time) FROM olap.stock_price_history WHERE symbol = 'FPT')) AS latest,
       any(close) FILTER (WHERE time <= today() - 365) AS year_ago
FROM olap.stock_price_history
WHERE symbol = 'FPT' AND time >= today() - 380
```

### 16. `stock_intraday` — Tick data trong phiên (append-only)

| Cột | Ý nghĩa |
|---|---|
| `symbol`, `time` (DateTime) | PK logic. |
| `price` | Giá khớp (VND). |
| `volume` | Khối lượng khớp lệnh đó. |
| `accumulated_val` / `accumulated_vol` | Giá trị/khối lượng lũy kế trong phiên. |
| `match_type` | `'BU'` (buy up) / `'SD'` (sell down) / khác. |
| Partition: `toYYYYMM(time)`. |

**Khi dùng:** Hiếm — chỉ khi user hỏi rõ về tick / khớp lệnh trong phiên / phân tích áp lực mua-bán intraday.

**Mẫu:**
```sql
SELECT toStartOfMinute(time) AS minute,
       avg(price) AS avg_price,
       sum(volume) AS total_vol
FROM olap.stock_intraday
WHERE symbol = 'VNM'
  AND time >= toDateTime('2024-12-30 09:00:00')
  AND time <  toDateTime('2024-12-30 15:00:00')
GROUP BY minute ORDER BY minute
```

### 17. `stock_industry` — Ngành ICB chi tiết theo mã (FINAL)

| Cột | Ý nghĩa |
|---|---|
| `ticker` | Mã CK (giống `stocks.ticker`). |
| `icb_code` | Mã ngành ICB chính. |
| `icb_name2/3/4` + `icb_code1/2/3/4` | Ngành ICB 4 cấp (1 = lớn nhất, 4 = chi tiết nhất). |
| `en_icb_name2/3/4` | Tên tiếng Anh các cấp. |

**Khi dùng:** "X thuộc ngành nào", lọc danh mục theo ngành ICB cụ thể. Cần xếp hạng theo `financial_ratios` trong 1 ngành → **không JOIN**, dùng subquery `WHERE symbol IN (SELECT ticker FROM olap.stock_industry FINAL WHERE icb_name2 = '…')`.

**Mẫu:**
```sql
-- Ngành ICB của 1 mã
SELECT ticker, icb_name2, icb_name3, icb_name4
FROM olap.stock_industry FINAL
WHERE ticker = 'HPG' LIMIT 1

-- Lấy tất cả mã trong ngành "Ngân hàng" cấp 2
SELECT ticker, icb_name3
FROM olap.stock_industry FINAL
WHERE icb_name2 = 'Ngân hàng'
LIMIT 50
```

### 18. Master data còn lại

| Bảng | Cột chính | Khi nào dùng |
|---|---|---|
| `exchanges` | `exchange` (PK = `'HOSE'`/`'HNX'`/`'UPCOM'`), `exchange_name`, `exchange_code` | Trừ khi user hỏi rõ về sàn — hiếm dùng. |
| `indices` | `index_code` (PK), `index_name`, `description`, `group_name`, `index_id`, `sector_id` | Lọc theo VN30, VNINDEX… |
| `industries` | `icb_code` (PK), `icb_name`, `en_icb_name`, `level` (1–4) | Lookup tên ngành ICB. Thường thì `stock_industry.icb_name2/3/4` đã đủ. |
| `stock_exchange` | `ticker`, `exchange`, `type` | Junction — mã X niêm yết trên sàn nào. |
| `stock_index` | `ticker`, `index_code` | Junction — mã X có trong rổ chỉ số nào. |

**Mẫu:**
```sql
-- Mã thuộc sàn nào
SELECT ticker, exchange FROM olap.stock_exchange FINAL
WHERE ticker = 'HPG' LIMIT 5

-- Tất cả mã trong rổ VN30
SELECT ticker FROM olap.stock_index FINAL
WHERE index_code = 'VN30' LIMIT 50
```

### 19. Logs (KHÔNG dùng cho câu hỏi user)

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

### Top mã theo vốn hoá ngành ngân hàng (KHÔNG JOIN — dùng subquery)

```sql
-- Câu 1: top theo market cap, lọc ngành bằng subquery IN
SELECT symbol, market_cap_billions, round(roe * 100, 2) AS roe_pct
FROM olap.financial_ratios FINAL
WHERE symbol IN (
    SELECT ticker FROM olap.stock_industry FINAL
    WHERE icb_name2 = 'Ngân hàng'
)
  AND year = 2024 AND quarter = 0
ORDER BY market_cap_billions DESC
LIMIT 10

-- Câu 2 (song song nếu cần tên công ty): SELECT riêng rồi tự ghép
SELECT ticker, organ_name FROM olap.stocks FINAL
WHERE ticker IN ('VCB','BID','CTG','TCB','MBB','VPB','ACB','HDB','STB','SHB')
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

- ❌ **Coi ticker là table name**: `SHOW TABLES LIKE '%HPG%'`, `SELECT * FROM HPG`, `FROM olap.HPG`. HPG/VNM/FPT là **giá trị** trong cột `symbol` hoặc `ticker`. Đúng: `WHERE symbol = 'HPG'`.
- ❌ Lượt SQL đầu tiên là `SHOW TABLES` / `DESCRIBE` thay vì đi thẳng vào `company_overview` / `income_statement`. Schema đã có ở trên — đi thẳng.
- ❌ Quên `FINAL` → dữ liệu trùng/cũ (đặc biệt với các bảng tài chính ReplacingMergeTree).
- ❌ Hỏi "doanh thu năm 2024" mà filter `quarter > 0` → ra số quý, cộng lại sai.
- ❌ Filter `period = '2024'` — `period` là enum nội bộ (`'Y'`/`'Q'`), không phải năm. Luôn dùng `year = 2024`.
- ❌ Coi `roe` đã ở dạng %  — thực tế là decimal, cần `* 100`.
- ❌ **Bất kỳ JOIN nào** — kể cả `INNER JOIN ... USING (symbol)` "có vẻ an toàn". Nhiều bảng có cột trùng tên (`symbol`/`ticker`/`year`/`quarter`/`organ_name`/`update_date`/`source`/`data_json`/`icb_name*`) → ClickHouse báo `AMBIGUOUS_COLUMN_NAME` hoặc nhân số dòng. Thay bằng nhiều `SELECT` riêng (gọi song song) hoặc subquery `WHERE col IN (SELECT …)`.
- ❌ Hỏi "ngành ngân hàng" rồi filter `icb_name = '...'` — bảng `stock_industry` có 3 cấp (`icb_name2`, `icb_name3`, `icb_name4`); chọn cấp phù hợp với câu hỏi (cấp 2 thường = ngành lớn).
- ❌ Format `news.public_date` thành Date trực tiếp — nó là epoch ms, dùng `toDateTime(public_date / 1000)`.

## KHI NÀO KHÔNG DÙNG TOOL NÀY

- Câu hỏi định nghĩa khái niệm thuần ("EBITDA là gì?") — trả lời từ kiến thức, không cần SQL.
- Tin tức / sự kiện sau ngày cutoff training và không có trong bảng `news` / `events` — dùng `web_search` thay thế.
- Câu hỏi về tài liệu nội bộ đã có trong RAG context — ưu tiên trích dẫn [1], [2] từ context, không SQL lại.
- **Tool trả lỗi "table does not exist"** → bảng bạn vừa gõ không có trong WHITELIST. Đối chiếu lại bản đồ câu hỏi → bảng. Nếu thực sự không có bảng nào trong WHITELIST chứa thông tin user hỏi → dừng SQL, chuyển `web_search`. KHÔNG được "thử bảng khác" với tên bịa.

## KHI QUERY TRẢ VỀ 0 ROWS (PHẢI ĐỌC)

Tuyệt đối **không** im lặng đổi sang năm/quý/công ty khác rồi trả lời như thật. Đi theo bậc thang:

1. **Kiểm tra entity có tồn tại không** — chạy 1 câu nhỏ:
   ```sql
   SELECT symbol FROM olap.company_overview FINAL WHERE symbol = '<TICKER>' LIMIT 1
   ```
   Nếu rỗng → entity không có trong DB. Báo user kiểm tra lại ticker/tên, KHÔNG đoán mã khác.

2. **Entity có nhưng không có timeframe user hỏi** — liệt kê các mốc sẵn có để user chọn:
   ```sql
   SELECT DISTINCT year, quarter FROM olap.income_statement FINAL
   WHERE symbol = '<TICKER>' ORDER BY year DESC, quarter DESC LIMIT 12
   ```
   Sau đó nói thẳng với user: *"Hệ thống có dữ liệu \<TICKER\> các năm/quý: …. Bạn muốn xem mốc nào?"* và **chờ user trả lời**, không tự chọn.

3. **Cả entity + timeframe đều có nhưng cột cụ thể NULL** (ví dụ `eps` null cho 1 năm cũ) — báo rõ "Trường \<X\> không có dữ liệu cho \<timeframe\>", liệt kê các trường còn lại đã lấy được.

4. **DB hoàn toàn không phù hợp với câu hỏi** (ví dụ user hỏi tin tức mới sau cutoff) → chuyển `web_search`, KHÔNG cố ép vào DB.

Nguyên tắc lõi: **trả lời đúng entity + đúng timeframe user yêu cầu, hoặc nói thẳng là không có rồi nhường quyết định cho user**. Đừng tự ý "fallback ngầm" sang dữ liệu năm khác.
