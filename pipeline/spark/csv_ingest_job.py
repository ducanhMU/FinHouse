"""
FinHouse — Spark CSV Ingest Job (CSV → ClickHouse)

Reads CSV files and writes them to a target ClickHouse table. Currently supports:
    - cash_dividend
    - stock_dividend
    - subsidiaries

Behavior:
    - CSV header columns are case/whitespace-normalized (lowercase, spaces→_, etc.)
      and matched against the target schema. Extra columns in the CSV are dropped.
    - Target columns missing from the CSV are NOT written, so ClickHouse fills them
      via DEFAULT (e.g. created_at = now()) or NULL (Nullable columns).
    - Non-nullable columns without a DEFAULT (`symbol`, `id`) get a fallback value
      when missing/empty so the write doesn't fail; bad rows are still ingested
      with placeholders rather than dropped.

Usage:
    spark-submit csv_ingest_job.py \
        --files-json '[{"file_path":"/opt/finhouse/data/ACB/csv/cash_dividend.csv","target_table":"cash_dividend"}]' \
        --clickhouse-host finhouse-clickhouse \
        --clickhouse-port 8123 \
        --clickhouse-database olap \
        --batch-id <batch_id>

ClickHouse credentials are read from CLICKHOUSE_USER / CLICKHOUSE_PASSWORD env vars.
"""

import argparse
import json
import os
import sys

from pyspark.sql import SparkSession
from pyspark.sql.functions import coalesce, col, expr, lit, regexp_replace, to_date, when


# Target table schemas. Each entry maps source-normalized column → spec:
#   type     : "string" | "date" | "double" | "long" | "int"
#   nullable : whether ClickHouse column accepts NULL
#   fallback : value used for non-nullable columns when CSV is missing/empty
#              (omitted ⇒ column relies on ClickHouse DEFAULT or remains NULL)
TARGET_SCHEMAS: dict[str, dict[str, dict]] = {
    "cash_dividend": {
        "symbol":        {"type": "string", "nullable": False, "fallback": "UNKNOWN"},
        "record_date":   {"type": "date",   "nullable": True},
        "payment_date":  {"type": "date",   "nullable": True},
        "exercise_rate": {"type": "double", "nullable": True},
        "dps":           {"type": "double", "nullable": True},
        "currency":      {"type": "string", "nullable": True},
        "dividend_year": {"type": "int",    "nullable": True},
        "duration":      {"type": "string", "nullable": True},
    },
    "stock_dividend": {
        "symbol":        {"type": "string", "nullable": False, "fallback": "UNKNOWN"},
        "record_date":   {"type": "date",   "nullable": True},
        "payment_date":  {"type": "date",   "nullable": True},
        "exercise_rate": {"type": "double", "nullable": True},
        "plan_volume":   {"type": "long",   "nullable": True},
        "issue_volume":  {"type": "long",   "nullable": True},
        "currency":      {"type": "string", "nullable": True},
        "dividend_year": {"type": "int",    "nullable": True},
        "duration":      {"type": "string", "nullable": True},
    },
    "subsidiaries": {
        # `id` is non-nullable in ClickHouse without DEFAULT — generate one if missing.
        "id":                {"type": "string", "nullable": False},
        "symbol":            {"type": "string", "nullable": True},
        "sub_organ_code":    {"type": "string", "nullable": True},
        "ownership_percent": {"type": "double", "nullable": True},
        "organ_name":        {"type": "string", "nullable": True},
        "type":              {"type": "string", "nullable": True},
        # created_at handled by ClickHouse DEFAULT now()
    },
}

# Common header aliases → canonical column name. Lets us tolerate exports that use
# different casing or naming conventions for the same field.
HEADER_ALIASES: dict[str, dict[str, str]] = {
    "cash_dividend": {
        "ticker": "symbol",
        "ex_rate": "exercise_rate",
        "exerciserate": "exercise_rate",
        "dividend_per_share": "dps",
        "ex_date": "record_date",
        "pay_date": "payment_date",
        "year": "dividend_year",
    },
    "stock_dividend": {
        "ticker": "symbol",
        "ex_rate": "exercise_rate",
        "exerciserate": "exercise_rate",
        "ex_date": "record_date",
        "pay_date": "payment_date",
        "year": "dividend_year",
        "planvolume": "plan_volume",
        "issuevolume": "issue_volume",
    },
    "subsidiaries": {
        "ticker": "symbol",
        "subsidiary_id": "id",
        "sub_id": "id",
        "code": "sub_organ_code",
        "subcode": "sub_organ_code",
        "name": "organ_name",
        "ownership": "ownership_percent",
        "ownership_pct": "ownership_percent",
    },
}


def build_spark() -> SparkSession:
    return (
        SparkSession.builder
        .appName("FinHouse-CSV-Ingest")
        .config("spark.sql.session.timeZone", "UTC")
        .getOrCreate()
    )


def normalize_header(name: str) -> str:
    return (
        name.strip()
        .lower()
        .replace(" ", "_")
        .replace("-", "_")
        .replace(".", "_")
        .replace("(", "")
        .replace(")", "")
        .replace("%", "pct")
    )


def cast_column(c, kind: str):
    if kind == "date":
        # Try ISO first, fall back to common Vietnamese-export formats.
        return coalesce(
            to_date(c, "yyyy-MM-dd"),
            to_date(c, "dd/MM/yyyy"),
            to_date(c, "dd-MM-yyyy"),
            to_date(c, "MM/dd/yyyy"),
        )
    if kind in ("double", "long", "int"):
        # Strip thousands separators that some exports add.
        return regexp_replace(c.cast("string"), ",", "").cast(kind)
    return c.cast("string")


def shape_to_schema(df, table: str):
    """Drop unknown columns, rename via aliases, cast to target types,
    and add fallbacks for non-nullable columns missing from the CSV."""
    schema = TARGET_SCHEMAS[table]
    aliases = HEADER_ALIASES.get(table, {})

    # 1. Normalize headers and resolve aliases.
    rename: dict[str, str] = {}
    for src in df.columns:
        norm = normalize_header(src)
        canonical = aliases.get(norm, norm)
        rename[src] = canonical
    df = df.toDF(*[rename[c] for c in df.columns])

    # 2. If the CSV has duplicate canonical columns after aliasing, keep the first.
    seen: set[str] = set()
    keep_cols: list[str] = []
    for c in df.columns:
        if c not in seen:
            seen.add(c)
            keep_cols.append(c)
    df = df.select(*keep_cols)

    # 3. Drop columns that aren't part of the target schema.
    df = df.select(*[c for c in df.columns if c in schema])

    # 4. Cast and apply fallbacks for known target columns.
    for col_name, spec in schema.items():
        if col_name in df.columns:
            casted = cast_column(col(col_name), spec["type"])
            if not spec["nullable"]:
                fallback = spec.get("fallback")
                if fallback is not None:
                    # Replace NULL with fallback literal for non-nullable cols.
                    casted = when(casted.isNull(), lit(fallback)).otherwise(casted)
            df = df.withColumn(col_name, casted)

    # 5. For non-nullable columns missing from the CSV, fill in a fallback so the
    #    write doesn't fail. If no fallback (e.g. subsidiaries.id), generate a UUID.
    for col_name, spec in schema.items():
        if col_name in df.columns or spec["nullable"]:
            continue
        fallback = spec.get("fallback")
        if fallback is not None:
            print(f"[{table}] WARN: column '{col_name}' missing from CSV — filling with {fallback!r}")
            df = df.withColumn(col_name, lit(fallback).cast(spec["type"] if spec["type"] != "date" else "date"))
        elif col_name == "id":
            print(f"[{table}] WARN: column 'id' missing from CSV — generating UUIDs")
            df = df.withColumn("id", expr("uuid()"))
        else:
            print(f"[{table}] WARN: non-nullable column '{col_name}' missing — filling with empty string")
            df = df.withColumn(col_name, lit("").cast("string"))

    return df


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


def ingest_file(spark: SparkSession, file_info: dict, args) -> tuple[int, str | None]:
    csv_path = file_info["file_path"]
    target_table = file_info.get("target_table")

    # Chỉ xử lý file có tên (không tính đuôi) trùng với một bảng trong TARGET_SCHEMAS.
    filename_stem = os.path.splitext(os.path.basename(csv_path))[0]
    if filename_stem not in TARGET_SCHEMAS:
        print(f"SKIP: filename {filename_stem!r} không khớp bảng nào — {csv_path}")
        return 0, None

    if not target_table or target_table not in TARGET_SCHEMAS:
        print(f"ERROR: unsupported target_table={target_table!r} for {csv_path}", file=sys.stderr)
        return 0, target_table or "<unknown>"

    if not os.path.exists(csv_path):
        print(f"ERROR file not found: {csv_path}", file=sys.stderr)
        return 0, target_table

    file_size = os.path.getsize(csv_path)
    file_type = os.path.splitext(csv_path)[1].lstrip(".").lower() or "csv"
    ch_url = f"jdbc:clickhouse://{args.clickhouse_host}:{args.clickhouse_port}/{args.clickhouse_database}"
    ch_user = os.environ["CLICKHOUSE_USER"]
    ch_password = os.environ["CLICKHOUSE_PASSWORD"]
    manifest = f"airflow-{args.batch_id}-{os.path.basename(csv_path)}"

    try:
        df = (
            spark.read
            .option("header", "true")
            .option("inferSchema", "false")
            .option("multiLine", "true")
            .option("escape", "\"")
            .option("mode", "PERMISSIVE")
            .csv(csv_path)
        )

        df = shape_to_schema(df, target_table)
        rows = df.count()

        if rows == 0:
            print(f"[{target_table}] empty — skipping write ({csv_path})")
            log_ingestion(args.clickhouse_host, args.clickhouse_port,
                          args.clickhouse_database, ch_user, ch_password,
                          manifest, target_table, csv_path, file_type, 0, file_size, "success")
            return 0, None

        print(f"[{target_table}] writing {rows} rows from {csv_path} → "
              f"{args.clickhouse_database}.{target_table}")
        write_clickhouse(df, ch_url, ch_user, ch_password, target_table)
        log_ingestion(args.clickhouse_host, args.clickhouse_port,
                      args.clickhouse_database, ch_user, ch_password,
                      manifest, target_table, csv_path, file_type, rows, file_size, "success")
        return rows, None

    except Exception as exc:
        print(f"[{target_table}] FAILED ({csv_path}): {exc}", file=sys.stderr)
        log_ingestion(args.clickhouse_host, args.clickhouse_port,
                      args.clickhouse_database, ch_user, ch_password,
                      manifest, target_table, csv_path, file_type, 0, file_size, "failed")
        return 0, f"{target_table}:{os.path.basename(csv_path)}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--files-json", required=True,
                        help='JSON array: [{"file_path":"...","target_table":"cash_dividend"}, ...]')
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
                if failed:
                    all_failed.append(failed)
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
