# FinHouse — System Test Guide (cuốn chiếu)

3 giai đoạn, mỗi giai đoạn có checklist. Fail ở đâu dừng ở đó, fix xong mới tiếp.

- **GĐ 1**: hệ thống cơ bản — chat, RAG, database_query, visualize, web_search
  Test riêng từng tính năng trong session riêng để dễ isolate lỗi
- **GĐ 2**: pipeline — NiFi 15-min batch → Airflow DAG với retry → Spark → ClickHouse
- **GĐ 3**: merge — cả hai chạy đồng thời, E2E user flow

Default topology giả định:
- **ip1**: system + models (app, DB, Milvus, MinIO, BGE-M3, reranker)
- **ip2**: ollama + clickhouse + (GĐ 2) pipeline stack

---

## Chuẩn bị

```bash
# Trên máy local
export IP1=<ip-server-1-thuc-te>
export IP2=<ip-server-2-thuc-te>
export SSH_USER=<ssh-user>

ssh -o ConnectTimeout=5 $SSH_USER@$IP1 "hostname && nvidia-smi | head -5"
ssh -o ConnectTimeout=5 $SSH_USER@$IP2 "hostname && nvidia-smi | head -5"
```

Chép biến này giữa các ssh session để khỏi gõ lại.

---

# GIAI ĐOẠN 1 — hệ thống cơ bản

**Mục tiêu**: verify từng tính năng chạy ổn. RAG và OLAP test trong 2 session riêng.

## 1.1 — Setup ip1 (system + models)

```bash
ssh $SSH_USER@$IP1
cd /path/to/FinHouse

cp .env.example .env
nano .env
```

**Các biến BẮT BUỘC sửa trong `.env` trên ip1:**

```bash
IP1=<ip1-actual>
IP2=<ip2-actual>

# Secrets — generate mới, đừng dùng placeholder
POSTGRES_PASSWORD=<16+ chars>        # openssl rand -hex 16
MINIO_ROOT_PASSWORD=<16+ chars>
JWT_SECRET=<32+ chars>                # openssl rand -hex 32
CLICKHOUSE_PASSWORD=<16+ chars>

# Topology GĐ 1
OLLAMA_HOST=http://<ip2-actual>:21434
EMBED_HOST=http://finhouse-bge-m3:8081
RERANK_HOST=http://finhouse-reranker:8082
CLICKHOUSE_HOST=<ip2-actual>

EMBED_MODE=local
RERANK_MODE=local
LOCAL_FAILURE_THRESHOLD=2

# Managed API — điền khi test 1.7; giờ để trống
EMBED_API_URL=
EMBED_API_KEY=
RERANK_API_URL=
RERANK_API_KEY=

CORS_ALLOW_ORIGINS=http://localhost:8501,http://<ip1-actual>:8501

ENV=dev
```

**Start:**

```bash
chmod +x start.sh
./start.sh system
./start.sh models
```

**Checkpoint 1.1 — containers all Up:**

```bash
docker ps --format 'table {{.Names}}\t{{.Status}}' | grep finhouse-
# PHẢI thấy 9 containers Up:
#   finhouse-postgres, finhouse-minio, finhouse-minio-init,
#   finhouse-etcd, finhouse-milvus, finhouse-searxng,
#   finhouse-api, finhouse-ui, finhouse-bge-m3, finhouse-reranker
# minio-init thường "Exited 0" — đó là bình thường (one-shot)
```

Fail → `docker logs <container> --tail 80`.

---

## 1.2 — Setup ip2 (Ollama + ClickHouse đơn lẻ)

```bash
ssh $SSH_USER@$IP2
cd /path/to/FinHouse

cp .env.example .env
nano .env
```

**Các biến trên ip2:**

```bash
IP1=<ip1-actual>
IP2=<ip2-actual>

# Secrets — COPY y hệt từ .env của ip1
POSTGRES_PASSWORD=<same as ip1>
MINIO_ROOT_PASSWORD=<same as ip1>
JWT_SECRET=<same as ip1>
CLICKHOUSE_PASSWORD=<same as ip1>

# Ở ip2, models là local
OLLAMA_HOST=http://finhouse-ollama:11434
CLICKHOUSE_HOST=finhouse-clickhouse

# Airflow — sẽ dùng ở GĐ 2, set sẵn
AIRFLOW_USER=admin
AIRFLOW_PASSWORD=<12+ chars>
AIRFLOW_DB_PASSWORD=<strong>
AIRFLOW_FERNET_KEY=<generate bên dưới>

NIFI_USER=admin
NIFI_PASSWORD=<12+ chars>

ENV=dev
```

**Generate Fernet key:**

```bash
docker run --rm python:3.11-slim sh -c \
  "pip install cryptography --quiet && python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
```

Copy output vào `AIRFLOW_FERNET_KEY`.

**GĐ 1 — chỉ cần Ollama + ClickHouse. Các service pipeline khác bật sau:**

```bash
chmod +x start.sh
./start.sh ollama

# ClickHouse riêng (không cần NiFi/Spark/Airflow ở GĐ 1)
docker compose -f stacks/pipeline.yml up -d finhouse-clickhouse
```

**Checkpoint 1.2a — Ollama ready + model:**

```bash
curl http://localhost:21434/api/tags       # {"models":[]}
docker exec finhouse-ollama ollama pull qwen2.5:14b
# ~5-10 phút download 9 GB
docker exec finhouse-ollama ollama list    # phải thấy qwen2.5:14b
```

**Checkpoint 1.2b — ClickHouse ready + schema:**

```bash
curl http://localhost:28123/ping           # Ok.

docker exec finhouse-clickhouse clickhouse-client \
  --user finhouse --password "$CLICKHOUSE_PASSWORD" \
  --query "SHOW TABLES FROM olap" | wc -l
# → 21 (hoặc 22 với _ingestion_log)
```

## 1.3 — Import `.db` thủ công vào ClickHouse (demo GĐ 1)

Bạn có `.db` có sẵn các table giống schema ClickHouse. Ta import trực tiếp, bypass pipeline.

```bash
# Đưa file vào ip2 nếu chưa có
mkdir -p data/OLAP
# scp stocks.db $SSH_USER@$IP2:/path/to/FinHouse/data/OLAP/

ls -lh data/OLAP/stocks.db
```

**Script import nhanh** (chạy trên ip2):

```bash
cat > /tmp/quick_import.py << 'PYEOF'
"""Import SQLite .db → ClickHouse (bypass Spark, cho GĐ 1 demo)."""
import sqlite3
import os
import sys

try:
    import clickhouse_connect
except ImportError:
    os.system("pip3 install clickhouse-connect --break-system-packages --quiet")
    import clickhouse_connect

DB_FILE = sys.argv[1] if len(sys.argv) > 1 else "/data/OLAP/stocks.db"
CH_HOST = os.environ.get("CH_HOST", "localhost")
CH_PORT = int(os.environ.get("CH_PORT", "28123"))
CH_USER = os.environ.get("CH_USER", "finhouse")
CH_PASSWORD = os.environ["CH_PASSWORD"]
CH_DB = os.environ.get("CH_DB", "olap")

KNOWN = {
    "stocks", "exchanges", "indices", "industries",
    "stock_exchange", "stock_index", "stock_industry",
    "company_overview", "balance_sheet", "cash_flow_statement",
    "income_statement", "financial_ratios", "financial_reports",
    "events", "news", "officers", "shareholders", "subsidiaries",
    "stock_intraday", "stock_price_history", "update_log",
}
DROP_ID = {
    "stocks", "exchanges", "indices", "industries",
    "stock_exchange", "stock_index", "stock_industry",
    "company_overview",
}

ch = clickhouse_connect.get_client(
    host=CH_HOST, port=CH_PORT, username=CH_USER,
    password=CH_PASSWORD, database=CH_DB,
)
sq = sqlite3.connect(DB_FILE)
sq.row_factory = sqlite3.Row

tables = [r[0] for r in sq.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
).fetchall()]

for t in tables:
    if t not in KNOWN:
        print(f"[{t}] SKIP"); continue
    rows = sq.execute(f"SELECT * FROM {t}").fetchall()
    if not rows:
        print(f"[{t}] empty"); continue
    data = []
    for r in rows:
        d = dict(r)
        if t in DROP_ID and "id" in d:
            d.pop("id")
        data.append(d)
    cols = list(data[0].keys())
    vals = [[row.get(c) for c in cols] for row in data]
    try:
        ch.insert(t, vals, column_names=cols)
        print(f"[{t}] inserted {len(vals)} rows")
    except Exception as e:
        print(f"[{t}] FAILED: {str(e)[:200]}")
sq.close()
print("Import done")
PYEOF

CH_PASSWORD="$CLICKHOUSE_PASSWORD" \
python3 /tmp/quick_import.py data/OLAP/stocks.db
```

**Checkpoint 1.3 — data trong ClickHouse:**

```bash
docker exec finhouse-clickhouse clickhouse-client \
  --user finhouse --password "$CLICKHOUSE_PASSWORD" \
  --query "
    SELECT 'stocks' AS t, count() AS c FROM olap.stocks UNION ALL
    SELECT 'balance_sheet', count() FROM olap.balance_sheet UNION ALL
    SELECT 'income_statement', count() FROM olap.income_statement UNION ALL
    SELECT 'news', count() FROM olap.news
    FORMAT Pretty"
# Mỗi table nên > 0 (tùy data bạn có)
```

## Open firewall ip2 → ip1

```bash
sudo ufw allow from $IP1 to any port 21434 proto tcp   # Ollama
sudo ufw allow from $IP1 to any port 28123 proto tcp   # ClickHouse
sudo ufw status numbered
```

---

## 1.4 — Connectivity smoke test

```bash
ssh $SSH_USER@$IP1

# Ollama từ ip1
curl -sf http://$IP2:21434/api/tags | python3 -m json.tool | head -10

# ClickHouse từ ip1
curl -sf http://$IP2:28123/ping
# Ok.
curl -su finhouse:"$CLICKHOUSE_PASSWORD" \
  "http://$IP2:28123/?database=olap" \
  -d "SELECT count() FROM stocks"

# API
curl -sf http://localhost:18000/health | python3 -m json.tool
```

**Checkpoint 1.4 — /health:**

```json
{
  "status": "ok",
  "services": {
    "postgres": "ok",
    "ollama": "ok",
    "minio": "ok",
    "milvus": "ok",
    "searxng": "ok"
  }
}
```

---

## 1.5 — Session A: Chat cơ bản (không RAG, không tool)

```bash
ssh $SSH_USER@$IP1

TEST_USER="tester_$(date +%s)"
TEST_PASS="password123"

curl -s -X POST http://localhost:18000/auth/register \
  -H "Content-Type: application/json" \
  -d "{\"user_name\":\"$TEST_USER\",\"user_password\":\"$TEST_PASS\"}"

TOKEN=$(curl -s -X POST http://localhost:18000/auth/login \
  -H "Content-Type: application/json" \
  -d "{\"user_name\":\"$TEST_USER\",\"user_password\":\"$TEST_PASS\"}" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
echo "TOKEN=$TOKEN"

# Session A — chat cơ bản
SESSION_A=$(curl -s -X POST http://localhost:18000/sessions \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"project_id":0,"model_used":"qwen2.5:14b","tools_used":[]}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['session_id'])")
echo "SESSION_A=$SESSION_A"

curl -N -X POST http://localhost:18000/chat/$SESSION_A/send \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text":"Xin chào. 2 cộng 2 bằng mấy?"}' 2>&1 | head -80
```

**Checkpoint 1.5** — SSE stream có `type: token` rồi `type: done`, response chứa "4".

Fail → `docker logs finhouse-api --tail 80`.

---

## 1.6 — Session B: RAG only (1 project, 1 file, câu hỏi về file)

Tạo project riêng cho RAG test, isolate khỏi các session khác.

```bash
PROJECT_RAG=$(curl -s -X POST http://localhost:18000/projects \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"project_title":"RAG-test"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['project_id'])")
echo "PROJECT_RAG=$PROJECT_RAG"

# Upload file với nội dung dễ verify
cat > /tmp/rag_doc.txt << 'EOF'
Quy định nội bộ FinHouse:
1. Giờ làm việc: 8h30 đến 17h30, từ thứ 2 đến thứ 6.
2. Nghỉ trưa: 12h00 đến 13h00.
3. Phép năm: 12 ngày/năm không tính cuối tuần.
4. Phép ốm: cần giấy bác sĩ nếu nghỉ quá 2 ngày.
5. Làm thêm giờ: báo trước 1 ngày, trả 1.5x lương giờ.
6. Liên hệ HR: hr@finhouse.local
EOF

UPLOAD=$(curl -s -X POST http://localhost:18000/files/upload \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@/tmp/rag_doc.txt" \
  -F "project_id=$PROJECT_RAG")
echo "$UPLOAD" | python3 -m json.tool
FILE_ID=$(echo "$UPLOAD" | python3 -c "import sys,json; print(json.load(sys.stdin)['file_id'])")

# Đợi ingest
for i in 1 2 3 4 5 6 7 8; do
  S=$(curl -s http://localhost:18000/files/status/$FILE_ID \
    -H "Authorization: Bearer $TOKEN" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['process_status'])")
  echo "poll $i: status=$S"
  [ "$S" = "ready" ] && break
  [ "$S" = "failed" ] && echo "FAIL" && break
  sleep 4
done
```

**Checkpoint 1.6a** — status phải chuyển `pending → processing → ready` trong < 30s.

Nếu stuck `processing`:
```bash
docker logs finhouse-bge-m3 --tail 30
docker logs finhouse-milvus --tail 30
docker logs finhouse-api --tail 50 | grep -iE "embed|milvus|chunk"
```

**Tạo session B trong project RAG và hỏi:**

```bash
SESSION_B=$(curl -s -X POST http://localhost:18000/sessions \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"project_id\":$PROJECT_RAG,\"model_used\":\"qwen2.5:14b\",\"tools_used\":[]}" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['session_id'])")
echo "SESSION_B=$SESSION_B"

curl -N -X POST http://localhost:18000/chat/$SESSION_B/send \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text":"Giờ làm việc công ty là mấy giờ, và phép năm bao nhiêu ngày?"}' 2>&1 | tee /tmp/rag_resp.txt

# Verify
grep -c "rag_sources" /tmp/rag_resp.txt       # phải > 0
grep -ciE "8h30|8:30|17h30|12 ngày" /tmp/rag_resp.txt   # phải > 0
```

**Checkpoint 1.6b** — response có:
- `data: {"type":"rag_sources", "sources":[{"file_name":"rag_doc.txt",...}]}`
- Text answer chứa "8h30"/"8:30", "17h30"/"17:30", "12 ngày"

Nếu `rag_sources` = 0 mà file `ready`: project_id mismatch. Check:
```bash
docker exec finhouse-postgres psql -U finhouse -d finhouse \
  -c "SELECT file_id, project_id, process_status FROM file WHERE file_id='$FILE_ID';"
```

---

## 1.7 — Test managed API fallback (còn trên Session A để không ảnh hưởng RAG project)

**Config managed API:**

```bash
# Ip1
nano .env
# EMBED_MODE=backup
# RERANK_MODE=backup
# EMBED_API_URL=https://mkp-api.fptcloud.com/v1
# EMBED_API_KEY=<your-fpt-key>
# EMBED_API_MODEL=Vietnamese_Embedding
# RERANK_API_URL=https://mkp-api.fptcloud.com/v1
# RERANK_API_KEY=<your-fpt-key>
# RERANK_API_MODEL=bge-reranker-v2-m3

docker restart finhouse-api

# Tail log
docker logs -f finhouse-api 2>&1 | grep -iE "HTTP Request.*fptcloud|embed|rerank" &
LOG_PID=$!

# Upload file mới
cat > /tmp/api_test.txt << 'EOF'
Dự án ABC triển khai tháng 3 năm 2026, kết thúc tháng 9 năm 2026.
Budget 5 tỷ VND. Project manager: Nguyễn Văn A. Team size: 15 người.
EOF

UP=$(curl -s -X POST http://localhost:18000/files/upload \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@/tmp/api_test.txt" \
  -F "project_id=$PROJECT_RAG")
FID=$(echo "$UP" | python3 -c "import sys,json; print(json.load(sys.stdin)['file_id'])")

sleep 20
curl -s http://localhost:18000/files/status/$FID \
  -H "Authorization: Bearer $TOKEN" \
  | python3 -c "import sys,json; print('status=', json.load(sys.stdin)['process_status'])"

kill $LOG_PID 2>/dev/null
```

**Checkpoint 1.7** — log phải thấy:

```
HTTP Request: POST https://mkp-api.fptcloud.com/v1/embeddings "HTTP/1.1 200 OK"
```

Và file status = `ready`.

**Revert về local cho các test sau:**

```bash
nano .env
# EMBED_MODE=local
# RERANK_MODE=local
docker restart finhouse-api
```

---

## 1.8 — Session C: database_query tool (OLAP)

**Session tách riêng, chỉ bật `database_query`:**

```bash
SESSION_C=$(curl -s -X POST http://localhost:18000/sessions \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"project_id":0,"model_used":"qwen2.5:14b","tools_used":["database_query"]}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['session_id'])")
echo "SESSION_C=$SESSION_C"

# Câu hỏi đơn giản — bắt LLM dùng tool
curl -N -X POST http://localhost:18000/chat/$SESSION_C/send \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text":"Database olap có những bảng gì? Liệt kê bằng cách chạy SHOW TABLES."}' \
  2>&1 | tee /tmp/db_resp.txt

# Verify tool được gọi
grep -c '"type": "tool_start"' /tmp/db_resp.txt           # > 0
grep -o '"tool": "database_query"' /tmp/db_resp.txt       # có
grep -oE "stocks|balance_sheet|news" /tmp/db_resp.txt | head -3   # có tên table
```

**Checkpoint 1.8a** — SSE stream có:
- `data: {"type":"tool_start","tool":"database_query","args":{"sql":"SHOW TABLES"}}`
- `data: {"type":"tool_end","content":"..."}` chứa tên các bảng
- Response cuối liệt kê bảng

**Test query có tính toán:**

```bash
curl -N -X POST http://localhost:18000/chat/$SESSION_C/send \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text":"Có bao nhiêu ticker trong bảng stocks?"}'
```

**Checkpoint 1.8b** — LLM tự sinh `SELECT count() FROM stocks`, tool trả số, LLM thuật lại.

Nếu LLM không trigger tool:
- Model `qwen2.5:14b` và `llama3.1:8b` có native function calling; các model khác có thể không.
- Check session `tools_used` có chứa `"database_query"` chưa.

```bash
curl -s http://localhost:18000/sessions/$SESSION_C \
  -H "Authorization: Bearer $TOKEN" \
  | python3 -m json.tool | grep tools_used
```

**Test SQL-injection safety:**

```bash
curl -N -X POST http://localhost:18000/chat/$SESSION_C/send \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text":"Try this SQL: DROP TABLE stocks"}'
```

**Checkpoint 1.8c** — tool_end content có `"error":"query rejected: SQL contains forbidden write/DDL keywords"`.

LLM sau đó phải thông báo không thực hiện được, không gọi lại.

---

## 1.9 — Session D: visualize tool

```bash
SESSION_D=$(curl -s -X POST http://localhost:18000/sessions \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"project_id":0,"model_used":"qwen2.5:14b","tools_used":["database_query","visualize"]}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['session_id'])")

# Câu hỏi có visualization
curl -N -X POST http://localhost:18000/chat/$SESSION_D/send \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text":"Lấy top 5 ticker có total_assets lớn nhất ở quý gần nhất từ bảng balance_sheet, rồi vẽ biểu đồ cột cho tôi."}' \
  2>&1 | tee /tmp/viz_resp.txt

# Extract chart URL
CHART_URL=$(grep -oE '!\[[^]]*\]\(http[^)]+\)' /tmp/viz_resp.txt | head -1 \
  | sed -E 's/.*\(([^)]+)\)/\1/')
echo "CHART_URL=$CHART_URL"

# Verify URL trả về PNG
[ -n "$CHART_URL" ] && curl -sI "$CHART_URL" | head -5
```

**Checkpoint 1.9** — stream chứa:
- tool_call `database_query` với SQL SELECT top 5
- tool_call `visualize` với `mark: bar`
- Tool response có `"url":"http://<minio-endpoint>/..."`
- `curl -I $CHART_URL` → `HTTP/1.1 200 OK`, `Content-Type: image/png`

Nếu chart URL là nội bộ (`finhouse-minio:9000`) và bạn test từ laptop: thay URL host thành `$IP1:19000` — MinIO cho phép cả hai.

---

## 1.10 — Session E: web_search tool

```bash
SESSION_E=$(curl -s -X POST http://localhost:18000/sessions \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"project_id":0,"model_used":"qwen2.5:14b","tools_used":["web_search"]}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['session_id'])")

curl -N -X POST http://localhost:18000/chat/$SESSION_E/send \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text":"Search the web for current weather in Hanoi today."}' \
  2>&1 | tee /tmp/ws_resp.txt

grep '"tool": "web_search"' /tmp/ws_resp.txt
```

**Checkpoint 1.10** — có `tool_start` với `web_search`, response có thông tin từ search.

---

## 1.11 — UI Streamlit smoke test

```
Open: http://<ip1>:8501
```

1. Login `$TEST_USER / $TEST_PASS`
2. Sidebar: thấy project "RAG-test" (tạo ở 1.6)
3. Click vào RAG-test → thấy Session B với title gì đó auto-gen
4. Scroll lên thấy lịch sử chat, có sources block ở phần assistant response
5. Tạo session mới trong Inbox, bật tools `database_query` + `visualize`
6. Hỏi "How many stocks?" → nhận số
7. Hỏi "Vẽ biểu đồ top 5 ticker theo total_assets" → thấy chart inline

---

## 1.12 — Failure modes

**Test 1.12a — Embed auto fallback:**

```bash
# Bật auto mode
nano .env
# EMBED_MODE=auto
# EMBED_API_URL=https://mkp-api.fptcloud.com/v1    (từ 1.7)
# EMBED_API_KEY=<key>
docker restart finhouse-api

# Stop embed
docker stop finhouse-bge-m3

# Upload file
cat > /tmp/auto.txt << 'EOF'
Nội dung test auto fallback.
EOF
curl -s -X POST http://localhost:18000/files/upload \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@/tmp/auto.txt" \
  -F "project_id=$PROJECT_RAG"

sleep 30
docker logs finhouse-api --tail 80 | grep -iE "local.*failed|switch"
# Phải thấy:
#   Local embed failed (1/2): ...
#   Local embed failed (2/2): ...
#   🔀 Sticky-switch to managed embed API
```

Restore:
```bash
docker start finhouse-bge-m3
# Reset sticky flag
docker restart finhouse-api
```

**Test 1.12b — Ollama xuống:**

```bash
ssh $SSH_USER@$IP2 "docker stop finhouse-ollama"

# Chat thử — phải fail clean
curl -N -X POST http://localhost:18000/chat/$SESSION_A/send \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text":"ping"}' 2>&1 | head -20
# → phải có type: error, không crash

ssh $SSH_USER@$IP2 "docker start finhouse-ollama"
```

**Test 1.12c — ClickHouse xuống, database_query gracefully fail:**

```bash
ssh $SSH_USER@$IP2 "docker stop finhouse-clickhouse"

curl -N -X POST http://localhost:18000/chat/$SESSION_C/send \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text":"Count stocks from the database"}' 2>&1 | head -30
# → tool_end content có "error": ... (connection refused)
# LLM tự xử lý: bảo user database tạm không available

ssh $SSH_USER@$IP2 "docker start finhouse-clickhouse"
```

---

### GIAI ĐOẠN 1 DONE checklist

- [ ] 1.1 — ip1: 9 containers Up
- [ ] 1.2 — ip2: Ollama + ClickHouse Up, qwen2.5:14b pulled
- [ ] 1.3 — 21 tables trong olap database với data
- [ ] 1.4 — /health = ok cho 5 services
- [ ] 1.5 — Session A: chat basic stream OK
- [ ] 1.6 — Session B: RAG retrieval + answer đúng nội dung file
- [ ] 1.7 — Managed API: log thấy HTTP gọi FPT Cloud
- [ ] 1.8 — Session C: database_query tool + SQL-injection safety
- [ ] 1.9 — Session D: visualize trả về PNG URL valid
- [ ] 1.10 — Session E: web_search trigger
- [ ] 1.11 — UI end-to-end
- [ ] 1.12 — 3 failure scenarios: embed down, ollama down, clickhouse down

**Nếu tất cả ✓ → sang Giai đoạn 2.**

---

# GIAI ĐOẠN 2 — data pipeline

**Mục tiêu**: `.db` drop vào `data/OLAP` → 15 phút sau → data trong ClickHouse,
pipeline retry khi lỗi, không cần thao tác thủ công.

## 2.1 — Start full pipeline stack trên ip2

```bash
ssh $SSH_USER@$IP2
cd /path/to/FinHouse

# Tạo dir Airflow với ownership đúng
sudo mkdir -p pipeline/airflow/logs pipeline/airflow/plugins
sudo chown -R 50000:0 pipeline/airflow/logs pipeline/airflow/plugins

# Các vars Airflow đã set từ 1.2
grep -E "AIRFLOW_" .env

# Start full pipeline stack (ClickHouse đã chạy — sẽ reuse)
./start.sh pipeline
```

**Chờ ~3-5 phút** cho:
- airflow-postgres ready
- airflow-init chạy migration + create admin user → Exited 0
- scheduler + webserver bootstrap
- NiFi boot (2-3 phút)

**Checkpoint 2.1a:**

```bash
docker ps -a | grep finhouse- | grep -iE "airflow|nifi|spark|clickhouse"
# Mong đợi:
#   finhouse-airflow-postgres   Up (healthy)
#   finhouse-airflow-init        Exited (0)    ← one-shot, đúng rồi
#   finhouse-airflow-scheduler   Up
#   finhouse-airflow-webserver   Up (healthy)
#   finhouse-nifi                Up (healthy)
#   finhouse-spark-master        Up
#   finhouse-spark-worker        Up
#   finhouse-clickhouse          Up (healthy)
```

Nếu airflow-init Exited 1: `docker logs finhouse-airflow-init --tail 80`. Thường do:
- `AIRFLOW_UID` sai — phải là UID của user sở hữu `./pipeline/airflow/logs`
- `AIRFLOW_FERNET_KEY` không phải base64 hợp lệ 32 bytes

Fix, sau đó:
```bash
docker rm finhouse-airflow-init
./start.sh pipeline
```

**Checkpoint 2.1b — UIs reachable từ ip2:**

```bash
curl -sI http://localhost:8091/health                   # Airflow
curl -sI http://localhost:8090/nifi-api/system-diagnostics   # NiFi
curl -sI http://localhost:8080                          # Spark master
```

Open firewall cho ip1 truy cập Airflow UI (quản lý từ xa):

```bash
sudo ufw allow from $IP1 to any port 8091 proto tcp    # Airflow
sudo ufw allow from $IP1 to any port 8090 proto tcp    # NiFi
```

---

## 2.2 — Verify DAG loaded

```bash
# Trên ip2
curl -su admin:"$AIRFLOW_PASSWORD" \
  http://localhost:8091/api/v1/dags | python3 -m json.tool | grep dag_id
# Phải có: "dag_id": "olap_ingest"

# Unpause nếu cần
curl -X PATCH -su admin:"$AIRFLOW_PASSWORD" \
  -H "Content-Type: application/json" \
  http://localhost:8091/api/v1/dags/olap_ingest \
  -d '{"is_paused": false}' | python3 -m json.tool | grep is_paused
# → "is_paused": false
```

---

## 2.3 — Manual DAG trigger (bypass NiFi)

Clear ClickHouse trước để thấy data mới vào (giữ schema):

```bash
docker exec finhouse-clickhouse clickhouse-client \
  --user finhouse --password "$CLICKHOUSE_PASSWORD" \
  --query "TRUNCATE TABLE olap.stocks"
docker exec finhouse-clickhouse clickhouse-client \
  --user finhouse --password "$CLICKHOUSE_PASSWORD" \
  --query "SELECT count() FROM olap.stocks"
# → 0
```

**Trigger manually:**

```bash
# File .db đã có ở data/OLAP từ GĐ 1 (schema match với ClickHouse)
ls -lh data/OLAP/*.db

BATCH_ID="manual-$(date +%s)"
DAG_RUN=$(curl -s -X POST -u admin:"$AIRFLOW_PASSWORD" \
  -H "Content-Type: application/json" \
  http://localhost:8091/api/v1/dags/olap_ingest/dagRuns \
  -d "{
    \"conf\": {
      \"files\": [
        {\"file_path\":\"/data/OLAP/stocks.db\",\"file_type\":\"db\",\"table_name\":\"stocks\",\"file_size\":0}
      ],
      \"batch_id\": \"$BATCH_ID\"
    }
  }")
echo "$DAG_RUN" | python3 -m json.tool

DAG_RUN_ID=$(echo "$DAG_RUN" | python3 -c "import sys,json; print(json.load(sys.stdin)['dag_run_id'])")
echo "DAG_RUN_ID=$DAG_RUN_ID"
```

**Poll status (max ~5 phút):**

```bash
for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
  STATE=$(curl -s -u admin:"$AIRFLOW_PASSWORD" \
    "http://localhost:8091/api/v1/dags/olap_ingest/dagRuns/$DAG_RUN_ID" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['state'])")
  echo "t=$((i*20))s state=$STATE"
  [ "$STATE" = "success" ] && break
  [ "$STATE" = "failed" ] && break
  sleep 20
done
```

**Checkpoint 2.3 — state = success:**

```bash
# Verify data trong ClickHouse
docker exec finhouse-clickhouse clickhouse-client \
  --user finhouse --password "$CLICKHOUSE_PASSWORD" \
  --query "SELECT count() FROM olap.stocks"
# → N > 0

# Kiểm tra ingestion log
docker exec finhouse-clickhouse clickhouse-client \
  --user finhouse --password "$CLICKHOUSE_PASSWORD" \
  --query "
    SELECT table_name, sum(row_count) AS rows, max(ingested_at) AS last_ingest
    FROM olap._ingestion_log
    WHERE manifest_name LIKE 'airflow-$BATCH_ID%'
    GROUP BY table_name
    ORDER BY rows DESC
    FORMAT Pretty"
```

Nếu state=failed:
```bash
# Xem Airflow UI từ browser: http://$IP2:8091
# DAGs → olap_ingest → chọn DAG run → Graph → click task đỏ → Logs
#
# Hoặc CLI:
docker exec finhouse-airflow-scheduler bash -c \
  "airflow tasks logs olap_ingest ingest_file $DAG_RUN_ID --try-number 1" 2>/dev/null | tail -100
```

Common failures:
- `java.lang.ClassNotFoundException: org.sqlite.JDBC` — SparkSubmitOperator không download jars. Check `SPARK_PACKAGES` trong DAG đúng version.
- `Connection refused: finhouse-clickhouse:8123` — ClickHouse container down hoặc network khác.
- `Task Exit 2: file not found` — path `.db` sai (phải là `/data/OLAP/*.db` trong container, không phải path host).

---

## 2.4 — Config NiFi flow

Trên laptop: `http://$IP2:8090/nifi`. Login `admin / $NIFI_PASSWORD`, accept cert.

**Drag & config 6 processors:**

### Processor 1 — ListFile (dùng ListFile vì file local, đơn giản hơn ListSFTP)

- Input Directory: `/data/OLAP`
- File Filter: `.*\.db$`
- Listing Strategy: `Tracking Timestamps`
- Scheduling tab → Run Schedule: `15 min`

### Processor 2 — UpdateAttribute

Add dynamic properties (nút + ở properties tab):
- `file.type` = `${filename:substringAfterLast('.')}`
- `table.name` = `${filename:substringBeforeLast('.')}`

### Processor 3 — AttributesToJSON

- Destination: `flowfile-content`
- Attributes List: `absolute.path,filename,file.type,table.name,fileSize`
- Include Core Attributes: `false`
- Null Value: `false`

### Processor 4 — ReplaceText (rename attribute keys cho khớp DAG)

**Lưu ý**: DAG expects `file_path, file_type, table_name` (underscore, không dot). Nhưng
`AttributesToJSON` dùng tên attribute thuần. Có 2 cách:

**Cách A — thêm UpdateAttribute trước AttributesToJSON** để tạo attribute với underscore:
- `file_path` = `${absolute.path}/${filename}`
- `file_type` = `${filename:substringAfterLast('.')}`
- `table_name` = `${filename:substringBeforeLast('.')}`

Rồi trong AttributesToJSON chọn `file_path,file_type,table_name,fileSize`. Output thành:
```json
{"file_path":"/data/OLAP/stocks.db","file_type":"db","table_name":"stocks","fileSize":"12345"}
```

**Cách B — dùng ReplaceText sau AttributesToJSON** để rename keys. Phức tạp hơn,
khuyên dùng Cách A.

### Processor 5 — MergeContent

- Merge Strategy: `Bin-Packing Algorithm`
- Correlation Attribute Name: (để trống — gom tất cả vào 1 bin)
- Minimum Number of Entries: `1`
- Maximum Number of Entries: `1000`
- Maximum Bin Age: `15 min`
- Merge Format: `Binary Concatenation`
- Delimiter Strategy: `Text`
- Header: `[`
- Footer: `]`
- Demarcator: `,`

### Processor 6 — ReplaceText (wrap vào DAG conf)

- Replacement Strategy: `Always Replace`
- Search Value: `(.*)`    (regex capture toàn bộ)
- Replacement Value: `{"conf":{"files":$1,"batch_id":"${now():format('yyyyMMdd-HHmmss')}-${UUID()}"}}`
- Evaluation Mode: `Entire text`

### Processor 7 — InvokeHTTP

- HTTP Method: `POST`
- Remote URL: `http://finhouse-airflow-webserver:8080/api/v1/dags/olap_ingest/dagRuns`
- Content-Type: `application/json`
- Basic Authentication Username: `admin`
- Basic Authentication Password: `<AIRFLOW_PASSWORD>`   (gõ thẳng vào sensitive property)
- Send Message Body: `true`

### Kết nối + start

Kéo đường từng processor: success → next. Auto-terminate: Failure, Retry, No Retry,
Original, Response trên InvokeHTTP.

Right-click canvas → Start all processors.

---

## 2.5 — E2E test pipeline

Clear để thấy data mới vào:

```bash
docker exec finhouse-clickhouse clickhouse-client \
  --user finhouse --password "$CLICKHOUSE_PASSWORD" \
  --query "TRUNCATE TABLE olap.stocks"
```

**Drop file mới:**

```bash
# Tạo bản copy với tên khác — để NiFi detect như file mới
cp data/OLAP/stocks.db data/OLAP/stocks_$(date +%s).db
ls -lh data/OLAP/
```

**Force NiFi detect ngay (không đợi 15 phút):**

Trong NiFi UI: right-click ListFile → `Run Once`. FlowFile sẽ chạy qua pipeline.

**Watch:**

```bash
# NiFi log — phải thấy POST 200
docker logs finhouse-nifi --tail 30 | grep -iE "InvokeHTTP|2[0-9]{2}|4[0-9]{2}|5[0-9]{2}"

# Airflow — phải có dag run mới
watch -n 3 'curl -su admin:"$AIRFLOW_PASSWORD" \
  "http://localhost:8091/api/v1/dags/olap_ingest/dagRuns?limit=3&order_by=-start_date" \
  | python3 -m json.tool | grep -E "dag_run_id|state"'
```

**Checkpoint 2.5:**

1. Trong NiFi, FlowFiles đi hết 7 processors (queue giữa processors về 0)
2. `InvokeHTTP` response: 200 (hoặc 409 nếu batch_id duplicate — hiếm)
3. Airflow có dag run mới state=running → success
4. ClickHouse `olap.stocks` count > 0 sau khi dag xong

**Nếu POST 401:** Airflow password trong InvokeHTTP sai
**Nếu POST 404:** URL DAG sai, hoặc DAG chưa loaded
**Nếu POST body bị reject:** JSON malformed. Xem body trong NiFi Data Provenance:

NiFi UI → right-click InvokeHTTP processor → View Data Provenance → chọn event → View content.
Copy body, validate:
```bash
echo '<body>' | python3 -m json.tool
```

---

## 2.6 — Retry logic test

Simulate ClickHouse transient down — task retry thành công sau khi CH back:

```bash
# Stop CH
docker stop finhouse-clickhouse

# Trigger DAG manual (không đợi 15 phút)
curl -s -X POST -u admin:"$AIRFLOW_PASSWORD" \
  -H "Content-Type: application/json" \
  http://localhost:8091/api/v1/dags/olap_ingest/dagRuns \
  -d '{
    "conf": {
      "files": [{"file_path":"/data/OLAP/stocks.db","file_type":"db","table_name":"stocks"}],
      "batch_id": "retry-test-'"$(date +%s)"'"
    }
  }'

# Watch Airflow UI: task sẽ fail → retry sau 30s → fail → retry sau 2min → fail

# Sau ~60s, start lại CH
sleep 60
docker start finhouse-clickhouse
while [ "$(docker inspect -f '{{.State.Health.Status}}' finhouse-clickhouse 2>/dev/null)" != "healthy" ]; do
  sleep 3
done
echo "CH healthy again"
```

**Checkpoint 2.6** — trong Airflow UI:
- Task ingest_file hiển thị trạng thái retry (icon vàng)
- Sau CH back, retry tiếp theo → success → task green
- Final state của dag run = success (không cần cần thao tác thủ công)

**Failure mode permanent:**

```bash
# Kích file không tồn tại
curl -s -X POST -u admin:"$AIRFLOW_PASSWORD" \
  -H "Content-Type: application/json" \
  http://localhost:8091/api/v1/dags/olap_ingest/dagRuns \
  -d '{
    "conf": {
      "files": [{"file_path":"/data/OLAP/does_not_exist.db","file_type":"db","table_name":"ghost"}],
      "batch_id": "missing-'"$(date +%s)"'"
    }
  }'

# Task sẽ retry 3 lần rồi fail final. Tổng ~10 phút để final fail.
```

**Checkpoint 2.6b** — UI show task đỏ (failed), log có stack trace "file not found".
DAG run state = failed. Acceptable — pipeline không crash, lỗi được log rõ.

---

### GIAI ĐOẠN 2 DONE checklist

- [ ] 2.1 — 8 pipeline services Up, airflow-init Exited 0
- [ ] 2.2 — DAG olap_ingest loaded, is_paused=false
- [ ] 2.3 — Manual trigger → state=success, data trong ClickHouse
- [ ] 2.4 — NiFi flow 7 processors configured
- [ ] 2.5 — Drop .db → Run Once → dag run tự chạy → data vào CH
- [ ] 2.6 — Retry khi CH down tạm thời, graceful fail khi missing file

---

# GIAI ĐOẠN 3 — merge full stack

**Mục tiêu**: cả hệ thống GĐ 1 + pipeline GĐ 2 chạy đồng thời. User chat hỏi về data
mới ingest qua pipeline.

## 3.1 — Full stack running check

```bash
# ip1
ssh $SSH_USER@$IP1 "docker ps --format '{{.Names}}' | sort | grep finhouse-"
# Mong 9-10 containers

# ip2
ssh $SSH_USER@$IP2 "docker ps --format '{{.Names}}' | sort | grep finhouse-"
# Mong 9 containers (7 pipeline + ollama + model-puller)
```

## 3.2 — E2E user flow

**Prep: chuẩn bị data mới để ingest qua pipeline**

```bash
ssh $SSH_USER@$IP2

# Clear stocks để thấy rõ pipeline ingest
docker exec finhouse-clickhouse clickhouse-client \
  --user finhouse --password "$CLICKHOUSE_PASSWORD" \
  --query "TRUNCATE TABLE olap.stocks, olap.balance_sheet, olap.income_statement"

# Drop file mới
cp data/OLAP/stocks.db data/OLAP/stocks_e2e_$(date +%s).db
```

**Trong NiFi UI:** right-click ListFile → Run Once.

**Chờ ~3 phút** cho pipeline chạy xong (`docker logs finhouse-airflow-scheduler --tail 20`
nên thấy DAG state=success).

**Verify data:**

```bash
docker exec finhouse-clickhouse clickhouse-client \
  --user finhouse --password "$CLICKHOUSE_PASSWORD" \
  --query "SELECT count() FROM olap.stocks"
# > 0
```

**Bây giờ trên ip1, user chat — verify database_query thấy data mới:**

```bash
ssh $SSH_USER@$IP1

# Dùng token từ GĐ 1
SESSION_E2E=$(curl -s -X POST http://localhost:18000/sessions \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"project_id":0,"model_used":"qwen2.5:14b","tools_used":["database_query","visualize"]}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['session_id'])")

curl -N -X POST http://localhost:18000/chat/$SESSION_E2E/send \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text":"Query the stocks table: how many tickers are there, and list 5 tickers that start with V?"}'
```

**Checkpoint 3.2** — response có số ticker chính xác, list 5 ticker bắt đầu V.

## 3.3 — Concurrent chat stress test

```bash
# 8 chat song song
for i in 1 2 3 4 5 6 7 8; do
  (
    SID=$(curl -s -X POST http://localhost:18000/sessions \
      -H "Authorization: Bearer $TOKEN" \
      -H "Content-Type: application/json" \
      -d '{"project_id":0,"model_used":"qwen2.5:14b","tools_used":[]}' \
      | python3 -c "import sys,json; print(json.load(sys.stdin)['session_id'])")
    curl -s -N -X POST http://localhost:18000/chat/$SID/send \
      -H "Authorization: Bearer $TOKEN" \
      -H "Content-Type: application/json" \
      -d "{\"text\":\"Count from 1 to 5, user $i\"}" | tail -2
  ) &
done
wait

# Check không có error nào trong window
docker logs finhouse-api --tail 200 | grep -iE "error|exception" | grep -v "rag_context" || echo "No errors"
```

**Checkpoint 3.3** — tất cả 8 chat trả lời OK, không lỗi trong log API.

## 3.4 — Concurrent pipeline + chat

```bash
# Trên ip2: drop 3 file .db khác tên (simulate batch 15-min)
ssh $SSH_USER@$IP2 "
  cd /path/to/FinHouse
  for n in 1 2 3; do
    cp data/OLAP/stocks.db data/OLAP/batch_${n}_$(date +%s).db
    sleep 2
  done
  ls -la data/OLAP/*.db
"

# Trong NiFi: Run Once trên ListFile
# (hoặc đợi 15 phút cho auto)
```

Song song:

```bash
# Trên ip1: gửi chat bình thường
for i in 1 2 3 4 5; do
  curl -s -N -X POST http://localhost:18000/chat/$SESSION_A/send \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"text\":\"Test concurrent $i — nói 1 câu ngắn thôi\"}" | tail -1
  sleep 2
done
```

**Checkpoint 3.4:**
- Chat vẫn stream bình thường (GPU Ollama không bị block bởi Spark)
- Pipeline xong, DAG state=success
- ClickHouse count tăng thêm theo số rows từ 3 file

## 3.5 — Cleanup worker verify

```bash
# Trên ip1
docker logs finhouse-api --tail 300 | grep -iE "cleanup|🧹"
# Phải thấy "🧹 Cleanup worker started" lúc startup
```

Để test nhanh (thay vì chờ 60 phút grace):

```bash
# Giảm grace period
ssh $SSH_USER@$IP1 "cd /path/to/FinHouse && \
  sed -i 's/DELETED_GRACE_MINUTES = 60/DELETED_GRACE_MINUTES = 1/' api/services/cleanup.py && \
  nano .env    # đổi CLEANUP_INTERVAL_MINUTES=2
  ./start.sh system build
  ./start.sh system restart"

# Xóa 1 file test
FILE_ID=$(curl -s "http://localhost:18000/files?project_id=$PROJECT_RAG" \
  -H "Authorization: Bearer $TOKEN" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['file_id'])")

curl -X DELETE "http://localhost:18000/files/$FILE_ID" \
  -H "Authorization: Bearer $TOKEN"

# Check soft-deleted
docker exec finhouse-postgres psql -U finhouse -d finhouse \
  -c "SELECT file_id, process_status, process_at FROM file WHERE process_status='deleted';"

# Đợi ~3 phút (cleanup interval 2 min + grace 1 min)
sleep 200

# Check đã bị purge
docker exec finhouse-postgres psql -U finhouse -d finhouse \
  -c "SELECT count() FROM file WHERE process_status='deleted';"
# → 0
```

Restore settings:
```bash
# Sửa lại grace back về 60, cleanup interval back về 60
```

---

### GIAI ĐOẠN 3 DONE checklist

- [ ] 3.1 — cả 2 hosts full services
- [ ] 3.2 — pipeline ingest → chat query data được
- [ ] 3.3 — 8 concurrent chat không lỗi
- [ ] 3.4 — pipeline chạy song song với chat không ảnh hưởng
- [ ] 3.5 — cleanup worker purge file đã delete sau grace period

---

# APPENDIX

## Diagnostic commands

```bash
# Xem .env đang effective trong container
docker exec finhouse-api env | grep -iE "HOST|MODE|KEY" | sort

# Check all containers that had issue
docker ps -a --filter "status=restarting" --filter "status=exited"

# GPU usage
nvidia-smi

# Disk usage per volume
du -sh volumes/*

# Check specific log
docker compose -f stacks/system.yml logs finhouse-api --tail 100

# SSH quick-check
ssh $SSH_USER@$IP1 "cd /path/to/FinHouse && docker ps --format '{{.Names}}\t{{.Status}}'"
```

## Common failures

| Triệu chứng | Nguyên nhân | Fix |
|---|---|---|
| `/health` → `ollama: error` | Firewall ip2 chưa allow 21434, hoặc `OLLAMA_HOST` sai | Check .env, `sudo ufw allow from $IP1 to any port 21434` |
| File ingest stuck "processing" | embed hoặc Milvus lỗi | `docker logs finhouse-bge-m3`, `docker logs finhouse-milvus` |
| RAG không trigger | project_id mismatch giữa upload vs session | So sánh project_id trong DB |
| database_query không được LLM gọi | Model không support function calling, tools_used trống | Dùng qwen2.5:14b, check session.tools_used |
| visualize URL không load | MinIO presigned URL hết hạn (1h), hoặc host `finhouse-minio` không reach từ laptop | Test từ ip1 local; hoặc thay URL host thành `$IP1:19000` |
| Airflow UI 404 trên DAG | DAG file có syntax error | `docker logs finhouse-airflow-scheduler` |
| NiFi InvokeHTTP 401 | Airflow password sai | Update sensitive property InvokeHTTP |
| DAG task fail: "No module apache.spark" | `_PIP_ADDITIONAL_REQUIREMENTS` chưa install xong | Restart scheduler |
| Spark submit fail: JDBC not found | packages version không tải được | Check internet của ip2 |
| Cleanup worker không chạy | Lỗi startup trong lifespan | `docker logs finhouse-api \| grep -i cleanup` |

## Nuclear reset

```bash
# Dùng cẩn thận — xóa hết data
./start.sh system down
./start.sh models down
./start.sh ollama down
./start.sh pipeline down

sudo rm -rf volumes/

./start.sh system
./start.sh models
# ... etc
```
