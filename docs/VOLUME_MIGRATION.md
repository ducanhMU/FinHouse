# FinHouse — Volume Migration Guide

Before applying all recent patches, review this guide to **keep existing
data** instead of starting from scratch.

## Volumes in the project

```
./volumes/
├── postgres/             ← main app DB (users, projects, sessions, files metadata)
├── minio/                ← uploaded file blobs
├── milvus/               ← RAG vector embeddings
├── etcd/                 ← Milvus metadata (needed for milvus to work)
├── ollama/               ← Ollama model files (~10 GB per model)
├── clickhouse/           ← OLAP database (added later)
├── airflow-postgres/     ← Airflow metadata DB (added later)
└── nifi/
    ├── conf/             ← NiFi flow definitions
    ├── state/            ← NiFi listing-timestamps, processor state
    └── logs/             ← NiFi application logs
```

## Compatibility summary per volume

| Volume | Compatible? | Migration needed? |
|---|---|---|
| postgres | **Yes**, but see auth username normalization below | Optional SQL one-liner |
| minio | **Yes** — path change is backward-compatible | None |
| milvus | **Yes** — schema unchanged, search filter updated | None |
| etcd | **Yes** — used by milvus internally | None |
| ollama | **Yes** — just model files | None |
| clickhouse | **Yes** if schema matches current `init.sql` | Conditional, see below |
| airflow-postgres | **Yes** | None |
| nifi | **Yes** | None |

## Things that changed in recent patches that might affect existing data

### 1. Username normalization (auth.py) — **needs action if you have existing users**

The register/login endpoints now normalize usernames to lowercase + strip whitespace
before comparing. If your existing `user` table has usernames like `"Alice"` or `" bob "`,
login will fail because it searches for `"alice"` / `"bob"` (lowercase).

**Fix: one-time SQL to normalize existing usernames.**

```bash
# On ip1 (or wherever Postgres volume lives)
docker exec -it finhouse-postgres psql -U finhouse -d finhouse

# Check current state
SELECT user_id, user_name, LOWER(TRIM(user_name)) AS normalized
  FROM "user"
  WHERE user_name != LOWER(TRIM(user_name));

# If any rows differ, update in place. Note: if two usernames differ only
# in case (e.g. "Alice" + "alice"), the UPDATE will fail on UNIQUE.
# Check first, then resolve manually.
UPDATE "user" SET user_name = LOWER(TRIM(user_name))
  WHERE user_name != LOWER(TRIM(user_name));

# Verify
SELECT user_id, user_name FROM "user";
\q
```

If you haven't registered anyone yet or all usernames were already lowercase,
this step is a no-op — run the SELECT only and confirm zero rows.

### 2. MinIO object paths — **no action needed (backward-compatible)**

Old uploads stored at `user_{id}/project_0/{hash}_{name}`. New uploads to
`project_id=0` go to `base/user_{id}/{hash}_{name}`.

The code reads back objects via `File.file_dir` in Postgres, which stores
the full path at upload time. Old rows → old paths → still work. New rows →
new paths → also work.

**No migration needed**, but if you want clean path consistency, you can
move old base-knowledge objects to the new prefix:

```bash
# OPTIONAL — only if you want every base file under base/ prefix.
# Skip unless you care about clean paths.
#
# On ip1 host:
docker exec finhouse-minio mc alias set local http://localhost:9000 \
  "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD"

# List files previously uploaded to project_0
docker exec finhouse-minio mc ls -r "local/$MINIO_BUCKET/" | grep "/project_0/"

# For each matching object, mv to the new base/ prefix. Example:
# mc mv "local/bucket/user_1/project_0/abc_file.pdf" "local/bucket/base/user_1/abc_file.pdf"
# You'd ALSO need to UPDATE file.file_dir in Postgres to match.
```

This is tedious and rarely worth it. Recommend skipping.

### 3. Milvus collection schema — **no action needed**

`project_id` field already existed as INT64 scalar. The only thing that changed
is the search `expr`: now `project_id in [0, current]` for base-knowledge dispatch.
Existing vectors work without re-ingestion.

### 4. ClickHouse schema — **action only if you had a prototype schema**

If you ran an earlier patch that created tables with a different schema (e.g. the
initial `_ingestion_log` only, or a generic `customers`/`orders` stub), compare
against the current `pipeline/clickhouse/init.sql` (21 Vietnamese stock tables +
ingestion_log + update_log).

```bash
# Check what you have
docker exec finhouse-clickhouse clickhouse-client \
  --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" \
  --query "SHOW TABLES FROM olap" 

# If the output matches the 22 tables we expect, you're done.
# If different (e.g. you ran an older init.sql), options:
```

**Option A — preserve data, migrate in place:**
```bash
# Copy new init.sql into running container and run ONLY the CREATE IF NOT EXISTS
# Existing tables are kept as-is; new tables are added.
docker exec -i finhouse-clickhouse clickhouse-client \
  --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" \
  < pipeline/clickhouse/init.sql
```

This is safe because every `CREATE TABLE` uses `IF NOT EXISTS`. Caveat: if an old
table shares a name with a new one but has different columns (e.g. old `stocks`
had only 3 columns), the old version stays — Spark insert will fail on column
mismatch. Easier path:

**Option B — wipe ClickHouse (lose data, clean state):**
```bash
./start.sh pipeline down
# NB: this only affects pipeline stack; system stack keeps running
sudo rm -rf volumes/clickhouse
./start.sh pipeline  # init.sql runs fresh
```

Only do this if you haven't ingested real production data yet. For the test
phase you're in now, this is the simplest path.

### 5. Airflow metadata — **no action needed**

`airflow-postgres` is internal to Airflow. Changing DAG code doesn't require
a DB reset. If you changed `AIRFLOW_FERNET_KEY` between patches, existing
encrypted connection credentials become unreadable — delete and re-add the
Spark connection in Airflow UI, or wipe `volumes/airflow-postgres/` and
restart.

### 6. NiFi state — **no action unless redesigning flow**

If you want NiFi to re-detect all files in `/data/OLAP/` (because you're
swapping from `PutFile → /data/checkpoint` to `InvokeHTTP → Airflow`),
clear the listing timestamps:

```bash
./start.sh pipeline down
# Remove state but keep conf (flow layout)
sudo rm -rf volumes/nifi/state
./start.sh pipeline
```

NiFi will reload the flow from `volumes/nifi/conf/` but see every file as
"new" and emit a batch on the first run. If you DO want to keep the
timestamps (only new files detected going forward), skip this step.

## Recommended migration order

Before applying the latest patches:

```bash
# 1. Stop everything cleanly
./start.sh system down
./start.sh models down  # if applicable
./start.sh ollama down
./start.sh pipeline down  # if applicable

# 2. Snapshot volumes as safety net (small, fast)
sudo tar -czf volumes-snapshot-$(date +%Y%m%d).tgz volumes/
ls -lh volumes-snapshot-*.tgz

# 3. Apply patches in order (chronological)
# (ux-fixes-patch was first, then query-rewriter-patch)
unzip -o ~/Downloads/ux-fixes-patch.zip
cp -r ux-fixes-patch/* ./ && rm -rf ux-fixes-patch

unzip -o ~/Downloads/query-rewriter-patch.zip
cp -r query-rewriter-patch/* ./ && rm -rf query-rewriter-patch

# 4. Update .env if new vars were added
grep -E "^REWRITER_" .env || echo "Add REWRITER_MODEL= and REWRITER_ENABLED=true to .env"

# 5. Normalize existing usernames (if any)
./start.sh system up -d finhouse-postgres
# Wait for healthy, then:
docker exec -it finhouse-postgres psql -U finhouse -d finhouse \
  -c "UPDATE \"user\" SET user_name = LOWER(TRIM(user_name)) WHERE user_name != LOWER(TRIM(user_name));"

# 6. Full start
./start.sh system build   # rebuild because API code changed
./start.sh system restart

# If you had models/ollama/pipeline running, bring them back too
./start.sh models        # if on ip1
./start.sh ollama        # if on ip2
./start.sh pipeline      # if on ip2
```

## Rollback plan

If something goes wrong and you want to return to pre-patch state:

```bash
./start.sh system down
./start.sh pipeline down

# Restore from snapshot
sudo rm -rf volumes/
sudo tar -xzf volumes-snapshot-YYYYMMDD.tgz

# Revert code with git
git stash    # if you haven't committed
# OR
git checkout <previous-commit-hash> -- api/ ui/

./start.sh system build
./start.sh system
```

## Verification after migration

```bash
# 1. All containers healthy
docker ps --format 'table {{.Names}}\t{{.Status}}' | grep finhouse-

# 2. Postgres users readable
docker exec finhouse-postgres psql -U finhouse -d finhouse \
  -c "SELECT user_id, user_name FROM \"user\";"

# 3. Old login works with lowercased username
curl -X POST http://localhost:18000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"user_name":"<your-existing-username-lowercase>","user_password":"<password>"}'
# Expect 200 + tokens

# 4. MinIO files intact
docker exec finhouse-minio mc ls local/"$MINIO_BUCKET"/ | head -5
# Expect existing objects listed

# 5. Milvus collection present
docker exec finhouse-api python3 -c "
from pymilvus import connections, utility
connections.connect(host='finhouse-milvus', port='19530')
print('collections:', utility.list_collections())
print('count:', utility.get_stats('finhouse_chunks') if 'finhouse_chunks' in utility.list_collections() else 'n/a')
"

# 6. RAG still retrieves from old uploads
# (Login, create session, ask question about an old uploaded file)

# 7. Query rewriter works
docker logs finhouse-api 2>&1 | grep -i "rewrite" | tail -5
```

## What to expect for each breaking change

| Change | Who is affected | Visible symptom if missed |
|---|---|---|
| Username lowercase | Users registered with mixed-case names | Login returns 401 "Invalid credentials" |
| MinIO path for project_0 | Only internal — no user visible change | n/a |
| Milvus search expr | Users who uploaded to a non-zero project | Their RAG results now also include project_0 files (base knowledge) — expected new behavior |
| ClickHouse schema | Only if you ran old pipeline tests | Spark ingest fails with "column mismatch" |
| Airflow Fernet key rotation | Only if you changed the key | Airflow can't decrypt stored connections; re-add them |
