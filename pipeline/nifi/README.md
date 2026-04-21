# FinHouse — NiFi Flow Setup

NiFi polls `/data/OLAP` every 15 minutes, batches the detected files,
and triggers an Airflow DAG via HTTP. Airflow owns retry / scheduling /
observability of the Spark ingest.

## Default credentials

```
NiFi    — http://<host>:8090/nifi
  user: admin
  pass: changeme_nifi_12chars_min       (override: NIFI_PASSWORD)

Airflow — http://<host>:8091
  user: admin
  pass: changeme_airflow_12c_min        (override: AIRFLOW_PASSWORD)
```

Login at NiFi, accept the self-signed cert warning.

## The flow (high level)

```
┌─────────────────────────────────────────┐
│ ListSFTP  (poll every 15 min)           │
│   Hostname: localhost                    │
│   Remote Path: /data/OLAP                │
│   File Filter: .*\.db$                   │
│   Listing Strategy: Tracking Timestamps  │
│   Scheduling:        Cron: 0 */15 * * *  │
│   (or: Timer, 15 min)                    │
└──────────────┬──────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────┐
│ FetchSFTP                                │
│   Downloads file bytes for MergeContent  │
│   (only strictly needed if the emitted   │
│    payload should contain the file;      │
│    for our metadata-only flow this is    │
│    OPTIONAL — see note below.)           │
└──────────────┬──────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────┐
│ UpdateAttribute                          │
│   file.type   = ${filename:              │
│                    substringAfterLast('.')} │
│   table.name  = ${filename:              │
│                    substringBeforeLast('.')} │
│   detected.at = ${now():format(          │
│                    "yyyy-MM-dd'T'HH:mm:ss'Z'")} │
└──────────────┬──────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────┐
│ AttributesToJSON                         │
│   Destination: flowfile-content          │
│   Attributes List: file.type, table.name,│
│                    detected.at, filename,│
│                    path, fileSize        │
│   (Replaces FlowFile content with a JSON │
│    object of selected attributes.)       │
└──────────────┬──────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────┐
│ MergeContent                             │
│   Merge Strategy: Bin-Packing Algorithm  │
│   Minimum Number of Entries: 1           │
│   Maximum Number of Entries: 1000        │
│   Maximum Bin Age: 15 min    ← key       │
│   Merge Format: Binary Concatenation     │
│     OR write a small Groovy script to    │
│     build a JSON array (see below)       │
│   Delimiter Strategy: Text               │
│   Header: [                              │
│   Footer: ]                              │
│   Demarcator: ,                          │
└──────────────┬──────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────┐
│ ReplaceText  (wrap batch into DAG conf)  │
│   Replacement Strategy: Always Replace   │
│   Replacement Value:                     │
│   {                                      │
│     "conf": {                            │
│       "files": ${flowfile.content},      │
│       "batch_id": "${now():format(       │
│         'yyyyMMdd-HHmmss')}-${UUID()}"   │
│     }                                    │
│   }                                      │
└──────────────┬──────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────┐
│ InvokeHTTP                               │
│   HTTP Method: POST                      │
│   Remote URL: http://finhouse-airflow-   │
│     webserver:8080/api/v1/dags/          │
│     olap_ingest/dagRuns                  │
│   Content-Type: application/json         │
│   SSL Context Service: —                 │
│   Basic Authentication Username: admin   │
│   Basic Authentication Password:         │
│     ${AIRFLOW_PASSWORD_RUNTIME_PROPERTY} │
│   Send Message Body: true                │
│   Response-Body-attribute-Name: response │
│   Penalize on No Retry: true             │
│   Auto-terminate relationships:          │
│     Failure, Retry, No Retry, Original   │
└─────────────────────────────────────────┘
```

## Key choices explained

### Listing strategy

`Tracking Timestamps` remembers the last-seen `lastModifiedTime` of each
path in NiFi state. Restart-safe; files are only listed once per modify.

If you prefer **`Tracking Entities`** (which uses a state entity per file
and survives filesystem timestamp tricks), switch it in the ListSFTP
properties — semantics are similar, storage cost slightly higher.

### Why MergeContent

Without merge, InvokeHTTP fires once per detected file → hundreds of
Airflow DAG runs, metadata-DB pressure. With merge, 15 minutes of
detections → **one DAG run**, each file becomes a mapped task inside
the DAG and runs in parallel up to Airflow's concurrency cap.

- **Minimum Number of Entries = 1** — don't block if only one file shows up
- **Maximum Bin Age = 15 min** — force-flush even if bin is not full
- **Maximum Number of Entries = 1000** — safety cap on batch size

### MergeContent format options

**Option A (recommended): JSON array via `AttributesToJSON` upstream**

Each FlowFile's content becomes a JSON object (the one you build in
`AttributesToJSON`). MergeContent concatenates them separated by commas,
wrapping in `[` / `]` header/footer. The merged content is a valid
JSON array — exactly what the DAG expects.

**Option B: Groovy `ExecuteScript` after merge**

If you want to transform the merged body further (add fields, rename keys,
etc.), put an `ExecuteScript` processor after MergeContent. Skip unless
you need it; the simple concatenation above is enough.

### Why InvokeHTTP instead of a direct Spark call

- Airflow provides retry logic, DAG-level observability, backfill, and
  pausing — all things you'd otherwise reimplement.
- NiFi is great at file detection, poor at retry orchestration.
- Separation: NiFi = "tell me when there's work", Airflow = "do the work reliably".

### Airflow REST API auth

Airflow's basic_auth backend uses the same username/password as the UI.
In NiFi's `InvokeHTTP`:
- Username: `admin` (or whatever `AIRFLOW_USER` is set to)
- Password: `${AIRFLOW_PASSWORD_RUNTIME_PROPERTY}` — store the password
  in NiFi's Variable Registry or Parameter Context so it doesn't show
  in the flow XML. OR just hardcode in a dev setup.

**Important:** Airflow's API requires a specific JSON shape. The request
body sent by `InvokeHTTP` must look EXACTLY like:

```json
{
  "conf": {
    "files": [
      {"file_path": "/data/OLAP/stocks.db", "file_type": "db",
       "table_name": "stocks", "file_size": 123, "detected_at": "..."}
    ],
    "batch_id": "20260420-143000-abc123"
  }
}
```

Our `ReplaceText` step constructs this envelope.

## Retry handling (errors from Airflow)

`InvokeHTTP` has relationships: `Response`, `Retry`, `Failure`, `No Retry`, `Original`.
- `Response` (HTTP 2xx) — DAG run accepted. You can terminate.
- `Retry` (HTTP 5xx / connection refused) — wire to `InvokeHTTP` itself
  via a Wait processor OR to a separate `LogMessage` → `Email` → DLQ.
- `Failure` (NiFi-internal) — log and terminate.
- `No Retry` (HTTP 4xx) — these are config errors. DON'T retry. Route to
  `LogMessage` with high severity so you get paged.
- `Original` — the FlowFile that was sent. Terminate.

Simple approach for now: auto-terminate all but `Response` with logging;
tune once you see real failure modes.

## Verification after setup

```bash
# 1. Drop a test .db file
cp /path/to/real/stocks.db /path/to/FinHouse/data/OLAP/

# 2. Within 15 min, NiFi should send a POST. Tail:
docker logs -f finhouse-nifi | grep -iE "InvokeHTTP|2[0-9]{2}"

# 3. Check Airflow UI at :8091 — you should see a dagRun for olap_ingest
#    Or via API:
curl -u "$AIRFLOW_USER:$AIRFLOW_PASSWORD" \
  http://localhost:8091/api/v1/dags/olap_ingest/dagRuns | jq

# 4. Check ClickHouse was populated
docker exec finhouse-clickhouse clickhouse-client \
  --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" \
  --query "SELECT table_name, sum(row_count) FROM olap._ingestion_log \
           WHERE ingested_at > now() - INTERVAL 1 HOUR \
           GROUP BY table_name"
```

## Forcing immediate detection (skip 15-min wait)

Right-click ListSFTP → `Run Once`. Pulls all new files immediately, then
resumes the 15-min schedule afterward.

## Troubleshooting

- **"401 Unauthorized" in InvokeHTTP response** — Airflow basic_auth password
  is wrong. Update the Properties of InvokeHTTP; the password is in `.env`
  as `AIRFLOW_PASSWORD`.
- **DAG gets triggered but fails on `parse_batch` with "No files"** — merge
  step didn't produce a valid JSON array. Inspect the FlowFile content
  just before InvokeHTTP (right-click → View Data Provenance).
- **Airflow DAG times out on Spark submit** — `spark_default` connection URL
  is wrong, or Spark master unreachable. Check Airflow → Admin → Connections.
- **Multiple DAG runs for the same file** — ListSFTP state got reset.
  Delete `volumes/nifi/state/` and restart NiFi. Files will be detected
  once, cleanly.
