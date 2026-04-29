"""
FinHouse — Spark Ingest Job (SQLite → ClickHouse)

Reads all known tables from one or more SQLite .db files and upserts them
into matching ClickHouse tables (ReplacingMergeTree handles dedup at merge time).

Usage:
    spark-submit ingest_job.py \
        --files-json '[{"file_path": "/opt/finhouse/data/OLAP/stocks.db", "file_type": "db"}]' \
        --clickhouse-host finhouse-clickhouse \
        --clickhouse-port 8123 \
        --clickhouse-database olap \
        --batch-id <batch_id>

ClickHouse credentials are read from CLICKHOUSE_USER / CLICKHOUSE_PASSWORD env vars.
"""

import argparse
import json
import os
import sqlite3
import sys
from pyspark.sql import SparkSession
from pyspark.sql.functions import coalesce, col, lit, to_date, to_timestamp
from pyspark.sql.types import StringType


KNOWN_TABLES = {
    "stocks", "exchanges", "indices", "industries",
    "stock_exchange", "stock_index", "stock_industry",
    "company_overview", "balance_sheet", "cash_flow_statement",
    "income_statement", "financial_ratios", "financial_reports",
    "events", "news", "officers", "shareholders", "subsidiaries",
    "stock_intraday", "stock_price_history", "update_log",
}

# These tables use composite ORDER BY in ClickHouse — drop SQLite's auto-id
DROP_ID_TABLES = {
    "stocks", "exchanges", "indices", "industries",
    "stock_exchange", "stock_index", "stock_industry",
    "company_overview",
}


def build_spark() -> SparkSession:
    return (
        SparkSession.builder
        .appName("FinHouse-OLAP-Ingest")
        .config("spark.sql.session.timeZone", "UTC")
        .getOrCreate()
    )


def sanitize_columns(df):
    mapping = {
        c: c.strip().replace(" ", "_").replace("-", "_").replace(".", "_")
        for c in df.columns
    }
    return df.toDF(*[mapping[c] for c in df.columns])


def list_sqlite_tables(path: str) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        )
        return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


def detect_date_typed_columns(db_path: str, table: str) -> set[str]:
    # sqlite-jdbc gọi getDate()/getTimestamp() cho mọi cột khai DATE/TIME/TIMESTAMP,
    # rồi parse bằng FastDateFormat strict `yyyy-MM-dd HH:mm:ss.SSS` → fail với
    # date-only như "2018-05-07". Cách duy nhất né là khai cột đó STRING trong
    # Spark customSchema để Spark gọi getString() thay vì getDate().
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(f"PRAGMA table_info({table})")
        out: set[str] = set()
        for row in cur.fetchall():
            name, ctype = row[1], (row[2] or "").upper()
            if "DATE" in ctype or "TIME" in ctype:
                out.add(name)
        return out
    finally:
        conn.close()


# ClickHouse Date/DateTime columns that come back as StringType from SQLite JDBC.
# Map: table -> {column: "date" | "datetime"}
DATE_COLUMNS: dict[str, dict[str, str]] = {
    "stock_price_history": {"time": "date", "created_at": "datetime"},
    "stock_intraday":      {"time": "datetime"},
    "update_log":          {"update_time": "datetime"},
    "stocks":              {"created_at": "datetime", "updated_at": "datetime"},
    "balance_sheet":       {"created_at": "datetime", "updated_at": "datetime"},
    "cash_flow_statement": {"created_at": "datetime", "updated_at": "datetime"},
    "income_statement":    {"created_at": "datetime", "updated_at": "datetime"},
    "financial_ratios":    {"created_at": "datetime", "updated_at": "datetime"},
    "financial_reports":   {"created_at": "datetime", "updated_at": "datetime"},
    "company_overview":    {"created_at": "datetime", "updated_at": "datetime"},
    "exchanges":           {"created_at": "datetime"},
    "indices":             {"created_at": "datetime"},
    "industries":          {"created_at": "datetime"},
    "events":              {"created_at": "datetime"},
    "news":                {"created_at": "datetime"},
    "officers":            {"created_at": "datetime"},
    "shareholders":        {"created_at": "datetime"},
    "subsidiaries":        {"created_at": "datetime"},
}

# Tables large enough to benefit from JDBC range-partitioned reads.
# key = table name, value = column to partition on.
PARTITION_TABLES = {
    "stock_price_history": "id",
    "balance_sheet":       "id",
    "cash_flow_statement": "id",
    "income_statement":    "id",
    "financial_ratios":    "id",
    "financial_reports":   "id",
}
PARTITION_COUNT = 8


# Non-nullable ClickHouse columns that SQLite stores as NULLable.
# Values match the DEFAULT expressions in init.sql.
NON_NULLABLE_DEFAULTS: dict[str, dict] = {
    "balance_sheet":       {"quarter": 0},
    "cash_flow_statement": {"quarter": 0},
    "income_statement":    {"quarter": 0},
    "financial_ratios":    {"quarter": 0},
    "financial_reports":   {"quarter": 0},
}


def cast_date_columns(df, table: str):
    """Convert SQLite string date/time columns to proper Spark Date/Timestamp types.

    Parsing order for datetime columns:
      1. Full "yyyy-MM-dd HH:mm:ss"
      2. Date-only "yyyy-MM-dd"  → fills HH:mm:ss with 00:00:00
      3. Fallback literal 1970-01-01 00:00:00 for completely unparseable values

    Parsing order for date columns:
      1. "yyyy-MM-dd"
      2. Fallback literal 1970-01-01
    """
    casts = DATE_COLUMNS.get(table, {})
    for col_name, kind in casts.items():
        if col_name not in df.columns:
            continue
        if not isinstance(df.schema[col_name].dataType, StringType):
            continue
        c = col(col_name)
        if kind == "date":
            df = df.withColumn(col_name, coalesce(
                to_date(c, "yyyy-MM-dd"),
                lit("1970-01-01").cast("date"),
            ))
        else:
            df = df.withColumn(col_name, coalesce(
                to_timestamp(c, "yyyy-MM-dd HH:mm:ss"),
                to_timestamp(c, "yyyy-MM-dd"),          # missing time → 00:00:00
                lit("1970-01-01 00:00:00").cast("timestamp"),
            ))
    return df


def apply_non_nullable_defaults(df, table: str):
    """Fill NULLs in columns that are non-nullable in ClickHouse with their DEFAULT values."""
    defaults = {k: v for k, v in NON_NULLABLE_DEFAULTS.get(table, {}).items() if k in df.columns}
    if defaults:
        df = df.fillna(defaults)
    return df


def read_sqlite_table(spark: SparkSession, db_path: str, table: str):
    # Force date/datetime columns to STRING via customSchema so the SQLite JDBC
    # driver doesn't call getDate()/getTimestamp() — that path uses a fixed
    # `yyyy-MM-dd HH:mm:ss.SSS` regex and throws on date-only values like
    # "2024-11-05". cast_date_columns() then parses the strings in Spark, with
    # fallbacks for both `yyyy-MM-dd` and `yyyy-MM-dd HH:mm:ss`.
    #
    # `open_mode=1` opens the SQLite file READ-ONLY, eliminating any chance the
    # JDBC driver attempts a write (rollback journal, hot journal recovery, etc.)
    # while 8 partitions read in parallel.
    base = (
        spark.read
        .format("jdbc")
        .option("url", f"jdbc:sqlite:{db_path}?open_mode=1")
        .option("driver", "org.sqlite.JDBC")
    )

    # Merge hard-coded mapping with columns SQLite itself declares as DATE/TIME-like.
    # Hard-coded list drives downstream casting in cast_date_columns(); PRAGMA-
    # detected extras stay STRING and are written as text — ClickHouse's Date/
    # DateTime columns accept ISO-8601 strings natively.
    known_date_cols = set(DATE_COLUMNS.get(table, {}).keys())
    pragma_date_cols = detect_date_typed_columns(db_path, table)
    all_date_cols = known_date_cols | pragma_date_cols
    if all_date_cols:
        base = base.option(
            "customSchema",
            ", ".join(f"`{c}` STRING" for c in all_date_cols),
        )

    if table in PARTITION_TABLES:
        part_col = PARTITION_TABLES[table]
        conn = sqlite3.connect(db_path)
        try:
            cur = conn.cursor()
            cur.execute(f"SELECT MIN({part_col}), MAX({part_col}) FROM {table}")
            lo, hi = cur.fetchone()
        finally:
            conn.close()

        if lo is None or hi is None or lo == hi:
            return base.option("dbtable", table).load()

        return (
            base
            .option("dbtable", table)
            .option("partitionColumn", part_col)
            .option("lowerBound", str(lo))
            .option("upperBound", str(hi))
            .option("numPartitions", str(PARTITION_COUNT))
            .load()
        )

    return base.option("dbtable", table).load()


def write_clickhouse(df, ch_url: str, ch_user: str, ch_password: str, table: str) -> None:
    (
        df.write
        .format("jdbc")
        .option("url", ch_url)
        .option("driver", "com.clickhouse.jdbc.ClickHouseDriver")
        .option("dbtable", table)
        .option("user", ch_user)
        .option("password", ch_password)
        .option("batchsize", "10000")
        .mode("append")
        .save()
    )


def log_ingestion(ch_host: str, ch_port: str, ch_db: str,
                  ch_user: str, ch_password: str,
                  manifest: str, table: str,
                  source_file: str, file_type: str,
                  row_count: int, file_size: int, status: str) -> None:
    try:
        import requests
        e = lambda s: s.replace("'", "''")  # noqa: E731
        sql = (
            f"INSERT INTO {ch_db}._ingestion_log "
            f"(manifest_name, table_name, source_file, file_type, row_count, file_size, status) "
            f"VALUES ('{e(manifest)}', '{e(table)}', '{e(source_file)}', "
            f"'{e(file_type)}', {row_count}, {file_size}, '{status}')"
        )
        resp = requests.post(
            f"http://{ch_host}:{ch_port}/",
            auth=(ch_user, ch_password),
            data=sql,
            timeout=10,
        )
        if resp.status_code != 200:
            print(f"WARN _ingestion_log: {resp.text[:200]}")
    except Exception as exc:
        print(f"WARN _ingestion_log (non-fatal): {exc}")


def ingest_file(spark: SparkSession, file_info: dict, args) -> tuple[int, list[str]]:
    db_path = file_info["file_path"]
    file_type = file_info.get("file_type", "db").lower().lstrip(".")

    if not os.path.exists(db_path):
        print(f"ERROR file not found: {db_path}", file=sys.stderr)
        return 0, [f"<file not found: {db_path}>"]

    file_size = os.path.getsize(db_path)
    ch_url = f"jdbc:clickhouse://{args.clickhouse_host}:{args.clickhouse_port}/{args.clickhouse_database}"
    ch_user = os.environ["CLICKHOUSE_USER"]
    ch_password = os.environ["CLICKHOUSE_PASSWORD"]
    manifest = f"airflow-{args.batch_id}-{os.path.basename(db_path)}"

    tables = list_sqlite_tables(db_path)
    print(f"[{db_path}] tables: {tables}")

    total_rows = 0
    failed: list[str] = []

    for tbl in tables:
        if tbl not in KNOWN_TABLES:
            print(f"[{tbl}] unknown — skipping")
            continue

        try:
            df = read_sqlite_table(spark, db_path, tbl)
            df = sanitize_columns(df)
            df = cast_date_columns(df, tbl)
            df = apply_non_nullable_defaults(df, tbl)
            if tbl in DROP_ID_TABLES and "id" in df.columns:
                df = df.drop("id")

            rows = df.count()
            if rows == 0:
                print(f"[{tbl}] empty — skipping write")
                log_ingestion(args.clickhouse_host, args.clickhouse_port,
                              args.clickhouse_database, ch_user, ch_password,
                              manifest, tbl, db_path, file_type, 0, file_size, "success")
                continue

            print(f"[{tbl}] writing {rows} rows → ClickHouse.{args.clickhouse_database}.{tbl}")
            write_clickhouse(df, ch_url, ch_user, ch_password, tbl)
            log_ingestion(args.clickhouse_host, args.clickhouse_port,
                          args.clickhouse_database, ch_user, ch_password,
                          manifest, tbl, db_path, file_type, rows, file_size, "success")
            total_rows += rows

        except Exception as exc:
            print(f"[{tbl}] FAILED: {exc}", file=sys.stderr)
            log_ingestion(args.clickhouse_host, args.clickhouse_port,
                          args.clickhouse_database, ch_user, ch_password,
                          manifest, tbl, db_path, file_type, 0, file_size, "failed")
            failed.append(tbl)

    return total_rows, failed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--files-json", required=True,
                        help='JSON array: [{"file_path": "...", "file_type": "db"}, ...]')
    parser.add_argument("--clickhouse-host", required=True)
    parser.add_argument("--clickhouse-port", default="8123")
    parser.add_argument("--clickhouse-database", required=True)
    parser.add_argument("--batch-id", default="unknown")
    args = parser.parse_args()

    if "CLICKHOUSE_USER" not in os.environ or "CLICKHOUSE_PASSWORD" not in os.environ:
        print("ERROR: CLICKHOUSE_USER and CLICKHOUSE_PASSWORD must be set as env vars",
              file=sys.stderr)
        sys.exit(2)

    try:
        files = json.loads(args.files_json)
    except json.JSONDecodeError as exc:
        print(f"ERROR: --files-json is not valid JSON: {exc}", file=sys.stderr)
        sys.exit(2)

    if not files:
        print("ERROR: --files-json list is empty", file=sys.stderr)
        sys.exit(2)

    spark = build_spark()
    try:
        grand_total = 0
        all_failed: list[str] = []

        for file_info in files:
            try:
                rows, failed = ingest_file(spark, file_info, args)
                grand_total += rows
                all_failed.extend(failed)
            except Exception as exc:
                print(f"ERROR ingest_file {file_info.get('file_path', '?')}: {exc}", file=sys.stderr)
                all_failed.append(f"<file:{file_info.get('file_path', '?')}>")

        if all_failed:
            print(f"WARN skipped (logged to _ingestion_log): {all_failed}", file=sys.stderr)

        print(f"OK — batch_id={args.batch_id}, rows ingested: {grand_total}, skipped: {len(all_failed)}")
    finally:
        try:
            spark.stop()
        except Exception as e:
            print(f"WARN spark.stop(): {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
