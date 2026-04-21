"""
FinHouse — Pipeline Runner

Watches /data/checkpoint for JSON manifests produced by NiFi.
Each manifest describes one file ready for ingestion.

Manifest filename convention (contains timestamp for easy debug):
    manifest-<YYYY-MM-DDTHH-MM-SS>-<filename>.json

    e.g.  manifest-2026-04-17T10-30-45-customers.json

Manifest content (written by NiFi):
{
    "file_path":   "/data/OLAP/customers.csv",
    "file_type":   "csv",
    "table_name":  "customers",
    "detected_at": "2026-04-17T10:30:45Z",
    "file_size":   12345,
    "file_hash":   "sha256..."     # optional but recommended
}

Marker files written by this runner:
    <manifest>.processed    — success
    <manifest>.failed       — failure (contents = error reason)

If a .processed or .failed marker exists, the manifest is skipped.
Delete the marker to force reprocessing.
"""

import json
import logging
import os
import subprocess
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [runner] %(levelname)s: %(message)s",
)
log = logging.getLogger("pipeline-runner")

CHECKPOINT_DIR = Path(os.environ.get("CHECKPOINT_DIR", "/data/checkpoint"))
OLAP_DIR = Path(os.environ.get("OLAP_DIR", "/data/OLAP"))
SPARK_MASTER = os.environ.get("SPARK_MASTER", "spark://finhouse-spark-master:7077")
CLICKHOUSE_HOST = os.environ.get("CLICKHOUSE_HOST", "finhouse-clickhouse")
CLICKHOUSE_PORT = os.environ.get("CLICKHOUSE_PORT", "8123")
CLICKHOUSE_USER = os.environ.get("CLICKHOUSE_USER", "finhouse")
CLICKHOUSE_PASSWORD = os.environ.get("CLICKHOUSE_PASSWORD", "changeme_clickhouse")
CLICKHOUSE_DB = os.environ.get("CLICKHOUSE_DB", "olap")

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", "10"))

PROCESSED_SUFFIX = ".processed"
FAILED_SUFFIX = ".failed"

SPARK_JOB = "/opt/pipeline/ingest_job.py"


def is_pending(path: Path) -> bool:
    if not path.is_file():
        return False
    if not path.name.endswith(".json"):
        return False
    if path.with_name(path.name + PROCESSED_SUFFIX).exists():
        return False
    if path.with_name(path.name + FAILED_SUFFIX).exists():
        return False
    return True


def mark_processed(manifest: Path):
    marker = manifest.with_name(manifest.name + PROCESSED_SUFFIX)
    marker.write_text(
        f"processed_at: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n"
    )


def mark_failed(manifest: Path, reason: str):
    marker = manifest.with_name(manifest.name + FAILED_SUFFIX)
    marker.write_text(
        f"failed_at: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n"
        f"reason: {reason}\n"
    )


def submit_ingest_job(manifest: Path, data: dict) -> tuple[bool, str]:
    file_path = data["file_path"]
    file_type = data["file_type"].lower().lstrip(".")
    table_name = data["table_name"]

    if not Path(file_path).exists():
        return False, f"file does not exist: {file_path}"

    cmd = [
        "spark-submit",
        "--master", SPARK_MASTER,
        "--deploy-mode", "client",
        "--conf", "spark.driver.memory=1g",
        "--packages",
        "com.clickhouse:clickhouse-jdbc:0.6.3,"
        "org.xerial:sqlite-jdbc:3.46.0.0,"
        "com.crealytics:spark-excel_2.12:3.5.1_0.20.4",
        SPARK_JOB,
        "--input-path", file_path,
        "--file-type", file_type,
        "--table-name", table_name,
        "--clickhouse-host", CLICKHOUSE_HOST,
        "--clickhouse-port", CLICKHOUSE_PORT,
        "--clickhouse-user", CLICKHOUSE_USER,
        "--clickhouse-password", CLICKHOUSE_PASSWORD,
        "--clickhouse-database", CLICKHOUSE_DB,
        "--manifest-name", manifest.name,
    ]

    log.info(f"→ Spark submit: {file_path} → `{table_name}` (manifest={manifest.name})")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if result.returncode != 0:
            tail = result.stderr[-2048:] if result.stderr else "(no stderr)"
            log.error(f"Spark exit {result.returncode}:\n{tail}")
            return False, f"spark exit={result.returncode}: {tail[-500:]}"
        log.info(f"✓ Loaded {file_path} into {CLICKHOUSE_DB}.{table_name}")
        return True, "ok"
    except subprocess.TimeoutExpired:
        return False, "spark job timed out (30min)"
    except Exception as e:
        return False, f"spark submit error: {e}"


def process_one(manifest: Path) -> bool:
    try:
        data = json.loads(manifest.read_text())
    except Exception as e:
        mark_failed(manifest, f"invalid JSON: {e}")
        log.error(f"Invalid JSON {manifest}: {e}")
        return False

    for key in ("file_path", "file_type", "table_name"):
        if key not in data:
            mark_failed(manifest, f"missing field: {key}")
            log.error(f"Missing '{key}' in {manifest}")
            return False

    success, msg = submit_ingest_job(manifest, data)
    if success:
        mark_processed(manifest)
    else:
        mark_failed(manifest, msg)
    return success


def main_loop():
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    log.info(f"Pipeline runner started")
    log.info(f"  checkpoint dir: {CHECKPOINT_DIR}")
    log.info(f"  olap dir:       {OLAP_DIR}")
    log.info(f"  spark master:   {SPARK_MASTER}")
    log.info(f"  clickhouse:     {CLICKHOUSE_HOST}:{CLICKHOUSE_PORT}/{CLICKHOUSE_DB}")

    while True:
        try:
            pending = [p for p in CHECKPOINT_DIR.iterdir() if is_pending(p)]
        except Exception as e:
            log.error(f"Error listing checkpoint dir: {e}")
            time.sleep(POLL_INTERVAL)
            continue

        pending.sort(key=lambda p: p.name)
        if pending:
            log.info(f"{len(pending)} pending manifest(s)")
            for manifest in pending:
                try:
                    process_one(manifest)
                except Exception as e:
                    log.exception(f"Runner error on {manifest}")
                    mark_failed(manifest, f"runner exception: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        main_loop()
    except KeyboardInterrupt:
        log.info("Shutting down...")
