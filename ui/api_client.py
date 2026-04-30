"""FinHouse UI — API Client helper for communicating with FastAPI backend."""

import json
import os
from typing import Optional, Generator

import httpx

API_BASE = os.getenv("API_BASE_URL", "http://finhouse-api:8000")
TIMEOUT = httpx.Timeout(300.0, connect=10.0)


def _headers(token: Optional[str] = None) -> dict:
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


class APIError(Exception):
    """Raised when the API returns a non-2xx response. .detail has server message."""
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"HTTP {status_code}: {detail}")


def _extract_error(r: httpx.Response) -> str:
    """Extract human-readable error message from FastAPI error response."""
    try:
        data = r.json()
        if isinstance(data, dict):
            detail = data.get("detail")
            if isinstance(detail, str):
                return detail
            if isinstance(detail, list) and detail:
                # Pydantic validation errors → [{loc, msg, type}, ...]
                msgs = [d.get("msg", str(d)) for d in detail]
                return "; ".join(msgs)
            if detail:
                return str(detail)
        return r.text[:200]
    except Exception:
        return f"HTTP {r.status_code}: {r.text[:200]}"


def _raise_for_status(r: httpx.Response):
    if r.is_success:
        return
    raise APIError(r.status_code, _extract_error(r))


# ── Auth ────────────────────────────────────────────────────

def register(username: str, password: str) -> dict:
    r = httpx.post(
        f"{API_BASE}/auth/register",
        json={"user_name": username, "user_password": password},
        timeout=TIMEOUT,
    )
    _raise_for_status(r)
    return r.json()


def login(username: str, password: str) -> dict:
    r = httpx.post(
        f"{API_BASE}/auth/login",
        json={"user_name": username, "user_password": password},
        timeout=TIMEOUT,
    )
    _raise_for_status(r)
    return r.json()


def refresh_token(refresh_tok: str) -> dict:
    r = httpx.post(
        f"{API_BASE}/auth/refresh",
        json={"refresh_token": refresh_tok},
        timeout=TIMEOUT,
    )
    _raise_for_status(r)
    return r.json()


# ── Health ──────────────────────────────────────────────────

def health() -> dict:
    try:
        r = httpx.get(f"{API_BASE}/health", timeout=5.0)
        return r.json()
    except Exception as e:
        return {"status": "error", "services": {"api": str(e)}}


# ── Models ──────────────────────────────────────────────────

def get_models() -> list[dict]:
    try:
        r = httpx.get(f"{API_BASE}/models", timeout=10.0)
        r.raise_for_status()
        return r.json()
    except Exception:
        return []


# ── Projects ────────────────────────────────────────────────

def list_projects(token: str) -> list[dict]:
    r = httpx.get(f"{API_BASE}/projects", headers=_headers(token), timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def create_project(token: str, title: str, description: str = "") -> dict:
    r = httpx.post(
        f"{API_BASE}/projects",
        json={"project_title": title, "description": description},
        headers=_headers(token),
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def delete_project(token: str, project_id: int):
    r = httpx.delete(
        f"{API_BASE}/projects/{project_id}",
        headers=_headers(token),
        timeout=TIMEOUT,
    )
    r.raise_for_status()


# ── Sessions ────────────────────────────────────────────────

def list_sessions(token: Optional[str], project_id: Optional[int] = None) -> list[dict]:
    params = {}
    if project_id is not None:
        params["project_id"] = project_id
    r = httpx.get(
        f"{API_BASE}/sessions",
        params=params,
        headers=_headers(token),
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def create_session(
    token: Optional[str],
    model_used: str,
    project_id: Optional[int] = None,
    tools_used: Optional[list[str]] = None,
) -> dict:
    body = {"model_used": model_used}
    if project_id is not None:
        body["project_id"] = project_id
    # Pass tools_used even when it's an empty list — that means "no tools".
    # Truthy check ([] is falsy) would silently fall back to backend defaults.
    if tools_used is not None:
        body["tools_used"] = tools_used
    r = httpx.post(
        f"{API_BASE}/sessions",
        json=body,
        headers=_headers(token),
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def get_session(session_id: str) -> dict:
    r = httpx.get(f"{API_BASE}/sessions/{session_id}", timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def update_session(token: Optional[str], session_id: str, title: str) -> dict:
    r = httpx.put(
        f"{API_BASE}/sessions/{session_id}",
        json={"session_title": title},
        headers=_headers(token),
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def delete_session(token: Optional[str], session_id: str):
    r = httpx.delete(
        f"{API_BASE}/sessions/{session_id}",
        headers=_headers(token),
        timeout=TIMEOUT,
    )
    r.raise_for_status()


# ── Chat ────────────────────────────────────────────────────

def send_message_stream(
    session_id: str,
    text: str,
    token: Optional[str] = None,
) -> Generator[dict, None, None]:
    """Stream SSE events from the chat endpoint. Yields parsed dicts."""
    with httpx.stream(
        "POST",
        f"{API_BASE}/chat/{session_id}/send",
        json={"text": text},
        headers=_headers(token),
        timeout=TIMEOUT,
    ) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if line.startswith("data: "):
                try:
                    yield json.loads(line[6:])
                except json.JSONDecodeError:
                    continue


def stop_stream(session_id: str):
    try:
        httpx.post(f"{API_BASE}/chat/{session_id}/stop", timeout=5.0)
    except Exception:
        pass


def get_events(session_id: str) -> list[dict]:
    r = httpx.get(f"{API_BASE}/chat/{session_id}/events", timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


# ── Files ───────────────────────────────────────────────────

def upload_file(
    token: Optional[str],
    file_bytes: bytes,
    file_name: str,
    project_id: int,
    session_id: Optional[str] = None,
) -> dict:
    files = {"file": (file_name, file_bytes)}
    data = {"project_id": str(project_id)}
    if session_id:
        data["session_id"] = session_id
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = httpx.post(
        f"{API_BASE}/files/upload",
        files=files,
        data=data,
        headers=headers,
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def list_files(
    token: Optional[str] = None,
    project_id: Optional[int] = None,
) -> list[dict]:
    """List files, optionally filtered by project_id."""
    params = {}
    if project_id is not None:
        params["project_id"] = project_id
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = httpx.get(
        f"{API_BASE}/files",
        params=params,
        headers=headers,
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def file_status(file_id: str) -> dict:
    """Get the processing status of a single file."""
    r = httpx.get(f"{API_BASE}/files/status/{file_id}", timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def delete_file(token: Optional[str], file_id: str):
    """Soft-delete a file."""
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = httpx.delete(
        f"{API_BASE}/files/{file_id}",
        headers=headers,
        timeout=TIMEOUT,
    )
    r.raise_for_status()


def reprocess_file(token: Optional[str], file_id: str) -> dict:
    """Re-process a failed file."""
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = httpx.post(
        f"{API_BASE}/files/reprocess/{file_id}",
        headers=headers,
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()