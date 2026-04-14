# FinHouse — System Flows

This document describes the key operational flows of the FinHouse platform using Mermaid diagrams.

---

## 1. Overall System Architecture

```mermaid
graph TB
    subgraph Presentation
        User([User]) --> StreamlitUI[Streamlit UI<br/>:8501]
    end

    subgraph Application
        StreamlitUI -->|HTTP / SSE| FastAPI[FastAPI Backend<br/>:8000]
        FastAPI --> BGW[Background Worker<br/>summary / checkpoint / ingest]
    end

    subgraph Storage["Storage Tier"]
        PostgreSQL[(PostgreSQL<br/>:5432)]
        MinIO[(MinIO<br/>:9000)]
        Milvus[(Milvus VectorDB<br/>:19530)]
    end

    subgraph AI["AI / Intelligence Tier"]
        BGEM3[BGE-M3<br/>Embedding]
        Reranker[BGE-M3<br/>Reranker]
        Ollama[Ollama LLM<br/>:11434]
    end

    subgraph Tools["LangChain / LangGraph Tools"]
        WebSearch[Web Search<br/>SearXNG]
        DBQuery[Database Query<br/>SQL Generation]
        Visualize[Visualize<br/>Chart / Graph via Python]
    end

    FastAPI -->|state & history| PostgreSQL
    FastAPI -->|file upload| MinIO
    BGW -->|chunking & embedding| BGEM3
    BGW -->|store vectors| Milvus
    FastAPI -->|retrieve top-K| Milvus
    FastAPI -->|rerank candidates| Reranker
    FastAPI -->|LLM inference| Ollama
    Ollama -->|tool calls| Tools
    DBQuery -->|query results| Visualize

    classDef storage fill:#e8f4fd,stroke:#4a90d9
    classDef ai fill:#fff3e0,stroke:#f5a623
    classDef tool fill:#e8f5e9,stroke:#4caf50
    class PostgreSQL,MinIO,Milvus storage
    class BGEM3,Reranker,Ollama ai
    class WebSearch,DBQuery,Visualize tool
```

---

## 2. Chat Message Flow (with RAG & Tools)

This is the main flow when a user sends a message.

```mermaid
sequenceDiagram
    actor User
    participant UI as Streamlit UI
    participant API as FastAPI
    participant PG as PostgreSQL
    participant MV as Milvus
    participant RR as BGE-M3 Reranker
    participant LG as LangGraph Agent
    participant LLM as Ollama LLM
    participant Tool as Tool Executor

    User->>UI: Type message & send
    UI->>API: POST /chat/send (session_id, message)
    API->>PG: Insert chat_event (role=user, event_type=message)
    API->>PG: Increment turn_count on chat_session

    Note over API: Prompt Assembly
    API->>PG: Fetch latest checkpoint event
    API->>PG: Fetch recent summary events (up to 3)
    API->>PG: Fetch last 6 message events

    Note over API: RAG Retrieval
    API->>MV: Query top-K chunks (user query embedding)
    MV-->>API: Candidate chunks
    API->>RR: Rerank candidates
    RR-->>API: Reranked top-N chunks
    API->>PG: Insert chat_event (event_type=rag_context)

    Note over API: Assembled prompt sent to agent
    API->>LG: Invoke agent (prompt + tool definitions)
    LG->>LLM: Generate response

    alt LLM decides to use a tool
        LLM-->>LG: tool_call (name, args)
        LG->>API: Log tool_call event
        API->>PG: Insert chat_event (event_type=tool_call)
        LG->>Tool: Execute tool
        Tool-->>LG: Tool result
        LG->>API: Log tool_result event
        API->>PG: Insert chat_event (event_type=tool_result)
        LG->>LLM: Continue with tool result
    end

    LLM-->>LG: Final response tokens (streaming)
    LG-->>API: Stream tokens
    API-->>UI: SSE stream tokens
    UI-->>User: Render streaming response

    API->>PG: Insert chat_event (role=assistant, event_type=message)

    Note over API: Check if background jobs needed
    alt turn_count % 3 == 0
        API->>API: Trigger summary generation
    end
```

---

## 3. File Upload & Ingest Pipeline

```mermaid
sequenceDiagram
    actor User
    participant UI as Streamlit UI
    participant API as FastAPI
    participant PG as PostgreSQL
    participant MIO as MinIO
    participant BGW as Background Worker
    participant EMB as BGE-M3 Embedding
    participant MV as Milvus

    User->>UI: Upload file (PDF/MD/TXT/DOCX)
    UI->>API: POST /files/upload (file, project_id, session_id)

    Note over API: Deduplication Check
    API->>API: Compute SHA-256 hash
    API->>PG: Check file_hash + project_id
    alt Duplicate found (status=ready)
        API-->>UI: Return existing file_id (skip upload)
    else New file
        API->>MIO: Store file object
        API->>PG: Insert file row (status=pending)
        API-->>UI: Return file_id + status=pending

        API->>BGW: Enqueue ingest job (file_id)
        BGW->>PG: Update status → processing
        BGW->>MIO: Download file
        BGW->>BGW: Parse & chunk document
        BGW->>EMB: Generate embeddings for chunks
        EMB-->>BGW: Chunk embeddings
        BGW->>MV: Upsert vectors (partitioned by project_id)
        BGW->>PG: Update status → ready
        BGW-->>UI: Notify status change (via polling or WebSocket)
    end
```

---

## 4. Context Compression (Summary & Checkpoint)

```mermaid
flowchart TD
    A[New turn completed] --> B{turn_count % 3 == 0?}
    B -->|No| Z[Done — no compression needed]
    B -->|Yes| C[Fetch last 6 message events]
    C --> D[Call Ollama LLM to generate summary]
    D --> E[Append summary event to chat_event]
    E --> F[Increment summary_count]
    F --> G{summary_count % 3 == 0?}
    G -->|No| Z
    G -->|Yes| H[Fetch last 3 summaries + latest checkpoint]
    H --> I[Call Ollama LLM to generate checkpoint]
    I --> J[Append checkpoint event to chat_event]
    J --> K[Update session_title from checkpoint]
    K --> Z
```

**Prompt window at inference time:**

```mermaid
graph LR
    subgraph Prompt["Assembled LLM Prompt"]
        CP[1. Latest Checkpoint] --> SM[2. Recent Summaries<br/>up to 3]
        SM --> MSG[3. Last 6 Messages<br/>3 raw turns]
        MSG --> RAG[4. RAG Context<br/>retrieved chunks]
        RAG --> UQ[5. Current User Query]
    end
```

---

## 5. Visualization Tool Flow (Chart/Graph Generation)

When a user asks about trends or data that benefits from a visual:

```mermaid
sequenceDiagram
    actor User
    participant LG as LangGraph Agent
    participant LLM as Ollama LLM
    participant DB as Database Query Tool
    participant VIZ as Visualize Tool
    participant SANDBOX as Python Sandbox
    participant MIO as MinIO

    User->>LG: "Show me revenue trends for Q1-Q4"
    LG->>LLM: Process query with tool definitions
    LLM-->>LG: tool_call → database_query("SELECT quarter, revenue FROM sales")
    LG->>DB: Execute SQL
    DB-->>LG: Tabular result (DataFrame)

    LLM-->>LG: tool_call → visualize("line chart of revenue by quarter")
    LG->>VIZ: Generate chart request
    VIZ->>LLM: "Write Python code using matplotlib/plotly to create this chart"
    LLM-->>VIZ: Python code snippet

    VIZ->>SANDBOX: Execute Python code with DataFrame
    SANDBOX-->>VIZ: Output image (PNG) or interactive HTML
    VIZ->>MIO: Save output file
    VIZ-->>LG: Return image URL / inline content

    LG-->>User: Final response with embedded chart
```

**Supported Python libraries for visualization:**
- `matplotlib` + `seaborn` — static charts (bar, line, scatter, heatmap, histogram)
- `plotly` — interactive charts rendered as HTML widgets in the Streamlit UI

---

## 6. Guest / Incognito Session Lifecycle

```mermaid
stateDiagram-v2
    [*] --> SessionCreated: User starts chat\n(guest or incognito toggle)

    SessionCreated --> ActiveChat: Assign user_id=0\nnegative project_id
    ActiveChat --> ActiveChat: Send messages\nupload files\nuse tools

    ActiveChat --> Abandoned: User closes tab\nor session idle

    Abandoned --> CronCleanup: Hourly cron job fires

    state CronCleanup {
        [*] --> DeleteEvents: DELETE chat_event
        DeleteEvents --> DeleteFiles: DELETE file rows
        DeleteFiles --> DeleteSessions: DELETE chat_session
        DeleteSessions --> DeleteProjects: DELETE project
        DeleteProjects --> PurgeMinIO: Remove MinIO objects\nunder incognito/
        PurgeMinIO --> PurgeMilvus: Remove vectors\nfor negative project_ids
    }

    CronCleanup --> [*]: All ephemeral data removed
```

---

## 7. Authentication Flow

```mermaid
sequenceDiagram
    actor User
    participant UI as Streamlit UI
    participant API as FastAPI
    participant PG as PostgreSQL

    alt New User Registration
        User->>UI: Fill register form
        UI->>API: POST /auth/register (username, password)
        API->>API: Hash password (bcrypt)
        API->>PG: INSERT user row
        API-->>UI: Success → redirect to login
    end

    alt Login
        User->>UI: Enter credentials
        UI->>API: POST /auth/login (username, password)
        API->>PG: Fetch user by username
        API->>API: Verify bcrypt hash
        API->>API: Generate JWT (access + refresh)
        API-->>UI: Return tokens
        UI->>UI: Store tokens in session_state
    end

    alt Guest Access
        User->>UI: Use without logging in
        UI->>UI: Set user_id = 0 in session
        Note over UI: No JWT issued\nHistory hidden\nAll data ephemeral
    end
```

---

## 8. Docker Container Dependency Graph

```mermaid
graph BT
    etcd[etcd] --> Milvus[Milvus]
    MIO_internal[MinIO<br/>Milvus internal] --> Milvus
    PostgreSQL --> API[FastAPI]
    MinIO --> API
    Milvus --> API
    Ollama --> API
    BGEM3[BGE-M3] --> API
    Reranker[BGE-M3 Reranker] --> API
    API --> UI[Streamlit UI]

    classDef gpu fill:#ffe0e0,stroke:#d32f2f
    class Ollama gpu
```

---

## Summary of Key Flows

| Flow | Trigger | Key Components |
|------|---------|----------------|
| Chat message | User sends message | UI → API → PG → Milvus → Reranker → LangGraph → Ollama → SSE |
| File upload | User uploads document | UI → API → MinIO → Background Worker → BGE-M3 → Milvus |
| Summary generation | Every 3 turns | Background Worker → PG → Ollama → PG |
| Checkpoint generation | Every 3 summaries | Background Worker → PG → Ollama → PG |
| Visualization | User asks for chart/trend | LangGraph → DB Query → Ollama (code gen) → Python Sandbox → MinIO |
| Ephemeral cleanup | Hourly cron | Cron → PG (cascade delete) → MinIO → Milvus |