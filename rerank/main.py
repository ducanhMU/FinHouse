"""FinHouse — BGE-M3 Reranker Microservice.

Auto-detects CUDA GPU and uses it if available, otherwise falls back to CPU.
Model is loaded eagerly at startup so the first request isn't slow.
"""

import logging

from fastapi import FastAPI
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("finhouse.rerank")

app = FastAPI(title="FinHouse Reranker Service")

_model = None
_device = "cpu"


def _detect_device() -> str:
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
    global _model, _device
    from sentence_transformers import CrossEncoder

    _device = _detect_device()
    logger.info(f"📦 Loading BGE Reranker on {_device}...")
    _model = CrossEncoder(
        "BAAI/bge-reranker-v2-m3",
        max_length=512,
        device=_device,
    )
    logger.info(f"✅ BGE Reranker loaded on {_device}")
    return _model


@app.on_event("startup")
async def startup_event():
    load_model()


class RerankRequest(BaseModel):
    query: str
    documents: list[str]
    top_n: int = 5


class RerankResult(BaseModel):
    index: int
    score: float
    text: str


class RerankResponse(BaseModel):
    results: list[RerankResult]


@app.get("/health")
async def health():
    return {
        "status": "ok" if _model is not None else "loading",
        "model": "BAAI/bge-reranker-v2-m3",
        "device": _device,
    }


@app.post("/rerank", response_model=RerankResponse)
async def rerank(req: RerankRequest):
    if _model is None:
        load_model()

    pairs = [[req.query, doc] for doc in req.documents]
    scores = _model.predict(
        pairs,
        batch_size=32 if _device == "cuda" else 8,
        show_progress_bar=False,
    )

    ranked = sorted(
        enumerate(scores), key=lambda x: x[1], reverse=True
    )[: req.top_n]

    results = [
        RerankResult(index=idx, score=float(score), text=req.documents[idx])
        for idx, score in ranked
    ]
    return RerankResponse(results=results)