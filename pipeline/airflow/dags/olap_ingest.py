"""
FinHouse — OLAP Ingestion DAG

NiFi triggers this DAG via InvokeHTTP processor:
    POST http://finhouse-airflow-webserver:8080/api/v1/dags/olap_ingest/dagRuns
    Authorization: Basic <base64(AIRFLOW_USER:AIRFLOW_PASSWORD)>
    Content-Type: application/json

    {
        "conf": {
            "batch_id": "20260427-<nifi-flowfile-uuid>",
            "files": [
                {"file_path": "/opt/finhouse/data/OLAP/stocks.db", "file_type": "db"}
            ]
        }
    }

The DAG validates the conf then submits a single Spark job that processes
all files in the batch (one SparkContext, multiple SQLite sources → ClickHouse).
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
SPARK_JOB_PATH = "/opt/airflow/spark/ingest_job.py"
SPARK_JARS = ",".join([
    "/opt/spark/jars/sqlite-jdbc-3.46.0.0.jar",
    "/opt/spark/jars/clickhouse-jdbc-0.6.3-all.jar",
])

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
    dag_id="olap_ingest",
    description="NiFi-triggered: read SQLite from /opt/finhouse/data, upsert to ClickHouse via Spark",
    default_args=DEFAULT_ARGS,
    schedule=None,
    start_date=datetime(2026, 1, 1),
    catchup=False,
    max_active_runs=4,
    tags=["olap", "ingest", "finhouse"],
    params={
        "files": [],
        "batch_id": "manual",
    },
) as dag:

    @task(task_id="validate_conf")
    def validate_conf(**context) -> dict[str, Any]:
        conf = context["dag_run"].conf or {}
        files = conf.get("files") or context["params"].get("files") or []

        valid = [
            f for f in files
            if isinstance(f, dict) and "file_path" in f and "file_type" in f
        ]
        if not valid:
            raise ValueError(
                "conf.files must be a non-empty list of {file_path, file_type} dicts. "
                "Example: [{\"file_path\": \"/opt/finhouse/data/OLAP/stocks.db\", "
                "\"file_type\": \"db\"}]"
            )

        batch_id = conf.get("batch_id") or context["params"].get("batch_id", "manual")
        print(f"[validate_conf] batch_id={batch_id}, files={len(valid)}")
        return {"files": valid, "batch_id": batch_id}

    @task(
        task_id="run_spark_ingest",
        retries=3,
        retry_delay=timedelta(seconds=30),
        retry_exponential_backoff=True,
        max_retry_delay=timedelta(minutes=15),
        execution_timeout=timedelta(hours=2),
    )
    def run_spark_ingest(batch: dict[str, Any]) -> None:
        cmd = [
            SPARK_SUBMIT,
            "--master", SPARK_MASTER,
            "--deploy-mode", "client",
            "--jars", SPARK_JARS,
            "--executor-memory", "2g",
            "--driver-memory", "1g",
            "--name", f"finhouse_olap_{batch['batch_id']}",
            SPARK_JOB_PATH,
            "--files-json", json.dumps(batch["files"]),
            "--clickhouse-host", os.environ["CLICKHOUSE_HOST"],
            "--clickhouse-port", os.environ.get("CLICKHOUSE_PORT", "8123"),
            "--clickhouse-database", os.environ["CLICKHOUSE_DB"],
            "--batch-id", batch["batch_id"],
        ]

        # CLICKHOUSE_USER and CLICKHOUSE_PASSWORD are passed via inherited env vars,
        # not as CLI args, to keep them out of process listings.
        print(f"[run_spark_ingest] submitting batch_id={batch['batch_id']} "
              f"with {len(batch['files'])} file(s)")

        result = subprocess.run(cmd, text=True, capture_output=True)
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        if result.returncode != 0:
            raise subprocess.CalledProcessError(result.returncode, cmd)

    batch = validate_conf()
    run_spark_ingest(batch)
