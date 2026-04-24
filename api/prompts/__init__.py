"""
FinHouse — Prompt loader.

Generic loader for Markdown prompt files. Each file has a header
(comments + front matter) and the actual prompt body after the first
`---` line on its own.

Cached per-file. Call reload_prompts() to clear cache.
"""

import logging
from functools import lru_cache
from pathlib import Path

log = logging.getLogger("finhouse.prompts")

_PROMPTS_DIR = Path(__file__).parent

_FALLBACK_SYSTEM = (
    "You are a helpful AI assistant specialized in Vietnamese corporate finance. "
    "Answer in Vietnamese if user speaks Vietnamese, English otherwise. "
    "Never use Chinese, Japanese, or other languages."
)

_FALLBACK_REWRITER = (
    "Rewrite the user's latest message into a self-contained question based on "
    "conversation history. Output a JSON object with fields: "
    "rewritten, needs_clarification, clarification, preserved_entities, preserved_timeframe."
)

_FALLBACKS = {
    "system": _FALLBACK_SYSTEM,
    "query_rewriter": _FALLBACK_REWRITER,
}


def _parse_markdown(raw: str) -> str:
    """Extract prompt body — everything after the first standalone `---` line."""
    lines = raw.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "---":
            return "\n".join(lines[i + 1:]).strip()
    return raw.strip()


@lru_cache(maxsize=16)
def load_prompt(name: str) -> str:
    """
    Load a prompt file by name (e.g. "system" → prompts/system.md).
    Returns the body (after front-matter separator) or a fallback.
    Results are cached; call `reload_prompts()` to force re-read.
    """
    path = _PROMPTS_DIR / f"{name}.md"
    try:
        if not path.exists():
            log.warning(f"Prompt file not found: {path}, using fallback for '{name}'")
            return _FALLBACKS.get(name, f"[Missing prompt: {name}]")
        raw = path.read_text(encoding="utf-8")
        body = _parse_markdown(raw)
        if not body:
            log.warning(f"Prompt file {path} is empty, using fallback")
            return _FALLBACKS.get(name, f"[Empty prompt: {name}]")
        log.info(f"Prompt loaded: {name} ({len(body)} chars)")
        return body
    except Exception as e:
        log.error(f"Failed to read prompt '{name}': {e}")
        return _FALLBACKS.get(name, f"[Error loading prompt: {name}]")


def reload_prompts() -> None:
    """Clear cache; next load_prompt() call re-reads from disk."""
    load_prompt.cache_clear()
    log.info("Prompt cache cleared")


# ── Convenience accessors ───────────────────────────────────

def get_system_prompt() -> str:
    return load_prompt("system")


def get_query_rewriter_prompt() -> str:
    return load_prompt("query_rewriter")
