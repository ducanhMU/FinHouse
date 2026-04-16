"""
FinHouse — File Ingest Service
Parse documents → chunk → embed via BGE-M3 → upsert into Milvus.

Changelog (enhanced):
- MinIO: singleton client, download wrapped in try/finally for conn release
- Parsing: per-page error isolation in PDF, table extraction in DOCX
- Chunking: infinite-loop guard, max-chunk cap, dedup identical chunks
- Embedding: single httpx client across batches, exponential-backoff retry
- Milvus: thread-safe init via lock, reconnect on stale conn, batched insert,
  delete-before-upsert for idempotent re-ingest, safe collection.load()
- Reranker: filters empty-text chunks, caps per-document length
- Retrieval: pre-filter low-relevance candidates before reranking
- Ingest: file-size guard, capped error message length
"""

import io
import os
import hashlib
import logging
import threading
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

import httpx
from minio import Minio

from config import get_settings

settings = get_settings()
logger = logging.getLogger("finhouse.ingest")

# ── Supported formats ───────────────────────────────────────
SUPPORTED_EXTENSIONS = {"pdf", "md", "txt", "docx"}

# ── Limits ──────────────────────────────────────────────────
MAX_FILE_SIZE_MB = 100          # reject files larger than this
MAX_CHUNKS_PER_FILE = 2000      # safety cap

# ── Chunking config ─────────────────────────────────────────
CHUNK_CHARS = 1500              # ~512 tokens per chunk
CHUNK_OVERLAP = 200             # overlap in chars
MIN_CHUNK_LENGTH = 50           # discard chunks shorter than this


# ════════════════════════════════════════════════════════════
# MinIO helpers
# ════════════════════════════════════════════════════════════

_minio_client: Optional[Minio] = None


def get_minio_client() -> Minio:
    """Return a reusable MinIO client (connection pooled internally by urllib3)."""
    global _minio_client
    if _minio_client is None:
        _minio_client = Minio(
            f"{settings.MINIO_HOST}:{settings.MINIO_PORT}",
            access_key=settings.MINIO_ROOT_USER,
            secret_key=settings.MINIO_ROOT_PASSWORD,
            secure=False,
        )
    return _minio_client


def ensure_bucket(client: Minio):
    try:
        if not client.bucket_exists(settings.MINIO_BUCKET):
            client.make_bucket(settings.MINIO_BUCKET)
    except Exception as e:
        logger.warning(f"Bucket check/create failed (may already exist): {e}")


def upload_to_minio(
    content: bytes,
    object_name: str,
    content_type: str = "application/octet-stream",
):
    client = get_minio_client()
    ensure_bucket(client)
    client.put_object(
        settings.MINIO_BUCKET,
        object_name,
        io.BytesIO(content),
        len(content),
        content_type=content_type,
    )


def download_from_minio(object_name: str) -> bytes:
    """Download with proper resource cleanup even on read errors."""
    client = get_minio_client()
    resp = None
    try:
        resp = client.get_object(settings.MINIO_BUCKET, object_name)
        data = resp.read()
        return data
    finally:
        if resp is not None:
            resp.close()
            resp.release_conn()


# ════════════════════════════════════════════════════════════
# Document Parsing
# ════════════════════════════════════════════════════════════

def parse_txt(content: bytes) -> str:
    """Parse plain text / markdown with multi-encoding fallback."""
    for enc in ("utf-8", "utf-8-sig", "latin-1", "cp1252"):
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def parse_pdf(content: bytes) -> str:
    """Parse PDF with per-page error isolation and dual-library fallback."""
    text_parts = []

    # Attempt 1: pdfplumber
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for i, page in enumerate(pdf.pages):
                try:
                    page_text = page.extract_text()
                    if page_text:
                        text_parts.append(page_text)
                except Exception as page_err:
                    logger.warning(f"pdfplumber: page {i} failed: {page_err}")
    except Exception as e:
        logger.warning(f"pdfplumber failed entirely, trying pypdf: {e}")
        text_parts = []

    # Attempt 2: pypdf (only if pdfplumber extracted nothing)
    if not text_parts:
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(content))
            for i, page in enumerate(reader.pages):
                try:
                    page_text = page.extract_text()
                    if page_text:
                        text_parts.append(page_text)
                except Exception as page_err:
                    logger.warning(f"pypdf: page {i} failed: {page_err}")
        except Exception as e2:
            logger.error(f"Both PDF parsers failed: {e2}")
            return ""

    return "\n\n".join(text_parts)


def parse_docx(content: bytes) -> str:
    """Parse DOCX — extracts paragraphs AND table content."""
    try:
        from docx import Document
        doc = Document(io.BytesIO(content))

        parts = []
        for p in doc.paragraphs:
            text = p.text.strip()
            if text:
                parts.append(text)

        # Tables (often contain structured data crucial for RAG)
        for table in doc.tables:
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells if c.text.strip()]
                if cells:
                    parts.append(" | ".join(cells))

        return "\n\n".join(parts)
    except Exception as e:
        logger.error(f"DOCX parse failed: {e}")
        return ""


def parse_document(content: bytes, file_type: str) -> str:
    """Route to the correct parser based on file extension."""
    file_type = file_type.lower().strip(".")
    if file_type in ("txt", "md"):
        return parse_txt(content)
    elif file_type == "pdf":
        return parse_pdf(content)
    elif file_type == "docx":
        return parse_docx(content)
    else:
        return ""


# ════════════════════════════════════════════════════════════
# Text Chunking
# ════════════════════════════════════════════════════════════

def chunk_text(
    text: str,
    chunk_size: int = CHUNK_CHARS,
    overlap: int = CHUNK_OVERLAP,
) -> list[str]:
    """
    Split text into overlapping chunks on paragraph/sentence boundaries.
    Guards against: infinite loops, excessive chunk counts, duplicate chunks.
    """
    if not text or not text.strip():
        return []

    text = text.strip()

    if len(text) <= chunk_size:
        return [text]

    chunks = []
    paragraphs = text.split("\n\n")

    current_chunk = ""
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        if current_chunk and len(current_chunk) + len(para) + 2 > chunk_size:
            chunks.append(current_chunk.strip())
            if overlap > 0 and len(current_chunk) > overlap:
                current_chunk = current_chunk[-overlap:] + "\n\n" + para
            else:
                current_chunk = para
        else:
            current_chunk = (current_chunk + "\n\n" + para) if current_chunk else para

        # Force-split oversized chunks — hard cap prevents infinite loops
        max_force_splits = 50
        split_count = 0
        while len(current_chunk) > chunk_size * 1.5 and split_count < max_force_splits:
            split_count += 1
            prev_len = len(current_chunk)

            split_at = chunk_size
            for sep in (". ", "! ", "? ", "\n", "; ", ", "):
                idx = current_chunk.rfind(sep, 0, chunk_size + 50)
                if idx > chunk_size * 0.5:
                    split_at = idx + len(sep)
                    break

            # Guarantee forward progress
            if split_at <= 0:
                split_at = chunk_size

            chunks.append(current_chunk[:split_at].strip())
            remainder = current_chunk[split_at:]

            if overlap > 0:
                overlap_start = max(0, split_at - overlap)
                tail = current_chunk[overlap_start:split_at]
                current_chunk = tail + remainder
            else:
                current_chunk = remainder

            # If we didn't actually shrink, force-break to avoid infinite loop
            if len(current_chunk) >= prev_len:
                logger.warning("Force-split not making progress, breaking out")
                break

        if len(chunks) >= MAX_CHUNKS_PER_FILE:
            logger.warning(f"Hit max chunk cap ({MAX_CHUNKS_PER_FILE}), truncating")
            break

    if current_chunk.strip():
        chunks.append(current_chunk.strip())

    # Filter tiny chunks + deduplicate
    seen = set()
    unique_chunks = []
    for c in chunks:
        if len(c) < MIN_CHUNK_LENGTH:
            continue
        h = hashlib.md5(c.encode()).hexdigest()
        if h not in seen:
            seen.add(h)
            unique_chunks.append(c)

    return unique_chunks[:MAX_CHUNKS_PER_FILE]


# ════════════════════════════════════════════════════════════
# Embedding Client (calls BGE-M3 service)
# ════════════════════════════════════════════════════════════

# Singleton HTTP clients — keep TCP connection pool alive across calls.
# Reduces latency significantly when ingesting many files (avoids
# repeated TCP handshake + SSL setup per batch).
_embed_client: Optional[httpx.AsyncClient] = None
_rerank_client: Optional[httpx.AsyncClient] = None


def get_embed_client() -> httpx.AsyncClient:
    """Return a reusable httpx client for the embed service."""
    global _embed_client
    if _embed_client is None or _embed_client.is_closed:
        _embed_client = httpx.AsyncClient(
            timeout=httpx.Timeout(180.0, connect=15.0),
            limits=httpx.Limits(
                max_keepalive_connections=10,
                max_connections=20,
                keepalive_expiry=30.0,
            ),
        )
    return _embed_client


def get_rerank_client() -> httpx.AsyncClient:
    """Return a reusable httpx client for the rerank service."""
    global _rerank_client
    if _rerank_client is None or _rerank_client.is_closed:
        _rerank_client = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=10.0),
            limits=httpx.Limits(
                max_keepalive_connections=5,
                max_connections=10,
                keepalive_expiry=30.0,
            ),
        )
    return _rerank_client


async def close_http_clients():
    """Close singleton clients cleanly on app shutdown."""
    global _embed_client, _rerank_client
    if _embed_client is not None and not _embed_client.is_closed:
        await _embed_client.aclose()
        _embed_client = None
    if _rerank_client is not None and not _rerank_client.is_closed:
        await _rerank_client.aclose()
        _rerank_client = None


async def embed_texts(
    texts: list[str],
    batch_size: int = 32,
    max_retries: int = 2,
) -> list[list[float]]:
    """
    Call BGE-M3 embedding service in batches.
    Uses singleton httpx client for connection reuse across the whole app.
    """
    if not texts:
        return []

    all_embeddings = []
    client = get_embed_client()

    for batch_idx, i in enumerate(range(0, len(texts), batch_size)):
        batch = texts[i : i + batch_size]

        last_err = None
        for attempt in range(max_retries + 1):
            try:
                resp = await client.post(
                    f"{settings.EMBED_HOST}/embed",
                    json={"texts": batch},
                )
                resp.raise_for_status()
                data = resp.json()

                batch_embs = data.get("embeddings", [])
                if len(batch_embs) != len(batch):
                    raise ValueError(
                        f"Embedding count mismatch: sent {len(batch)}, "
                        f"got {len(batch_embs)}"
                    )
                all_embeddings.extend(batch_embs)
                break  # success

            except (
                httpx.HTTPStatusError,
                httpx.ConnectError,
                httpx.ReadTimeout,
                httpx.RemoteProtocolError,
                ValueError,
            ) as e:
                last_err = e
                if attempt < max_retries:
                    import asyncio
                    wait = 2 ** attempt
                    logger.warning(
                        f"Embed batch {batch_idx} attempt {attempt+1} "
                        f"failed: {e}, retrying in {wait}s"
                    )
                    await asyncio.sleep(wait)
                else:
                    raise RuntimeError(
                        f"Embedding failed after {max_retries+1} attempts "
                        f"on batch {batch_idx}: {last_err}"
                    ) from last_err

    return all_embeddings


async def embed_query(query: str) -> list[float]:
    """Embed a single query string."""
    result = await embed_texts([query])
    return result[0] if result else []


# ════════════════════════════════════════════════════════════
# Milvus Client
# ════════════════════════════════════════════════════════════

COLLECTION_NAME = "finhouse_chunks"
EMBEDDING_DIM = 1024  # BGE-M3 output dimension

_milvus_initialized = False
_milvus_lock = threading.Lock()


def _get_milvus_connection():
    """
    Thread-safe Milvus connection + collection init.
    Reconnects if existing connection is stale.
    """
    global _milvus_initialized

    from pymilvus import (
        connections, Collection, FieldSchema, CollectionSchema,
        DataType, utility,
    )

    # Always (re-)connect — pymilvus handles pooling internally,
    # and connect() is idempotent for the same alias
    try:
        connections.connect(
            alias="default",
            host=settings.MILVUS_HOST,
            port=str(settings.MILVUS_PORT),
        )
    except Exception as e:
        logger.error(f"Milvus connection failed: {e}")
        raise

    with _milvus_lock:
        if not _milvus_initialized:
            if not utility.has_collection(COLLECTION_NAME):
                fields = [
                    FieldSchema(
                        name="id", dtype=DataType.VARCHAR,
                        is_primary=True, max_length=128,
                    ),
                    FieldSchema(name="file_id", dtype=DataType.VARCHAR, max_length=64),
                    FieldSchema(name="project_id", dtype=DataType.INT64),
                    FieldSchema(name="file_name", dtype=DataType.VARCHAR, max_length=512),
                    FieldSchema(name="chunk_index", dtype=DataType.INT64),
                    FieldSchema(name="text", dtype=DataType.VARCHAR, max_length=8192),
                    FieldSchema(
                        name="embedding", dtype=DataType.FLOAT_VECTOR,
                        dim=EMBEDDING_DIM,
                    ),
                ]
                schema = CollectionSchema(
                    fields=fields, description="FinHouse document chunks"
                )
                collection = Collection(name=COLLECTION_NAME, schema=schema)
                collection.create_index(
                    field_name="embedding",
                    index_params={
                        "metric_type": "COSINE",
                        "index_type": "IVF_FLAT",
                        "params": {"nlist": 128},
                    },
                )
                logger.info(f"Created Milvus collection: {COLLECTION_NAME}")
            _milvus_initialized = True


def upsert_chunks(
    file_id: str,
    project_id: int,
    file_name: str,
    chunks: list[str],
    embeddings: list[list[float]],
):
    """Insert chunks in batches to avoid Milvus payload size limits."""
    from pymilvus import Collection

    if not chunks:
        return

    _get_milvus_connection()
    collection = Collection(COLLECTION_NAME)

    INSERT_BATCH = 500
    total_inserted = 0

    for start in range(0, len(chunks), INSERT_BATCH):
        end = min(start + INSERT_BATCH, len(chunks))
        batch_chunks = chunks[start:end]
        batch_embs = embeddings[start:end]

        ids = [f"{file_id}_{i}" for i in range(start, end)]
        file_ids = [str(file_id)] * len(batch_chunks)
        project_ids = [int(project_id)] * len(batch_chunks)
        file_names = [file_name[:512]] * len(batch_chunks)
        chunk_indices = list(range(start, end))
        texts = [c[:8000] for c in batch_chunks]

        collection.insert([
            ids, file_ids, project_ids, file_names,
            chunk_indices, texts, batch_embs,
        ])
        total_inserted += len(batch_chunks)

    collection.flush()
    logger.info(
        f"Inserted {total_inserted} chunks for {file_name} (file_id={file_id})"
    )


def delete_file_chunks(file_id: str):
    """Remove all chunks for a file from Milvus. Non-fatal on error."""
    from pymilvus import Collection

    try:
        _get_milvus_connection()
        collection = Collection(COLLECTION_NAME)
        collection.load()
        collection.delete(f'file_id == "{file_id}"')
        collection.flush()
        logger.info(f"Deleted chunks for file_id={file_id}")
    except Exception as e:
        logger.warning(f"delete_file_chunks({file_id}) failed (non-fatal): {e}")


async def search_chunks(
    query_embedding: list[float],
    project_id: int,
    top_k: int = 20,
) -> list[dict]:
    """Search Milvus for similar chunks scoped to a project."""
    from pymilvus import Collection

    _get_milvus_connection()
    collection = Collection(COLLECTION_NAME)

    try:
        collection.load()
    except Exception as e:
        logger.debug(f"collection.load() note (may already be loaded): {e}")

    results = collection.search(
        data=[query_embedding],
        anns_field="embedding",
        param={"metric_type": "COSINE", "params": {"nprobe": 16}},
        limit=top_k,
        expr=f"project_id == {project_id}",
        output_fields=["file_id", "file_name", "chunk_index", "text"],
    )

    hits = []
    for hit_list in results:
        for hit in hit_list:
            hits.append({
                "id": hit.id,
                "score": float(hit.score),
                "file_id": hit.entity.get("file_id"),
                "file_name": hit.entity.get("file_name"),
                "chunk_index": hit.entity.get("chunk_index"),
                "text": hit.entity.get("text"),
            })
    return hits


# ════════════════════════════════════════════════════════════
# Reranker Client
# ════════════════════════════════════════════════════════════

async def rerank_chunks(
    query: str, chunks: list[dict], top_n: int = 5,
) -> list[dict]:
    """Rerank chunks via BGE reranker. Filters empty texts, caps doc length."""
    if not chunks:
        return []

    # Drop chunks with empty text (would break reranker)
    valid_chunks = [c for c in chunks if c.get("text", "").strip()]
    if not valid_chunks:
        return []

    # Cap per-document length to avoid reranker OOM
    documents = [c["text"][:2000] for c in valid_chunks]

    try:
        client = get_rerank_client()
        resp = await client.post(
            f"{settings.RERANK_HOST}/rerank",
            json={
                "query": query,
                "documents": documents,
                "top_n": top_n,
            },
        )
        resp.raise_for_status()
        data = resp.json()

        reranked = []
        for r in data.get("results", []):
            idx = r["index"]
            if 0 <= idx < len(valid_chunks):
                chunk = valid_chunks[idx].copy()
                chunk["rerank_score"] = float(r["score"])
                reranked.append(chunk)
        return reranked

    except Exception as e:
        logger.warning(f"Reranker failed, falling back to raw order: {e}")
        return valid_chunks[:top_n]


# ════════════════════════════════════════════════════════════
# Full Ingest Pipeline
# ════════════════════════════════════════════════════════════

async def ingest_file(
    file_id: str,
    file_content: bytes,
    file_name: str,
    file_type: str,
    project_id: int,
    update_status_callback=None,
) -> dict:
    """
    Full ingest pipeline: parse → chunk → embed → Milvus upsert.
    Returns {status, chunks_count, error?}.
    """
    result = {"status": "ready", "chunks_count": 0, "error": None}

    # 1. Validate format
    ft = file_type.lower().strip(".")
    if ft not in SUPPORTED_EXTENSIONS:
        result["status"] = "failed"
        result["error"] = f"Unsupported file type: {file_type}"
        return result

    # 2. Size guard
    size_mb = len(file_content) / (1024 * 1024)
    if size_mb > MAX_FILE_SIZE_MB:
        result["status"] = "failed"
        result["error"] = (
            f"File too large: {size_mb:.1f} MB (max {MAX_FILE_SIZE_MB} MB)"
        )
        return result

    try:
        # 3. Parse
        logger.info(f"Parsing {file_name} ({ft}, {size_mb:.1f} MB)...")
        text = parse_document(file_content, ft)
        if not text or len(text.strip()) < 10:
            result["status"] = "failed"
            result["error"] = "No extractable text found in document"
            return result

        logger.info(f"Extracted {len(text)} chars from {file_name}")

        # 4. Chunk
        chunks = chunk_text(text)
        if not chunks:
            result["status"] = "failed"
            result["error"] = "Document produced no usable text chunks"
            return result

        logger.info(f"Split into {len(chunks)} chunks")

        # 5. Embed
        logger.info(f"Embedding {len(chunks)} chunks...")
        embeddings = await embed_texts(chunks)
        if len(embeddings) != len(chunks):
            result["status"] = "failed"
            result["error"] = (
                f"Embedding count mismatch: {len(chunks)} chunks "
                f"but {len(embeddings)} embeddings"
            )
            return result

        # 6. Delete old chunks (idempotent re-ingest)
        delete_file_chunks(file_id)

        # 7. Upsert into Milvus
        logger.info("Upserting into Milvus...")
        upsert_chunks(
            file_id=file_id,
            project_id=project_id,
            file_name=file_name,
            chunks=chunks,
            embeddings=embeddings,
        )

        result["chunks_count"] = len(chunks)
        logger.info(f"✅ Ingest complete: {file_name} → {len(chunks)} chunks")

    except Exception as e:
        logger.error(f"Ingest failed for {file_name}: {e}", exc_info=True)
        result["status"] = "failed"
        result["error"] = str(e)[:500]

    return result


# ════════════════════════════════════════════════════════════
# RAG Retrieval (used by chat router)
# ════════════════════════════════════════════════════════════

MIN_RELEVANCE_SCORE = 0.3


async def retrieve_context(
    query: str,
    project_id: int,
    top_k: int = 20,
    top_n_rerank: int = 5,
) -> list[dict]:
    """
    Full RAG retrieval: embed query → Milvus search → rerank → top chunks.
    Pre-filters below MIN_RELEVANCE_SCORE to avoid polluting context.
    """
    try:
        query_emb = await embed_query(query)
        if not query_emb:
            return []

        candidates = await search_chunks(query_emb, project_id, top_k=top_k)
        if not candidates:
            return []

        # Drop very low relevance before expensive reranking
        relevant = [c for c in candidates if c.get("score", 0) >= MIN_RELEVANCE_SCORE]
        if not relevant:
            logger.info(
                f"All {len(candidates)} candidates below relevance "
                f"threshold ({MIN_RELEVANCE_SCORE})"
            )
            return []

        reranked = await rerank_chunks(query, relevant, top_n=top_n_rerank)
        return reranked

    except Exception as e:
        logger.warning(f"RAG retrieval failed: {e}")
        return []