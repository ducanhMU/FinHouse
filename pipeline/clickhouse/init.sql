-- ════════════════════════════════════════════════════════════
-- FinHouse — ClickHouse Schema (Vietnamese Stock Market)
-- ════════════════════════════════════════════════════════════
-- Translated from SQLite source (see /data/OLAP/stocks.db).
--
-- Engine choices:
--   • ReplacingMergeTree — for tables with natural UNIQUE keys from
--     SQLite (dedup happens at merge time; use FINAL for guaranteed
--     latest row in SELECT).
--   • MergeTree — for append-only tables (news, intraday, events).
--
-- Type mappings:
--   SQLite INTEGER    → Int64
--   SQLite REAL       → Float64
--   SQLite TEXT       → String (nullable where SQLite allowed NULL)
--   SQLite TIMESTAMP  → DateTime
--   SQLite DATE       → Date
--
-- How to apply schema updates later:
--   docker exec -i finhouse-clickhouse \
--     clickhouse-client --user $CLICKHOUSE_USER --password $CLICKHOUSE_PASSWORD \
--     < pipeline/clickhouse/init.sql
-- ════════════════════════════════════════════════════════════

CREATE DATABASE IF NOT EXISTS olap;
USE olap;


-- ── reference: stocks (master symbol list) ───────────────────
CREATE TABLE IF NOT EXISTS stocks (
    ticker              String,
    organ_name          Nullable(String),
    en_organ_name       Nullable(String),
    organ_short_name    Nullable(String),
    en_organ_short_name Nullable(String),
    com_type_code       Nullable(String),
    status              String DEFAULT 'listed',
    listed_date         Nullable(String),
    delisted_date       Nullable(String),
    company_id          Nullable(String),
    tax_code            Nullable(String),
    isin                Nullable(String),
    created_at          DateTime DEFAULT now(),
    updated_at          DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(updated_at)
ORDER BY ticker;


-- ── exchanges ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS exchanges (
    exchange       String,
    exchange_name  Nullable(String),
    exchange_code  Nullable(String),
    created_at     DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree()
ORDER BY exchange;


-- ── indices ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS indices (
    index_code   String,
    index_name   Nullable(String),
    description  Nullable(String),
    group_name   Nullable(String),
    index_id     Nullable(Int64),
    sector_id    Nullable(Float64),
    created_at   DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree()
ORDER BY index_code;


-- ── industries ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS industries (
    icb_code     String,
    icb_name     Nullable(String),
    en_icb_name  Nullable(String),
    level        Nullable(Int32),
    created_at   DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree()
ORDER BY icb_code;


-- ── junction: stock_exchange ─────────────────────────────────
CREATE TABLE IF NOT EXISTS stock_exchange (
    ticker    String,
    exchange  String,
    id        Nullable(Int64),
    type      Nullable(String)
) ENGINE = ReplacingMergeTree()
ORDER BY (ticker, exchange);


-- ── junction: stock_index ────────────────────────────────────
CREATE TABLE IF NOT EXISTS stock_index (
    ticker      String,
    index_code  String
) ENGINE = ReplacingMergeTree()
ORDER BY (ticker, index_code);


-- ── junction: stock_industry ─────────────────────────────────
CREATE TABLE IF NOT EXISTS stock_industry (
    ticker         String,
    icb_code       String,
    icb_name2      Nullable(String),
    en_icb_name2   Nullable(String),
    icb_name3      Nullable(String),
    en_icb_name3   Nullable(String),
    icb_name4      Nullable(String),
    en_icb_name4   Nullable(String),
    icb_code1      Nullable(String),
    icb_code2      Nullable(String),
    icb_code3      Nullable(String),
    icb_code4      Nullable(String)
) ENGINE = ReplacingMergeTree()
ORDER BY (ticker, icb_code);


-- ── company_overview ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS company_overview (
    symbol                         String,
    id                             Nullable(String),
    issue_share                    Nullable(Int64),
    history                        Nullable(String),
    company_profile                Nullable(String),
    icb_name3                      Nullable(String),
    icb_name2                      Nullable(String),
    icb_name4                      Nullable(String),
    financial_ratio_issue_share    Nullable(Int64),
    charter_capital                Nullable(Int64),
    created_at                     DateTime DEFAULT now(),
    updated_at                     DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(updated_at)
ORDER BY symbol;


-- ── balance_sheet ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS balance_sheet (
    id                          Int64,
    symbol                      String,
    period                      String,
    year                        Int32,
    quarter                     Int32 DEFAULT 0,
    asset_current               Nullable(Float64),
    cash_and_equivalents        Nullable(Float64),
    short_term_investments      Nullable(Float64),
    accounts_receivable         Nullable(Float64),
    inventory                   Nullable(Float64),
    current_assets_other        Nullable(Float64),
    asset_non_current           Nullable(Float64),
    long_term_receivables       Nullable(Float64),
    fixed_assets                Nullable(Float64),
    long_term_investments       Nullable(Float64),
    non_current_assets_other    Nullable(Float64),
    total_assets                Nullable(Float64),
    liabilities_total           Nullable(Float64),
    liabilities_current         Nullable(Float64),
    liabilities_non_current     Nullable(Float64),
    equity_total                Nullable(Float64),
    share_capital               Nullable(Float64),
    retained_earnings           Nullable(Float64),
    equity_other                Nullable(Float64),
    total_equity_and_liabilities Nullable(Float64),
    data_json                   Nullable(String),
    source                      String DEFAULT 'VCI',
    created_at                  DateTime DEFAULT now(),
    updated_at                  DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (symbol, period, year, quarter);


-- ── cash_flow_statement ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS cash_flow_statement (
    id                                                                   Int64,
    symbol                                                               String,
    period                                                               String,
    year                                                                 Int32,
    quarter                                                              Int32 DEFAULT 0,
    profit_before_tax                                                    Nullable(Float64),
    depreciation_fixed_assets                                            Nullable(Float64),
    provision_credit_loss_real_estate                                    Nullable(Float64),
    profit_loss_from_disposal_fixed_assets                               Nullable(Float64),
    profit_loss_investment_activities                                    Nullable(Float64),
    interest_income                                                      Nullable(Float64),
    interest_and_dividend_income                                         Nullable(Float64),
    net_cash_flow_from_operating_activities_before_working_capital       Nullable(Float64),
    increase_decrease_receivables                                        Nullable(Float64),
    increase_decrease_inventory                                          Nullable(Float64),
    increase_decrease_payables                                           Nullable(Float64),
    increase_decrease_prepaid_expenses                                   Nullable(Float64),
    interest_expense_paid                                                Nullable(Float64),
    corporate_income_tax_paid                                            Nullable(Float64),
    other_cash_from_operating_activities                                 Nullable(Float64),
    other_cash_paid_for_operating_activities                             Nullable(Float64),
    net_cash_from_operating_activities                                   Nullable(Float64),
    purchase_purchase_fixed_assets                                       Nullable(Float64),
    proceeds_from_disposal_fixed_assets                                  Nullable(Float64),
    loans_other_collections                                              Nullable(Float64),
    investments_other_companies                                          Nullable(Float64),
    proceeds_from_sale_investments_other_companies                       Nullable(Float64),
    dividends_and_profits_received                                       Nullable(Float64),
    net_cash_from_investing_activities                                   Nullable(Float64),
    increase_share_capital_contribution_equity                           Nullable(Float64),
    payment_for_capital_contribution_buyback_shares                      Nullable(Float64),
    proceeds_from_borrowings                                             Nullable(Float64),
    repayments_of_borrowings                                             Nullable(Float64),
    lease_principal_payments                                             Nullable(Float64),
    dividends_paid                                                       Nullable(Float64),
    other_cash_from_financing_activities                                 Nullable(Float64),
    net_cash_from_financing_activities                                   Nullable(Float64),
    net_cash_flow_period                                                 Nullable(Float64),
    cash_and_cash_equivalents_beginning                                  Nullable(Float64),
    cash_and_cash_equivalents_ending                                     Nullable(Float64),
    data_json                                                            Nullable(String),
    source                                                               String DEFAULT 'VCI',
    created_at                                                           DateTime DEFAULT now(),
    updated_at                                                           DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (symbol, period, year, quarter);


-- ── income_statement ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS income_statement (
    id                               Int64,
    symbol                           String,
    period                           String,
    year                             Int32,
    quarter                          Int32 DEFAULT 0,
    revenue                          Nullable(Float64),
    revenue_growth                   Nullable(Float64),
    net_profit_parent_company        Nullable(Float64),
    profit_growth                    Nullable(Float64),
    net_revenue                      Nullable(Float64),
    cost_of_goods_sold               Nullable(Float64),
    gross_profit                     Nullable(Float64),
    financial_income                 Nullable(Float64),
    financial_expense                Nullable(Float64),
    net_financial_income             Nullable(Float64),
    operating_expenses               Nullable(Float64),
    operating_profit                 Nullable(Float64),
    other_income                     Nullable(Float64),
    profit_before_tax                Nullable(Float64),
    corporate_income_tax             Nullable(Float64),
    deferred_income_tax              Nullable(Float64),
    net_profit                       Nullable(Float64),
    minority_interest                Nullable(Float64),
    net_profit_parent_company_post   Nullable(Float64),
    eps                              Nullable(Float64),
    data_json                        Nullable(String),
    source                           String DEFAULT 'VCI',
    created_at                       DateTime DEFAULT now(),
    updated_at                       DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (symbol, period, year, quarter);


-- ── financial_ratios ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS financial_ratios (
    id                            Int64,
    symbol                        String,
    period                        String,
    year                          Int32,
    quarter                       Int32 DEFAULT 0,
    price_to_book                 Nullable(Float64),
    market_cap_billions           Nullable(Float64),
    shares_outstanding_millions   Nullable(Float64),
    price_to_earnings             Nullable(Float64),
    price_to_sales                Nullable(Float64),
    price_to_cash_flow            Nullable(Float64),
    eps_vnd                       Nullable(Float64),
    bvps_vnd                      Nullable(Float64),
    ev_to_ebitda                  Nullable(Float64),
    debt_to_equity                Nullable(Float64),
    debt_to_equity_adjusted       Nullable(Float64),
    fixed_assets_to_equity        Nullable(Float64),
    equity_to_charter_capital     Nullable(Float64),
    asset_turnover                Nullable(Float64),
    fixed_asset_turnover          Nullable(Float64),
    days_sales_outstanding        Nullable(Float64),
    days_inventory_outstanding    Nullable(Float64),
    days_payable_outstanding      Nullable(Float64),
    cash_conversion_cycle         Nullable(Float64),
    inventory_turnover            Nullable(Float64),
    ebit_margin                   Nullable(Float64),
    gross_margin                  Nullable(Float64),
    net_profit_margin             Nullable(Float64),
    roe                           Nullable(Float64),
    roic                          Nullable(Float64),
    roa                           Nullable(Float64),
    ebitda_billions               Nullable(Float64),
    ebit_billions                 Nullable(Float64),
    dividend_payout_ratio         Nullable(Float64),
    current_ratio                 Nullable(Float64),
    quick_ratio                   Nullable(Float64),
    cash_ratio                    Nullable(Float64),
    interest_coverage_ratio       Nullable(Float64),
    financial_leverage            Nullable(Float64),
    beta                          Nullable(Float64),
    ev_to_ebit                    Nullable(Float64),
    data_json                     Nullable(String),
    source                        String DEFAULT 'VCI',
    created_at                    DateTime DEFAULT now(),
    updated_at                    DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (symbol, period, year, quarter);


-- ── financial_reports (wrapper of statements) ────────────────
CREATE TABLE IF NOT EXISTS financial_reports (
    id           Int64,
    symbol       String,
    report_type  String,
    period       String,
    year         Int32,
    quarter      Int32 DEFAULT 0,
    data_json    String,
    source       String DEFAULT 'VCI',
    created_at   DateTime DEFAULT now(),
    updated_at   DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (symbol, report_type, period, year, quarter);


-- ── events (corporate actions) ───────────────────────────────
CREATE TABLE IF NOT EXISTS events (
    id                  String,
    symbol              Nullable(String),
    event_title         Nullable(String),
    en_event_title      Nullable(String),
    public_date         Nullable(String),
    issue_date          Nullable(String),
    source_url          Nullable(String),
    event_list_code     Nullable(String),
    ratio               Nullable(Float64),
    value               Nullable(Float64),
    record_date         Nullable(String),
    exright_date        Nullable(String),
    event_list_name     Nullable(String),
    en_event_list_name  Nullable(String),
    created_at          DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree()
ORDER BY id;


-- ── news ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS news (
    id                   String,
    symbol               Nullable(String),
    news_title           Nullable(String),
    news_sub_title       Nullable(String),
    friendly_sub_title   Nullable(String),
    news_image_url       Nullable(String),
    news_source_link     Nullable(String),
    public_date          Nullable(Int64),
    news_id              Nullable(String),
    news_short_content   Nullable(String),
    news_full_content    Nullable(String),
    close_price          Nullable(Int64),
    ref_price            Nullable(Int64),
    floor                Nullable(Int64),
    ceiling              Nullable(Int64),
    price_change_pct     Nullable(Float64),
    created_at           DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree()
ORDER BY id;


-- ── officers ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS officers (
    id                   String,
    symbol               Nullable(String),
    officer_name         Nullable(String),
    officer_position     Nullable(String),
    position_short_name  Nullable(String),
    update_date          Nullable(String),
    officer_own_percent  Nullable(Float64),
    quantity             Nullable(Int64),
    status               String DEFAULT 'working',
    created_at           DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree()
ORDER BY id;


-- ── shareholders ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS shareholders (
    id                  String,
    symbol              Nullable(String),
    share_holder        Nullable(String),
    quantity            Nullable(Int64),
    share_own_percent   Nullable(Float64),
    update_date         Nullable(String),
    created_at          DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree()
ORDER BY id;


-- ── subsidiaries ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS subsidiaries (
    id                String,
    symbol            Nullable(String),
    sub_organ_code    Nullable(String),
    ownership_percent Nullable(Float64),
    organ_name        Nullable(String),
    type              Nullable(String),
    created_at        DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree()
ORDER BY id;


-- ── stock_intraday (tick data, append-only) ──────────────────
CREATE TABLE IF NOT EXISTS stock_intraday (
    symbol           String,
    time             DateTime,
    price            Nullable(Float64),
    accumulated_val  Nullable(Int64),
    accumulated_vol  Nullable(Int64),
    volume           Nullable(Int64),
    match_type       Nullable(String)
) ENGINE = MergeTree()
ORDER BY (symbol, time)
PARTITION BY toYYYYMM(time);


-- ── stock_price_history (OHLCV, one row per day) ─────────────
CREATE TABLE IF NOT EXISTS stock_price_history (
    id          Int64,
    symbol      String,
    time        Date,
    open        Nullable(Float64),
    high        Nullable(Float64),
    low         Nullable(Float64),
    close       Nullable(Float64),
    volume      Nullable(Int64),
    created_at  DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree()
ORDER BY (symbol, time)
PARTITION BY toYYYYMM(time);


-- ── ingestion log (written by Spark after each successful load)
CREATE TABLE IF NOT EXISTS _ingestion_log (
    manifest_name   String,
    table_name      String,
    source_file     String,
    file_type       String,
    row_count       UInt64,
    file_size       UInt64,
    ingested_at     DateTime DEFAULT now(),
    spark_job_id    String DEFAULT '',
    status          Enum8('success' = 1, 'failed' = 2) DEFAULT 'success'
) ENGINE = MergeTree()
ORDER BY (ingested_at, table_name)
TTL ingested_at + INTERVAL 90 DAY;


-- ── update_log (maintenance tracking) ────────────────────────
CREATE TABLE IF NOT EXISTS update_log (
    id              Int64,
    table_name      Nullable(String),
    records_updated Nullable(Int64),
    update_time     DateTime DEFAULT now(),
    status          Nullable(String)
) ENGINE = MergeTree()
ORDER BY update_time;
