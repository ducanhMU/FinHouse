# FinHouse — Test Guide

This document walks you through testing every component individually, then verifying how they work together, and finally running complete end-to-end scenarios that simulate real user behavior.

**How to use this guide:**
- Start from Part 1 and go in order — each part depends on the previous one passing.
- Every test has an **exact command** and an **expected result**.
- Mark each test ✅ or ❌ as you go. If something fails, fix it before continuing.
- The `$PG` shortcut used throughout means: `docker exec finhouse-postgres psql -U finhouse -d finhouse -c`

---

## Prerequisites

```bash
# 1. Copy env and start everything
cp .env.example .env
docker compose up -d --build

# 2. Wait for all services to be healthy (~60-90 seconds)
docker compose ps

# 3. Pull LLM models (do this once, takes 10-30 min)
chmod +x scripts/pull-models.sh
./scripts/pull-models.sh
```

**Shortcut variable (paste in your terminal first):**
```bash
PG="docker exec finhouse-postgres psql -U finhouse -d finhouse -c"
API="http://localhost:8000"
```

---

## Part 1 — Individual Component Health

Test each Docker container is alive and responding before testing any logic.

### 1.1 All containers running

```bash
docker compose ps --format "table {{.Name}}\t{{.Status}}"
```

**Expected:** Every service shows `Up` or `Up (healthy)`. The one-shot `finhouse-minio-init` may show `Exited (0)` — that's correct (it runs once then exits).

### 1.2 PostgreSQL

```bash
$PG "SELECT version();"
```

**Expected:** Returns PostgreSQL 16.x version string.

### 1.3 PostgreSQL — schema exists

```bash
$PG "\dt"
```

**Expected:** Lists 5 tables: `user`, `project`, `chat_session`, `chat_event`, `file`.

### 1.4 PostgreSQL — seed data

```bash
$PG "SELECT user_id, user_name FROM \"user\" WHERE user_id = 0;"
$PG "SELECT project_id, project_title FROM project WHERE project_id = 0;"
```

**Expected:**
- Row: `user_id=0, user_name=guest`
- Row: `project_id=0, project_title=Default`

### 1.5 MinIO

```bash
curl -sf http://localhost:9000/minio/health/live && echo "OK"
```

**Expected:** `OK`

Open http://localhost:9001 in browser (login with your MINIO_ROOT_USER / MINIO_ROOT_PASSWORD from `.env`).

**Expected:** MinIO console loads. Bucket `finhouse-files` exists.

### 1.6 Milvus

```bash
curl -sf http://localhost:9091/healthz && echo "OK"
```

**Expected:** `OK`

### 1.7 Ollama

```bash
curl -s http://localhost:11434/api/tags | python3 -c "
import sys, json
data = json.load(sys.stdin)
for m in data.get('models', []):
    print(f\"  {m['name']}\")
"
```

**Expected:** Lists `qwen2.5:14b` and `llama3.1:8b` (or whichever models you pulled).

### 1.8 SearXNG

```bash
curl -s "http://localhost:8080/search?q=test&format=json" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(f\"Results: {len(data.get('results', []))}\")"
```

**Expected:** `Results: N` where N > 0 (SearXNG returns web results).

### 1.9 BGE-M3 Embedding Service

```bash
curl -s http://localhost:8081/health
```

**Expected:** `{"status":"ok","model":"BAAI/bge-m3"}`

```bash
curl -s -X POST http://localhost:8081/embed \
  -H "Content-Type: application/json" \
  -d '{"texts": ["hello world"]}' | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(f\"Dimension: {data['dimension']}, Vectors: {len(data['embeddings'])}\")"
```

**Expected:** `Dimension: 1024, Vectors: 1`

### 1.10 BGE-M3 Reranker Service

```bash
curl -s http://localhost:8082/health
```

**Expected:** `{"status":"ok","model":"BAAI/bge-reranker-v2-m3"}`

### 1.11 FastAPI Backend

```bash
curl -s $API/health | python3 -m json.tool
```

**Expected:** JSON with `"status": "ok"` and all services showing `"ok"`.

```bash
curl -s $API/docs | head -5
```

**Expected:** HTML content (Swagger UI loads).

### 1.12 Streamlit UI

Open http://localhost:8501 in browser.

**Expected:** FinHouse chat interface loads with dark theme, brand title, sidebar with login form, model selector dropdown, welcome screen with prompt cards.

---

## Part 2 — Authentication & CRUD

### 2.1 Register a user

```bash
curl -s -X POST $API/auth/register \
  -H "Content-Type: application/json" \
  -d '{"user_name": "testuser", "user_password": "testpass123"}' | python3 -m json.tool
```

**Expected:** `{"user_id": 1, "user_name": "testuser"}`

### 2.2 Reject duplicate registration

```bash
curl -s -X POST $API/auth/register \
  -H "Content-Type: application/json" \
  -d '{"user_name": "testuser", "user_password": "testpass123"}'
```

**Expected:** `409` status with `"Username already taken"`.

### 2.3 Login

```bash
curl -s -X POST $API/auth/login \
  -H "Content-Type: application/json" \
  -d '{"user_name": "testuser", "user_password": "testpass123"}' | python3 -m json.tool
```

**Expected:** JSON with `access_token`, `refresh_token`, `user_id: 1`, `user_name: testuser`.

**Save the token for later tests:**
```bash
TOKEN=$(curl -s -X POST $API/auth/login \
  -H "Content-Type: application/json" \
  -d '{"user_name": "testuser", "user_password": "testpass123"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
echo "Token: $TOKEN"
```

### 2.4 Reject wrong password

```bash
curl -s -o /dev/null -w "%{http_code}" -X POST $API/auth/login \
  -H "Content-Type: application/json" \
  -d '{"user_name": "testuser", "user_password": "wrongpassword"}'
```

**Expected:** `401`

### 2.5 Create a project

```bash
curl -s -X POST $API/projects \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"project_title": "Test Project", "description": "For testing"}' | python3 -m json.tool
```

**Expected:** JSON with `project_id > 0`, `project_title: "Test Project"`.

**Save the project_id:**
```bash
PROJECT_ID=$(curl -s -X POST $API/projects \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"project_title": "RAG Test Project"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['project_id'])")
echo "Project ID: $PROJECT_ID"
```

### 2.6 List projects

```bash
curl -s $API/projects -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

**Expected:** Array containing your created project(s).

### 2.7 Create a session

```bash
SESSION_ID=$(curl -s -X POST $API/sessions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d "{\"project_id\": $PROJECT_ID, \"model_used\": \"qwen2.5:14b\", \"tools_used\": [\"web_search\"]}" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['session_id'])")
echo "Session ID: $SESSION_ID"
```

**Expected:** A UUID session_id is printed.

### 2.8 Guest session (no auth)

```bash
curl -s -X POST $API/sessions \
  -H "Content-Type: application/json" \
  -d '{"model_used": "qwen2.5:14b"}' | python3 -m json.tool
```

**Expected:** JSON with a `session_id` and `project_id < 0` (negative).

### 2.9 Verify in database

```bash
$PG "SELECT session_id, project_id, model_used, tools_used FROM chat_session ORDER BY create_at DESC LIMIT 3;"
```

**Expected:** Shows the sessions you just created, with correct project_ids and model.

---

## Part 3 — Basic Chat (No RAG)

### 3.1 Send a message via API and stream the response

```bash
curl -N -X POST "$API/chat/$SESSION_ID/send" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"text": "What is 2 + 2? Reply in one sentence."}' 2>&1 | head -20
```

**Expected:** Multiple `data: {"type":"token","content":"..."}` lines streaming in, followed by `data: {"type":"done"}`. The tokens spell out an answer containing "4".

### 3.2 Check events were persisted

```bash
$PG "SELECT num_order, role, event_type, LEFT(text, 80) as text_preview FROM chat_event WHERE session_id = '$SESSION_ID' ORDER BY num_order;"
```

**Expected:** At least 2 rows:
- `role=user, event_type=message, text=What is 2 + 2...`
- `role=assistant, event_type=message, text=<answer containing 4>`

### 3.3 Multi-turn context

```bash
curl -N -X POST "$API/chat/$SESSION_ID/send" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"text": "Now multiply that result by 10."}' 2>&1 | grep '"type":"token"' | head -5
```

**Expected:** Response references the previous answer (4) and responds with 40.

### 3.4 Session turn count incremented

```bash
$PG "SELECT turn_count, session_title FROM chat_session WHERE session_id = '$SESSION_ID';"
```

**Expected:** `turn_count = 2`, and `session_title` should now have an auto-generated title (set after the first turn).

### 3.5 Get all events via API

```bash
curl -s "$API/chat/$SESSION_ID/events" | python3 -c "
import sys, json
events = json.load(sys.stdin)
for e in events:
    print(f\"  [{e['num_order']}] {e['role']}/{e['event_type']}: {e['text'][:60]}...\")
"
```

**Expected:** Ordered list of all events for this session.

---

## Part 4 — File Upload & Ingest Pipeline

### 4.1 Prepare test files

```bash
# Create a test document in ./data for auto-scan
cat > data/test_finance.md << 'EOF'
# Q3 2024 Financial Report

## Revenue Summary
Total revenue for Q3 2024 was $4.2 million, up 15% from Q2 2024.
The growth was driven primarily by enterprise subscriptions which grew 23%.
Consumer revenue remained flat at $800K.

## Cost Analysis
Operating costs were $2.8 million, resulting in an operating margin of 33%.
The largest expense category was personnel at $1.6 million.
Cloud infrastructure costs decreased 8% due to optimization efforts.

## Outlook
Q4 2024 guidance projects revenue between $4.5M and $4.8M.
Management expects operating margin to improve to 36% due to scaling efficiencies.
Two major enterprise deals are in late-stage negotiation.
EOF

echo "Plain text test file for RAG." > data/test_simple.txt
```

### 4.2 Restart API to trigger auto-scan

```bash
docker compose restart finhouse-api
sleep 10
docker compose logs finhouse-api --tail 30 | grep -i "scan\|process\|ingest\|chunk"
```

**Expected:** Logs show the data scanner finding files, processing `test_finance.md` and `test_simple.txt`, reporting chunk counts, and marking them as `ready`.

### 4.3 Verify files in database

```bash
$PG "SELECT file_name, file_type, process_status, file_dir FROM file ORDER BY file_name;"
```

**Expected:** Both files show `process_status = 'ready'`.

### 4.4 Verify files in MinIO

Open http://localhost:9001, navigate to the `finhouse-files` bucket → `data/` prefix.

**Expected:** Both files are stored there.

### 4.5 Verify vectors in Milvus

```bash
# From inside the API container (pymilvus is installed there)
docker exec finhouse-api python3 -c "
from pymilvus import connections, Collection, utility
connections.connect(host='finhouse-milvus', port='19530')
if utility.has_collection('finhouse_chunks'):
    c = Collection('finhouse_chunks')
    c.load()
    print(f'Collection exists, entities: {c.num_entities}')
else:
    print('Collection not found')
"
```

**Expected:** `Collection exists, entities: N` where N > 0 (number of chunks).

### 4.6 Upload via API (supported format)

```bash
curl -s -X POST "$API/files/upload" \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@data/test_finance.md" \
  -F "project_id=$PROJECT_ID" | python3 -m json.tool
```

**Expected:** JSON with `process_status` of either `"ready"` (duplicate detected — same hash, already indexed for project 0) or `"pending"` (new project, will process in background). Check again after 10 seconds:

```bash
# Get the file_id from the response above, then:
FILE_ID="<paste file_id here>"
curl -s "$API/files/status/$FILE_ID" | python3 -m json.tool
```

**Expected:** `process_status: "ready"`.

### 4.7 Upload unsupported format

```bash
echo '{"not": "a document"}' > /tmp/test.json
curl -s -X POST "$API/files/upload" \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@/tmp/test.json;filename=test.json" \
  -F "project_id=$PROJECT_ID" | python3 -m json.tool
```

**Expected:** JSON with `process_status: "failed"`, `file_type: "json"`.

### 4.8 Deduplication

```bash
# Upload the exact same file again
curl -s -X POST "$API/files/upload" \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@data/test_finance.md" \
  -F "project_id=$PROJECT_ID" | python3 -m json.tool
```

**Expected:** Returns the existing file record (same `file_id` as test 4.6) with `process_status: "ready"` — no re-processing.

### 4.9 List files

```bash
curl -s "$API/files?project_id=$PROJECT_ID" \
  -H "Authorization: Bearer $TOKEN" | python3 -c "
import sys, json
files = json.load(sys.stdin)
for f in files:
    print(f\"  {f['file_name']} ({f['file_type']}) → {f['process_status']}\")
"
```

**Expected:** Lists uploaded files with their statuses.

### 4.10 Auto-scan retry behavior

```bash
# Corrupt a file's status in DB to simulate a prior failure
$PG "UPDATE file SET process_status = 'failed' WHERE file_name = 'test_simple.txt' LIMIT 1;"

# Restart API — scanner should retry it
docker compose restart finhouse-api
sleep 10
docker compose logs finhouse-api --tail 20 | grep -i "retry\|test_simple"
```

**Expected:** Logs show "Retrying previously failed file: test_simple.txt" and then processing it back to `ready`.

---

## Part 5 — RAG Retrieval (Files + Chat)

This is the critical integration test: uploaded documents should influence chat responses.

### 5.1 Create a new session for RAG testing

Use project 0 (where the auto-scanned data files live):

```bash
RAG_SESSION=$(curl -s -X POST $API/sessions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"project_id": 0, "model_used": "qwen2.5:14b"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['session_id'])")
echo "RAG Session: $RAG_SESSION"
```

### 5.2 Ask a question about uploaded document content

```bash
curl -N -X POST "$API/chat/$RAG_SESSION/send" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"text": "What was the total revenue in Q3 2024 and how much did it grow?"}' 2>&1 | tee /tmp/rag_response.txt
```

**Expected in the SSE stream:**
1. A `rag_sources` event showing retrieved chunks from `test_finance.md`
2. Token events that form a response mentioning **$4.2 million** and **15%** (from the document)
3. The response may include citation markers like `[1]`

### 5.3 Verify RAG context event was logged

```bash
$PG "SELECT event_type, LEFT(text, 120) as preview FROM chat_event WHERE session_id = '$RAG_SESSION' AND event_type = 'rag_context';"
```

**Expected:** At least 1 row with `event_type = 'rag_context'` containing JSON source references.

### 5.4 Ask a follow-up that requires document knowledge

```bash
curl -N -X POST "$API/chat/$RAG_SESSION/send" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"text": "What is the Q4 revenue guidance and expected operating margin?"}' 2>&1 | grep '"type":"token"' | sed 's/data: //g' | python3 -c "
import sys, json
full = ''
for line in sys.stdin:
    try:
        d = json.loads(line.strip())
        full += d.get('content', '')
    except: pass
print(full[:500])
"
```

**Expected:** Response mentions **$4.5M-$4.8M** and **36%** operating margin — information only available in the uploaded document.

### 5.5 Ask something NOT in the documents

```bash
curl -N -X POST "$API/chat/$RAG_SESSION/send" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"text": "What is the speed of light?"}' 2>&1 | grep '"type":"token"' | head -5
```

**Expected:** Responds with general knowledge (~299,792 km/s). The RAG retrieval may find no relevant chunks or low-relevance ones, and the model falls back to its own knowledge.

---

## Part 6 — Web Search Tool

### 6.1 Create a session with web_search enabled

```bash
SEARCH_SESSION=$(curl -s -X POST $API/sessions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"project_id": 0, "model_used": "qwen2.5:14b", "tools_used": ["web_search"]}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['session_id'])")
echo "Search Session: $SEARCH_SESSION"
```

### 6.2 Ask something that requires current information

```bash
curl -N -X POST "$API/chat/$SEARCH_SESSION/send" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"text": "What are the top news headlines today?"}' 2>&1 | tee /tmp/search_response.txt
```

**Expected in the SSE stream:**
1. `tool_start` event with `"tool": "web_search"` and a query
2. `tool_end` event with search results (titles, URLs, snippets)
3. Token events forming a response that references current news

### 6.3 Verify tool events in database

```bash
$PG "SELECT event_type, LEFT(text, 100) as preview FROM chat_event WHERE session_id = '$SEARCH_SESSION' AND event_type IN ('tool_call', 'tool_result') ORDER BY num_order;"
```

**Expected:** At least 2 rows: one `tool_call` (JSON with web_search tool name and query) and one `tool_result` (JSON array of search results).

### 6.4 Non-tool question in same session

```bash
curl -N -X POST "$API/chat/$SEARCH_SESSION/send" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"text": "What is 100 divided by 4?"}' 2>&1 | grep "tool_start" | wc -l
```

**Expected:** `0` — the model should answer directly without calling the search tool for simple math.

---

## Part 7 — Streamlit UI Tests

These tests are manual — perform them in the browser at http://localhost:8501.

### 7.1 Guest mode (no login)

1. Open http://localhost:8501 in a fresh browser / incognito tab
2. **Check:** Sidebar shows login/register tabs, no chat history panel
3. Click "➕ New Chat"
4. Type "Hello, what can you do?" and press Enter
5. **Check:** Response streams in with markdown rendering
6. **Check:** No session appears in sidebar history (guest mode)

### 7.2 Registration and login

1. Click the "Register" tab in the sidebar
2. Fill in username: `uitester`, password: `testpass123`, confirm password
3. Click "Create Account"
4. **Check:** Success message appears
5. Switch to "Sign In" tab, enter the same credentials
6. Click "Sign In"
7. **Check:** Sidebar now shows project selector, chat history, logout button

### 7.3 Project management

1. Click the ➕ button next to the project selector
2. Enter project name: "UI Test Project"
3. Click "Create"
4. **Check:** Project appears in the dropdown

### 7.4 Chat with model selector

1. Click "➕ New Chat"
2. Check the model dropdown — it should list available models (e.g., `qwen2.5:14b`)
3. Send a message: "Explain recursion in one sentence"
4. **Check:** Response streams in token by token
5. **Check:** Model dropdown is now disabled (locked after first message)
6. **Check:** Session appears in the sidebar history with an auto-generated title

### 7.5 File upload via UI

1. Expand the "⚙️ Tools & Files" section
2. Click the file upload area
3. Upload a `.txt` or `.pdf` file
4. **Check:** File appears in the file list below with a status indicator
5. Wait 10-15 seconds, then collapse and re-expand the section
6. **Check:** File status should change to ✅ ready
7. Try uploading a `.jpg` or `.json` file
8. **Check:** Status shows ❌ failed immediately

### 7.6 RAG-aware chat via UI

1. Make sure the project has at least one `ready` file (from test 7.5 or the auto-scanned files in default project)
2. Start a new chat in the default project (project_id=0 where `./data` files live)
3. Ask: "What was the Q3 revenue?"
4. **Check:** Response includes information from the uploaded `test_finance.md`
5. **Check:** A "📚 Sources" expander appears below the response
6. Click it → shows file name, relevance score, and text preview

### 7.7 Web search via UI

1. Expand "⚙️ Tools & Files"
2. Enable the "🔍 Web Search" toggle (must be before first message in a new session)
3. Start a new chat
4. Ask: "What happened in tech news this week?"
5. **Check:** A tool card appears showing the search query
6. **Check:** A "📋 Search results" expander appears with titled links
7. **Check:** The final response references the search results

### 7.8 Database Query & Visualize (coming soon)

1. In the "⚙️ Tools & Files" section, observe the "🗄️ Database Query" and "📊 Visualize" checkboxes
2. **Check:** Both are grayed out / disabled
3. **Check:** Hovering shows "Coming soon" tooltip

### 7.9 Incognito mode

1. Toggle "🕵️ Incognito" ON in the top bar
2. **Check:** Purple incognito banner appears
3. **Check:** Sidebar history disappears
4. Send a message — it should work normally
5. Toggle incognito OFF
6. **Check:** Banner disappears, history returns
7. **Check:** The incognito session does NOT appear in history

### 7.10 Session management

1. In the sidebar history, hover over a session
2. Click the 🗑 delete button
3. **Check:** Session is removed from the list
4. Create 3-4 chats with different messages
5. **Check:** Sessions are grouped by "Today" in the history

### 7.11 Health indicator

1. Look at the bottom of the sidebar
2. **Check:** Green dot with "All systems operational"
3. Stop the Ollama container: `docker compose stop finhouse-ollama`
4. Refresh the page
5. **Check:** Dot turns red, "Some services degraded" message, details show `ollama: ✗`
6. Restart: `docker compose start finhouse-ollama`

---

## Part 8 — Data Folder Auto-Scan Scenarios

### 8.1 Add new files while system is running

```bash
cat > data/new_document.md << 'EOF'
# Product Roadmap 2025

Phase 1: Launch mobile app (Q1)
Phase 2: Enterprise SSO integration (Q2)
Phase 3: Multi-region deployment (Q3)
EOF
```

Restart API to trigger scan:
```bash
docker compose restart finhouse-api
sleep 10
$PG "SELECT file_name, process_status FROM file WHERE file_name = 'new_document.md';"
```

**Expected:** Row with `process_status = 'ready'`.

### 8.2 Add unsupported file format

```bash
echo "binary content" > data/image_placeholder.png
docker compose restart finhouse-api
sleep 10
$PG "SELECT file_name, file_type, process_status FROM file WHERE file_name = 'image_placeholder.png';"
```

**Expected:** Row with `file_type = 'png'`, `process_status = 'failed'`.

### 8.3 Unchanged files are skipped on re-scan

```bash
docker compose restart finhouse-api
sleep 10
docker compose logs finhouse-api --tail 15 | grep "Already processed\|skipped"
```

**Expected:** Logs show previously processed files being skipped (not re-indexed).

### 8.4 Modified file is re-detected

```bash
# Modify an existing file (changes its SHA-256 hash)
echo -e "\n## Appendix\nAdditional notes added." >> data/test_finance.md
docker compose restart finhouse-api
sleep 15
$PG "SELECT file_name, process_status, file_hash FROM file WHERE file_name = 'test_finance.md' ORDER BY process_at DESC;"
```

**Expected:** A new row with the updated hash and `process_status = 'ready'`. The old row may still exist with the previous hash.

---

## Part 9 — End-to-End Scenario: "New Employee Onboarding"

This simulates a realistic use case from start to finish.

**Scenario:** A new employee joins the company, creates an account, uploads company documents, and uses FinHouse to ask questions about them and search the web.

### Step 1: Register

```bash
curl -s -X POST $API/auth/register \
  -H "Content-Type: application/json" \
  -d '{"user_name": "alice", "user_password": "welcome2024"}'
```

### Step 2: Login and save token

```bash
ALICE_TOKEN=$(curl -s -X POST $API/auth/login \
  -H "Content-Type: application/json" \
  -d '{"user_name": "alice", "user_password": "welcome2024"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
```

### Step 3: Create a project

```bash
ALICE_PROJECT=$(curl -s -X POST $API/projects \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $ALICE_TOKEN" \
  -d '{"project_title": "Onboarding", "description": "Company docs and Q&A"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['project_id'])")
echo "Alice Project: $ALICE_PROJECT"
```

### Step 4: Upload a company document

```bash
cat > /tmp/company_handbook.md << 'EOF'
# Company Handbook

## Working Hours
Standard working hours are 9:00 AM to 5:30 PM, Monday through Friday.
Flexible hours are available after the 3-month probation period.

## Leave Policy
All employees receive 20 days of paid leave per year.
Sick leave is 10 days per year with a doctor's note required after 3 consecutive days.
Maternity leave is 16 weeks at full pay.

## Benefits
Health insurance is provided through BlueCross, covering employee and dependents.
401(k) matching up to 4% of salary.
Annual learning budget of $2,000 per employee.
EOF

HANDBOOK_ID=$(curl -s -X POST "$API/files/upload" \
  -H "Authorization: Bearer $ALICE_TOKEN" \
  -F "file=@/tmp/company_handbook.md" \
  -F "project_id=$ALICE_PROJECT" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['file_id'])")
echo "Handbook file: $HANDBOOK_ID"
```

### Step 5: Wait for processing and verify

```bash
sleep 15
curl -s "$API/files/status/$HANDBOOK_ID" | python3 -c "
import sys, json
f = json.load(sys.stdin)
print(f\"Status: {f['process_status']}\")"
```

**Expected:** `Status: ready`

### Step 6: Create a chat session and ask questions

```bash
ALICE_SESSION=$(curl -s -X POST $API/sessions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $ALICE_TOKEN" \
  -d "{\"project_id\": $ALICE_PROJECT, \"model_used\": \"qwen2.5:14b\", \"tools_used\": [\"web_search\"]}" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['session_id'])")
```

### Step 7: Ask about the uploaded document

```bash
curl -N -X POST "$API/chat/$ALICE_SESSION/send" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $ALICE_TOKEN" \
  -d '{"text": "How many days of paid leave do I get, and what is the maternity leave policy?"}' 2>&1 \
  | grep '"type":"token"' | sed 's/data: //g' | python3 -c "
import sys, json
full = ''
for line in sys.stdin:
    try: full += json.loads(line.strip()).get('content','')
    except: pass
print(full[:500])
"
```

**Expected:** Response mentions **20 days** paid leave and **16 weeks** maternity leave — sourced from the uploaded handbook.

### Step 8: Ask something that needs web search

```bash
curl -N -X POST "$API/chat/$ALICE_SESSION/send" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $ALICE_TOKEN" \
  -d '{"text": "What are the current average 401k contribution limits for 2024?"}' 2>&1 \
  | grep '"type":"tool_start\|tool_end\|token"' | head -10
```

**Expected:** Shows `tool_start` (web search triggered), `tool_end` (results), then tokens with current 401k limit info from the web.

### Step 9: Verify the complete session

```bash
curl -s "$API/chat/$ALICE_SESSION/events" | python3 -c "
import sys, json
events = json.load(sys.stdin)
print(f'Total events: {len(events)}')
types = {}
for e in events:
    t = e['event_type']
    types[t] = types.get(t, 0) + 1
for t, c in sorted(types.items()):
    print(f'  {t}: {c}')
"
```

**Expected:**
```
Total events: ~8-12
  message: 4 (2 user + 2 assistant)
  rag_context: 1-2
  tool_call: 1
  tool_result: 1
```

### Step 10: Verify session has a title

```bash
curl -s "$API/sessions/$ALICE_SESSION" | python3 -c "
import sys, json
s = json.load(sys.stdin)
print(f\"Title: {s.get('session_title', 'None')}  Turns: {s['turn_count']}\")
"
```

**Expected:** A meaningful auto-generated title and `turn_count = 2`.

---

## Part 10 — Edge Cases & Error Handling

### 10.1 Empty message

```bash
curl -s -o /dev/null -w "%{http_code}" -X POST "$API/chat/$SESSION_ID/send" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"text": ""}'
```

**Expected:** `422` validation error (empty text).

### 10.2 Invalid session ID

```bash
curl -s -X POST "$API/chat/00000000-0000-0000-0000-000000000000/send" \
  -H "Content-Type: application/json" \
  -d '{"text": "hello"}' 2>&1 | head -3
```

**Expected:** Error event in SSE stream: `"Session not found"`.

### 10.3 Upload empty file

```bash
touch /tmp/empty.txt
curl -s -X POST "$API/files/upload" \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@/tmp/empty.txt" \
  -F "project_id=$PROJECT_ID" | python3 -m json.tool
```

**Expected:** File is created, but ingest will fail with "No extractable text found" → `process_status: "failed"`.

### 10.4 Very large message

```bash
LONG_MSG=$(python3 -c "print('Tell me about this. ' * 500)")
curl -s -o /dev/null -w "%{http_code}" -X POST "$API/chat/$SESSION_ID/send" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d "{\"text\": \"$LONG_MSG\"}"
```

**Expected:** `200` — the system should handle long messages (Ollama will truncate if exceeding context window).

### 10.5 Delete file removes from RAG

```bash
# Get a file_id
DEL_FILE=$(curl -s "$API/files?project_id=$PROJECT_ID" \
  -H "Authorization: Bearer $TOKEN" \
  | python3 -c "import sys,json; files=json.load(sys.stdin); print(files[0]['file_id'] if files else 'none')")

# Delete it
curl -s -o /dev/null -w "%{http_code}" -X DELETE "$API/files/$DEL_FILE" \
  -H "Authorization: Bearer $TOKEN"
```

**Expected:** `204`. Verify it no longer appears in file list:
```bash
curl -s "$API/files?project_id=$PROJECT_ID" -H "Authorization: Bearer $TOKEN" \
  | python3 -c "import sys,json; print(len(json.load(sys.stdin)), 'files remaining')"
```

### 10.6 Data persistence across restart

```bash
docker compose down
docker compose up -d
sleep 30

# Verify data survived
$PG "SELECT COUNT(*) FROM \"user\" WHERE user_name = 'alice';"
$PG "SELECT COUNT(*) FROM chat_event;"
```

**Expected:** User alice still exists, chat events are preserved (PostgreSQL volumes persist).

---

## Test Results Summary

Copy this table and fill it in as you go:

```
Part 1 — Component Health
  [ ] 1.1  All containers running
  [ ] 1.2  PostgreSQL responds
  [ ] 1.3  Schema exists (5 tables)
  [ ] 1.4  Seed data present
  [ ] 1.5  MinIO healthy + bucket
  [ ] 1.6  Milvus healthy
  [ ] 1.7  Ollama responds
  [ ] 1.8  SearXNG returns results
  [ ] 1.9  BGE-M3 embeds text
  [ ] 1.10 Reranker healthy
  [ ] 1.11 FastAPI /health OK
  [ ] 1.12 Streamlit UI loads

Part 2 — Auth & CRUD
  [ ] 2.1  Register user
  [ ] 2.2  Duplicate rejected
  [ ] 2.3  Login returns JWT
  [ ] 2.4  Wrong password rejected
  [ ] 2.5  Create project
  [ ] 2.6  List projects
  [ ] 2.7  Create session
  [ ] 2.8  Guest session (negative project_id)
  [ ] 2.9  DB verification

Part 3 — Basic Chat
  [ ] 3.1  Send + stream response
  [ ] 3.2  Events persisted
  [ ] 3.3  Multi-turn context works
  [ ] 3.4  Turn count + auto-title
  [ ] 3.5  Get events API

Part 4 — File Upload & Ingest
  [ ] 4.1  Test files created
  [ ] 4.2  Auto-scan processes files
  [ ] 4.3  DB status = ready
  [ ] 4.4  Files in MinIO
  [ ] 4.5  Vectors in Milvus
  [ ] 4.6  API upload (supported)
  [ ] 4.7  API upload (unsupported → failed)
  [ ] 4.8  Dedup works
  [ ] 4.9  List files
  [ ] 4.10 Retry failed files on re-scan

Part 5 — RAG Retrieval
  [ ] 5.1  RAG session created
  [ ] 5.2  Query returns document-sourced answer
  [ ] 5.3  rag_context event logged
  [ ] 5.4  Follow-up uses document knowledge
  [ ] 5.5  Non-document question uses general knowledge

Part 6 — Web Search Tool
  [ ] 6.1  Search session created
  [ ] 6.2  Search triggered + results returned
  [ ] 6.3  Tool events in DB
  [ ] 6.4  Simple question skips search

Part 7 — Streamlit UI
  [ ] 7.1  Guest mode works
  [ ] 7.2  Register + login
  [ ] 7.3  Project management
  [ ] 7.4  Chat + streaming + model lock
  [ ] 7.5  File upload + status chips
  [ ] 7.6  RAG-aware chat + sources panel
  [ ] 7.7  Web search tool in UI
  [ ] 7.8  DB/Viz tools disabled
  [ ] 7.9  Incognito mode
  [ ] 7.10 Session management
  [ ] 7.11 Health indicator

Part 8 — Data Folder Auto-Scan
  [ ] 8.1  New file detected + processed
  [ ] 8.2  Unsupported format → failed
  [ ] 8.3  Unchanged files skipped
  [ ] 8.4  Modified file re-detected

Part 9 — E2E: Onboarding Scenario
  [ ] Steps 1-10 all pass

Part 10 — Edge Cases
  [ ] 10.1 Empty message rejected
  [ ] 10.2 Invalid session handled
  [ ] 10.3 Empty file → failed
  [ ] 10.4 Large message handled
  [ ] 10.5 Delete removes from RAG
  [ ] 10.6 Data persists across restart
```

**Total: 59 tests across 10 parts.**

---

## Troubleshooting Quick Reference

| Symptom | Likely Cause | Fix |
|---|---|---|
| Container won't start | Port conflict | `docker compose down`, check `lsof -i :PORT`, change ports in `.env` |
| Ollama returns empty model list | Models not pulled | Run `scripts/pull-models.sh` |
| BGE-M3 service slow to start | Model downloading on first run | Wait 3-5 min, check `docker compose logs finhouse-bge-m3` |
| File stuck in `pending`/`processing` | BGE-M3 or Milvus unreachable | Check `docker compose logs finhouse-api`, verify embedding service health |
| RAG returns no sources | No files in project, or Milvus not loaded | Verify `file.process_status = 'ready'` and Milvus entity count > 0 |
| Web search returns errors | SearXNG misconfigured or rate-limited | Check `docker compose logs finhouse-searxng`, try direct curl to `:8080` |
| Chat hangs / no streaming | Ollama overloaded or OOM | Check GPU memory with `nvidia-smi`, try smaller model `llama3.1:8b` |
| JWT expired | Token timeout | Re-login to get fresh token, or call `/auth/refresh` |
| Data scan skips everything | Files unchanged (same hash) | Modify file content or delete DB rows and restart |