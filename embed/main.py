"""FinHouse — BGE-M3 Embedding Microservice."""

from fastapi import FastAPI
from pydantic import BaseModel
import numpy as np

app = FastAPI(title="FinHouse Embedding Service")

# Lazy-loaded model
_model = None


def get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        print("📦 Loading BGE-M3 model...")
        _model = SentenceTransformer("BAAI/bge-m3")
        print("✅ BGE-M3 loaded")
    return _model


class EmbedRequest(BaseModel):
    texts: list[str]


class EmbedResponse(BaseModel):
    embeddings: list[list[float]]
    dimension: int


@app.get("/health")
async def health():
    return {"status": "ok", "model": "BAAI/bge-m3"}


@app.post("/embed", response_model=EmbedResponse)
async def embed(req: EmbedRequest):
    model = get_model()
    embeddings = model.encode(req.texts, normalize_embeddings=True)
    return EmbedResponse(
        embeddings=embeddings.tolist(),
        dimension=embeddings.shape[1],
    )
