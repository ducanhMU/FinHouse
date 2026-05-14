"""
URL fetcher — pulls the main text content from a single URL.

The web_search tool only returns ~200-char snippets. When the agent finds
a result that looks promising, this tool fetches the full article and
extracts the main body (drops nav / ads / footers) so the agent can
summarize accurately.

Backend: `trafilatura` (best-of-class boilerplate stripper for news
articles). If trafilatura isn't installed or returns nothing, falls back
to a plain HTML → text strip via stdlib `html.parser`.

Hard limits (defensive — these are NOT security boundaries):
    • http(s) only
    • Per-request timeout (settings.URL_FETCH_TIMEOUT_SEC)
    • Output capped at settings.URL_FETCH_MAX_CHARS
    • Single follow-redirect chain via httpx default

Returns a dict so the LLM can pick fields:
    {"url": <final url>, "title": <best-effort>, "text": <extracted>,
     "truncated": <bool>, "error": <optional str>}
"""

from __future__ import annotations

import logging
import re
from html.parser import HTMLParser
from typing import Optional

import httpx

from config import get_settings

settings = get_settings()
log = logging.getLogger("finhouse.tools.url_fetch")

# Singleton client — keeps TCP pool warm between agent rounds.
_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                settings.URL_FETCH_TIMEOUT_SEC, connect=5.0,
            ),
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "FinHouse/1.0 (+https://finhouse.local) "
                    "Mozilla/5.0 (compatible; ResearchBot)"
                ),
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "vi,en;q=0.8",
            },
        )
    return _client


async def close_client():
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
        _client = None


class _StripHTML(HTMLParser):
    """Stdlib fallback when trafilatura is absent or fails."""

    SKIP_TAGS = {"script", "style", "nav", "footer", "header", "aside", "form"}

    def __init__(self):
        super().__init__()
        self._buf: list[str] = []
        self._skip_depth = 0
        self.title: str = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in ("p", "br", "li", "h1", "h2", "h3", "h4"):
            self._buf.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._skip_depth > 0:
            return
        if self._in_title:
            self.title += data
            return
        s = data.strip()
        if s:
            self._buf.append(s + " ")

    @property
    def text(self) -> str:
        out = "".join(self._buf)
        # Collapse runs of whitespace
        out = re.sub(r"[ \t]+", " ", out)
        out = re.sub(r"\n{3,}", "\n\n", out)
        return out.strip()


def _extract_with_trafilatura(html: str) -> tuple[str, str]:
    """Returns (text, title) or ('', '') on failure."""
    try:
        import trafilatura
    except Exception:
        return "", ""
    try:
        text = trafilatura.extract(
            html, include_comments=False, include_tables=True,
            no_fallback=False, deduplicate=True,
        ) or ""
        meta = trafilatura.extract_metadata(html)
        title = (getattr(meta, "title", None) or "") if meta else ""
        return text.strip(), title.strip()
    except Exception as e:
        log.debug(f"trafilatura extract failed: {e}")
        return "", ""


async def fetch_url(url: str) -> dict:
    """Fetch a single URL and return cleaned main text + title."""
    if not url or not isinstance(url, str):
        return {"url": "", "title": "", "text": "", "truncated": False,
                "error": "empty url"}

    url = url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        return {"url": url, "title": "", "text": "", "truncated": False,
                "error": "only http/https schemes are allowed"}

    client = _get_client()
    try:
        resp = await client.get(url)
    except httpx.TimeoutException:
        return {"url": url, "title": "", "text": "", "truncated": False,
                "error": f"timeout after {settings.URL_FETCH_TIMEOUT_SEC}s"}
    except Exception as e:
        return {"url": url, "title": "", "text": "", "truncated": False,
                "error": f"fetch failed: {e}"}

    if resp.status_code >= 400:
        return {"url": str(resp.url), "title": "", "text": "", "truncated": False,
                "error": f"HTTP {resp.status_code}"}

    ctype = resp.headers.get("content-type", "").lower()
    if "html" not in ctype and "text" not in ctype:
        return {"url": str(resp.url), "title": "", "text": "", "truncated": False,
                "error": f"unsupported content-type: {ctype}"}

    html = resp.text or ""

    text, title = _extract_with_trafilatura(html)
    if not text:
        # Stdlib fallback
        try:
            parser = _StripHTML()
            parser.feed(html)
            text = parser.text
            if not title:
                title = parser.title.strip()
        except Exception as e:
            return {"url": str(resp.url), "title": "", "text": "", "truncated": False,
                    "error": f"html parse failed: {e}"}

    cap = settings.URL_FETCH_MAX_CHARS
    truncated = len(text) > cap
    if truncated:
        text = text[:cap]

    return {
        "url": str(resp.url),
        "title": title[:300],
        "text": text,
        "truncated": truncated,
    }


URL_FETCH_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "fetch_url",
        "description": (
            "Tải toàn bộ nội dung chính của 1 trang web (đã strip nav/quảng "
            "cáo) khi snippet từ web_search không đủ thông tin. Dùng SAU "
            "khi web_search đã trả URL có vẻ đúng chủ đề. KHÔNG dùng để "
            "lùng URL random — phải là URL đã thấy trong kết quả search "
            "hoặc do user dán."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "URL đầy đủ (bắt buộc bắt đầu bằng http:// hoặc https://)",
                },
            },
            "required": ["url"],
        },
    },
}
