"""
FinHouse — Spark Ingest Job (SQLite → ClickHouse)

Reads all known tables from a SQLite .db file and appends into matching
ClickHouse tables. ClickHouse's ReplacingMergeTree engine handles dedup
at merge time based on the ORDER BY key.

Retry / error handling is the caller's responsibility (Airflow).
This script either succeeds entirely or exits non-zero.

Usage:
    spark-submit ingest_job.py \
        --input-path /data/OLAP/stocks.db \
        --file-type db \
        --table-name stocks \
        --clickhouse-host finhouse-clickhouse \
        --clickhouse-port 8123 \
        --clickhouse-user finhouse \
        --clickhouse-password xxx \
        --clickhouse-database olap \
        --manifest-name airflow-<batch-id>-stocks.db
"""

import argparse
import os
import sys
from pyspark.sql import SparkSession


# Tables we know how to target in ClickHouse (must match init.sql)
KNOWN_TABLES = {
    "stocks", "exchanges", "indices", "industries",
    "stock_exchange", "stock_index", "stock_industry",
    "company_overview", "balance_sheet", "cash_flow_statement",
    "income_statement", "financial_ratios", "financial_reports",
    "events", "news", "officers", "shareholders", "subsidiaries",
    "stock_intraday", "stock_price_history", "update_log",
}

# Reference tables where ClickHouse uses composite ORDER BY keys
# instead of SQLite's auto-increment id
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
    import sqlite3
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


def read_sqlite_table(spark, db_path: str, table: str):
    url = f"jdbc:sqlite:{db_path}"
    return (
        spark.read
        .format("jdbc")
        .option("url", url)
        .option("driver", "org.sqlite.JDBC")
        .option("dbtable", table)
        .load()
    )


def write_clickhouse(df, args, table_name: str):
    url = (
        f"jdbc:clickhouse://{args.clickhouse_host}:{args.clickhouse_port}"
        f"/{args.clickhouse_database}"
    )
    (
        df.write
        .format("jdbc")
        .option("url", url)
        .option("driver", "com.clickhouse.jdbc.ClickHouseDriver")
        .option("dbtable", table_name)
        .option("user", args.clickhouse_user)
        .option("password", args.clickhouse_password)
        .option("batchsize", "10000")
        # append — ReplacingMergeTree handles dedup at merge time
        .mode("append")
        .save()
    )


def log_ingestion(args, table_name: str, row_count: int,
                  file_size: int, status: str) -> None:
    """Best-effort insert into olap._ingestion_log."""
    try:
        import requests
        q_esc = chr(39)  # '
        sql = (
            f"INSERT INTO {args.clickhouse_database}._ingestion_log "
            f"(manifest_name, table_name, source_file, file_type, "
            f" row_count, file_size, status) VALUES "
            f"('{args.manifest_name.replace(q_esc, q_esc*2)}', "
            f" '{table_name}', "
            f" '{args.input_path.replace(q_esc, q_esc*2)}', "
            f" '{args.file_type}', "
            f" {row_count}, {file_size}, '{status}')"
        )
        resp = requests.post(
            f"http://{args.clickhouse_host}:{args.clickhouse_port}/",
            auth=(args.clickhouse_user, args.clickhouse_password),
            params={"database": args.clickhouse_database},
            data=sql,
            timeout=10,
        )
        if resp.status_code != 200:
            print(f"WARN: ingestion_log insert failed: {resp.text[:200]}")
    except Exception as e:
        print(f"WARN: ingestion_log write failed (non-fatal): {e}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-path", required=True)
    parser.add_argument("--file-type", required=True,
                        choices=["db", "sqlite"])
    parser.add_argument("--table-name", required=True,
                        help="Informational only; tables come from the .db file")
    parser.add_argument("--clickhouse-host", required=True)
    parser.add_argument("--clickhouse-port", default="8123")
    parser.add_argument("--clickhouse-user", required=True)
    parser.add_argument("--clickhouse-password", required=True)
    parser.add_argument("--clickhouse-database", required=True)
    parser.add_argument("--manifest-name", default="unknown")
    args = parser.parse_args()

    if not os.path.exists(args.input_path):
        print(f"ERROR: file not found: {args.input_path}", file=sys.stderr)
        sys.exit(2)

    file_size = os.path.getsize(args.input_path)
    spark = build_spark()

    try:
        sqlite_tables = list_sqlite_tables(args.input_path)
        print(f"[{args.input_path}] tables found: {sqlite_tables}")

        total_rows = 0
        skipped: list[str] = []
        failed_tables: list[str] = []

        for tbl in sqlite_tables:
            if tbl not in KNOWN_TABLES:
                skipped.append(tbl)
                continue

            try:
                df = read_sqlite_table(spark, args.input_path, tbl)
                df = sanitize_columns(df)
                if tbl in DROP_ID_TABLES and "id" in df.columns:
                    df = df.drop("id")

                rows = df.count()
                if rows == 0:
                    print(f"[{tbl}] empty — skipping write")
                    log_ingestion(args, tbl, 0, file_size, "success")
                    continue

                print(f"[{tbl}] writing {rows} rows → "
                      f"ClickHouse.{args.clickhouse_database}.{tbl}")
                write_clickhouse(df, args, tbl)
                log_ingestion(args, tbl, rows, file_size, "success")
                total_rows += rows

            except Exception as e:
                print(f"[{tbl}] FAILED: {e}", file=sys.stderr)
                log_ingestion(args, tbl, 0, file_size, "failed")
                failed_tables.append(tbl)

        if skipped:
            print(f"Skipped {len(skipped)} unknown table(s): {skipped}")
        if failed_tables:
            print(f"FAILED tables: {failed_tables}", file=sys.stderr)
            sys.exit(1)

        print(f"OK — total rows ingested: {total_rows}")

    finally:
        spark.stop()


if __name__ == "__main__":
    main()
