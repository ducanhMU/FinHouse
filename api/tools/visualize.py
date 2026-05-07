"""
FinHouse — visualize tools (server-side chart rendering)

Three narrow tools, one per chart type. Each fetches data from ONE
OLAP table via select_rows(), renders a PNG with matplotlib, uploads
to MinIO, and returns a presigned URL. The LLM picks which tool fits
the question and only fills typed args — it doesn't pass data rows
around or pick a generic `mark` enum.

Public tools (exposed to the LLM):
  • bar(table, x_column, y_columns, filters?, order_by?, limit?, use_final?, title?)
  • line(table, x_column, y_columns, filters?, order_by?, limit?, use_final?, title?)
  • pie(table, label_column, value_column, filters?, order_by?, limit?, use_final?, title?)

bar / line accept a list of y columns — multiple bars per group, or
multiple lines on the same axes. pie takes a single value column.

Out of scope for v1 (the agent should tell the user, not work around):
  • scatter, area, hist
  • aggregation inside the chart tool (use aggregate() first, then
    cite the numbers in text — we don't accept pre-fetched data_rows)
  • time bucketing (toStartOfMonth / GROUP BY etc. — same reason)
  • multi-table joins
"""

import io
import logging
import uuid
from typing import Any, Optional
from urllib.parse import urlparse

import matplotlib
matplotlib.use("Agg")  # headless backend — no GUI
import matplotlib.pyplot as plt

from config import get_settings
from tools.database_query import (
    describe_table as db_describe_table,
    list_tables as db_list_tables,
    select_rows,
)

settings = get_settings()
logger = logging.getLogger("finhouse.tools.visualize")

# Presigned URL expiry (seconds) — short-lived since charts are per-session
PRESIGN_EXPIRY = 3600  # 1 hour

# MinIO object prefix for chart images
CHART_PREFIX = "charts/"

matplotlib.rcParams['font.sans-serif'] = ['Noto Sans', 'DejaVu Sans']
matplotlib.rcParams['axes.unicode_minus'] = False


def _get_minio_client():
    """Lazy-import Minio to keep API startup fast when this tool isn't used."""
    from minio import Minio
    return Minio(
        f"{settings.MINIO_HOST}:{settings.MINIO_PORT}",
        access_key=settings.MINIO_ROOT_USER,
        secret_key=settings.MINIO_ROOT_PASSWORD,
        secure=False,
    )


def _get_minio_presign_client():
    """
    Client used only to sign URLs handed to the user's browser.

    `_get_minio_client()` targets the Docker-internal hostname so the API
    container can upload — but a presigned URL signed with that hostname
    is not reachable from outside the Docker network. When
    MINIO_PUBLIC_URL is set, we build a separate client whose endpoint
    matches the browser-reachable host so the signature lines up with
    the host the browser actually hits. Presigning is purely local URL
    construction (no network call), so this client never connects.
    """
    public_url = (settings.MINIO_PUBLIC_URL or "").strip()
    if not public_url:
        return _get_minio_client()
    from minio import Minio
    parsed = urlparse(public_url)
    host = parsed.hostname or ""
    port = parsed.port
    endpoint = f"{host}:{port}" if port else host
    return Minio(
        endpoint,
        access_key=settings.MINIO_ROOT_USER,
        secret_key=settings.MINIO_ROOT_PASSWORD,
        secure=parsed.scheme == "https",
    )


def _to_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _coerce_rows(raw_rows: list[Any], columns: list[str]) -> list[dict]:
    """
    select_rows returns {columns: [...], rows: [[...], ...]}. Convert to
    list of dicts so the renderer can address columns by name.
    """
    out = []
    for r in raw_rows:
        if isinstance(r, dict):
            out.append(r)
        elif isinstance(r, (list, tuple)):
            out.append({c: r[i] if i < len(r) else None for i, c in enumerate(columns)})
        else:
            out.append({})
    return out


_MISSING_TABLE_RE = (
    "UNKNOWN_TABLE",
    "Table ",
    "doesn't exist",
    "does not exist",
)
_MISSING_COLUMN_RE = (
    "UNKNOWN_IDENTIFIER",
    "Missing columns",
    "There's no column",
    "Unknown identifier",
)


async def _fetch(
    table: str,
    columns: list[str],
    filters: Optional[list[dict]],
    order_by: Optional[list[dict]],
    limit: int,
    use_final: bool,
) -> dict[str, Any]:
    """Fetch the data via select_rows.

    On ClickHouse "table does not exist" / "unknown column" errors,
    auto-attach the real table or column list so the ReAct loop can
    retry with the correct name on the next round instead of dying.
    """
    result = await select_rows(
        table=table,
        columns=columns,
        filters=filters,
        order_by=order_by,
        limit=limit,
        use_final=use_final,
    )
    if isinstance(result, dict) and result.get("error"):
        err = str(result["error"])
        hint = await _schema_hint_for_error(err, table)
        if hint:
            err = err + " | " + hint
        return {"error": err}
    cols = result.get("columns", []) if isinstance(result, dict) else []
    rows = result.get("rows", []) if isinstance(result, dict) else []
    return {"columns": cols, "rows": _coerce_rows(rows, cols)}


async def _schema_hint_for_error(err: str, table: str) -> str:
    """Build a short schema hint to append to a failed-fetch error.

    Designed so the LLM can self-correct on the next ReAct round:
      • Bad table → list real tables.
      • Bad column → list real columns of the (correct) table.
    """
    if any(s in err for s in _MISSING_TABLE_RE):
        try:
            tables_res = await db_list_tables()
            rows = tables_res.get("rows") or [] if isinstance(tables_res, dict) else []
            names = [r[0] for r in rows if r] if rows and isinstance(rows[0], (list, tuple)) else []
            if names:
                return (
                    f"available tables in OLAP DB: {', '.join(names)}. "
                    "Pick the correct one and retry — do NOT invent a table name."
                )
        except Exception:
            pass
        return (
            "table not in OLAP DB — call list_tables() first to see real "
            "names. Common tables: stocks, company_overview, balance_sheet, "
            "income_statement, cash_flow_statement, financial_ratios, "
            "shareholders, officers, news, events, stock_price_history."
        )
    if any(s in err for s in _MISSING_COLUMN_RE):
        try:
            desc = await db_describe_table(table)
            rows = desc.get("rows") or [] if isinstance(desc, dict) else []
            names = [r[0] for r in rows if r] if rows and isinstance(rows[0], (list, tuple)) else []
            if names:
                return (
                    f"columns in `{table}`: {', '.join(names)}. "
                    "Pick a column from this list and retry."
                )
        except Exception:
            pass
        return f"column not found — call describe_table('{table}') to see real columns."
    return ""


def _render_multi_series(
    rows: list[dict],
    x_column: str,
    y_columns: list[str],
    mark: str,
    title: Optional[str],
) -> bytes:
    """Render a bar (grouped) or line chart with one or more y series."""
    if mark not in {"bar", "line"}:
        raise ValueError(f"_render_multi_series got unsupported mark={mark!r}")
    if not rows:
        raise ValueError("no rows to plot")
    if not y_columns:
        raise ValueError("y_columns must contain at least one column")

    x_labels = [str(r.get(x_column, "")) for r in rows]
    n_groups = len(rows)

    # Collect numeric series and skip rows that are entirely null across all
    # series (rare but possible if select_rows returned NULLs).
    series: list[tuple[str, list[Optional[float]]]] = []
    for ycol in y_columns:
        series.append((ycol, [_to_float(r.get(ycol)) for r in rows]))

    keep = [
        i for i in range(n_groups)
        if any(vals[i] is not None for _, vals in series)
    ]
    if not keep:
        raise ValueError(
            f"all values are null across {y_columns} — nothing to plot"
        )
    x_labels = [x_labels[i] for i in keep]
    series = [(name, [vals[i] for i in keep]) for name, vals in series]
    n_groups = len(x_labels)

    fig, ax = plt.subplots(figsize=(10, 6), dpi=100)

    if mark == "bar":
        n_series = len(series)
        group_width = 0.8
        bar_width = group_width / n_series
        positions = list(range(n_groups))
        for i, (name, vals) in enumerate(series):
            offsets = [
                p - group_width / 2 + bar_width * (i + 0.5) for p in positions
            ]
            # matplotlib treats None as missing and warns; replace with NaN
            safe = [float("nan") if v is None else v for v in vals]
            ax.bar(offsets, safe, width=bar_width, label=name)
        ax.set_xticks(positions)
        ax.set_xticklabels(x_labels, rotation=45, ha="right")
    else:  # line
        positions = list(range(n_groups))
        for name, vals in series:
            safe = [float("nan") if v is None else v for v in vals]
            ax.plot(positions, safe, marker="o", label=name)
        ax.set_xticks(positions)
        ax.set_xticklabels(x_labels, rotation=45, ha="right")

    ax.set_xlabel(x_column)
    if len(series) == 1:
        ax.set_ylabel(series[0][0])
    if len(series) > 1:
        ax.legend()
    if title:
        ax.set_title(title)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def _render_pie(
    rows: list[dict],
    label_column: str,
    value_column: str,
    title: Optional[str],
) -> bytes:
    """Render a pie chart. Drops zero/null/negative slices."""
    if not rows:
        raise ValueError("no rows to plot")

    pairs = []
    for r in rows:
        label = r.get(label_column)
        val = _to_float(r.get(value_column))
        if val is None or val <= 0:
            continue
        pairs.append((str(label) if label is not None else "(null)", val))

    if not pairs:
        raise ValueError(
            f"no positive values in '{value_column}' to plot as pie slices"
        )

    labels, sizes = zip(*pairs)

    fig, ax = plt.subplots(figsize=(8, 8), dpi=100)
    ax.pie(sizes, labels=labels, autopct="%1.1f%%", startangle=90)
    ax.axis("equal")
    if title:
        ax.set_title(title)
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


async def _upload_png(png_bytes: bytes) -> dict[str, Any]:
    """Push bytes to MinIO and return the presigned URL payload."""
    try:
        client = _get_minio_client()
        bucket = settings.MINIO_BUCKET
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)

        object_name = f"{CHART_PREFIX}{uuid.uuid4().hex}.png"
        client.put_object(
            bucket_name=bucket,
            object_name=object_name,
            data=io.BytesIO(png_bytes),
            length=len(png_bytes),
            content_type="image/png",
        )

        from datetime import timedelta
        # Sign with the browser-reachable host so the URL we hand back
        # actually loads in the user's browser (the upload client points
        # at the Docker-internal hostname).
        url = _get_minio_presign_client().presigned_get_object(
            bucket_name=bucket,
            object_name=object_name,
            expires=timedelta(seconds=PRESIGN_EXPIRY),
        )
    except Exception as e:
        logger.exception("MinIO upload failed")
        return {"error": f"storage error: {e}"}

    return {
        "url": url,
        "expires_in_seconds": PRESIGN_EXPIRY,
        "object_name": object_name,
    }


# ── Public tool functions ───────────────────────────────────

async def bar(
    table: str,
    x_column: str,
    y_columns: list[str],
    filters: Optional[list[dict]] = None,
    order_by: Optional[list[dict]] = None,
    limit: int = 50,
    use_final: bool = True,
    title: Optional[str] = None,
) -> dict[str, Any]:
    """Render a bar chart from one OLAP table."""
    if not isinstance(y_columns, list) or not y_columns:
        return {"error": "y_columns must be a non-empty list"}
    fetch = await _fetch(
        table, [x_column, *y_columns], filters, order_by, limit, use_final,
    )
    if "error" in fetch:
        return fetch
    try:
        png = _render_multi_series(
            fetch["rows"], x_column, y_columns, "bar", title,
        )
    except ValueError as e:
        return {"error": str(e)}
    upload = await _upload_png(png)
    if "error" in upload:
        return upload
    return {**upload, "mark": "bar", "title": title or "", "row_count": len(fetch["rows"])}


async def line(
    table: str,
    x_column: str,
    y_columns: list[str],
    filters: Optional[list[dict]] = None,
    order_by: Optional[list[dict]] = None,
    limit: int = 50,
    use_final: bool = True,
    title: Optional[str] = None,
) -> dict[str, Any]:
    """Render a line chart from one OLAP table."""
    if not isinstance(y_columns, list) or not y_columns:
        return {"error": "y_columns must be a non-empty list"}
    fetch = await _fetch(
        table, [x_column, *y_columns], filters, order_by, limit, use_final,
    )
    if "error" in fetch:
        return fetch
    try:
        png = _render_multi_series(
            fetch["rows"], x_column, y_columns, "line", title,
        )
    except ValueError as e:
        return {"error": str(e)}
    upload = await _upload_png(png)
    if "error" in upload:
        return upload
    return {**upload, "mark": "line", "title": title or "", "row_count": len(fetch["rows"])}


async def pie(
    table: str,
    label_column: str,
    value_column: str,
    filters: Optional[list[dict]] = None,
    order_by: Optional[list[dict]] = None,
    limit: int = 10,
    use_final: bool = True,
    title: Optional[str] = None,
) -> dict[str, Any]:
    """Render a pie chart from one OLAP table."""
    fetch = await _fetch(
        table, [label_column, value_column], filters, order_by, limit, use_final,
    )
    if "error" in fetch:
        return fetch
    try:
        png = _render_pie(fetch["rows"], label_column, value_column, title)
    except ValueError as e:
        return {"error": str(e)}
    upload = await _upload_png(png)
    if "error" in upload:
        return upload
    return {**upload, "mark": "pie", "title": title or "", "row_count": len(fetch["rows"])}


async def chart_from_data(
    mark: str,
    x_labels: list[Any],
    y_series: list[dict],
    title: Optional[str] = None,
) -> dict[str, Any]:
    """Render a chart from inline data (no OLAP fetch).

    Use this when the data came from web_search (or anywhere outside
    OLAP) and you've parsed it into labels + values yourself. The
    end-goal is still a chart — this tool keeps that promise even when
    `bar`/`line`/`pie` can't (because they only read OLAP).

    Args:
      mark: "bar" | "line" | "pie".
      x_labels: list of category/time labels (one per data point).
        For pie, these are slice labels.
      y_series:
        - bar/line: list of {"name": str, "values": [number, ...]}
          where len(values) == len(x_labels). Multiple series → grouped
          bars / multi-line.
        - pie: list with ONE entry {"name": str, "values": [number, ...]}
          where len(values) == len(x_labels) (one positive number per
          slice — negatives/zero/null are dropped).
      title: chart title (Latin/Vietnamese only — no CJK).
    """
    mark = (mark or "").lower().strip()
    if mark not in {"bar", "line", "pie"}:
        return {"error": f"mark must be 'bar', 'line' or 'pie' — got {mark!r}"}
    if not isinstance(x_labels, list) or not x_labels:
        return {"error": "x_labels must be a non-empty list"}
    if not isinstance(y_series, list) or not y_series:
        return {"error": "y_series must be a non-empty list"}

    n = len(x_labels)
    norm_series: list[tuple[str, list[Optional[float]]]] = []
    for i, s in enumerate(y_series):
        if not isinstance(s, dict):
            return {"error": f"y_series[{i}] must be an object"}
        name = str(s.get("name") or f"series_{i+1}").strip() or f"series_{i+1}"
        vals = s.get("values")
        if not isinstance(vals, list):
            return {"error": f"y_series[{i}].values must be a list"}
        if len(vals) != n:
            return {
                "error": (
                    f"y_series[{i}].values length {len(vals)} != x_labels "
                    f"length {n}"
                )
            }
        norm_series.append((name, [_to_float(v) for v in vals]))

    if mark in {"bar", "line"}:
        rows = [{"_x": x_labels[i]} for i in range(n)]
        for name, vals in norm_series:
            for i in range(n):
                rows[i][name] = vals[i]
        try:
            png = _render_multi_series(
                rows, "_x", [n for n, _ in norm_series], mark, title,
            )
        except ValueError as e:
            return {"error": str(e)}
    else:  # pie
        if len(norm_series) != 1:
            return {"error": "pie requires exactly one entry in y_series"}
        _, vals = norm_series[0]
        rows = [{"label": x_labels[i], "value": vals[i]} for i in range(n)]
        try:
            png = _render_pie(rows, "label", "value", title)
        except ValueError as e:
            return {"error": str(e)}

    upload = await _upload_png(png)
    if "error" in upload:
        return upload
    return {
        **upload,
        "mark": mark,
        "title": title or "",
        "row_count": n,
        "source": "inline",
    }


# ── Tool schemas (for Ollama function calling) ──────────────

_FILTERS_SCHEMA = {
    "type": "array",
    "description": (
        "WHERE conditions ANDed together (same shape as select_rows). "
        "Each item: {column, op, value}. op ∈ =, !=, <, <=, >, >=, IN. "
        "For IN, value must be an array."
    ),
    "items": {
        "type": "object",
        "properties": {
            "column": {"type": "string"},
            "op": {
                "type": "string",
                "enum": ["=", "!=", "<", "<=", ">", ">=", "IN"],
            },
            "value": {},
        },
        "required": ["column", "value"],
    },
}

_ORDER_BY_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "column": {"type": "string"},
            "dir": {"type": "string", "enum": ["asc", "desc"]},
        },
        "required": ["column"],
    },
}


BAR_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "bar",
        "description": (
            "Render a BAR chart from ONE OLAP table. Use for comparing a "
            "metric across categories (companies, sectors, quarters). "
            "Pass multiple y_columns to draw grouped bars side-by-side. "
            "Returns a presigned image URL — embed it in your reply with "
            "markdown ![title](url)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "table": {"type": "string"},
                "x_column": {
                    "type": "string",
                    "description": "Column for category labels (ticker, year, etc).",
                },
                "y_columns": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string"},
                    "description": "Numeric columns for bar heights.",
                },
                "filters": _FILTERS_SCHEMA,
                "order_by": _ORDER_BY_SCHEMA,
                "limit": {"type": "integer", "minimum": 1, "default": 50},
                "use_final": {"type": "boolean", "default": True},
                "title": {"type": "string"},
            },
            "required": ["table", "x_column", "y_columns"],
        },
    },
}

LINE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "line",
        "description": (
            "Render a LINE chart from ONE OLAP table. Use for trends over "
            "an ordered axis (year, quarter, date). Pass multiple "
            "y_columns to draw multiple lines on the same axes. ALWAYS "
            "pass order_by on the time column (asc) — the tool does not "
            "auto-sort. Returns a presigned image URL."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "table": {"type": "string"},
                "x_column": {
                    "type": "string",
                    "description": "Time / ordinal column (year, time, period_label).",
                },
                "y_columns": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string"},
                    "description": "Numeric columns to plot as lines.",
                },
                "filters": _FILTERS_SCHEMA,
                "order_by": _ORDER_BY_SCHEMA,
                "limit": {"type": "integer", "minimum": 1, "default": 50},
                "use_final": {"type": "boolean", "default": True},
                "title": {"type": "string"},
            },
            "required": ["table", "x_column", "y_columns"],
        },
    },
}

PIE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "pie",
        "description": (
            "Render a PIE chart from ONE OLAP table. Use ONLY when "
            "value_column represents share-of-whole (percentages, "
            "shareholder ownership, segment mix). Negative or zero values "
            "are dropped. Returns a presigned image URL."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "table": {"type": "string"},
                "label_column": {
                    "type": "string",
                    "description": "Column for slice labels (share_holder, segment).",
                },
                "value_column": {
                    "type": "string",
                    "description": "Numeric column for slice sizes.",
                },
                "filters": _FILTERS_SCHEMA,
                "order_by": _ORDER_BY_SCHEMA,
                "limit": {"type": "integer", "minimum": 1, "default": 10},
                "use_final": {"type": "boolean", "default": True},
                "title": {"type": "string"},
            },
            "required": ["table", "label_column", "value_column"],
        },
    },
}

CHART_FROM_DATA_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "chart_from_data",
        "description": (
            "Render a chart from INLINE data (you supply labels + numbers "
            "directly). Use this AFTER web_search when OLAP doesn't have "
            "the data and you've parsed numbers from search results. The "
            "end goal is still a chart — this tool ensures we always "
            "deliver a PNG when a chart was asked for. Returns a "
            "presigned image URL like bar/line/pie."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "mark": {
                    "type": "string",
                    "enum": ["bar", "line", "pie"],
                    "description": "Chart type.",
                },
                "x_labels": {
                    "type": "array",
                    "items": {},
                    "minItems": 1,
                    "description": (
                        "Category/time labels for bar/line, or slice labels "
                        "for pie. Strings or numbers."
                    ),
                },
                "y_series": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "values": {
                                "type": "array",
                                "items": {"type": ["number", "null"]},
                            },
                        },
                        "required": ["name", "values"],
                    },
                    "description": (
                        "Numeric series. For bar/line: multiple entries → "
                        "grouped bars / multi-line. For pie: exactly one "
                        "entry; values length must equal x_labels length."
                    ),
                },
                "title": {"type": "string"},
            },
            "required": ["mark", "x_labels", "y_series"],
        },
    },
}


VISUALIZE_TOOL_SCHEMAS = [
    BAR_TOOL_SCHEMA,
    LINE_TOOL_SCHEMA,
    PIE_TOOL_SCHEMA,
    CHART_FROM_DATA_TOOL_SCHEMA,
]
