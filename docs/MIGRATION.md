# Migration from pipeline-runner → Airflow

## What changed

- **Removed**: `pipeline/runner/` directory (pipeline-runner container)
- **Added**: `pipeline/airflow/dags/olap_ingest.py` — Airflow DAG with retry logic
- **Added**: 4 Airflow services in `stacks/pipeline.yml` (postgres, init, scheduler, webserver)
- **Changed**: NiFi flow. Old: writes JSON to `/data/checkpoint`.
                              New: sends POST to Airflow REST API.
- **Simplified**: `pipeline/spark/ingest_job.py` — only handles SQLite `.db` now

## Apply

```bash
cd /path/to/FinHouse

# Stop the old pipeline stack (includes the deprecated runner)
./start.sh pipeline down

# Remove the old runner directory
rm -rf pipeline/runner

# Apply patch (overwrites stacks/pipeline.yml, spark job, NiFi readme, .env.example)
unzip -o ~/Downloads/airflow-pipeline-patch.zip
cp -r airflow-pipeline-patch/* ./
rm -rf airflow-pipeline-patch

# Create Airflow dirs with the right ownership
sudo chown -R ${AIRFLOW_UID:-50000}:0 pipeline/airflow/logs pipeline/airflow/plugins

# Add new env vars. Open .env.example to see the new Airflow section,
# then copy the variables to your .env:
grep -A6 "Airflow" .env.example  # see them
# Edit .env — add AIRFLOW_USER, AIRFLOW_PASSWORD, AIRFLOW_DB_PASSWORD, AIRFLOW_FERNET_KEY

# Generate a Fernet key (required for prod, optional for dev):
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Paste output into AIRFLOW_FERNET_KEY in .env

# Start
./start.sh pipeline

# Wait ~3 min for init + Airflow + NiFi to all boot
docker compose -f stacks/pipeline.yml logs -f finhouse-airflow-init
# Should eventually see "User admin created" and exit 0

# Check Airflow UI: http://localhost:8091 (admin / AIRFLOW_PASSWORD)
# Check NiFi UI: http://localhost:8090 (admin / NIFI_PASSWORD)
# Check ClickHouse: curl http://localhost:28123/ping
```

## Configure NiFi flow

See `pipeline/nifi/README.md` for the complete 15-min batch flow.
Key shift: replace `PutFile → /data/checkpoint` with `MergeContent → InvokeHTTP → Airflow`.

## Wipe old checkpoint files (optional)

They're not read by anything anymore:

```bash
rm -rf data/checkpoint/*
```
