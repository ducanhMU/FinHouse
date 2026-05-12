"""FinHouse — BGE-M3 Embedding Microservice.

Auto-detects CUDA GPU and uses it if available, otherwise falls back to CPU.
Model is loaded eagerly at startup so the first request isn't slow.

Endpoints:
    POST /embed         — dense embeddings only (legacy, backward-compatible)
    POST /embed_hybrid  — dense + sparse (lexical_weights) in one call
                          Used by hybrid retrieval (dense + BM25-like fusion).

The hybrid endpoint prefers `FlagEmbedding.BGEM3FlagModel` which natively
produces both dense and sparse vectors from BGE-M3. If FlagEmbedding is
not installed, /embed_hybrid degrades gracefully: it returns dense from
sentence-transformers and an empty `sparse` list. Callers should treat
empty sparse as "hybrid disabled, fall back to dense-only".
"""

import logging

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("finhouse.embed")

app = FastAPI(title="FinHouse Embedding Service")

# Two model handles. We prefer FlagEmbedding's BGEM3FlagModel (gives sparse
# weights natively), but keep sentence-transformers as a safety net so
# /embed still works even if FlagEmbedding isn't installed.
_flag_model = None   # FlagEmbedding.BGEM3FlagModel — preferred
_st_model = None     # sentence_transformers.SentenceTransformer — fallback
_device = "cpu"


def _detect_device() -> str:
    """Prefer CUDA if available; fall back to CPU."""
    try:
        import torch
        if torch.cuda.is_available():
            gpu_name = torch.cuda.get_device_name(0)
            vram_gb = torch.cuda.get_device_properties(0).total_memory / 1024**3
            logger.info(f"CUDA available: {gpu_name} ({vram_gb:.1f} GB VRAM)")
            return "cuda"
        logger.info("CUDA not available, using CPU (slower)")
        return "cpu"
    except Exception as e:
        logger.warning(f"Device detection failed, defaulting to CPU: {e}")
        return "cpu"


def load_models():
    """Load BGE-M3 once. Try FlagEmbedding first (has sparse); fall back."""
    global _flag_model, _st_model, _device
    _device = _detect_device()

    try:
        from FlagEmbedding import BGEM3FlagModel
        logger.info(f"Loading BGE-M3 (FlagEmbedding) on {_device}...")
        _flag_model = BGEM3FlagModel(
            "BAAI/bge-m3",
            use_fp16=(_device == "cuda"),
            devices=[_device] if _device == "cuda" else None,
        )
        logger.info(f"BGE-M3 (FlagEmbedding) loaded on {_device}")
        return
    except Exception as e:
        logger.warning(
            f"FlagEmbedding unavailable ({e}); falling back to sentence-transformers. "
            "Hybrid retrieval will be disabled — install FlagEmbedding for sparse vectors."
        )

    from sentence_transformers import SentenceTransformer
    logger.info(f"Loading BGE-M3 (sentence-transformers) on {_device}...")
    _st_model = SentenceTransformer("BAAI/bge-m3", device=_device)
    logger.info(f"BGE-M3 (sentence-transformers) loaded on {_device}")


@app.on_event("startup")
async def startup_event():
    load_models()


class EmbedRequest(BaseModel):
    texts: list[str]


class EmbedResponse(BaseModel):
    embeddings: list[list[float]]
    dimension: int


class HybridEmbedRequest(BaseModel):
    texts: list[str]
    return_dense: bool = True
    return_sparse: bool = True


class HybridEmbedResponse(BaseModel):
    # Dense list-of-list of floats, same as /embed.
    dense: list[list[float]]
    dimension: int
    # Sparse: list of dicts {token_id_as_str: weight}. Empty list per item
    # if sparse not produced (FlagEmbedding missing). JSON keys are strings
    # — clients must cast back to int for Milvus.
    sparse: list[dict[str, float]]
    # Tells the caller whether sparse weights are real or just placeholders.
    sparse_available: bool


@app.get("/health")
async def health():
    if _flag_model is not None:
        backend = "flag_embedding"
        status = "ok"
    elif _st_model is not None:
        backend = "sentence_transformers"
        status = "ok"
    else:
        backend = "none"
        status = "loading"
    return {
        "status": status,
        "model": "BAAI/bge-m3",
        "device": _device,
        "backend": backend,
        "sparse_available": _flag_model is not None,
    }


def _dense_via_st(texts: list[str]) -> list[list[float]]:
    if _st_model is None:
        raise HTTPException(503, "sentence-transformers model not loaded")
    arr = _st_model.encode(
        texts,
        normalize_embeddings=True,
        batch_size=64 if _device == "cuda" else 16,
        show_progress_bar=False,
    )
    return arr.tolist()


def _encode_via_flag(
    texts: list[str], return_sparse: bool,
) -> tuple[list[list[float]], list[dict[str, float]]]:
    """Encode via FlagEmbedding, returning (dense, sparse)."""
    if _flag_model is None:
        raise HTTPException(503, "FlagEmbedding model not loaded")
    out = _flag_model.encode(
        texts,
        batch_size=64 if _device == "cuda" else 16,
        max_length=8192,
        return_dense=True,
        return_sparse=return_sparse,
        return_colbert_vecs=False,
    )
    dense = out["dense_vecs"]
    # numpy → list
    if hasattr(dense, "tolist"):
        dense_list = dense.tolist()
    else:
        dense_list = [list(map(float, row)) for row in dense]

    sparse_list: list[dict[str, float]] = []
    if return_sparse:
        for w in out.get("lexical_weights", []):
            # w is a dict[int | str, float] — stringify keys for JSON safety
            sparse_list.append({str(k): float(v) for k, v in (w or {}).items()})
    return dense_list, sparse_list


@app.post("/embed", response_model=EmbedResponse)
async def embed(req: EmbedRequest):
    """Dense-only endpoint. Kept stable for callers that don't need sparse."""
    if not req.texts:
        return EmbedResponse(embeddings=[], dimension=1024)

    if _flag_model is not None:
        dense, _ = _encode_via_flag(req.texts, return_sparse=False)
    else:
        dense = _dense_via_st(req.texts)

    dim = len(dense[0]) if dense else 1024
    return EmbedResponse(embeddings=dense, dimension=dim)


@app.post("/embed_hybrid", response_model=HybridEmbedResponse)
async def embed_hybrid(req: HybridEmbedRequest):
    """Dense + sparse in one call. Sparse may be empty if FlagEmbedding absent."""
    if not req.texts:
        return HybridEmbedResponse(
            dense=[], dimension=1024, sparse=[],
            sparse_available=(_flag_model is not None),
        )

    if _flag_model is not None:
        dense, sparse = _encode_via_flag(
            req.texts, return_sparse=req.return_sparse,
        )
        dim = len(dense[0]) if dense else 1024
        return HybridEmbedResponse(
            dense=dense if req.return_dense else [],
            dimension=dim,
            sparse=sparse if req.return_sparse else [],
            sparse_available=True,
        )

    # Fallback: only dense from sentence-transformers
    dense = _dense_via_st(req.texts) if req.return_dense else []
    dim = len(dense[0]) if dense else 1024
    return HybridEmbedResponse(
        dense=dense, dimension=dim,
        sparse=[{} for _ in req.texts] if req.return_sparse else [],
        sparse_available=False,
    )
