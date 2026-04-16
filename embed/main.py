"""FinHouse — BGE-M3 Embedding Microservice.

Auto-detects CUDA GPU and uses it if available, otherwise falls back to CPU.
Model is loaded eagerly at startup so the first request isn't slow.
"""

import os
import logging

from fastapi import FastAPI
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("finhouse.embed")

app = FastAPI(title="FinHouse Embedding Service")

_model = None
_device = "cpu"


def _detect_device() -> str:
    """Prefer CUDA if available; fall back to CPU."""
    try:
        import torch
        if torch.cuda.is_available():
            gpu_name = torch.cuda.get_device_name(0)
            vram_gb = torch.cuda.get_device_properties(0).total_memory / 1024**3
            logger.info(f"✅ CUDA available: {gpu_name} ({vram_gb:.1f} GB VRAM)")
            return "cuda"
        else:
            logger.info("⚠️  CUDA not available, using CPU (slower)")
            return "cpu"
    except Exception as e:
        logger.warning(f"Device detection failed, defaulting to CPU: {e}")
        return "cpu"


def load_model():
    """Load BGE-M3 model onto the detected device."""
    global _model, _device
    from sentence_transformers import SentenceTransformer

    _device = _detect_device()
    logger.info(f"📦 Loading BGE-M3 on {_device}...")
    _model = SentenceTransformer("BAAI/bge-m3", device=_device)
    logger.info(f"✅ BGE-M3 loaded on {_device}")
    return _model


@app.on_event("startup")
async def startup_event():
    """Eagerly load model at startup — first API call will be instant."""
    load_model()


class EmbedRequest(BaseModel):
    texts: list[str]


class EmbedResponse(BaseModel):
    embeddings: list[list[float]]
    dimension: int


@app.get("/health")
async def health():
    return {
        "status": "ok" if _model is not None else "loading",
        "model": "BAAI/bge-m3",
        "device": _device,
    }


@app.post("/embed", response_model=EmbedResponse)
async def embed(req: EmbedRequest):
    if _model is None:
        load_model()
    embeddings = _model.encode(
        req.texts,
        normalize_embeddings=True,
        batch_size=64 if _device == "cuda" else 16,
        show_progress_bar=False,
    )
    return EmbedResponse(
        embeddings=embeddings.tolist(),
        dimension=embeddings.shape[1],
    )