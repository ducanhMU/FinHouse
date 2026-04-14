# FinHouse — Chat Interface UI Specification

## Overview

FinHouse is a RAG-based AI chat system with a UI modeled after Claude.ai. It is built as a **Streamlit** application (Python) communicating with a FastAPI backend. All LLM inference runs locally via Ollama — no external API keys required. The interface supports multi-model selection, file uploads, tool toggles, incognito mode, and project-based chat organization. Behavior adapts based on whether the user is authenticated or operating as the default guest (user_id = 0).

**Note on framework choice:** Streamlit was selected for rapid prototyping, native Python integration, and built-in streaming support. The sidebar uses `st.sidebar`, the main area uses `st.container`, and interactive state is managed through `st.session_state`. Custom CSS is injected via `st.markdown` for styling (bubbles, banners, tooltips).

---

## Layout Structure

The interface has two primary regions:

**Sidebar** (collapsible, left panel) — branding, project/session navigation, chat history.

**Main Area** (right panel) — model selector, incognito toggle, chat viewport, input toolbar.

---

## Sidebar

### Header

- **Brand name**: "FinHouse" at the top left.
- **Sidebar toggle button**: Icon button to the right of the brand name. Collapses the sidebar to a narrow icon-only rail; clicking again expands it. State is persisted in localStorage.

### Top Actions

Two icon buttons stacked vertically below the header:

- **New Chat** — creates a new `chat_session`. For authenticated users, it is placed under the currently active project (or project_id = 0 if none selected). For guests (user_id = 0), a temporary session with a negative `project_id` is created and never added to history.
- **Search Chats by Date/Time** — opens a date-time range picker to filter the history list. **Hidden entirely for user_id = 0.**

### Project List (authenticated users only)

A compact list of the user's named projects above the session history. Clicking a project filters the history list to sessions belonging to it. Includes a "+ New Project" button.

### Chat History List

A scrollable list of past sessions grouped by recency (Today, Yesterday, Last 7 days, Older). Each row shows:
- Session title (from `chat_session.session_title`, auto-set by the checkpoint background job).
- Relative timestamp.
- On hover: rename and delete icons.

If the list overflows, it scrolls with a subtle "Load more" link at the bottom.

**Guest users (user_id = 0)**: The entire history section is **not rendered**. A sign-in call-to-action is shown in its place.

### Footer

Fixed at the bottom of the sidebar, always visible:

- **User Settings** — opens a drawer for profile, password, preferences, and default model selection.
- **API Docs** — links to FastAPI's `/docs` (Swagger UI).
- **Health Check** — a small status dot showing backend reachability (`/health`). Green = healthy, red = unreachable.

---

## Main Area

### Top Bar

A horizontal strip spanning the full main area width:

**Model Selector** (left side): Dropdown showing the currently active Ollama model tag (e.g., `qwen2.5:14b`, `llama3.1:8b`). Clicking opens a scrollable list of models fetched from the backend at startup. The selected model is stored in `chat_session.model_used` at session creation and cannot be changed mid-session (a warning is shown if the user attempts this). Models that support tool-calling are visually marked (e.g., a wrench icon).

**Incognito Mode Toggle** (right side): A ghost/mask icon button. When active:
- Session is attributed to user_id = 0 regardless of login state.
- Session receives a negative `project_id`.
- A persistent banner appears below the top bar: *"Incognito — this conversation will not be saved."* with a distinct background (dark slate or muted indigo).
- History panel collapses and is replaced with the sign-in prompt.
- New chat button creates another incognito session without confirmation.

### Chat Viewport

The central scrollable content area. Message rendering:

- **User messages**: right-aligned bubble, plain text.
- **Assistant messages**: left-aligned, with a small Ollama/model icon. Supports full Markdown rendering — code blocks with syntax highlighting, tables, ordered/unordered lists, inline math.
- **RAG citations**: retrieved source chunks are shown as numbered footnote references inline in the assistant response (e.g., `[1]`). A collapsible "Sources" panel below each response lists the cited file names and chunk previews.
- **Tool activity**: when the agent calls a tool (web search, OLAP query, visualize), a collapsible "Tool use" row appears between the user message and the final response, showing the tool name, input arguments, and output.
- **Visualization output**: when the `visualize` tool produces a chart, the output is rendered inline — PNG images via `st.image()` and interactive Plotly charts via `st.plotly_chart()`. A "View code" expander shows the generated Python code for transparency.
- **Streaming**: assistant responses stream token-by-token via SSE. A blinking cursor indicates active streaming.
- **Auto-scroll**: viewport follows the latest token. A "↓ scroll to bottom" button appears if the user scrolls up during streaming.
- **Empty state**: welcome screen with the FinHouse logo and 3–4 suggested prompt cards (e.g., "Summarize a document", "Query the database", "Search the web").

### Input Toolbar

Fixed at the bottom of the main area.

**Feature Button (`+` icon, left)**: Opens an expandable popover with two sections:

*Upload Files:*
- Drag-and-drop or file picker. Accepted types: PDF, Markdown (.md), TXT, DOCX.
- Uploaded files appear as chips above the text input with a processing status indicator (pending → processing → ready).
- Files are associated with the current session. Incognito file chips show a 🕵 indicator.

*Tools:*
- Toggle switches for tools the agent can use during this session. Stored in `chat_session.tools_used`.
  - **Web Search** — queries SearXNG or DuckDuckGo.
  - **Database Query** — generates and runs SQL against the OLAP database; results shown as a table in the chat.
  - **Visualize (Chart/Graph)** — when enabled alongside Database Query, the agent can generate Python code (matplotlib, plotly, seaborn) to produce charts and graphs from query results. The code runs in a sandboxed Python environment and the output is rendered inline as an image or interactive widget.
- Tool toggles are enabled only for models marked as tool-capable.

**Text Input** (center): Auto-resizing multiline textarea.
- `Enter` to send; `Shift+Enter` for newlines.
- Supports paste of plain text.
- Placeholder:
  - Normal: *"Ask FinHouse..."*
  - Incognito: *"Incognito — not saved. Ask anything..."*
- Disabled with a spinner while the assistant is responding.

**Send / Stop Button** (right):
- **Send** (arrow icon): disabled when input is empty or files are still processing.
- Switches to **Stop** (square icon) while the assistant is streaming. Clicking stop cancels the SSE stream and saves a partial `message` event with a `cancelled` note appended.

---

## Guest User (user_id = 0) Behavior Summary

| Feature | Authenticated User | Guest / Incognito (user_id = 0) |
|---|---|---|
| Chat history list | Yes, grouped by project | **Not rendered** (sign-in prompt shown) |
| Project list | Yes | **Not rendered** |
| Sessions persisted | Yes | **No** (temp negative project_id, deleted by cron) |
| New Chat button | Yes | Yes (creates temp incognito session) |
| Search chats | Yes | **Hidden** |
| File uploads | Yes (persisted) | Yes (temp, stored in incognito MinIO bucket, deleted by cron) |
| Model selector | Yes | Yes |
| Tool toggles | Yes | Yes |
| RAG citations | Yes | Yes (from incognito-scoped vectors, deleted by cron) |
| Context compression (summary/checkpoint) | Yes | **No** (sessions too short-lived to warrant it) |
| User settings | Yes | Shows sign-in / register prompt |
| Incognito banner | N/A | Shown prominently below top bar |
| Session title (auto-updated) | Yes (via checkpoint job) | **No** |

---

## Prompt Context Indicator

For authenticated users, a subtle context status badge is shown near the session title in the top bar:

- **No compression yet** (< 3 turns): "Full history"
- **Summaries active** (≥ 3 turns): "Summarized context"
- **Checkpoint active** (≥ 3 summaries): "Checkpoint + summaries"

Hovering the badge shows a tooltip explaining that older messages are compressed to stay within the model's context window, and that detail may be lost for very early messages.

---

## Normal Authenticated User Flow

1. Register / log in → receive JWT → real user_id assigned.
2. Optionally create a **Project** (named workspace) from the sidebar.
3. Click **New Chat** → session created under the active project (or project_id = 0).
4. Select an Ollama model from the model selector.
5. Optionally upload files (PDF, MD, TXT, DOCX) and enable tools.
6. Chat — responses stream in real time with RAG citations and tool activity visible.
7. After every 3 turns, a summary is silently generated in the background.
8. After every 3 summaries, a checkpoint is generated and the session title is updated automatically.
9. All history is browsable and searchable from the sidebar.

---

## Responsive & Accessibility Notes

- Sidebar collapses to an icon rail on screens < 768px wide.
- All icon buttons have `aria-label` and visible tooltips.
- Model selector is keyboard-navigable (arrow keys, Enter to select, Escape to close).
- Chat viewport is screen-reader-friendly: each message has `role="article"` and a visually hidden label indicating role (User / Assistant).
- Color contrast meets WCAG AA for both light and dark themes.
- Dark mode is supported; incognito banner uses a distinct hue (muted indigo / dark slate) that is visually unambiguous in both themes.

---

## Component Summary

| Component | Location | Notes |
|---|---|---|
| Sidebar toggle | Sidebar header | Collapses to icon rail; state in localStorage |
| New Chat button | Sidebar top | Temp negative project_id if guest/incognito |
| Search by date/time | Sidebar top | Hidden for user_id = 0 |
| Project list | Sidebar body (top) | Authenticated only |
| Chat history list | Sidebar body | Hidden for user_id = 0 |
| User settings / API Docs / Health | Sidebar footer | Always visible |
| Model selector | Main top bar, left | Fetched from backend; locked mid-session |
| Incognito toggle + banner | Main top bar, right + below bar | Triggers guest-mode behavior |
| Context status badge | Main top bar, near title | Shows compression state (summary/checkpoint) |
| Chat viewport | Main center | Streaming, Markdown, RAG citations, tool traces |
| Feature button (+) | Input toolbar, left | File upload + tool toggles |
| File chips | Above text input | Show per-file process_status |
| Text input | Input toolbar, center | Auto-resize, Enter to send |
| Send / Stop button | Input toolbar, right | Stop cancels stream mid-response |