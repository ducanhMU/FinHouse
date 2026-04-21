"""
FinHouse — OLAP Ingestion DAG

Triggered by NiFi via POST to:
    /api/v1/dags/olap_ingest/dagRuns
with JSON body:
    {
        "conf": {
            "files": [
                {"file_path": "/data/OLAP/stocks.db", "file_type": "db",
                 "table_name": "stocks", "file_size": 12345, "file_hash": "..."},
                ...
            ],
            "batch_id": "<timestamp>-<nifi-flowfile-uuid>"
        }
    }

Behavior:
  • One task per file (processed in parallel up to DAG-level concurrency)
  • Each task retries 3 times with exponential backoff (30s, 2min, 8min)
  • On final failure, Airflow marks the task failed; NiFi can detect this
    via the DAG run status endpoint if it polls.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any

from airflow import DAG
from airflow.decorators import task
from airflow.operators.python import PythonOperator
from airflow.providers.apache.spark.operators.spark_submit import SparkSubmitOperator


SPARK_JOB_PATH = "/opt/pipeline/ingest_job.py"

# Jars needed on the Spark classpath for JDBC to both SQLite (read) and ClickHouse (write)
SPARK_PACKAGES = ",".join([
    "com.clickhouse:clickhouse-jdbc:0.6.3",
    "org.xerial:sqlite-jdbc:3.46.0.0",
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
    description="Load .db files from /data/OLAP into ClickHouse",
    default_args=DEFAULT_ARGS,
    schedule=None,              # Only triggered externally (by NiFi)
    start_date=datetime(2026, 1, 1),
    catchup=False,
    max_active_runs=4,          # cap parallel batches
    tags=["olap", "ingest", "finhouse"],
    params={
        "files": [],
        "batch_id": "manual",
    },
) as dag:

    @task(task_id="parse_batch")
    def parse_batch(**context) -> list[dict[str, Any]]:
        """Extract file list from DagRun conf, validate, return expanded list."""
        conf = context["dag_run"].conf or {}
        files = conf.get("files") or context["params"].get("files") or []

        if not files:
            raise ValueError(
                "No files in DagRun conf. Expected: {'files': [{...}, ...]}"
            )

        validated = []
        for f in files:
            if not isinstance(f, dict):
                continue
            if "file_path" not in f or "file_type" not in f:
                print(f"[parse_batch] skipping invalid entry: {f}")
                continue

            # Derive table_name from filename if NiFi didn't supply it
            if "table_name" not in f or not f["table_name"]:
                base = os.path.basename(f["file_path"])
                f["table_name"] = base.rsplit(".", 1)[0]
            validated.append(f)

        print(f"[parse_batch] {len(validated)} file(s) ready for ingest")
        return validated

    def _spark_submit_for_file(file_info: dict, **context) -> None:
        """
        Submit a Spark job for one file. Uses SparkSubmitOperator under
        the hood via direct instantiation so we can dynamically build per-file args.
        """
        file_path = file_info["file_path"]
        file_type = file_info["file_type"].lower().lstrip(".")
        table_name = file_info["table_name"]
        batch_id = (context["dag_run"].conf or {}).get("batch_id", "unknown")

        op = SparkSubmitOperator(
            task_id=f"_dynamic_submit_{os.path.basename(file_path)}",
            application=SPARK_JOB_PATH,
            conn_id="spark_default",
            packages=SPARK_PACKAGES,
            deploy_mode="client",
            executor_memory="1g",
            driver_memory="1g",
            name=f"olap_ingest_{os.path.basename(file_path)}",
            application_args=[
                "--input-path", file_path,
                "--file-type", file_type,
                "--table-name", table_name,
                "--clickhouse-host", os.environ["CLICKHOUSE_HOST"],
                "--clickhouse-port", os.environ.get("CLICKHOUSE_PORT", "8123"),
                "--clickhouse-user", os.environ["CLICKHOUSE_USER"],
                "--clickhouse-password", os.environ["CLICKHOUSE_PASSWORD"],
                "--clickhouse-database", os.environ["CLICKHOUSE_DB"],
                "--manifest-name", f"airflow-{batch_id}-{os.path.basename(file_path)}",
            ],
            verbose=False,
        )
        op.execute(context=context)

    @task(
        task_id="ingest_file",
        retries=3,
        retry_delay=timedelta(seconds=30),
        retry_exponential_backoff=True,
        max_retry_delay=timedelta(minutes=15),
    )
    def ingest_file(file_info: dict, **context) -> dict:
        """Wrapped Spark submit so per-file retry logic applies cleanly."""
        print(f"[ingest_file] processing: {file_info}")
        _spark_submit_for_file(file_info, **context)
        return {"file_path": file_info["file_path"], "status": "success"}

    # Expand one task per file (parallel)
    files = parse_batch()
    ingest_file.expand(file_info=files)
