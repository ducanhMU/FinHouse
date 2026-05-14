"""
Wikipedia lookup — definitions, company background, encyclopedia entries.

Backed by `wikipedia` PyPI lib via LangChain's WikipediaAPIWrapper for
robust fallback handling. Two-pass: try Vietnamese first (better match
for VN-specific topics), fall back to English if VI returns nothing.

Returns a single concatenated string per LangChain convention. Capped to
keep token budget sane.
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("finhouse.tools.wikipedia")

# Singletons per language so we don't re-instantiate on every call.
_wrappers: dict[str, object] = {}
_load_attempted = False


def _get_wrapper(lang: str):
    """Return a WikipediaQueryRun for `lang`, or None on failure."""
    global _load_attempted
    if lang in _wrappers:
        return _wrappers[lang]

    try:
        from langchain_community.tools import WikipediaQueryRun
        from langchain_community.utilities import WikipediaAPIWrapper
    except Exception as e:
        if not _load_attempted:
            log.warning(f"langchain_community wikipedia import failed: {e}")
            _load_attempted = True
        return None

    try:
        runner = WikipediaQueryRun(
            api_wrapper=WikipediaAPIWrapper(
                lang=lang,
                top_k_results=2,
                doc_content_chars_max=2000,
            ),
        )
        _wrappers[lang] = runner
        return runner
    except Exception as e:
        log.warning(f"WikipediaQueryRun init for lang={lang} failed: {e}")
        return None


def _query_sync(query: str, lang: str) -> str:
    runner = _get_wrapper(lang)
    if runner is None:
        return ""
    try:
        result = runner.invoke(query[:300])
        return (result or "").strip()
    except Exception as e:
        log.debug(f"wikipedia({lang}) failed: {e}")
        return ""


async def wikipedia_search(query: str, lang: str = "vi") -> dict:
    """
    Look up `query` on Wikipedia. Tries `lang` first, falls back to 'en'
    if the result is empty (common for VN-specific topics that exist on
    EN Wikipedia but not VI).
    """
    if not query or not query.strip():
        return {"query": query, "lang": lang, "result": "", "error": "empty query"}

    lang = (lang or "vi").lower().strip() or "vi"
    primary = await asyncio.to_thread(_query_sync, query, lang)
    used_lang = lang

    if not primary and lang != "en":
        fallback = await asyncio.to_thread(_query_sync, query, "en")
        if fallback:
            primary = fallback
            used_lang = "en"

    if not primary:
        return {"query": query, "lang": used_lang, "result": "", "error": "no results"}

    return {"query": query, "lang": used_lang, "result": primary[:3000]}


WIKIPEDIA_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "wikipedia",
        "description": (
            "Tra cứu Wikipedia (mặc định tiếng Việt, fallback tiếng Anh nếu "
            "VI rỗng) cho ĐỊNH NGHĨA khái niệm tài chính, lịch sử/giới "
            "thiệu doanh nghiệp đại chúng, thuật ngữ kinh tế. KHÔNG dùng "
            "cho số liệu thời gian thực, giá CK, hay BCTC. KHÔNG dùng nếu "
            "web_search đã trả kết quả Wikipedia trong snippet (tránh trùng)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Từ khoá tra cứu (1-5 từ ngắn gọn).",
                },
                "lang": {
                    "type": "string",
                    "description": "Ngôn ngữ ưu tiên: 'vi' hoặc 'en'. Mặc định 'vi'.",
                },
            },
            "required": ["query"],
        },
    },
}
