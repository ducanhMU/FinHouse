# FinHouse — Backend Database Specification

## Overview

The FinHouse relational database (PostgreSQL) stores all structured state: users, projects, chat sessions, the full chat event log, and file metadata. It supports a **guest / default user** pattern (user_id = 0), incognito sessions using negative project IDs, a rolling context-compression mechanism (summary + checkpoint events), and a background cleanup mechanism for ephemeral data.

---

## Design Principles

- **user_id = 0** is the reserved guest identity. All unauthenticated and incognito interactions are mapped to this user. No authentication record exists for user_id = 0 — it is a synthetic default seeded at DB init.
- **project_id = 0** is the default "inbox" project for authenticated users who have not placed a session into an explicit project.
- **Negative project_id** values (e.g., -1, -2, ...) are assigned to temporary sessions created by guest users or in incognito mode. A cron job purges these on a schedule.
- **Normal flow**: An authenticated user creates named projects (positive project_ids), creates sessions within them, and uploads files. All data is persisted and queryable.
- `chat_event` is an **append-only log** — every turn, tool call, RAG retrieval, summary, and checkpoint is a row here.

---

## Tables

### `user`

Stores registered user accounts.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| user_id | SERIAL | PRIMARY KEY | 0 = reserved guest; positive = real users |
| user_name | VARCHAR(128) | NOT NULL, UNIQUE | Login identifier / display name |
| user_password | VARCHAR(256) | NULLABLE | Bcrypt-hashed password. NULL for user_id = 0 |
| create_at | TIMESTAMPTZ | NOT NULL, DEFAULT NOW() | Registration timestamp |

**Notes:**
- user_id = 0 is pre-seeded at database initialization with `user_name = 'guest'` and `user_password = NULL`.
- For incognito mode, all session data is attributed to user_id = 0 at the application layer, even if the real user is authenticated.
- JWT tokens are issued on login; user_id = 0 can never receive a token.

---

### `project`

Groups chat sessions into named workspaces owned by a user.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| project_id | INTEGER | PRIMARY KEY | 0 = default inbox; positive = user-created; negative = temp/incognito |
| user_id | INTEGER | FK → user.user_id, NOT NULL | Owner of the project |
| project_title | VARCHAR(256) | NOT NULL | Display name |
| description | TEXT | NULLABLE | Optional project description |
| create_at | TIMESTAMPTZ | NOT NULL, DEFAULT NOW() | Creation timestamp |
| update_at | TIMESTAMPTZ | NOT NULL, DEFAULT NOW() | Last modification timestamp |

**Special project_id values:**

| Value | Meaning |
|---|---|
| 0 | Default/uncategorized project for authenticated users (pre-seeded) |
| > 0 | Normal user-created project |
| < 0 | Temporary incognito / guest session project — scheduled for deletion by cron |

**Notes:**
- Negative project_ids are generated using a dedicated descending counter (e.g., a PostgreSQL sequence starting at -1 decrementing). They are never exposed in the UI.
- The cron cleanup job targets `project_id < 0`. See the Cron Job section for cascade order.

---

### `chat_session`

Represents a single conversation thread within a project.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| session_id | UUID | PRIMARY KEY, DEFAULT gen_random_uuid() | Unique session identifier |
| project_id | INTEGER | FK → project.project_id, NOT NULL | Parent project (0 = default, negative = temp) |
| session_title | VARCHAR(512) | NULLABLE | Set/updated by checkpoint background job |
| create_at | TIMESTAMPTZ | NOT NULL, DEFAULT NOW() | Session creation time |
| update_at | TIMESTAMPTZ | NOT NULL, DEFAULT NOW() | Updated on every new chat_event |
| model_used | VARCHAR(128) | NOT NULL | Ollama model tag at session start (e.g., `qwen2.5:14b`) |
| tools_used | TEXT[] | NULLABLE | Tools enabled at session start (e.g., `{web_search,database_query,visualize}`) |
| turn_count | INTEGER | NOT NULL, DEFAULT 0 | Total turns (1 turn = 1 user message + 1 assistant response) |
| summary_count | INTEGER | NOT NULL, DEFAULT 0 | Total summaries generated for this session |

**Notes:**
- `session_title` starts as NULL and is updated by the background `generate_checkpoint` job based on accumulated context.
- `turn_count` and `summary_count` are incremented by the Background Worker and used to trigger summary/checkpoint jobs without re-counting events.
- For guest/incognito sessions, `project_id` is negative. The session and its title are never displayed in the history panel.

---

### `chat_event`

The append-only log of all events within a conversation. Every message, tool interaction, RAG retrieval, summary, and checkpoint is a row here.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| message_id | UUID | PRIMARY KEY, DEFAULT gen_random_uuid() | Unique event identifier |
| session_id | UUID | FK → chat_session.session_id, NOT NULL | Parent session |
| num_order | INTEGER | NOT NULL | Monotonically increasing sequence within the session |
| role | VARCHAR(32) | NOT NULL | `user`, `assistant`, or `system` |
| text | TEXT | NOT NULL | Event content (message text, tool JSON, summary prose, etc.) |
| event_type | VARCHAR(32) | NOT NULL | See event_type values below |
| create_at | TIMESTAMPTZ | NOT NULL, DEFAULT NOW() | Event creation timestamp |

**`event_type` values:**

| Value | Role | Description |
|---|---|---|
| `message` | user / assistant | A standard chat turn — user query or assistant final answer |
| `tool_call` | assistant | LangGraph agent decided to call a tool; `text` contains JSON with tool name and arguments |
| `tool_result` | system | Result returned from a tool call; `text` contains the tool output |
| `rag_context` | system | Top-K reranked document chunks retrieved from Milvus and injected into the prompt |
| `summary` | system | A rolling summary generated by the background worker every 3 turns |
| `checkpoint` | system | A condensed long-term memory generated every 3 summaries; also used to update `session_title` |
| `error` | system | An error event (model failure, tool error, ingest failure, etc.) |

**Context compression mechanism:**

The background worker operates on `turn_count` and `summary_count`:

```
Every 3 turns  → generate_summary:
  Input:  last 6 message events (3 turns)
  Output: 1 summary event appended to chat_event

Every 3 summaries → generate_checkpoint:
  Input:  last 3 summary events + existing checkpoint (if any)
  Output: 1 new/updated checkpoint event appended to chat_event
          + update chat_session.session_title
```

FastAPI assembles the LLM prompt before each call as:
1. Latest `checkpoint` event text (if exists)
2. Recent `summary` events since last checkpoint (up to 3)
3. Last 6 `message` events (3 raw turns)
4. Current `rag_context` (retrieved chunks for this turn)
5. Current user query

This keeps the effective context bounded at ~(1 checkpoint + 3 summaries + 6 messages) regardless of conversation length.

**Notes:**
- `num_order` uses a per-session counter (maintained by the application) to allow correct sequence reconstruction even under concurrent writes.
- `rag_context` events are stored for auditability and to power inline source citations in the UI.
- `tool_call` and `tool_result` pairs are stored so the full agentic trace is replayable.

---

### `file`

Tracks files uploaded by users. The physical file lives in MinIO; this table holds the metadata and processing state.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| file_id | UUID | PRIMARY KEY, DEFAULT gen_random_uuid() | Unique file identifier |
| user_id | INTEGER | FK → user.user_id, NOT NULL | Uploader (0 for guest/incognito) |
| project_id | INTEGER | FK → project.project_id, NOT NULL | Associated project (negative = temp/incognito) |
| session_id | UUID | FK → chat_session.session_id, NULLABLE | Session the file was uploaded in (nullable for project-level uploads) |
| file_hash | VARCHAR(64) | NOT NULL | SHA-256 of file content — used for deduplication |
| file_name | VARCHAR(512) | NOT NULL | Original file name (shown in UI) |
| file_type | VARCHAR(16) | NOT NULL | File extension: `pdf`, `md`, `txt`, `docx` |
| process_status | VARCHAR(32) | NOT NULL, DEFAULT 'pending' | Processing state (see below) |
| process_at | TIMESTAMPTZ | NULLABLE | Timestamp when processing completed or failed |
| file_dir | VARCHAR(1024) | NOT NULL | Full MinIO object path (e.g., `finhouse-files/user_3/project_7/abc_report.pdf`) |

**`process_status` values:**

| Value | Description |
|---|---|
| `pending` | File stored in MinIO, ingest task enqueued |
| `processing` | Background worker is chunking, embedding, and indexing |
| `ready` | File fully indexed in Milvus; available for RAG retrieval |
| `failed` | Processing failed; see associated `error` chat_event for details |
| `deleted` | Soft-deleted; physical file and Milvus vectors removed by cron or user action |

**MinIO path conventions:**
- Normal file: `finhouse-files/user_{user_id}/project_{project_id}/{file_id}_{file_name}`
- Incognito file: `finhouse-files/incognito/{session_id}/{file_id}_{file_name}`

**Deduplication logic:** Before uploading, FastAPI computes the SHA-256 hash. If a row with the same `file_hash` and `project_id` already has `process_status = 'ready'`, the upload is skipped and the existing `file_id` is reused — no re-ingest needed.

---

## Entity Relationships

```
user (user_id)
  │
  ├──< project (user_id FK)
  │      │
  │      ├──< chat_session (project_id FK)
  │      │        │
  │      │        └──< chat_event (session_id FK)
  │      │
  │      └──< file (project_id FK)
  │               │
  │               └── (session_id FK → chat_session, nullable)
  │
  └──< file (user_id FK)
```

---

## Special State Handling

### Scenario 1 — Unauthenticated / Guest User

- All activity is attributed to **user_id = 0**.
- A new chat creates a `project` row with **project_id = negative integer** owned by user_id = 0.
- A `chat_session` is created under that negative `project_id`.
- No history is surfaced in the UI; sidebar history panel is hidden.
- Cron job deletes all records (events → files → sessions → projects) after the cleanup window.
- MinIO files under `incognito/{session_id}/` are also deleted.

### Scenario 2 — Authenticated User, Incognito Mode

- The real user is authenticated, but the session is attributed to **user_id = 0**.
- Behavior is identical to Scenario 1. No data is persisted under the real user's account.

### Scenario 3 — Authenticated User, No Project Selected

- Sessions are placed under **project_id = 0** (pre-seeded default inbox project).
- History is fully persisted, visible, and searchable.
- User can later move sessions into a named project.

### Scenario 4 — Authenticated User, Named Project

- User creates a project → positive `project_id` assigned.
- All sessions, files, and events are associated with that project.
- Full history, file management, search, and context compression available.

---

## Cron Jobs

### 1. Ephemeral Data Cleanup (hourly or configurable)

Targets all records with `project_id < 0`. Cascade order to respect FK constraints:

1. Collect `session_id`s from `chat_session WHERE project_id < 0`.
2. Delete `chat_event WHERE session_id IN (...)`.
3. Collect `file_dir` paths from `file WHERE project_id < 0`.
4. Delete `file WHERE project_id < 0`.
5. Delete `chat_session WHERE project_id < 0`.
6. Delete `project WHERE project_id < 0`.
7. Delete MinIO objects at collected `file_dir` paths.
8. Purge entire `finhouse-files/incognito/` prefix from MinIO for orphaned objects.

### 2. Summary Generation (event-driven, per session)

Triggered by Background Worker when `turn_count % 3 == 0`:
- Fetch last 6 `message` events for the session.
- Call Ollama LLM to produce a concise summary.
- Append 1 `summary` event to `chat_event`.
- Increment `summary_count` on `chat_session`.

### 3. Checkpoint Generation (event-driven, per session)

Triggered by Background Worker when `summary_count % 3 == 0` (after a new summary is written):
- Fetch last 3 `summary` events + latest `checkpoint` event.
- Call Ollama LLM to produce an updated checkpoint.
- Append 1 `checkpoint` event to `chat_event`.
- Update `chat_session.session_title` with a title derived from the checkpoint.

---

## Indexing Recommendations

| Table | Index | Purpose |
|---|---|---|
| `chat_session` | `(project_id, update_at DESC)` | History list sorted by recency |
| `chat_session` | `(project_id)` WHERE `project_id < 0` | Cron cleanup scan |
| `chat_event` | `(session_id, num_order ASC)` | Ordered event retrieval |
| `chat_event` | `(session_id, event_type)` | Fast lookup of summaries / checkpoints |
| `file` | `(project_id, process_status)` | Filter by project and status |
| `file` | `(file_hash, project_id)` | Deduplication check |
| `project` | `(user_id, update_at DESC)` | User's project listing |