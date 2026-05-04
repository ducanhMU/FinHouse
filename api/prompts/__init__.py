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

_FALLBACK_DATABASE_QUERY = (
    "Use select_rows(table, columns?, filters?, order_by?, limit?, use_final?) and "
    "aggregate(table, aggregations, group_by?, filters?, order_by?, limit?, use_final?) "
    "to read the OLAP database. ONE table per call — no JOIN. "
    "Tables include stocks, company_overview, balance_sheet, income_statement, "
    "cash_flow_statement, financial_ratios, shareholders, officers, news, events, "
    "stock_price_history. Set use_final=true on ReplacingMergeTree tables for latest "
    "rows; false on append-only (stock_price_history, stock_intraday, news, events). "
    "Filter financial tables by symbol/year/quarter; quarter=0 means annual."
)

_FALLBACK_VISUALIZE = (
    "Use bar(table, x_column, y_columns, filters?, order_by?, ...), "
    "line(table, x_column, y_columns, filters?, order_by?, ...), or "
    "pie(table, label_column, value_column, filters?, ...) to render charts. "
    "Each tool reads one OLAP table directly — do NOT pre-fetch data. "
    "Pie only for share-of-whole (shareholders, segment mix). Bar for "
    "cross-entity comparison. Line for trends — always pass order_by asc "
    "on the time column. Embed the returned URL with markdown ![title](url)."
)

_FALLBACK_WEB_SEARCH = (
    "Use web_search only for fresh info outside training cutoff and outside "
    "the OLAP database. Resolve pronouns first. Include ticker/company and "
    "year/quarter in the query. Cite sources [1], [2] in the answer."
)

_FALLBACK_ORCHESTRATOR = (
    "Decompose the user's question into 0+ tasks, one per tool agent "
    "(database / web_search / visualize). Output a single JSON object: "
    "{reasoning, tasks: [{goal, tool_type, args}]}. No markdown, no tool calls."
)

_FALLBACK_COLLECTOR = _FALLBACK_SYSTEM

_FALLBACKS = {
    "system": _FALLBACK_SYSTEM,
    "collector": _FALLBACK_COLLECTOR,
    "orchestrator": _FALLBACK_ORCHESTRATOR,
    "query_rewriter": _FALLBACK_REWRITER,
    "database_query": _FALLBACK_DATABASE_QUERY,
    "visualize": _FALLBACK_VISUALIZE,
    "web_search": _FALLBACK_WEB_SEARCH,
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
    """Legacy alias — prefer get_collector_prompt() for the collector role."""
    return load_prompt("system")


def get_collector_prompt() -> str:
    """Persona + synthesis instructions for the collector node."""
    return load_prompt("collector")


def get_orchestrator_prompt() -> str:
    """Plan-emission instructions for the orchestrator node."""
    return load_prompt("orchestrator")


def get_query_rewriter_prompt() -> str:
    return load_prompt("query_rewriter")


def get_database_query_prompt() -> str:
    return load_prompt("database_query")


def get_visualize_prompt() -> str:
    return load_prompt("visualize")


def get_web_search_prompt() -> str:
    return load_prompt("web_search")
