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


def read_sqlite_table(spark: SparkSession, db_path: str, table: str):
    return (
        spark.read
        .format("jdbc")
        .option("url", f"jdbc:sqlite:{db_path}")
        .option("driver", "org.sqlite.JDBC")
        .option("dbtable", table)
        .load()
    )


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
            rows, failed = ingest_file(spark, file_info, args)
            grand_total += rows
            all_failed.extend(failed)

        if all_failed:
            print(f"FAILED tables: {all_failed}", file=sys.stderr)
            sys.exit(1)

        print(f"OK — batch_id={args.batch_id}, total rows ingested: {grand_total}")
    finally:
        spark.stop()


if __name__ == "__main__":
    main()
