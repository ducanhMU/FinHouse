"""
FinHouse — File Ingest Service
Parse documents → chunk → embed via BGE-M3 → upsert into Milvus.
"""

import io
import os
import hashlib
import logging
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

# ── Chunking config ─────────────────────────────────────────
CHUNK_SIZE = 512       # target tokens (~chars/4 rough approx → use chars)
CHUNK_CHARS = 1500     # ~512 tokens
CHUNK_OVERLAP = 200    # overlap in chars


# ════════════════════════════════════════════════════════════
# MinIO helpers
# ════════════════════════════════════════════════════════════

def get_minio_client() -> Minio:
    return Minio(
        f"{settings.MINIO_HOST}:{settings.MINIO_PORT}",
        access_key=settings.MINIO_ROOT_USER,
        secret_key=settings.MINIO_ROOT_PASSWORD,
        secure=False,
    )


def ensure_bucket(client: Minio):
    if not client.bucket_exists(settings.MINIO_BUCKET):
        client.make_bucket(settings.MINIO_BUCKET)


def upload_to_minio(content: bytes, object_name: str, content_type: str = "application/octet-stream"):
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
    client = get_minio_client()
    resp = client.get_object(settings.MINIO_BUCKET, object_name)
    data = resp.read()
    resp.close()
    resp.release_conn()
    return data


# ════════════════════════════════════════════════════════════
# Document Parsing
# ════════════════════════════════════════════════════════════

def parse_txt(content: bytes) -> str:
    """Parse plain text / markdown."""
    for enc in ("utf-8", "utf-8-sig", "latin-1", "cp1252"):
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def parse_pdf(content: bytes) -> str:
    """Parse PDF using pdfplumber (fallback to pypdf)."""
    text_parts = []
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(page_text)
    except Exception as e:
        logger.warning(f"pdfplumber failed, trying pypdf: {e}")
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(content))
            for page in reader.pages:
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(page_text)
        except Exception as e2:
            logger.error(f"Both PDF parsers failed: {e2}")
            return ""
    return "\n\n".join(text_parts)


def parse_docx(content: bytes) -> str:
    """Parse DOCX using python-docx."""
    try:
        from docx import Document
        doc = Document(io.BytesIO(content))
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        return "\n\n".join(paragraphs)
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

def chunk_text(text: str, chunk_size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """
    Split text into overlapping chunks.
    Tries to split on paragraph/sentence boundaries.
    """
    if not text or not text.strip():
        return []

    # Normalize whitespace
    text = text.strip()

    # If text is small enough, return as single chunk
    if len(text) <= chunk_size:
        return [text]

    chunks = []
    # Split on double newlines first (paragraphs)
    paragraphs = text.split("\n\n")

    current_chunk = ""
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        # If adding this paragraph exceeds chunk size, save current and start new
        if current_chunk and len(current_chunk) + len(para) + 2 > chunk_size:
            chunks.append(current_chunk.strip())
            # Overlap: keep the tail of the current chunk
            if overlap > 0 and len(current_chunk) > overlap:
                current_chunk = current_chunk[-overlap:] + "\n\n" + para
            else:
                current_chunk = para
        else:
            if current_chunk:
                current_chunk += "\n\n" + para
            else:
                current_chunk = para

        # If a single paragraph is too long, force-split it
        while len(current_chunk) > chunk_size * 1.5:
            # Try to split on sentence boundary
            split_at = chunk_size
            for sep in (". ", "! ", "? ", "\n", "; ", ", "):
                idx = current_chunk.rfind(sep, 0, chunk_size + 50)
                if idx > chunk_size * 0.5:
                    split_at = idx + len(sep)
                    break

            chunks.append(current_chunk[:split_at].strip())
            tail = current_chunk[max(0, split_at - overlap):split_at]
            current_chunk = tail + current_chunk[split_at:]

    if current_chunk.strip():
        chunks.append(current_chunk.strip())

    # Filter out very small chunks
    chunks = [c for c in chunks if len(c) > 50]

    return chunks


# ════════════════════════════════════════════════════════════
# Embedding Client (calls BGE-M3 service)
# ════════════════════════════════════════════════════════════

async def embed_texts(texts: list[str]) -> list[list[float]]:
    """Call the BGE-M3 embedding microservice."""
    if not texts:
        return []

    # Batch in groups of 32 to avoid overloading
    all_embeddings = []
    batch_size = 32
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{settings.EMBED_HOST}/embed",
                json={"texts": batch},
            )
            resp.raise_for_status()
            data = resp.json()
            all_embeddings.extend(data["embeddings"])

    return all_embeddings


async def embed_query(query: str) -> list[float]:
    """Embed a single query string."""
    result = await embed_texts([query])
    return result[0] if result else []


# ════════════════════════════════════════════════════════════
# Milvus Client
# ════════════════════════════════════════════════════════════

COLLECTION_NAME = "finhouse_chunks"
EMBEDDING_DIM = 1024  # BGE-M3 dimension

_milvus_initialized = False


def _get_milvus_connection():
    """Ensure Milvus connection and collection exist."""
    global _milvus_initialized
    from pymilvus import connections, Collection, FieldSchema, CollectionSchema, DataType, utility

    connections.connect(
        alias="default",
        host=settings.MILVUS_HOST,
        port=str(settings.MILVUS_PORT),
    )

    if not _milvus_initialized:
        if not utility.has_collection(COLLECTION_NAME):
            fields = [
                FieldSchema(name="id", dtype=DataType.VARCHAR, is_primary=True, max_length=128),
                FieldSchema(name="file_id", dtype=DataType.VARCHAR, max_length=64),
                FieldSchema(name="project_id", dtype=DataType.INT64),
                FieldSchema(name="file_name", dtype=DataType.VARCHAR, max_length=512),
                FieldSchema(name="chunk_index", dtype=DataType.INT64),
                FieldSchema(name="text", dtype=DataType.VARCHAR, max_length=8192),
                FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=EMBEDDING_DIM),
            ]
            schema = CollectionSchema(fields=fields, description="FinHouse document chunks")
            collection = Collection(name=COLLECTION_NAME, schema=schema)
            # Create index on embedding field
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
    """Insert chunk embeddings into Milvus."""
    from pymilvus import Collection

    _get_milvus_connection()
    collection = Collection(COLLECTION_NAME)

    ids = [f"{file_id}_{i}" for i in range(len(chunks))]
    file_ids = [str(file_id)] * len(chunks)
    project_ids = [int(project_id)] * len(chunks)
    file_names = [file_name[:512]] * len(chunks)
    chunk_indices = list(range(len(chunks)))
    texts = [c[:8000] for c in chunks]  # Milvus VARCHAR limit

    collection.insert([
        ids, file_ids, project_ids, file_names, chunk_indices, texts, embeddings,
    ])
    collection.flush()
    logger.info(f"Inserted {len(chunks)} chunks for file {file_name} (file_id={file_id})")


def delete_file_chunks(file_id: str):
    """Remove all chunks for a file from Milvus."""
    from pymilvus import Collection

    _get_milvus_connection()
    collection = Collection(COLLECTION_NAME)
    collection.delete(f'file_id == "{file_id}"')
    collection.flush()
    logger.info(f"Deleted chunks for file_id={file_id}")


async def search_chunks(
    query_embedding: list[float],
    project_id: int,
    top_k: int = 20,
) -> list[dict]:
    """Search Milvus for similar chunks scoped to a project."""
    from pymilvus import Collection

    _get_milvus_connection()
    collection = Collection(COLLECTION_NAME)
    collection.load()

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
                "score": hit.score,
                "file_id": hit.entity.get("file_id"),
                "file_name": hit.entity.get("file_name"),
                "chunk_index": hit.entity.get("chunk_index"),
                "text": hit.entity.get("text"),
            })
    return hits


# ════════════════════════════════════════════════════════════
# Reranker Client
# ════════════════════════════════════════════════════════════

async def rerank_chunks(query: str, chunks: list[dict], top_n: int = 5) -> list[dict]:
    """Rerank retrieved chunks using the BGE reranker service."""
    if not chunks:
        return []

    documents = [c["text"] for c in chunks]
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
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
            if idx < len(chunks):
                chunk = chunks[idx].copy()
                chunk["rerank_score"] = r["score"]
                reranked.append(chunk)
        return reranked

    except Exception as e:
        logger.warning(f"Reranker failed, falling back to raw order: {e}")
        return chunks[:top_n]


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
    if file_type.lower().strip(".") not in SUPPORTED_EXTENSIONS:
        result["status"] = "failed"
        result["error"] = f"Unsupported file type: {file_type}"
        return result

    try:
        # 2. Parse
        logger.info(f"Parsing {file_name} ({file_type})...")
        text = parse_document(file_content, file_type)
        if not text or len(text.strip()) < 10:
            result["status"] = "failed"
            result["error"] = "No extractable text found in document"
            return result

        logger.info(f"Extracted {len(text)} chars from {file_name}")

        # 3. Chunk
        chunks = chunk_text(text)
        if not chunks:
            result["status"] = "failed"
            result["error"] = "Document produced no usable text chunks"
            return result

        logger.info(f"Split into {len(chunks)} chunks")

        # 4. Embed
        logger.info(f"Embedding {len(chunks)} chunks...")
        embeddings = await embed_texts(chunks)
        if len(embeddings) != len(chunks):
            result["status"] = "failed"
            result["error"] = f"Embedding mismatch: {len(chunks)} chunks but {len(embeddings)} embeddings"
            return result

        # 5. Upsert into Milvus
        logger.info(f"Upserting into Milvus...")
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
        result["error"] = str(e)

    return result


# ════════════════════════════════════════════════════════════
# RAG Retrieval (used by chat router)
# ════════════════════════════════════════════════════════════

async def retrieve_context(
    query: str,
    project_id: int,
    top_k: int = 20,
    top_n_rerank: int = 5,
) -> list[dict]:
    """
    Full RAG retrieval: embed query → Milvus search → rerank → return top chunks.
    Returns list of {file_name, chunk_index, text, score}.
    """
    try:
        # Embed query
        query_emb = await embed_query(query)
        if not query_emb:
            return []

        # Search Milvus
        candidates = await search_chunks(query_emb, project_id, top_k=top_k)
        if not candidates:
            return []

        # Rerank
        reranked = await rerank_chunks(query, candidates, top_n=top_n_rerank)
        return reranked

    except Exception as e:
        logger.warning(f"RAG retrieval failed: {e}")
        return []
