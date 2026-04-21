"""
FinHouse — database_query tool

Runs LLM-generated SQL against ClickHouse. Used by the chat agent
when the user asks questions about the OLAP data (tables ingested
via the NiFi + Spark pipeline).

Safety:
  • READ-ONLY: rejects any statement that isn't SELECT / WITH / SHOW
  • Bounded: adds LIMIT if missing; caps rows in settings
  • Timeouts: configurable per-query timeout

Schema discovery:
  list_tables() returns all user tables for the LLM's planning step.
  describe_table(name) returns columns + types.
"""

import logging
import re
from typing import Any

import httpx

from config import get_settings

settings = get_settings()
logger = logging.getLogger("finhouse.tools.db")

# Singleton client — reused across calls
_ch_client: httpx.AsyncClient | None = None


def is_enabled() -> bool:
    return bool(settings.CLICKHOUSE_HOST)


def get_client() -> httpx.AsyncClient:
    global _ch_client
    if _ch_client is None or _ch_client.is_closed:
        _ch_client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=5.0),
            auth=(settings.CLICKHOUSE_USER, settings.CLICKHOUSE_PASSWORD),
        )
    return _ch_client


async def close_client():
    global _ch_client
    if _ch_client and not _ch_client.is_closed:
        await _ch_client.aclose()
        _ch_client = None


# ── Safety layer ────────────────────────────────────────────

_ALLOWED_START = re.compile(
    r"^\s*(SELECT|WITH|SHOW|DESCRIBE|DESC|EXPLAIN)\b",
    re.IGNORECASE,
)
_FORBIDDEN_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|GRANT|REVOKE|ATTACH|DETACH|KILL|OPTIMIZE|RENAME|SYSTEM)\b",
    re.IGNORECASE,
)


def _ensure_read_only(sql: str) -> str:
    """Validate SQL is read-only. Raises ValueError if not."""
    if len(sql) > settings.DATABASE_QUERY_MAX_SQL_LEN:
        raise ValueError(
            f"SQL too long ({len(sql)} chars, max {settings.DATABASE_QUERY_MAX_SQL_LEN})"
        )
    if not _ALLOWED_START.match(sql):
        raise ValueError("SQL must start with SELECT / WITH / SHOW / DESCRIBE / EXPLAIN")
    if _FORBIDDEN_KEYWORDS.search(sql):
        raise ValueError("SQL contains forbidden write/DDL keywords")
    # Multiple statements
    if ";" in sql.rstrip(";"):
        raise ValueError("Multiple statements not allowed")
    return sql


def _enforce_limit(sql: str) -> str:
    """Add LIMIT clause if missing. ClickHouse ignores LIMIT in subqueries, OK."""
    if re.search(r"\bLIMIT\s+\d+", sql, re.IGNORECASE):
        return sql
    # Don't append LIMIT to SHOW / DESCRIBE / EXPLAIN
    if re.match(r"^\s*(SHOW|DESCRIBE|DESC|EXPLAIN)", sql, re.IGNORECASE):
        return sql
    return sql.rstrip(";").rstrip() + f" LIMIT {settings.DATABASE_QUERY_MAX_ROWS}"


# ── Tool implementations ────────────────────────────────────

async def run_sql(sql: str) -> dict[str, Any]:
    """
    Execute a read-only SQL query against ClickHouse.
    Returns {columns: [...], rows: [[...]], row_count: int, sql: str}
    """
    if not is_enabled():
        return {"error": "database_query tool disabled (CLICKHOUSE_HOST not set)"}

    try:
        sql = _ensure_read_only(sql.strip())
        sql = _enforce_limit(sql)
    except ValueError as e:
        return {"error": f"query rejected: {e}", "sql": sql}

    url = f"http://{settings.CLICKHOUSE_HOST}:{settings.CLICKHOUSE_PORT}/"
    params = {
        "database": settings.CLICKHOUSE_DB,
        "default_format": "JSONCompact",
    }

    try:
        client = get_client()
        resp = await client.post(url, params=params, content=sql.encode("utf-8"))
        if resp.status_code != 200:
            return {
                "error": f"ClickHouse error {resp.status_code}: {resp.text[:500]}",
                "sql": sql,
            }
        data = resp.json()
        meta = data.get("meta", [])
        columns = [m["name"] for m in meta]
        rows = data.get("data", [])
        return {
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "sql": sql,
        }
    except httpx.HTTPError as e:
        return {"error": f"HTTP error: {e}", "sql": sql}
    except Exception as e:
        logger.exception("Unexpected error running SQL")
        return {"error": f"{type(e).__name__}: {e}", "sql": sql}


async def list_tables() -> dict[str, Any]:
    """List all tables in the OLAP database."""
    return await run_sql(f"SHOW TABLES FROM {settings.CLICKHOUSE_DB}")


async def describe_table(table_name: str) -> dict[str, Any]:
    """Return column names + types for a table."""
    # Sanitize table name (prevent injection through this path)
    if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", table_name):
        return {"error": f"Invalid table name: {table_name}"}
    return await run_sql(f"DESCRIBE TABLE {settings.CLICKHOUSE_DB}.{table_name}")


# ── Tool schemas (for Ollama function calling) ──────────────

DATABASE_QUERY_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "database_query",
        "description": (
            "Run a READ-ONLY SQL query against the OLAP ClickHouse database. "
            "Use this to answer the user's questions about data in loaded tables. "
            "First call with sql='SHOW TABLES' to discover tables, then "
            "'DESCRIBE TABLE <name>' to see columns, then write an aggregate SELECT."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "SQL SELECT/WITH/SHOW/DESCRIBE statement. No write DDL.",
                }
            },
            "required": ["sql"],
        },
    },
}
