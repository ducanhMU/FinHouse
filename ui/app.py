"""
FinHouse — Streamlit Chat UI
RAG-based AI Chat Platform with Ollama
"""

import json
import time
from datetime import datetime, timedelta

import streamlit as st
import api_client as api

# ════════════════════════════════════════════════════════════
# Page Config
# ════════════════════════════════════════════════════════════

st.set_page_config(
    page_title="FinHouse",
    page_icon="🏠",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ════════════════════════════════════════════════════════════
# Custom CSS
# ════════════════════════════════════════════════════════════

st.markdown("""
<style>
    /* Global */
    .stApp { background-color: var(--background-color); }

    /* Sidebar brand */
    .brand-title {
        font-size: 1.5rem;
        font-weight: 700;
        letter-spacing: -0.02em;
        margin-bottom: 0.25rem;
    }
    .brand-sub {
        font-size: 0.75rem;
        opacity: 0.6;
        margin-bottom: 1rem;
    }

    /* Session list items */
    .session-item {
        padding: 0.5rem 0.75rem;
        border-radius: 8px;
        margin-bottom: 2px;
        cursor: pointer;
        transition: background 0.15s;
        font-size: 0.875rem;
        line-height: 1.3;
    }
    .session-item:hover { background: rgba(128,128,128,0.1); }
    .session-active { background: rgba(99,102,241,0.15) !important; }
    .session-time {
        font-size: 0.7rem;
        opacity: 0.5;
        margin-top: 2px;
    }

    /* Incognito banner */
    .incognito-banner {
        background: linear-gradient(135deg, #312e81, #4338ca);
        color: #e0e7ff;
        padding: 0.6rem 1rem;
        border-radius: 8px;
        margin-bottom: 1rem;
        font-size: 0.85rem;
        display: flex;
        align-items: center;
        gap: 0.5rem;
    }

    /* Tool activity cards */
    .tool-card {
        background: rgba(128,128,128,0.06);
        border: 1px solid rgba(128,128,128,0.12);
        border-radius: 8px;
        padding: 0.75rem 1rem;
        margin: 0.5rem 0;
        font-size: 0.825rem;
    }
    .tool-card-header {
        font-weight: 600;
        margin-bottom: 0.3rem;
        display: flex;
        align-items: center;
        gap: 0.4rem;
    }

    /* Welcome cards */
    .welcome-card {
        background: rgba(128,128,128,0.05);
        border: 1px solid rgba(128,128,128,0.1);
        border-radius: 12px;
        padding: 1.25rem;
        text-align: center;
        cursor: pointer;
        transition: all 0.2s;
    }
    .welcome-card:hover {
        background: rgba(99,102,241,0.08);
        border-color: rgba(99,102,241,0.3);
    }

    /* Health dot */
    .health-dot {
        display: inline-block;
        width: 8px; height: 8px;
        border-radius: 50%;
        margin-right: 6px;
    }
    .health-ok { background: #22c55e; }
    .health-err { background: #ef4444; }

    /* Context badge */
    .ctx-badge {
        font-size: 0.7rem;
        padding: 2px 8px;
        border-radius: 10px;
        background: rgba(128,128,128,0.1);
        display: inline-block;
    }

    /* Hide Streamlit defaults */
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    .stDeployButton {display: none;}
</style>
""", unsafe_allow_html=True)


# ════════════════════════════════════════════════════════════
# Session State Init
# ════════════════════════════════════════════════════════════

DEFAULTS = {
    "authenticated": False,
    "user_id": 0,
    "user_name": "guest",
    "access_token": None,
    "refresh_token": None,
    "incognito": False,
    "current_session_id": None,
    "current_project_id": None,
    "messages": [],           # [{role, content, tool_events?}]
    "models": [],
    "selected_model": None,
    "tools_enabled": ["web_search"],   # web_search bật mặc định
    "is_streaming": False,
    "show_auth": "login",     # "login" or "register"
    "session_meta": None,     # current session metadata
    "view": "chat",           # "chat" | "files"
}
for k, v in DEFAULTS.items():
    if k not in st.session_state:
        st.session_state[k] = v

# Fetch models once
if not st.session_state.models:
    st.session_state.models = api.get_models()
    if st.session_state.models and not st.session_state.selected_model:
        st.session_state.selected_model = st.session_state.models[0]["name"]


def is_guest() -> bool:
    return st.session_state.user_id == 0 or st.session_state.incognito


def get_token():
    if st.session_state.incognito:
        return None
    return st.session_state.access_token


# ════════════════════════════════════════════════════════════
# Helper: Time grouping
# ════════════════════════════════════════════════════════════

def group_sessions_by_time(sessions: list[dict]) -> dict[str, list]:
    groups = {"Today": [], "Yesterday": [], "Last 7 days": [], "Older": []}
    now = datetime.utcnow()
    today = now.date()
    yesterday = today - timedelta(days=1)
    week_ago = today - timedelta(days=7)

    for s in sessions:
        try:
            dt = datetime.fromisoformat(s["update_at"].replace("Z", "+00:00"))
            d = dt.date()
        except Exception:
            d = today

        if d == today:
            groups["Today"].append(s)
        elif d == yesterday:
            groups["Yesterday"].append(s)
        elif d >= week_ago:
            groups["Last 7 days"].append(s)
        else:
            groups["Older"].append(s)
    return groups


# ════════════════════════════════════════════════════════════
# Helper: Load session messages from events
# ════════════════════════════════════════════════════════════

def load_session_events(session_id: str):
    """Load all events for a session and populate st.session_state.messages."""
    events = api.get_events(session_id)
    messages = []
    current_tool_events = []
    current_rag_sources = []

    for ev in events:
        if ev["event_type"] == "message":
            msg = {"role": ev["role"], "content": ev["text"]}
            if current_tool_events and ev["role"] == "assistant":
                msg["tool_events"] = current_tool_events
                current_tool_events = []
            if current_rag_sources and ev["role"] == "assistant":
                msg["rag_sources"] = current_rag_sources
                current_rag_sources = []
            messages.append(msg)
        elif ev["event_type"] == "tool_call":
            try:
                data = json.loads(ev["text"])
                current_tool_events.append({"type": "tool_call", **data})
            except Exception:
                current_tool_events.append({"type": "tool_call", "raw": ev["text"]})
        elif ev["event_type"] == "tool_result":
            current_tool_events.append({"type": "tool_result", "content": ev["text"]})
        elif ev["event_type"] == "rag_context":
            try:
                current_rag_sources = json.loads(ev["text"])
            except Exception:
                current_rag_sources = []

    st.session_state.messages = messages
    st.session_state.current_session_id = session_id
    # Load session meta
    try:
        meta = api.get_session(session_id)
        st.session_state.session_meta = meta
        st.session_state.selected_model = meta.get("model_used", st.session_state.selected_model)
        st.session_state.tools_enabled = meta.get("tools_used") or []
    except Exception:
        pass


# ════════════════════════════════════════════════════════════
# Helper: Create new session
# ════════════════════════════════════════════════════════════

def create_new_session():
    token = get_token()
    project_id = st.session_state.current_project_id
    if is_guest():
        project_id = None

    session = api.create_session(
        token=token,
        model_used=st.session_state.selected_model or "qwen2.5:14b",
        project_id=project_id,
        tools_used=st.session_state.tools_enabled or None,
    )
    st.session_state.current_session_id = session["session_id"]
    st.session_state.messages = []
    st.session_state.session_meta = session
    st.session_state.is_streaming = False


# ════════════════════════════════════════════════════════════
# SIDEBAR
# ════════════════════════════════════════════════════════════

with st.sidebar:
    # Brand
    st.markdown('<div class="brand-title">🏠 FinHouse</div>', unsafe_allow_html=True)
    st.markdown('<div class="brand-sub">RAG-based AI Chat · Powered by Ollama</div>', unsafe_allow_html=True)

    st.divider()

    # ── Auth section ────────────────────────────────────────
    if not st.session_state.authenticated and not st.session_state.incognito:
        tab_login, tab_register = st.tabs(["Sign In", "Register"])

        with tab_login:
            with st.form("login_form"):
                lu = st.text_input("Username", key="login_user")
                lp = st.text_input("Password", type="password", key="login_pass")
                submitted = st.form_submit_button("Sign In", use_container_width=True)
                if submitted and lu and lp:
                    try:
                        data = api.login(lu, lp)
                        st.session_state.authenticated = True
                        st.session_state.user_id = data["user_id"]
                        st.session_state.user_name = data["user_name"]
                        st.session_state.access_token = data["access_token"]
                        st.session_state.refresh_token = data["refresh_token"]
                        st.rerun()
                    except api.APIError as e:
                        if e.status_code == 401:
                            st.error("Sai tên đăng nhập hoặc mật khẩu")
                        else:
                            st.error(f"Lỗi đăng nhập: {e.detail}")
                    except Exception as e:
                        st.error(f"Không kết nối được máy chủ: {e}")

        with tab_register:
            with st.form("register_form"):
                ru = st.text_input("Username", key="reg_user")
                rp = st.text_input("Password", type="password", key="reg_pass")
                rp2 = st.text_input("Confirm Password", type="password", key="reg_pass2")
                submitted = st.form_submit_button("Create Account", use_container_width=True)
                if submitted:
                    if not ru or not rp:
                        st.error("Vui lòng nhập đầy đủ thông tin")
                    elif rp != rp2:
                        st.error("Mật khẩu xác nhận không khớp")
                    elif len(ru) < 3:
                        st.error("Username phải có ít nhất 3 ký tự")
                    elif len(rp) < 8:
                        st.error("Mật khẩu phải có ít nhất 8 ký tự")
                    else:
                        try:
                            api.register(ru, rp)
                            st.success("Tạo tài khoản thành công! Vui lòng đăng nhập.")
                        except api.APIError as e:
                            if e.status_code == 409:
                                st.error("Username đã tồn tại, vui lòng chọn tên khác")
                            elif e.status_code == 400:
                                st.error(f"Dữ liệu không hợp lệ: {e.detail}")
                            else:
                                st.error(f"Lỗi đăng ký ({e.status_code}): {e.detail}")
                        except Exception as e:
                            st.error(f"Không kết nối được máy chủ: {e}")

        st.divider()
        st.caption("Or continue as guest — no history saved")

    # ── New Chat button ─────────────────────────────────────
    if st.button("➕ New Chat", use_container_width=True, type="primary"):
        create_new_session()
        st.session_state.view = "chat"
        st.rerun()

    # ── View switcher (Chat / Files) ────────────────────────
    nav_col1, nav_col2 = st.columns(2)
    with nav_col1:
        chat_clicked = st.button(
            "💬 Chat",
            use_container_width=True,
            type="secondary" if st.session_state.view == "files" else "primary",
            key="nav_chat",
        )
    with nav_col2:
        files_clicked = st.button(
            "📁 Files",
            use_container_width=True,
            type="secondary" if st.session_state.view == "chat" else "primary",
            key="nav_files",
        )

    if chat_clicked and st.session_state.view != "chat":
        st.session_state.view = "chat"
        st.rerun()
    if files_clicked and st.session_state.view != "files":
        st.session_state.view = "files"
        st.rerun()

    # ── Projects & History (authenticated only) ─────────────
    if st.session_state.authenticated and not st.session_state.incognito:
        st.divider()

        # Project selector
        try:
            projects = api.list_projects(get_token())
        except Exception:
            projects = []

        project_names = ["All Projects"] + [p["project_title"] for p in projects]
        project_ids = [None] + [p["project_id"] for p in projects]

        col_proj, col_new = st.columns([3, 1])
        with col_proj:
            sel_idx = st.selectbox(
                "Project",
                range(len(project_names)),
                format_func=lambda i: project_names[i],
                key="project_selector",
                label_visibility="collapsed",
            )
            st.session_state.current_project_id = project_ids[sel_idx]
        with col_new:
            if st.button("➕", help="New Project", key="new_proj_btn"):
                st.session_state["_show_new_project"] = True

        if st.session_state.get("_show_new_project"):
            with st.form("new_project_form"):
                pt = st.text_input("Project name")
                if st.form_submit_button("Create"):
                    if pt:
                        try:
                            api.create_project(get_token(), pt)
                            st.session_state["_show_new_project"] = False
                            st.rerun()
                        except Exception:
                            st.error("Failed to create project")

        # Session history
        st.markdown("##### Chat History")
        try:
            sessions = api.list_sessions(get_token(), st.session_state.current_project_id)
        except Exception:
            sessions = []

        if sessions:
            grouped = group_sessions_by_time(sessions)
            for group_name, group_sessions in grouped.items():
                if not group_sessions:
                    continue
                st.caption(group_name)
                for s in group_sessions:
                    sid = s["session_id"]
                    title = s.get("session_title") or "Untitled chat"
                    is_active = sid == st.session_state.current_session_id
                    css = "session-item session-active" if is_active else "session-item"

                    col_title, col_del = st.columns([5, 1])
                    with col_title:
                        if st.button(
                            f"{'● ' if is_active else ''}{title[:40]}",
                            key=f"sess_{sid}",
                            use_container_width=True,
                        ):
                            load_session_events(sid)
                            st.rerun()
                    with col_del:
                        if st.button("🗑", key=f"del_{sid}", help="Delete"):
                            try:
                                api.delete_session(get_token(), sid)
                                if st.session_state.current_session_id == sid:
                                    st.session_state.current_session_id = None
                                    st.session_state.messages = []
                                st.rerun()
                            except Exception:
                                pass
        else:
            st.caption("No conversations yet")

    # ── Sidebar Footer ──────────────────────────────────────
    st.divider()

    # Health check
    try:
        h = api.health()
        all_ok = h.get("status") == "ok"
    except Exception:
        all_ok = False
        h = {"services": {}}

    dot = "health-ok" if all_ok else "health-err"
    services_summary = ", ".join(
        f"{k}: {'✓' if v == 'ok' else '✗'}" for k, v in h.get("services", {}).items()
    )
    st.markdown(
        f'<span class="health-dot {dot}"></span> '
        f'<small>{"All systems operational" if all_ok else "Some services degraded"}</small>',
        unsafe_allow_html=True,
    )
    if not all_ok:
        st.caption(services_summary)

    col_a, col_b = st.columns(2)
    with col_a:
        if st.button("📖 API Docs", use_container_width=True):
            st.markdown("[Open Swagger UI](http://localhost:8000/docs)")
    with col_b:
        if st.session_state.authenticated:
            if st.button("🚪 Logout", use_container_width=True):
                for k in DEFAULTS:
                    st.session_state[k] = DEFAULTS[k]
                st.rerun()


# ════════════════════════════════════════════════════════════
# MAIN AREA — Top Bar
# ════════════════════════════════════════════════════════════

top_col1, top_col2, top_col3 = st.columns([3, 4, 2])

with top_col1:
    # Model selector
    model_names = [m["name"] for m in st.session_state.models]
    if model_names:
        current_idx = 0
        if st.session_state.selected_model in model_names:
            current_idx = model_names.index(st.session_state.selected_model)

        has_messages = len(st.session_state.messages) > 0
        selected = st.selectbox(
            "Model",
            model_names,
            index=current_idx,
            disabled=has_messages,
            help="Model is locked after first message" if has_messages else "Select Ollama model",
            label_visibility="collapsed",
        )
        if selected != st.session_state.selected_model and not has_messages:
            st.session_state.selected_model = selected
    else:
        st.warning("No models available — pull models via Ollama")

with top_col2:
    # Context badge
    meta = st.session_state.session_meta
    if meta:
        tc = meta.get("turn_count", 0)
        sc = meta.get("summary_count", 0)
        title = meta.get("session_title", "")
        if sc > 0 and tc >= 9:
            badge = "🔵 Checkpoint + summaries"
        elif tc >= 3:
            badge = "🟡 Summarized context"
        else:
            badge = "🟢 Full history"
        display_title = title[:50] if title else "New conversation"
        st.markdown(
            f"**{display_title}** &nbsp; <span class='ctx-badge'>{badge}</span>",
            unsafe_allow_html=True,
        )

with top_col3:
    # Incognito toggle
    incog = st.toggle(
        "🕵️ Incognito",
        value=st.session_state.incognito,
        help="Session won't be saved",
    )
    if incog != st.session_state.incognito:
        st.session_state.incognito = incog
        st.session_state.current_session_id = None
        st.session_state.messages = []
        st.session_state.session_meta = None
        st.rerun()


# Incognito banner
if st.session_state.incognito:
    st.markdown(
        '<div class="incognito-banner">'
        '🕵️ <strong>Incognito mode</strong> — this conversation will not be saved.'
        '</div>',
        unsafe_allow_html=True,
    )


# ════════════════════════════════════════════════════════════
# Tool Toggles & File Upload (expandable)
# ════════════════════════════════════════════════════════════

# ════════════════════════════════════════════════════════════
# FILES PAGE — full-screen file manager (view = "files")
# ════════════════════════════════════════════════════════════

if st.session_state.view == "files":
    st.markdown("## 📁 Files & Documents")
    st.caption(
        "Tất cả file ở project 0 (Inbox) = kho kiến thức chung, mọi user "
        "truy cập được. File ở project khác là riêng của bạn."
    )

    # Upload area
    st.markdown("### Upload")
    uploaded = st.file_uploader(
        "Chọn file để upload",
        type=["pdf", "md", "txt", "docx", "csv", "json", "xlsx", "jpg", "png"],
        accept_multiple_files=True,
        help="Hỗ trợ RAG: PDF, MD, TXT, DOCX. Định dạng khác sẽ lưu nhưng không search được.",
    )

    if uploaded:
        up_project_id = st.session_state.current_project_id
        if up_project_id is None or is_guest():
            up_project_id = 0

        for f in uploaded:
            up_key = f"uploaded_{f.name}_{f.size}"
            if up_key not in st.session_state:
                try:
                    result = api.upload_file(
                        token=get_token(),
                        file_bytes=f.read(),
                        file_name=f.name,
                        project_id=up_project_id,
                        session_id=None,
                    )
                    st.session_state[up_key] = result
                    status = result.get("process_status", "unknown")
                    if status == "ready":
                        st.success(f"✅ **{f.name}** — đã có sẵn (trùng file)")
                    elif status == "failed":
                        st.warning(f"⚠️ **{f.name}** — định dạng không hỗ trợ")
                    else:
                        st.info(f"📄 **{f.name}** — đã upload, đang xử lý...")
                except Exception as e:
                    st.error(f"Upload thất bại — {f.name}: {e}")

    # File list with filter
    st.divider()
    st.markdown("### Danh sách file")

    filter_col1, filter_col2, filter_col3 = st.columns([2, 2, 1])
    with filter_col1:
        filter_scope = st.selectbox(
            "Phạm vi",
            ["Tất cả", "Kiến thức chung (base)", "Project hiện tại"],
            index=0,
        )
    with filter_col2:
        filter_status = st.selectbox(
            "Trạng thái",
            ["Tất cả", "ready", "processing", "pending", "failed"],
            index=0,
        )
    with filter_col3:
        if st.button("🔄 Reload", use_container_width=True):
            st.rerun()

    # Fetch files according to filter
    try:
        if filter_scope == "Kiến thức chung (base)":
            files_list = api.list_files(token=get_token(), project_id=0)
        elif filter_scope == "Project hiện tại":
            pid = st.session_state.current_project_id
            if pid is None:
                pid = 0
            files_list = api.list_files(token=get_token(), project_id=pid)
        else:
            # Tất cả — merge base + user files
            base_files = api.list_files(token=get_token(), project_id=0)
            try:
                user_files = api.list_files(token=get_token())   # no project_id = all user's
            except Exception:
                user_files = []
            # Dedup by file_id
            seen = set()
            files_list = []
            for f in base_files + user_files:
                fid = f.get("file_id")
                if fid and fid not in seen:
                    seen.add(fid)
                    files_list.append(f)
    except Exception as e:
        st.error(f"Không tải được danh sách file: {e}")
        files_list = []

    if filter_status != "Tất cả":
        files_list = [f for f in files_list if f.get("process_status") == filter_status]

    if not files_list:
        st.info("Chưa có file nào. Upload ở trên để bắt đầu.")
    else:
        st.caption(f"Tổng: {len(files_list)} file")
        for finfo in files_list:
            fname = finfo.get("file_name", "?")
            fstatus = finfo.get("process_status", "?")
            fid = finfo.get("file_id", "")
            ftype = finfo.get("file_type", "")
            fproject = finfo.get("project_id", 0)

            status_icon = {
                "ready": "✅", "pending": "⏳",
                "processing": "🔄", "failed": "❌",
            }.get(fstatus, "❓")

            scope_tag = "🌐 base" if fproject == 0 else f"🔒 p{fproject}" if fproject > 0 else "🕵️ incog"

            fcol1, fcol2, fcol3, fcol4 = st.columns([4, 2, 2, 2])
            with fcol1:
                st.markdown(f"{status_icon} **{fname}**")
            with fcol2:
                st.caption(f".{ftype} · {scope_tag}")
            with fcol3:
                st.caption(fstatus)
            with fcol4:
                bc1, bc2 = st.columns(2)
                with bc1:
                    if fstatus == "failed" and ftype in ("pdf", "md", "txt", "docx"):
                        if st.button("🔄", key=f"retry_{fid}", help="Retry"):
                            try:
                                api.reprocess_file(get_token(), fid)
                                st.rerun()
                            except Exception as e:
                                st.error(str(e))
                with bc2:
                    if st.button("🗑", key=f"fdel_{fid}", help="Xóa"):
                        try:
                            api.delete_file(get_token(), fid)
                            st.rerun()
                        except Exception as e:
                            st.error(str(e))

    st.divider()
    st.caption(
        "💡 File trong thư mục `./data` của server tự động scan lúc startup. "
        "File xóa sẽ vào thùng rác, xóa vĩnh viễn sau 1 giờ."
    )
    # Stop here — don't render chat area when viewing files
    st.stop()


# ════════════════════════════════════════════════════════════
# Chat view: compact tools panel (no file management here)
# ════════════════════════════════════════════════════════════

with st.expander("⚙️ Tools", expanded=False):
    tool_col1, tool_col2, tool_col3 = st.columns(3)

    # Check if current model supports tools
    current_model = st.session_state.selected_model or ""
    model_info = next((m for m in st.session_state.models if m["name"] == current_model), {})
    tool_capable = model_info.get("tool_capable", False)
    has_msgs = len(st.session_state.messages) > 0

    with tool_col1:
        ws_enabled = st.checkbox(
            "🔍 Web Search",
            value="web_search" in st.session_state.tools_enabled,
            disabled=not tool_capable or has_msgs,
            help="SearXNG-based web search (bật mặc định)" if tool_capable else "Model không support tools",
        )
    with tool_col2:
        dbq_enabled = st.checkbox(
            "🗄️ Database Query",
            value="database_query" in st.session_state.tools_enabled,
            disabled=not tool_capable or has_msgs,
            help="Truy vấn ClickHouse OLAP (yêu cầu CLICKHOUSE_HOST)",
        )
    with tool_col3:
        viz_enabled = st.checkbox(
            "📊 Visualize",
            value="visualize" in st.session_state.tools_enabled,
            disabled=not tool_capable or has_msgs,
            help="Vẽ biểu đồ từ dữ liệu bảng",
        )

    # Update tool list only before first message (tools lock after chat starts)
    if not has_msgs:
        new_tools = []
        if ws_enabled and tool_capable:
            new_tools.append("web_search")
        if dbq_enabled and tool_capable:
            new_tools.append("database_query")
        if viz_enabled and tool_capable:
            new_tools.append("visualize")
        st.session_state.tools_enabled = new_tools

    st.caption(
        "📁 File management đã chuyển sang trang riêng — click **Files** ở sidebar."
    )


# ════════════════════════════════════════════════════════════
# CHAT VIEWPORT
# ════════════════════════════════════════════════════════════

# Welcome state (no session)
if not st.session_state.current_session_id and not st.session_state.messages:
    st.markdown("")
    st.markdown("")
    col_w1, col_w2, col_w3 = st.columns([1, 2, 1])
    with col_w2:
        st.markdown("## 🏠 Welcome to FinHouse")
        st.markdown("Your private, self-hosted AI assistant powered by Ollama.")
        st.markdown("")

        # Suggested prompts
        prompts = [
            ("💬 Just chat", "Hello! What can you help me with?"),
            ("🔍 Search the web", "What are the latest developments in AI?"),
            ("📄 Summarize a doc", "I'd like to upload a document for analysis"),
            ("📊 Data insights", "Show me trends in our quarterly data"),
        ]
        cols = st.columns(2)
        for i, (label, prompt) in enumerate(prompts):
            with cols[i % 2]:
                if st.button(label, key=f"welcome_{i}", use_container_width=True):
                    create_new_session()
                    st.session_state["_pending_prompt"] = prompt
                    st.rerun()

else:
    # ── Render existing messages ────────────────────────────
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            # Tool activity (shown above assistant response)
            if msg.get("tool_events"):
                for tev in msg["tool_events"]:
                    if tev["type"] == "tool_call":
                        tool_name = tev.get("tool", "Unknown")
                        args = tev.get("args", {})
                        st.markdown(
                            f'<div class="tool-card">'
                            f'<div class="tool-card-header">🔧 {tool_name}</div>'
                            f'<code>{json.dumps(args, ensure_ascii=False)[:200]}</code>'
                            f'</div>',
                            unsafe_allow_html=True,
                        )
                    elif tev["type"] == "tool_result":
                        with st.expander("📋 Tool results", expanded=False):
                            try:
                                results = json.loads(tev["content"])
                                if isinstance(results, list):
                                    for r in results[:5]:
                                        if isinstance(r, dict) and "title" in r:
                                            st.markdown(f"**[{r['title']}]({r.get('url','')})**")
                                            st.caption(r.get("snippet", "")[:200])
                                        else:
                                            st.json(r)
                                else:
                                    st.json(results)
                            except Exception:
                                st.text(tev["content"][:500])

            st.markdown(msg["content"])

            # RAG Sources (shown below assistant response)
            if msg.get("rag_sources") and msg["role"] == "assistant":
                with st.expander(f"📚 Sources ({len(msg['rag_sources'])})", expanded=False):
                    for src in msg["rag_sources"]:
                        idx = src.get("index", "?")
                        fname = src.get("file_name", "unknown")
                        text_preview = src.get("text", "")[:200]
                        score = src.get("score", 0)
                        st.markdown(
                            f"**[{idx}]** `{fname}` (score: {score:.3f})"
                        )
                        st.caption(text_preview)

    # ── Chat input ──────────────────────────────────────────
    pending = st.session_state.pop("_pending_prompt", None)
    user_input = st.chat_input(
        "Incognito — not saved. Ask anything..." if st.session_state.incognito
        else "Ask FinHouse...",
        disabled=st.session_state.is_streaming,
    )
    prompt = pending or user_input

    if prompt:
        # Ensure session exists
        if not st.session_state.current_session_id:
            create_new_session()

        # Display user message
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        # Stream assistant response
        with st.chat_message("assistant"):
            response_area = st.empty()
            tool_container = st.container()
            source_container = st.container()
            full_response = ""
            tool_events = []
            rag_sources = []
            st.session_state.is_streaming = True

            try:
                for event in api.send_message_stream(
                    st.session_state.current_session_id,
                    prompt,
                    get_token(),
                ):
                    etype = event.get("type")

                    if etype == "token":
                        full_response += event.get("content", "")
                        response_area.markdown(full_response + "▌")

                    elif etype == "rag_sources":
                        rag_sources = event.get("sources", [])
                        if rag_sources:
                            with tool_container:
                                st.markdown(
                                    f'<div class="tool-card">'
                                    f'<div class="tool-card-header">📚 Retrieved {len(rag_sources)} source(s)</div>'
                                    f'</div>',
                                    unsafe_allow_html=True,
                                )

                    elif etype == "query_rewrite":
                        # Show rewrite info for transparency
                        rewritten = event.get("rewritten", "")
                        entities = event.get("entities", [])
                        timeframe = event.get("timeframe", "")
                        with tool_container:
                            with st.expander("🔎 Câu hỏi đã rewrite", expanded=False):
                                st.markdown(f"**Rewritten:** {rewritten}")
                                if entities:
                                    st.caption(f"Entities: {', '.join(entities)}")
                                if timeframe:
                                    st.caption(f"Timeframe: {timeframe}")

                    elif etype == "clarification":
                        # Rewriter asked for clarification — just log a small
                        # note; the actual text comes as `token` events after
                        clar_text = event.get("content", "")
                        with tool_container:
                            st.markdown(
                                f'<div class="tool-card">'
                                f'<div class="tool-card-header">❓ Cần làm rõ thêm</div>'
                                f'</div>',
                                unsafe_allow_html=True,
                            )

                    elif etype == "tool_start":
                        tool_events.append({"type": "tool_call", **event})
                        with tool_container:
                            st.markdown(
                                f'<div class="tool-card">'
                                f'<div class="tool-card-header">🔧 {event.get("tool","")}</div>'
                                f'<code>{json.dumps(event.get("args",{}), ensure_ascii=False)[:200]}</code>'
                                f'</div>',
                                unsafe_allow_html=True,
                            )

                    elif etype == "tool_end":
                        tool_events.append({"type": "tool_result", "content": event.get("content", "")})
                        with tool_container:
                            with st.expander("📋 Search results", expanded=False):
                                try:
                                    results = json.loads(event.get("content", "[]"))
                                    if isinstance(results, list):
                                        for r in results[:5]:
                                            if isinstance(r, dict) and "title" in r:
                                                st.markdown(f"**[{r['title']}]({r.get('url','')})**")
                                                st.caption(r.get("snippet", "")[:200])
                                except Exception:
                                    st.text(event.get("content", "")[:500])

                    elif etype == "title":
                        if st.session_state.session_meta:
                            st.session_state.session_meta["session_title"] = event.get("content", "")

                    elif etype == "done":
                        break

                    elif etype == "error":
                        st.error(event.get("content", "Unknown error"))
                        break

            except Exception as e:
                st.error(f"Stream error: {e}")
                full_response = full_response or f"Error: {e}"

            # Finalize
            response_area.markdown(full_response)
            st.session_state.is_streaming = False

            # Show RAG sources below response
            if rag_sources:
                with source_container:
                    with st.expander(f"📚 Sources ({len(rag_sources)})", expanded=False):
                        for src in rag_sources:
                            idx = src.get("index", "?")
                            fname = src.get("file_name", "unknown")
                            text_preview = src.get("text", "")[:200]
                            score = src.get("score", 0)
                            st.markdown(
                                f"**[{idx}]** `{fname}` (score: {score:.3f})"
                            )
                            st.caption(text_preview)

            assistant_msg = {"role": "assistant", "content": full_response}
            if tool_events:
                assistant_msg["tool_events"] = tool_events
            if rag_sources:
                assistant_msg["rag_sources"] = rag_sources
            st.session_state.messages.append(assistant_msg)

            # Refresh session meta
            if st.session_state.current_session_id:
                try:
                    st.session_state.session_meta = api.get_session(
                        st.session_state.current_session_id
                    )
                except Exception:
                    pass
