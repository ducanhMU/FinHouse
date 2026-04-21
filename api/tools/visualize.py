"""
FinHouse — visualize tool (server-side chart rendering)

Takes tabular data + chart specification, renders a PNG via matplotlib,
uploads to MinIO, returns a presigned URL the UI can display.

Why server-side:
  • No frontend JS dependencies — UI just shows an <img>
  • LLM doesn't need to generate valid Vega-Lite grammar
  • Images are cacheable and shareable

Flow:
  LLM calls visualize(data_rows, mark, x_field, y_field, ...) →
  matplotlib renders → PNG bytes → MinIO upload → presigned URL →
  returned to LLM which cites it in the chat response.
"""

import io
import logging
import uuid
from typing import Any, Optional

import matplotlib
matplotlib.use("Agg")  # headless backend — no GUI
import matplotlib.pyplot as plt

from config import get_settings

settings = get_settings()
logger = logging.getLogger("finhouse.tools.visualize")

# Valid chart types mapped to matplotlib plotting functions
SUPPORTED_MARKS = {"bar", "line", "scatter", "area", "pie", "hist"}

# Presigned URL expiry (seconds) — short-lived since charts are per-session
PRESIGN_EXPIRY = 3600  # 1 hour

# MinIO object prefix for chart images
CHART_PREFIX = "charts/"

matplotlib.rcParams['font.sans-serif'] = ['Noto Sans', 'DejaVu Sans']
matplotlib.rcParams['axes.unicode_minus'] = False


def _get_minio_client():
    """
    Lazy-import Minio client to avoid importing at module load
    (makes API startup faster when this tool isn't used).
    """
    from minio import Minio
    return Minio(
        f"{settings.MINIO_HOST}:{settings.MINIO_PORT}",
        access_key=settings.MINIO_ROOT_USER,
        secret_key=settings.MINIO_ROOT_PASSWORD,
        secure=False,
    )


def _render_chart(
    data_rows: list[dict],
    mark: str,
    x_field: str,
    y_field: str,
    color_field: Optional[str] = None,
    title: Optional[str] = None,
) -> bytes:
    """Render a matplotlib chart to PNG bytes."""
    if not data_rows:
        raise ValueError("no data to visualize")
    if mark not in SUPPORTED_MARKS:
        raise ValueError(f"mark must be one of {SUPPORTED_MARKS}")

    # Extract columns
    x_vals = [row.get(x_field) for row in data_rows]
    y_vals = [row.get(y_field) for row in data_rows] if y_field else None

    # Clean None values (matplotlib dislikes them)
    if y_vals and any(v is None for v in y_vals):
        cleaned = [(x, y) for x, y in zip(x_vals, y_vals) if y is not None]
        if not cleaned:
            raise ValueError(f"all values in y_field '{y_field}' are null")
        x_vals, y_vals = zip(*cleaned)
        x_vals, y_vals = list(x_vals), list(y_vals)

    # Coerce y to float where possible
    if y_vals:
        try:
            y_vals = [float(v) for v in y_vals]
        except (ValueError, TypeError) as e:
            raise ValueError(f"y_field '{y_field}' contains non-numeric values: {e}")

    fig, ax = plt.subplots(figsize=(10, 6), dpi=100)

    if mark == "bar":
        ax.bar(range(len(x_vals)), y_vals)
        ax.set_xticks(range(len(x_vals)))
        ax.set_xticklabels([str(x) for x in x_vals], rotation=45, ha="right")
    elif mark == "line":
        ax.plot(range(len(x_vals)), y_vals, marker="o")
        ax.set_xticks(range(len(x_vals)))
        ax.set_xticklabels([str(x) for x in x_vals], rotation=45, ha="right")
    elif mark == "scatter":
        # For scatter, try numeric x too
        try:
            x_numeric = [float(v) for v in x_vals]
            ax.scatter(x_numeric, y_vals)
        except (ValueError, TypeError):
            ax.scatter(range(len(x_vals)), y_vals)
            ax.set_xticks(range(len(x_vals)))
            ax.set_xticklabels([str(x) for x in x_vals], rotation=45, ha="right")
    elif mark == "area":
        ax.fill_between(range(len(x_vals)), y_vals, alpha=0.4)
        ax.plot(range(len(x_vals)), y_vals)
        ax.set_xticks(range(len(x_vals)))
        ax.set_xticklabels([str(x) for x in x_vals], rotation=45, ha="right")
    elif mark == "pie":
        # For pie: x_vals are labels, y_vals are sizes
        ax.pie(y_vals, labels=[str(x) for x in x_vals], autopct="%1.1f%%")
        ax.axis("equal")
    elif mark == "hist":
        # Histogram uses y_vals only (or x_vals if no y given)
        data = y_vals if y_vals else [float(v) for v in x_vals if v is not None]
        ax.hist(data, bins=min(30, max(5, len(data) // 4)))

    ax.set_xlabel(x_field)
    if y_field and mark not in ("pie", "hist"):
        ax.set_ylabel(y_field)
    if title:
        ax.set_title(title)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


async def build_chart(
    data_rows: list[dict],
    mark: str,
    x_field: str,
    y_field: str,
    color_field: Optional[str] = None,
    title: Optional[str] = None,
) -> dict[str, Any]:
    """
    Main entry point called by the tool dispatcher.
    Returns {url, expires_in, mark, title} or {error}.
    """
    try:
        png_bytes = _render_chart(
            data_rows=data_rows,
            mark=mark,
            x_field=x_field,
            y_field=y_field,
            color_field=color_field,
            title=title,
        )
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        logger.exception("chart rendering failed")
        return {"error": f"render error: {type(e).__name__}: {e}"}

    # Upload to MinIO
    try:
        client = _get_minio_client()
        # Ensure bucket exists (the main bucket created by minio-init)
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

        # Generate presigned URL (short-lived read link)
        from datetime import timedelta
        url = client.presigned_get_object(
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
        "mark": mark,
        "title": title or "",
        "object_name": object_name,
    }


VISUALIZE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "visualize",
        "description": (
            "Render a chart from tabular rows and return an image URL. "
            "Call AFTER database_query to visualize query results. "
            "Supports bar, line, scatter, area, pie, hist. Cite the returned URL "
            "in your response using markdown: ![chart](URL)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "data_rows": {
                    "type": "array",
                    "description": (
                        "Row data. Each row is an object. Usually taken from "
                        "a previous database_query result's 'rows' converted to dicts."
                    ),
                    "items": {"type": "object"},
                },
                "mark": {
                    "type": "string",
                    "enum": list(SUPPORTED_MARKS),
                    "description": "Chart type",
                },
                "x_field": {
                    "type": "string",
                    "description": "Key in each row for x-axis / labels",
                },
                "y_field": {
                    "type": "string",
                    "description": "Key in each row for y-axis / sizes. For 'hist' can be same as x_field",
                },
                "color_field": {
                    "type": "string",
                    "description": "Optional: key for color grouping",
                },
                "title": {
                    "type": "string",
                    "description": "Chart title shown above the plot",
                },
            },
            "required": ["data_rows", "mark", "x_field", "y_field"],
        },
    },
}
