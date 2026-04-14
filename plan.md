# FinHouse — Implementation Plan

## How to Use This Document

The build is split into **7 phases**. Each phase ends with a **Verification Checklist** — a set of concrete, pass/fail tests you can run to confirm the phase is complete before moving on. Do not start phase N+1 until every checklist item in phase N passes.

---

## Phase 1 — Docker Infrastructure & Storage Tier

**Goal:** All infrastructure containers start, communicate, and persist data across restarts. No application code yet.

### Deliverables

1. **Project skeleton**
   - Repository root with `docker-compose.yml`, `.env.example`, and directory stubs: `api/`, `ui/`, `embed/`, `rerank/`.
   - `.env.example` containing all configurable secrets with placeholder values: `POSTGRES_PASSWORD`, `MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD`, `JWT_SECRET`.

2. **docker-compose.yml — infrastructure services only**
   - `finhouse-postgres` (postgres:16) — port 5432, named volume `pg-data`.
   - `finhouse-minio` (minio/minio) — ports 9000/9001, named volume `minio-data`, auto-creates `finhouse-files` bucket on startup via entrypoint or init container.
   - `finhouse-etcd` (quay.io/coreos/etcd) — internal only.
   - `finhouse-milvus` (milvusdb/milvus standalone) — port 19530, depends on etcd and MinIO, named volume `milvus-data`.
   - `finhouse-ollama` (ollama/ollama) — port 11434, GPU passthrough (`deploy.resources.reservations.devices`), named volume `ollama-models`.
   - Shared bridge network `finhouse-net`.

3. **PostgreSQL init script** (`api/db/init.sql`)
   - Creates all 5 tables: `user`, `project`, `chat_session`, `chat_event`, `file`.
   - Seeds user_id = 0 (`guest`, password = NULL).
   - Seeds project_id = 0 (default inbox, owned by user_id = 0).
   - Creates the negative-id sequence for incognito projects.
   - Creates all recommended indexes from `db.md`.

4. **Ollama model pull script** (`scripts/pull-models.sh`)
   - Pulls `qwen2.5:14b` (default) via `ollama pull` against the running container.

### Verification Checklist

| # | Test | Command / Action | Expected Result |
|---|------|-----------------|-----------------|
| 1.1 | All containers start | `docker compose up -d && docker compose ps` | All 5 services show `running` / `healthy` |
| 1.2 | PostgreSQL schema exists | `docker exec finhouse-postgres psql -U finhouse -c "\dt"` | Lists `user`, `project`, `chat_session`, `chat_event`, `file` |
| 1.3 | Guest user seeded | `docker exec finhouse-postgres psql -U finhouse -c "SELECT * FROM \"user\" WHERE user_id = 0"` | Returns 1 row: user_id=0, user_name='guest' |
| 1.4 | Default project seeded | `docker exec finhouse-postgres psql -U finhouse -c "SELECT * FROM project WHERE project_id = 0"` | Returns 1 row: project_id=0 |
| 1.5 | MinIO bucket exists | `docker exec finhouse-minio mc ls local/finhouse-files` or MinIO console at :9001 | Bucket `finhouse-files` is listed |
| 1.6 | Milvus is reachable | `curl http://localhost:19530/v1/vector/collections` or pymilvus connection test | Returns 200 / empty collection list |
| 1.7 | Ollama responds | `curl http://localhost:11434/api/tags` | Returns JSON with model list |
| 1.8 | Model pulled | `curl http://localhost:11434/api/tags \| grep qwen2.5` | `qwen2.5:14b` appears in the list |
| 1.9 | Data survives restart | `docker compose down && docker compose up -d`, repeat test 1.3 | Guest user still exists (volume persisted) |

---

## Phase 2 — FastAPI Core & Authentication

**Goal:** A running FastAPI server with health check, user registration, login, JWT auth, and full CRUD for projects and sessions. No chat logic yet.

### Deliverables

1. **FastAPI application** (`api/`)
   - `main.py` — app factory, CORS middleware, lifespan events.
   - `config.py` — reads all env vars from `.env`.
   - `database.py` — async SQLAlchemy engine + session factory connecting to PostgreSQL.
   - `models.py` — SQLAlchemy ORM models for all 5 tables.

2. **Health endpoint**
   - `GET /health` — checks PostgreSQL, MinIO, Milvus, and Ollama connectivity. Returns `{ "status": "ok", "services": { ... } }`.

3. **Auth module** (`api/routers/auth.py`)
   - `POST /auth/register` — create user, hash password with bcrypt, return user_id.
   - `POST /auth/login` — verify credentials, return JWT access token (short-lived, e.g. 30min) + refresh token.
   - `POST /auth/refresh` — issue new access token from refresh token.
   - Dependency `get_current_user` — extracts user_id from JWT; returns user_id = 0 if no token provided (guest mode).

4. **Project CRUD** (`api/routers/projects.py`)
   - `POST /projects` — create named project (positive project_id). Auth required.
   - `GET /projects` — list user's projects ordered by `update_at DESC`. Auth required.
   - `PUT /projects/{project_id}` — rename project or update description. Auth required.
   - `DELETE /projects/{project_id}` — delete project and cascade (sessions, events, files). Auth required.

5. **Session CRUD** (`api/routers/sessions.py`)
   - `POST /sessions` — create chat_session under a project_id. Accepts `model_used` and `tools_used`. For guest/incognito: auto-creates a negative project_id project first.
   - `GET /sessions?project_id=X` — list sessions for a project, ordered by `update_at DESC`, grouped by recency.
   - `GET /sessions/{session_id}` — get session details.
   - `PUT /sessions/{session_id}` — rename session.
   - `DELETE /sessions/{session_id}` — delete session and cascade events.

6. **Dockerfile** for FastAPI container (`api/Dockerfile`)
   - Python 3.11 slim, installs dependencies from `requirements.txt`.
   - Runs with Uvicorn.

7. **Add `finhouse-api` service** to `docker-compose.yml`
   - Depends on postgres, minio, milvus, ollama.
   - Port 8000.

### Verification Checklist

| # | Test | Command / Action | Expected Result |
|---|------|-----------------|-----------------|
| 2.1 | API starts | `curl http://localhost:8000/health` | Returns 200 with all services `ok` |
| 2.2 | Swagger docs load | Open `http://localhost:8000/docs` in browser | Interactive API docs render |
| 2.3 | Register user | `curl -X POST /auth/register -d '{"user_name":"alice","user_password":"secret123"}'` | Returns 201 with `user_id > 0` |
| 2.4 | Duplicate register rejected | Repeat 2.3 with same username | Returns 409 Conflict |
| 2.5 | Login returns JWT | `curl -X POST /auth/login -d '{"user_name":"alice","user_password":"secret123"}'` | Returns 200 with `access_token` and `refresh_token` |
| 2.6 | Wrong password rejected | Login with wrong password | Returns 401 Unauthorized |
| 2.7 | Create project (authed) | `POST /projects` with valid JWT, body `{"project_title":"My Project"}` | Returns 201 with `project_id > 0` |
| 2.8 | List projects | `GET /projects` with JWT | Returns array containing the created project |
| 2.9 | Create project (no auth) rejected | `POST /projects` without JWT | Returns 401 |
| 2.10 | Create session (authed) | `POST /sessions` with `{"project_id": <from 2.7>, "model_used": "qwen2.5:14b"}` | Returns 201 with `session_id` (UUID) |
| 2.11 | Create session (guest) | `POST /sessions` without JWT, body `{"model_used": "qwen2.5:14b"}` | Returns 201, `project_id < 0` auto-assigned |
| 2.12 | List sessions | `GET /sessions?project_id=<from 2.7>` with JWT | Returns array with the session from 2.10 |
| 2.13 | Delete cascade works | `DELETE /sessions/<session_id>` then `GET /sessions/<session_id>` | DELETE returns 204, GET returns 404 |
| 2.14 | Refresh token works | `POST /auth/refresh` with refresh_token | Returns new access_token |

---

## Phase 3 — Basic Chat (No RAG, No Tools)

**Goal:** End-to-end chat works: user sends a message, FastAPI calls Ollama, streams the response back via SSE, all events are logged in `chat_event`. Streamlit UI renders a functional chat interface.

### Deliverables

1. **Chat endpoint** (`api/routers/chat.py`)
   - `POST /chat/{session_id}/send` — accepts `{ "text": "..." }`.
     - Inserts user message as `chat_event` (role=user, event_type=message).
     - Increments `turn_count`.
     - Assembles prompt (Phase 3: no RAG, no summaries — just last N messages).
     - Calls Ollama `/api/chat` with streaming enabled.
     - Returns SSE stream (`text/event-stream`).
     - After stream completes, inserts assistant message as `chat_event`.
   - `GET /chat/{session_id}/events` — returns all events for the session ordered by `num_order`.
   - `POST /chat/{session_id}/stop` — cancels an in-progress stream (sets a cancellation flag).

2. **Ollama client module** (`api/services/ollama.py`)
   - Async HTTP client wrapping Ollama's `/api/chat` and `/api/tags` endpoints.
   - `list_models()` — returns available model tags.
   - `chat_stream(model, messages)` — yields tokens as async generator.

3. **Streamlit UI — basic chat** (`ui/`)
   - `app.py` — main Streamlit entrypoint.
   - **Sidebar**: brand name "FinHouse", new chat button, placeholder for future history panel.
   - **Main area**: model selector dropdown (fetched from `GET /ollama/tags`), chat viewport with `st.chat_message` components, text input with `st.chat_input`.
   - Streaming: uses `st.write_stream` or manual token-by-token rendering.
   - Session state: tracks `session_id`, `messages` list, `selected_model`.
   - No auth UI yet — everything runs as guest (user_id = 0).

4. **Dockerfile** for Streamlit container (`ui/Dockerfile`)
5. **Add `finhouse-ui` service** to `docker-compose.yml` — port 8501, depends on `finhouse-api`.

### Verification Checklist

| # | Test | Command / Action | Expected Result |
|---|------|-----------------|-----------------|
| 3.1 | UI loads | Open `http://localhost:8501` | Streamlit app renders with chat interface |
| 3.2 | Model list populates | Check the model selector dropdown | Shows `qwen2.5:14b` (and any other pulled models) |
| 3.3 | Send message (UI) | Type "Hello, who are you?" and press Enter | Assistant response streams in token by token |
| 3.4 | Send message (API) | `curl -N -X POST /chat/<session_id>/send -d '{"text":"What is 2+2?"}'` | SSE stream returns tokens, final answer includes "4" |
| 3.5 | Events persisted | `GET /chat/<session_id>/events` | Returns at least 2 events: user message + assistant message, ordered by num_order |
| 3.6 | Multi-turn context | Send 3 messages in same session, last referencing first | Assistant correctly references earlier context |
| 3.7 | New chat resets | Click "New Chat" in sidebar, send a message | New session_id, no carryover from previous conversation |
| 3.8 | Stop button works | Send a long prompt, click Stop mid-stream | Stream halts, partial response is saved as chat_event |
| 3.9 | Empty state renders | Open app with no sessions | Welcome screen with suggested prompts appears |
| 3.10 | Event ordering correct | Send 5 messages, check `num_order` values in DB | Strictly monotonic: 1, 2, 3, 4, 5, ... |

---

## Phase 4 — Authentication UI, Projects, History & Incognito

**Goal:** Full user lifecycle in the Streamlit UI — register, login, create projects, browse history, rename/delete sessions, and incognito mode. Sidebar is fully functional.

### Deliverables

1. **Auth UI in Streamlit**
   - Login / register page (shown when no JWT in session_state).
   - JWT stored in `st.session_state`; attached to all API calls.
   - Logout button in sidebar footer clears session state.

2. **Sidebar — project list**
   - Fetches projects from `GET /projects`.
   - "+ New Project" button opens a text input for project title.
   - Clicking a project filters the chat history list.

3. **Sidebar — chat history list**
   - Fetches sessions from `GET /sessions?project_id=X`.
   - Grouped by recency: Today, Yesterday, Last 7 days, Older.
   - Each row shows `session_title` (or "Untitled" if NULL) and relative timestamp.
   - Click to load session; on hover: rename icon, delete icon.
   - "Load more" pagination if list overflows.
   - **Hidden for guest users** — replaced with sign-in prompt.

4. **Sidebar — search chats**
   - Date-time range picker (using `st.date_input`).
   - Filters session list by `create_at` range.
   - Hidden for user_id = 0.

5. **Incognito mode**
   - Ghost icon toggle in main top bar.
   - When active: session attributed to user_id = 0, negative project_id, incognito banner below top bar, history panel hidden.
   - Toggle off returns to normal authenticated mode.

6. **Sidebar footer**
   - User settings (placeholder drawer — profile, default model).
   - API Docs link → `http://localhost:8000/docs`.
   - Health check status dot: green/red based on `GET /health`.

7. **Context status badge** (top bar, near session title)
   - "Full history" for turn_count < 3. (Compression not implemented yet — badge is wired to the data, ready for Phase 6.)

### Verification Checklist

| # | Test | Command / Action | Expected Result |
|---|------|-----------------|-----------------|
| 4.1 | Register from UI | Fill register form, submit | Account created, redirected to login |
| 4.2 | Login from UI | Enter credentials, submit | Sidebar shows projects and history, chat is functional |
| 4.3 | Guest mode (no login) | Open app without logging in | No sidebar history, incognito banner, chat works |
| 4.4 | Create project | Click "+ New Project", enter name | Project appears in sidebar list |
| 4.5 | History shows sessions | Create 3 sessions with messages | All 3 appear in history list with correct grouping |
| 4.6 | Click session loads it | Click a history item | Chat viewport loads that session's messages |
| 4.7 | Rename session | Hover session → click rename → enter new title | Title updates in sidebar and DB |
| 4.8 | Delete session | Hover session → click delete → confirm | Session removed from sidebar, events deleted from DB |
| 4.9 | Incognito toggle | Toggle incognito ON while logged in | Banner appears, history hides, new session has negative project_id |
| 4.10 | Incognito toggle OFF | Toggle incognito OFF | History reappears, next new session uses normal project_id |
| 4.11 | Search by date | Pick a date range in search | Only sessions within range are shown |
| 4.12 | Health dot works | Stop PostgreSQL container, check sidebar | Health dot turns red; restart container → turns green |
| 4.13 | Logout clears state | Click logout | Redirected to login page, session_state cleared |
| 4.14 | Model locked mid-session | Start a session with model A, try to change to model B | Warning shown, model does not change |

---

## Phase 5 — File Upload & RAG Pipeline

**Goal:** Users can upload documents, files are ingested and embedded, and chat responses include RAG-retrieved context with inline citations.

### Deliverables

1. **File upload endpoint** (`api/routers/files.py`)
   - `POST /files/upload` — accepts multipart file + project_id + optional session_id.
   - Computes SHA-256 hash, checks for duplicates.
   - Stores file in MinIO (path per conventions in `db.md`).
   - Inserts `file` row with status=`pending`.
   - Enqueues ingest background task.
   - `GET /files?project_id=X` — list files with their `process_status`.
   - `DELETE /files/{file_id}` — soft-delete (sets status=`deleted`, removes from Milvus).

2. **Background ingest worker** (`api/services/ingest.py`)
   - Downloads file from MinIO.
   - Parses document: PDF (via pdfplumber/pypdf), DOCX (via python-docx), MD/TXT (direct read).
   - Chunks text (recursive character splitter, ~512 tokens per chunk with overlap).
   - Calls BGE-M3 embedding service to get dense vectors.
   - Upserts vectors into Milvus collection, partitioned by `project_id`.
   - Updates `file.process_status` to `ready` or `failed`.

3. **BGE-M3 embedding service** (`embed/`)
   - Simple FastAPI wrapper around `sentence-transformers` or `FlagEmbedding` library.
   - `POST /embed` — accepts list of text chunks, returns list of vectors.
   - Dockerfile and added to `docker-compose.yml`.

4. **BGE-M3 reranker service** (`rerank/`)
   - `POST /rerank` — accepts query + list of candidate texts, returns reranked scores.
   - Dockerfile and added to `docker-compose.yml`.

5. **RAG retrieval in chat flow**
   - Before calling Ollama, FastAPI:
     - Embeds the user query via BGE-M3.
     - Queries Milvus for top-K chunks (K=20) scoped to the session's project_id.
     - Reranks via BGE-M3 reranker, takes top-N (N=5).
     - Inserts a `rag_context` event into `chat_event`.
     - Injects retrieved chunks into the prompt as numbered sources.
   - Assistant response prompt instructs the model to cite sources as `[1]`, `[2]`, etc.

6. **Streamlit UI — file upload & citations**
   - File upload via `st.file_uploader` in the "+" feature popover.
   - File chips above text input showing `process_status` (pending → processing → ready).
   - Incognito files show 🕵 indicator.
   - Assistant messages render `[1]`, `[2]` as footnote links.
   - Collapsible "Sources" panel below each assistant response showing cited file names and chunk previews.

### Verification Checklist

| # | Test | Command / Action | Expected Result |
|---|------|-----------------|-----------------|
| 5.1 | Upload a PDF | Upload a 5-page PDF via UI or `POST /files/upload` | File chip shows "pending" → "processing" → "ready" |
| 5.2 | File in MinIO | Check MinIO console at :9001 | File object exists at correct path |
| 5.3 | File in database | `SELECT * FROM file WHERE file_id = '<id>'` | Row exists with `process_status = 'ready'` |
| 5.4 | Vectors in Milvus | Query Milvus collection for the project_id partition | Returns >0 vectors (number depends on document length) |
| 5.5 | Duplicate skipped | Upload the exact same file again | API returns existing file_id, no new Milvus vectors created |
| 5.6 | RAG retrieval works | Ask a question about the uploaded document content | Response references information from the document |
| 5.7 | Citations appear | Check assistant response | Inline `[1]`, `[2]` references appear in the response text |
| 5.8 | Sources panel works | Expand "Sources" below the response | Shows file name(s) and chunk preview(s) matching the citations |
| 5.9 | rag_context event logged | `GET /chat/<session_id>/events` | An event with `event_type = 'rag_context'` exists for that turn |
| 5.10 | Upload DOCX | Upload a `.docx` file | Ingested successfully, status = ready |
| 5.11 | Upload MD/TXT | Upload a `.md` and a `.txt` file | Both ingested successfully |
| 5.12 | Unsupported file rejected | Upload a `.jpg` file | API returns 400 with clear error message |
| 5.13 | Delete file | `DELETE /files/<file_id>` | Status set to `deleted`, Milvus vectors removed, file no longer retrieved in RAG |
| 5.14 | Incognito file path | Upload a file in incognito mode, check MinIO | File stored under `finhouse-files/incognito/{session_id}/` |
| 5.15 | Embedding service healthy | `curl http://finhouse-bge-m3:8081/health` (from inside Docker network) | Returns 200 |
| 5.16 | Reranker service healthy | `curl http://finhouse-reranker:8082/health` (from inside Docker network) | Returns 200 |

---

## Phase 6 — Context Compression & Cron Jobs

**Goal:** Long conversations remain functional via rolling summaries and checkpoints. Ephemeral data is cleaned up automatically.

### Deliverables

1. **Summary generation** (`api/services/compression.py`)
   - Triggered after `turn_count` becomes divisible by 3.
   - Fetches last 6 message events (the 3 most recent turns).
   - Calls Ollama with a summarization prompt.
   - Appends 1 `summary` event to `chat_event`.
   - Increments `summary_count` on `chat_session`.

2. **Checkpoint generation**
   - Triggered after `summary_count` becomes divisible by 3.
   - Fetches last 3 `summary` events + existing `checkpoint` event.
   - Calls Ollama with a checkpoint/condensation prompt.
   - Appends 1 `checkpoint` event to `chat_event`.
   - Updates `chat_session.session_title` with a title derived from the checkpoint.

3. **Updated prompt assembly**
   - Prompt now built as: latest checkpoint → recent summaries (up to 3) → last 6 messages → RAG context → user query.
   - Falls back gracefully when no checkpoint or summaries exist yet.

4. **Context status badge in UI**
   - Reads `turn_count` and `summary_count` from session metadata.
   - Displays: "Full history" / "Summarized context" / "Checkpoint + summaries".
   - Tooltip on hover explaining compression.

5. **Ephemeral cleanup cron** (`api/services/cleanup.py`)
   - Runs on a schedule (configurable, default: hourly) via APScheduler or a dedicated script triggered by cron in Docker.
   - Targets all records with `project_id < 0`.
   - Cascade delete order: chat_event → file → chat_session → project.
   - Purges MinIO objects under `incognito/` prefix.
   - Purges Milvus vectors for negative project_id partitions.
   - Logs the count of deleted records for observability.

### Verification Checklist

| # | Test | Command / Action | Expected Result |
|---|------|-----------------|-----------------|
| 6.1 | Summary at 3 turns | Send 3 complete turns (user+assistant each) in one session | `chat_event` contains exactly 1 event with `event_type = 'summary'` |
| 6.2 | Summary at 6 turns | Send 3 more turns (6 total) | Now 2 summary events in the session |
| 6.3 | Checkpoint at 9 turns | Send 3 more turns (9 total, 3 summaries) | 1 `checkpoint` event appears; `session_title` is no longer NULL |
| 6.4 | Session title auto-set | Check sidebar history after checkpoint | Session shows a meaningful auto-generated title |
| 6.5 | Prompt uses compression | Send turn 10+; inspect the prompt sent to Ollama (via debug logging) | Prompt contains: checkpoint text + recent summaries + last 6 messages (not all 20+ messages) |
| 6.6 | Context badge updates | Check the context badge in top bar at 2 turns, 4 turns, 10 turns | Shows "Full history" → "Summarized context" → "Checkpoint + summaries" |
| 6.7 | Long conversation coherent | At turn 15+, reference something from turn 1 | Assistant recalls it (via checkpoint/summary), not verbatim but conceptually correct |
| 6.8 | Cron cleanup runs | Create an incognito session with messages and files, wait for cron (or trigger manually) | All records with negative project_id deleted from DB |
| 6.9 | MinIO incognito cleaned | After cron, check MinIO console | No objects under `finhouse-files/incognito/` |
| 6.10 | Milvus incognito cleaned | After cron, query Milvus for negative project_id partition | Returns 0 vectors |
| 6.11 | Authenticated data untouched | After cron, check an authenticated user's sessions | All data intact — cron only targets negative project_ids |
| 6.12 | Cron logs output | Check cron job logs | Reports count of deleted sessions, events, files |

---

## Phase 7 — LangGraph Agent & Tools

**Goal:** The agentic layer is functional — the LLM can decide to call tools (web search, database query, visualize) and the results are rendered in the UI.

### Deliverables

1. **LangGraph agent setup** (`api/services/agent.py`)
   - LangGraph state machine with nodes: `call_model`, `call_tool`, `respond`.
   - Tool definitions registered with the agent (name, description, input schema).
   - Agent receives the assembled prompt + tool definitions and decides whether to call tools or respond directly.
   - Tool calls and results are logged as `chat_event` rows (`tool_call` and `tool_result` event types).

2. **Web Search tool** (`api/tools/web_search.py`)
   - Queries SearXNG (self-hosted container added to docker-compose) or DuckDuckGo API.
   - Input: search query string.
   - Output: top-N results with title, URL, snippet.
   - Add `finhouse-searxng` container to `docker-compose.yml` (optional — can use DuckDuckGo as fallback).

3. **Database Query tool** (`api/tools/database_query.py`)
   - Input: natural language question about data.
   - The tool prompts the LLM to generate SQL from the question + database schema.
   - Executes SQL against the OLAP database via a **read-only** connection.
   - Output: tabular result (list of dicts or formatted table).
   - Safety: SQL validation (reject DDL/DML), query timeout, row limit.

4. **Visualize tool** (`api/tools/visualize.py`)
   - Input: natural language chart description + DataFrame (from a prior `database_query` result).
   - Prompts the LLM to generate Python code using matplotlib, plotly, or seaborn.
   - Executes the generated code in a **sandboxed subprocess** (restricted imports, no network, no filesystem write except output path).
   - Output: PNG image path (saved to MinIO) or Plotly JSON for interactive rendering.
   - Error handling: if code execution fails, returns the error to the LLM for one retry.

5. **Tool toggles in UI**
   - "+" feature popover includes toggle switches for each tool.
   - Toggles stored in `chat_session.tools_used`.
   - Disabled for models not marked as tool-capable.
   - Only togglable at session start (or before first message).

6. **Tool activity rendering in UI**
   - Collapsible "Tool use" row between user message and final response.
   - Shows: tool name, input arguments (abbreviated), output preview.
   - For `database_query`: output rendered as a `st.dataframe` table.
   - For `visualize`: output rendered as `st.image` (PNG) or `st.plotly_chart` (interactive).
   - "View code" expander for the `visualize` tool showing the generated Python code.
   - For `web_search`: output rendered as a list of result cards (title, URL, snippet).

7. **Updated chat endpoint**
   - `POST /chat/{session_id}/send` now routes through the LangGraph agent instead of calling Ollama directly.
   - Tool calls may produce multiple intermediate events before the final response.
   - SSE stream includes event types: `token` (streaming text), `tool_start` (tool invocation), `tool_end` (tool result), `done`.

### Verification Checklist

| # | Test | Command / Action | Expected Result |
|---|------|-----------------|-----------------|
| 7.1 | Agent responds without tools | Ask "What is the capital of France?" (no tools needed) | Normal response, no tool_call events |
| 7.2 | Web search works | Enable web_search tool, ask "What happened in the news today?" | Tool activity shows search query + results; response incorporates search findings |
| 7.3 | web_search event logged | Check `chat_event` for the session | Contains `tool_call` event (web_search) and `tool_result` event |
| 7.4 | Database query works | Enable database_query, ask "Show me the total sales by month" (against a test OLAP table) | Tool activity shows generated SQL + tabular result; response summarizes the data |
| 7.5 | SQL injection blocked | Ask "DROP TABLE user" or other DDL | Tool rejects the query with an error message, no data modified |
| 7.6 | Visualize works | Ask "Show me a bar chart of sales by month" | Tool activity shows generated Python code + rendered chart image |
| 7.7 | Chart renders in UI | Check the chat viewport | Chart appears inline as an image or interactive Plotly widget |
| 7.8 | "View code" expander | Expand the code viewer under the chart | Shows the matplotlib/plotly Python code that generated the chart |
| 7.9 | Chained tools | Ask "Query the monthly revenue and plot it as a line chart" | Agent calls `database_query` first, then `visualize` — both tool traces visible |
| 7.10 | Tool toggle disabled | Disable all tools, ask a question that would normally trigger a tool | Agent responds directly without calling any tools |
| 7.11 | Non-tool-capable model | Select a model without tool support, check tool toggles | Tool toggles are grayed out / disabled |
| 7.12 | Tool error recovery | Trigger a chart code error (e.g., ask for a chart type the data doesn't support) | Agent retries once; if still fails, returns a text explanation instead of crashing |
| 7.13 | SSE event types | Monitor the SSE stream during a tool call | Stream includes `tool_start`, `tool_end`, and `token` events in correct order |
| 7.14 | Visualize sandbox isolation | Check that generated code cannot access filesystem or network | Code runs in restricted subprocess; attempts to open files or make HTTP calls fail |

---

## Phase Dependencies

```
Phase 1 ─── Phase 2 ─── Phase 3 ─── Phase 4
                │                       │
                │                       │
                └──── Phase 5 ──── Phase 6
                                        │
                                   Phase 7
```

- **Phase 1** is the foundation — everything depends on it.
- **Phase 2** requires Phase 1 (database + containers).
- **Phase 3** requires Phase 2 (API + auth + session CRUD).
- **Phase 4** requires Phase 3 (working chat to build UI around).
- **Phase 5** requires Phase 2 (API endpoints) and can be developed in parallel with Phase 4. However, the UI parts of Phase 5 (file chips, citations) require Phase 4's UI to be in place.
- **Phase 6** requires Phase 5 (RAG pipeline must work before compression makes sense to test end-to-end).
- **Phase 7** requires Phase 6 (full prompt assembly with compression must work before adding tool calls on top).

---

## Estimated Timeline

| Phase | Duration | Cumulative |
|-------|----------|------------|
| Phase 1 — Docker & Storage | 2–3 days | 2–3 days |
| Phase 2 — FastAPI & Auth | 3–4 days | 5–7 days |
| Phase 3 — Basic Chat | 3–4 days | 8–11 days |
| Phase 4 — UI, History, Incognito | 4–5 days | 12–16 days |
| Phase 5 — File Upload & RAG | 5–7 days | 17–23 days |
| Phase 6 — Compression & Cron | 3–4 days | 20–27 days |
| Phase 7 — Agent & Tools | 5–7 days | 25–34 days |

Total estimate: **4–5 weeks** for a single developer working full-time.

---

## Post-Launch Improvements (Not Phased)

These are enhancements to consider after all 7 phases are complete and stable:

- **Dark mode** — Streamlit custom theme with incognito-specific hue.
- **Session export** — download conversation as Markdown or PDF.
- **Multi-user concurrency testing** — load testing with multiple simultaneous sessions.
- **Model hot-swap** — allow pulling new Ollama models from the UI.
- **Advanced RAG** — hybrid search (dense + sparse), query expansion, multi-hop retrieval.
- **Monitoring** — Prometheus metrics + Grafana dashboards for latency, token usage, error rates.
- **HTTPS / reverse proxy** — Nginx or Traefik in front of Streamlit and FastAPI for production deployment.