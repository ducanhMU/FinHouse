"""
FinHouse — CSV Ingestion DAG (cash_dividend / stock_dividend / subsidiaries)

NiFi triggers this DAG via InvokeHTTP processor:
    POST http://finhouse-airflow-webserver:8080/api/v1/dags/csv_ingest/dagRuns
    Authorization: Basic <base64(AIRFLOW_USER:AIRFLOW_PASSWORD)>
    Content-Type: application/json

    {
        "conf": {
            "batch_id": "20260501-<nifi-flowfile-uuid>",
            "files": [
                {"file_path": "/opt/finhouse/data/ACB/csv/cash_dividend.csv",
                 "target_table": "cash_dividend"},
                {"file_path": "/opt/finhouse/data/ACB/csv/subs.csv",
                 "target_table": "subsidiaries"}
            ]
        }
    }

Each entry must include `file_path` and `target_table`. `target_table` must be one
of: cash_dividend, stock_dividend, subsidiaries.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from typing import Any

from airflow import DAG
from airflow.decorators import task

SPARK_HOME = os.environ.get("SPARK_HOME", "/opt/spark")
SPARK_SUBMIT = f"{SPARK_HOME}/bin/spark-submit"
SPARK_MASTER = "spark://finhouse-spark-master:7077"
SPARK_JOB_PATH = "/opt/airflow/spark/csv_ingest_job.py"
SPARK_JARS = "/opt/spark/jars/clickhouse-jdbc-0.6.3-all.jar"

SUPPORTED_TABLES = {"cash_dividend", "stock_dividend", "subsidiaries"}

DEFAULT_ARGS = {
    "owner": "finhouse",
    "retries": 3,
    "retry_delay": timedelta(seconds=30),
    "retry_exponential_backoff": True,
    "max_retry_delay": timedelta(minutes=15),
    "depends_on_past": False,
    "email_on_failure": False,
    "email_on_retry": False,
}

with DAG(
    dag_id="csv_ingest",
    description="NiFi-triggered: read CSV from /opt/finhouse/data, append to ClickHouse via Spark",
    default_args=DEFAULT_ARGS,
    schedule=None,
    start_date=datetime(2026, 1, 1),
    catchup=False,
    max_active_runs=4,
    tags=["olap", "ingest", "csv", "finhouse"],
    params={
        "files": [],
        "batch_id": "manual",
    },
) as dag:

    @task(task_id="validate_conf")
    def validate_conf(**context) -> dict[str, Any]:
        conf = context["dag_run"].conf or {}
        files = conf.get("files") or context["params"].get("files") or []

        valid: list[dict] = []
        for f in files:
            if not isinstance(f, dict):
                continue
            if "file_path" not in f or "target_table" not in f:
                continue
            if f["target_table"] not in SUPPORTED_TABLES:
                print(f"[validate_conf] skipping unsupported target_table={f['target_table']!r}")
                continue
            valid.append({"file_path": f["file_path"], "target_table": f["target_table"]})

        if not valid:
            raise ValueError(
                "conf.files must be a non-empty list of {file_path, target_table} dicts. "
                f"target_table must be one of {sorted(SUPPORTED_TABLES)}. "
                "Example: [{\"file_path\": \"/opt/finhouse/data/ACB/csv/cash_dividend.csv\", "
                "\"target_table\": \"cash_dividend\"}]"
            )

        batch_id = conf.get("batch_id") or context["params"].get("batch_id", "manual")
        print(f"[validate_conf] batch_id={batch_id}, files={len(valid)}")
        return {"files": valid, "batch_id": batch_id}

    @task(
        task_id="run_spark_csv_ingest",
        retries=3,
        retry_delay=timedelta(seconds=30),
        retry_exponential_backoff=True,
        max_retry_delay=timedelta(minutes=15),
        execution_timeout=timedelta(hours=1),
    )
    def run_spark_csv_ingest(batch: dict[str, Any]) -> None:
        cmd = [
            SPARK_SUBMIT,
            "--master", SPARK_MASTER,
            "--deploy-mode", "client",
            "--jars", SPARK_JARS,
            "--executor-memory", "2g",
            "--driver-memory", "1g",
            "--name", f"finhouse_csv_{batch['batch_id']}",
            SPARK_JOB_PATH,
            "--files-json", json.dumps(batch["files"]),
            "--clickhouse-host", os.environ["CLICKHOUSE_HOST"],
            "--clickhouse-port", os.environ.get("CLICKHOUSE_PORT", "8123"),
            "--clickhouse-database", os.environ["CLICKHOUSE_DB"],
            "--batch-id", batch["batch_id"],
        ]

        print(f"[run_spark_csv_ingest] submitting batch_id={batch['batch_id']} "
              f"with {len(batch['files'])} file(s)")

        result = subprocess.run(cmd, text=True, capture_output=True)
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        if result.returncode != 0:
            raise subprocess.CalledProcessError(result.returncode, cmd)

    batch = validate_conf()
    run_spark_csv_ingest(batch)
