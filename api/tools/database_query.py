"""
FinHouse — database_query tools

Narrow, structured tools the chat agent calls instead of writing raw
SQL. Each tool maps to ONE ClickHouse table; if the agent needs data
from multiple tables, it calls the tool again (in parallel within the
same assistant turn) and combines results when answering. This is the
mechanical version of the "no JOIN" rule — the tool surface itself
makes JOINs impossible.

Public tools (exposed to the LLM):
  • select_rows(table, columns?, filters?, order_by?, limit?, use_final?)
  • aggregate(table, aggregations, group_by?, filters?, order_by?, limit?, use_final?)

Internal helpers (used by the rewriter for company verification, etc.):
  • run_sql(sql)        — raw read-only SQL, NOT exposed to the LLM
  • list_tables()       — SHOW TABLES wrapper
  • describe_table(t)   — DESCRIBE wrapper

Safety:
  • Identifiers (table/column/alias) validated against a snake_case regex
    and backtick-quoted before splicing into SQL.
  • Filter values escaped via ClickHouse single-quote literal rules.
  • run_sql() still enforces SELECT/WITH/SHOW/DESCRIBE/EXPLAIN-only.
  • LIMIT auto-capped at settings.DATABASE_QUERY_MAX_ROWS.
"""

import logging
import re
from typing import Any, Optional

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


async def distinct_values(
    table: str,
    column: str,
    filters: Optional[list[dict]] = None,
    limit: int = 100,
    use_final: bool = True,
) -> dict[str, Any]:
    """Return distinct values of one column in a table.

    Useful for entity discovery — e.g. "which tickers does the OLAP know
    about?" or "which years are populated for income_statement?". Filters
    follow the same schema as select_rows so an agent can scope the
    distinct set (e.g. distinct year WHERE symbol='VNM').
    """
    try:
        col_sql = _ident(column)
        sql = (
            f"SELECT DISTINCT {col_sql} AS value "
            f"FROM {_table_ref(table, use_final)}"
        )
        sql += _build_where(filters)
        sql += f" ORDER BY value ASC LIMIT {_clamp_limit(limit)}"
    except (ValueError, KeyError, TypeError) as e:
        return {"error": str(e)}
    return await run_sql(sql)


# ── Structured query builders ───────────────────────────────
# These take typed args from the LLM and assemble safe SQL. Identifiers
# are whitelist-validated and backtick-quoted; literals go through
# _ch_lit() which escapes ClickHouse single-quote rules.

_IDENT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_FILTER_OPS = {"=", "!=", "<", "<=", ">", ">=", "IN", "NOT IN", "LIKE", "NOT LIKE", "MATCH"}
_AGG_FUNCS = {"sum", "avg", "min", "max", "count"}
_ORDER_DIRS = {"asc", "desc"}

# Aliases the model frequently emits but which aren't real ClickHouse ops.
# Map them onto the canonical op so a small-model typo doesn't kill the call.
_FILTER_OP_ALIASES = {
    "==": "=",
    "<>": "!=",
    "=~": "MATCH",       # regex match (PCRE) — ClickHouse match()
    "!~": "NOT MATCH",   # negated regex
    "REGEX": "MATCH",
    "REGEXP": "MATCH",
    "ILIKE": "LIKE",     # ClickHouse LIKE is already case-insensitive enough for our use
    "NOT_IN": "NOT IN",
    "NOTIN": "NOT IN",
    "NOT_LIKE": "NOT LIKE",
    "NOTLIKE": "NOT LIKE",
}


def _ident(name: str) -> str:
    """Validate identifier and return it backtick-quoted for SQL."""
    if not isinstance(name, str) or not _IDENT_RE.match(name):
        raise ValueError(f"Invalid identifier: {name!r}")
    return f"`{name}`"


def _ch_lit(val: Any) -> str:
    """Format a Python value as a ClickHouse SQL literal."""
    if val is None:
        return "NULL"
    if isinstance(val, bool):
        return "1" if val else "0"
    if isinstance(val, (int, float)):
        return str(val)
    if isinstance(val, str):
        return "'" + val.replace("\\", "\\\\").replace("'", "''") + "'"
    raise ValueError(f"Unsupported literal type: {type(val).__name__}")


def _build_where(filters: Optional[list[dict]]) -> str:
    if not filters:
        return ""
    clauses = []
    for f in filters:
        if not isinstance(f, dict) or "column" not in f:
            raise ValueError(f"Filter must be an object with 'column': {f!r}")
        col = _ident(f["column"])
        op = (f.get("op") or "=").upper().strip()
        op = _FILTER_OP_ALIASES.get(op, op)
        if op not in _FILTER_OPS:
            raise ValueError(f"Unsupported filter op: {op}")
        if op in ("IN", "NOT IN"):
            values = f.get("value") or f.get("values")
            if not isinstance(values, list) or not values:
                raise ValueError(f"{op} filter requires non-empty list of values for {f['column']!r}")
            literals = ", ".join(_ch_lit(v) for v in values)
            clauses.append(f"{col} {op} ({literals})")
        elif op == "MATCH":
            clauses.append(f"match({col}, {_ch_lit(f.get('value'))})")
        elif op == "NOT MATCH":
            clauses.append(f"NOT match({col}, {_ch_lit(f.get('value'))})")
        else:
            clauses.append(f"{col} {op} {_ch_lit(f.get('value'))}")
    return " WHERE " + " AND ".join(clauses)


def _build_order(order_by: Optional[list[dict]]) -> str:
    if not order_by:
        return ""
    parts = []
    for o in order_by:
        if not isinstance(o, dict) or "column" not in o:
            raise ValueError(f"order_by entry must have 'column': {o!r}")
        col = _ident(o["column"])
        d = (o.get("dir") or "asc").lower().strip()
        if d not in _ORDER_DIRS:
            raise ValueError(f"Unsupported order direction: {d}")
        parts.append(f"{col} {d.upper()}")
    return " ORDER BY " + ", ".join(parts)


def _table_ref(table: str, use_final: bool) -> str:
    if not isinstance(table, str) or not _IDENT_RE.match(table):
        raise ValueError(f"Invalid table name: {table!r}")
    db = settings.CLICKHOUSE_DB
    ref = f"`{db}`.`{table}`"
    return ref + " FINAL" if use_final else ref


def _clamp_limit(limit: Any) -> int:
    cap = settings.DATABASE_QUERY_MAX_ROWS
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return cap
    if n <= 0:
        return cap
    return min(n, cap)


async def select_rows(
    table: str,
    columns: Optional[list[str]] = None,
    filters: Optional[list[dict]] = None,
    order_by: Optional[list[dict]] = None,
    limit: int = 100,
    use_final: bool = True,
) -> dict[str, Any]:
    """
    Read rows from a single OLAP table.

    No JOINs — call once per table and combine results when answering.
    `use_final=True` adds FINAL for ReplacingMergeTree tables; set False
    for append-only tables (stock_price_history, stock_intraday, news,
    events, _ingestion_log, update_log).
    """
    try:
        cols_sql = "*" if not columns else ", ".join(_ident(c) for c in columns)
        sql = f"SELECT {cols_sql} FROM {_table_ref(table, use_final)}"
        sql += _build_where(filters)
        sql += _build_order(order_by)
        sql += f" LIMIT {_clamp_limit(limit)}"
    except (ValueError, KeyError, TypeError) as e:
        return {"error": str(e)}
    return await run_sql(sql)


async def aggregate(
    table: str,
    aggregations: list[dict],
    group_by: Optional[list[str]] = None,
    filters: Optional[list[dict]] = None,
    order_by: Optional[list[dict]] = None,
    limit: int = 100,
    use_final: bool = True,
) -> dict[str, Any]:
    """
    Aggregate rows from a single OLAP table.

    `aggregations` is a list of {func, column?, alias?} where func is
    one of sum/avg/min/max/count. Use func='count' with column='*' (or
    omit) for COUNT(*).
    """
    try:
        if not aggregations:
            raise ValueError("aggregate() requires at least one aggregation")

        agg_exprs = []
        for a in aggregations:
            if not isinstance(a, dict):
                raise ValueError(f"aggregation entry must be an object: {a!r}")
            func = (a.get("func") or "").lower().strip()
            if func not in _AGG_FUNCS:
                raise ValueError(f"Unsupported aggregation function: {func!r}")
            col = a.get("column")
            if func == "count" and (col is None or col == "*"):
                expr = "count(*)"
            else:
                if not col:
                    raise ValueError(f"aggregation func={func} requires a column")
                expr = f"{func}({_ident(col)})"
            alias = a.get("alias")
            if alias:
                expr += f" AS {_ident(alias)}"
            agg_exprs.append(expr)

        select_parts = []
        if group_by:
            select_parts.extend(_ident(c) for c in group_by)
        select_parts.extend(agg_exprs)

        sql = f"SELECT {', '.join(select_parts)} FROM {_table_ref(table, use_final)}"
        sql += _build_where(filters)
        if group_by:
            sql += " GROUP BY " + ", ".join(_ident(c) for c in group_by)
        sql += _build_order(order_by)
        sql += f" LIMIT {_clamp_limit(limit)}"
    except (ValueError, KeyError, TypeError) as e:
        return {"error": str(e)}
    return await run_sql(sql)


# ── Tool schemas (for Ollama function calling) ──────────────

_FILTERS_SCHEMA = {
    "type": "array",
    "description": (
        "WHERE conditions ANDed together. Each item is "
        "{column, op, value}. op ∈ =, !=, <, <=, >, >=, IN, NOT IN, "
        "LIKE, NOT LIKE, MATCH. For IN/NOT IN, value must be an array. "
        "For LIKE/NOT LIKE, value uses SQL wildcards (% and _) — e.g. "
        "value='2025-%' to match anything starting with '2025-'. "
        "For MATCH, value is a PCRE regex (ClickHouse match()) — e.g. "
        "value='^2025-' for the same prefix match. Do NOT use '=~'; "
        "use op='MATCH' instead."
    ),
    "items": {
        "type": "object",
        "properties": {
            "column": {"type": "string"},
            "op": {
                "type": "string",
                "enum": [
                    "=", "!=", "<", "<=", ">", ">=",
                    "IN", "NOT IN",
                    "LIKE", "NOT LIKE",
                    "MATCH",
                ],
            },
            "value": {
                "description": (
                    "Scalar for =/!=/</<=/>/>=/LIKE/MATCH. Array for IN/NOT IN."
                ),
            },
        },
        "required": ["column", "value"],
    },
}

_ORDER_BY_SCHEMA = {
    "type": "array",
    "description": "Sort order; first item is primary.",
    "items": {
        "type": "object",
        "properties": {
            "column": {"type": "string"},
            "dir": {"type": "string", "enum": ["asc", "desc"]},
        },
        "required": ["column"],
    },
}

SELECT_ROWS_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "select_rows",
        "description": (
            "Read rows from ONE OLAP table. No JOINs — if you need data "
            "from multiple tables, call this tool once per table (in "
            "parallel within the same turn) and combine the results when "
            "you answer. Set use_final=true for ReplacingMergeTree tables "
            "(all financial + master tables) to get the latest version; "
            "set use_final=false for append-only tables "
            "(stock_price_history, stock_intraday, news, events)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "table": {
                    "type": "string",
                    "description": "Table name without database prefix, e.g. 'income_statement'.",
                },
                "columns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Columns to return. Omit or empty to select all.",
                },
                "filters": _FILTERS_SCHEMA,
                "order_by": _ORDER_BY_SCHEMA,
                "limit": {"type": "integer", "minimum": 1, "default": 100},
                "use_final": {"type": "boolean", "default": True},
            },
            "required": ["table"],
        },
    },
}

AGGREGATE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "aggregate",
        "description": (
            "Aggregate rows from ONE OLAP table with optional GROUP BY. "
            "Functions: sum, avg, min, max, count. No JOINs. Same FINAL "
            "rules as select_rows."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "table": {"type": "string"},
                "aggregations": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "func": {
                                "type": "string",
                                "enum": ["sum", "avg", "min", "max", "count"],
                            },
                            "column": {
                                "type": "string",
                                "description": "Column to aggregate. Use '*' or omit for count.",
                            },
                            "alias": {
                                "type": "string",
                                "description": "Alias for the result column.",
                            },
                        },
                        "required": ["func"],
                    },
                },
                "group_by": {"type": "array", "items": {"type": "string"}},
                "filters": _FILTERS_SCHEMA,
                "order_by": _ORDER_BY_SCHEMA,
                "limit": {"type": "integer", "minimum": 1, "default": 100},
                "use_final": {"type": "boolean", "default": True},
            },
            "required": ["table", "aggregations"],
        },
    },
}

LIST_TABLES_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_tables",
        "description": (
            "List every table available in the OLAP database. Call this "
            "first if you are not sure a table name exists, instead of "
            "guessing — table inventory drifts as new datasets land. No "
            "arguments."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}

DESCRIBE_TABLE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "describe_table",
        "description": (
            "Return the column names + types of one OLAP table. Use this "
            "when you don't remember the exact column names, or to verify "
            "a column exists before calling select_rows / aggregate / "
            "visualize tools."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "table": {
                    "type": "string",
                    "description": "Table name without database prefix.",
                },
            },
            "required": ["table"],
        },
    },
}

DISTINCT_VALUES_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "distinct_values",
        "description": (
            "List distinct values of ONE column in a table, with optional "
            "filters. Use this for entity discovery: 'what tickers exist?', "
            "'which years have data for VNM?', 'what news categories?'. "
            "Cheaper and clearer than aggregate(count) when you only want "
            "the value list, not counts."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "table": {"type": "string"},
                "column": {
                    "type": "string",
                    "description": "Column whose distinct values you want.",
                },
                "filters": _FILTERS_SCHEMA,
                "limit": {"type": "integer", "minimum": 1, "default": 100},
                "use_final": {"type": "boolean", "default": True},
            },
            "required": ["table", "column"],
        },
    },
}

DATABASE_QUERY_TOOL_SCHEMAS = [
    LIST_TABLES_TOOL_SCHEMA,
    DESCRIBE_TABLE_TOOL_SCHEMA,
    SELECT_ROWS_TOOL_SCHEMA,
    DISTINCT_VALUES_TOOL_SCHEMA,
    AGGREGATE_TOOL_SCHEMA,
]


# ════════════════════════════════════════════════════════════
# Company resolution — used by the rewriter agent's lookup_company
# tool and by post-rewrite verification. Lives here (not in
# services/rewriter) so any agent can resolve a company without an
# upstream dependency on the rewriter package.
# ════════════════════════════════════════════════════════════

_ENTITY_OK_RE = re.compile(r"^[\w \-\.&,/À-ỹ]+$", re.UNICODE)
_MAX_ENTITY_LEN = 100
_MAX_VERIFY_ENTITIES = 10


def _ch_quote(s: str) -> str:
    """Escape a string literal for ClickHouse single-quoted form."""
    return "'" + s.replace("\\", "\\\\").replace("'", "''") + "'"


def _sanitize_entities(entities: list[str]) -> list[str]:
    """Drop entities that don't fit the whitelist or are too long."""
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in entities or []:
        e = (raw or "").strip()
        if not e or len(e) > _MAX_ENTITY_LEN:
            continue
        if not _ENTITY_OK_RE.match(e):
            continue
        key = e.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(e)
        if len(cleaned) >= _MAX_VERIFY_ENTITIES:
            break
    return cleaned


async def verify_company_entities(
    entities: list[str],
) -> tuple[list[dict], list[str], bool]:
    """Resolve LLM-extracted entity strings against `stocks` +
    `company_overview` in ClickHouse.

    Match rules (per entity):
      • exact ticker match (case-insensitive on `stocks.ticker`), OR
      • case-insensitive substring match on `stocks.organ_name`.

    Returns:
        (resolved, unresolved, ch_available)
        resolved      — list of {symbol, organ_name, icb_name3, icb_name2}
        unresolved    — input strings that did not match anything
        ch_available  — False when ClickHouse isn't configured or query
                        failed; in that case caller should treat the
                        scope as "couldn't verify, assume valid" to
                        avoid blocking the user on infra gaps.
    """
    if not entities:
        return [], [], True

    if not is_enabled():
        return [], list(entities), False

    cleaned = _sanitize_entities(entities)
    if not cleaned:
        return [], list(entities), True

    or_clauses = []
    for e in cleaned:
        q = _ch_quote(e)
        or_clauses.append(
            f"(upper(s.ticker) = upper({q}) OR "
            f"positionCaseInsensitive(coalesce(s.organ_name,''), {q}) > 0)"
        )
    where_clause = " OR ".join(or_clauses)

    # Fixed internal SQL with column aliases — JOIN here is safe (no
    # collision) because we control both sides. The "no JOIN" rule
    # applies to LLM-generated SQL via the public tool surface, not
    # to internal helpers like this one.
    sql = (
        "SELECT s.ticker AS symbol, "
        "       coalesce(s.organ_name,'') AS organ_name, "
        "       coalesce(co.icb_name3,'') AS icb_name3, "
        "       coalesce(co.icb_name2,'') AS icb_name2 "
        "FROM stocks s "
        "LEFT JOIN company_overview co ON s.ticker = co.symbol "
        f"WHERE {where_clause} "
        "LIMIT 20"
    )

    try:
        result = await run_sql(sql)
    except Exception as e:
        logger.warning("company verify query crashed: %s; treating as unavailable", e)
        return [], list(entities), False

    if isinstance(result, dict) and result.get("error"):
        logger.warning("company verify SQL rejected: %s", result.get("error"))
        return [], list(entities), False

    columns = result.get("columns", []) if isinstance(result, dict) else []
    rows = result.get("rows", []) if isinstance(result, dict) else []
    resolved: list[dict] = [dict(zip(columns, r)) for r in rows]

    resolved_tickers = {(row.get("symbol") or "").upper() for row in resolved}
    resolved_names = [(row.get("organ_name") or "").lower() for row in resolved]

    unresolved: list[str] = []
    for e in cleaned:
        e_upper = e.upper()
        e_lower = e.lower()
        if e_upper in resolved_tickers:
            continue
        if any(n and (e_lower in n or n in e_lower) for n in resolved_names):
            continue
        unresolved.append(e)

    return resolved, unresolved, True


async def lookup_company(query: str) -> dict[str, Any]:
    """Look up a company by ticker or name fragment.

    Wraps `verify_company_entities([query])` into a single-entity tool
    suitable for ReAct agents — returns a dict the LLM can read directly:

        {
          "query": "<input>",
          "ch_available": true,
          "matches": [{"symbol", "organ_name", "icb_name3", "icb_name2"}, ...],
          "match_count": int
        }

    If ClickHouse isn't configured, `ch_available=false`. If no match,
    `matches=[]` and the caller (rewriter) should ask the user to
    clarify which company they meant.
    """
    if not isinstance(query, str) or not query.strip():
        return {"error": "lookup_company requires a non-empty 'query' string"}
    resolved, _unresolved, ch_avail = await verify_company_entities([query])
    return {
        "query": query.strip(),
        "ch_available": ch_avail,
        "matches": resolved,
        "match_count": len(resolved),
    }


LOOKUP_COMPANY_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "lookup_company",
        "description": (
            "Verify a company exists in the OLAP database (tables: "
            "stocks + company_overview). Pass a ticker (e.g. 'VNM') or a "
            "name fragment ('Vinamilk', 'Hoa Phat'). Returns canonical "
            "matches with symbol + organ_name + ICB sector. Use this BEFORE "
            "deciding scope_type='company' so you don't ask the agent to "
            "answer about a non-existent ticker."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Ticker or company name fragment to resolve.",
                },
            },
            "required": ["query"],
        },
    },
}
