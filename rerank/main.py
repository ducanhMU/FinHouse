"""FinHouse — BGE-M3 Reranker Microservice."""

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="FinHouse Reranker Service")

_model = None


def get_model():
    global _model
    if _model is None:
        from sentence_transformers import CrossEncoder
        print("📦 Loading BGE Reranker model...")
        _model = CrossEncoder("BAAI/bge-reranker-v2-m3", max_length=512)
        print("✅ BGE Reranker loaded")
    return _model


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
    return {"status": "ok", "model": "BAAI/bge-reranker-v2-m3"}


@app.post("/rerank", response_model=RerankResponse)
async def rerank(req: RerankRequest):
    model = get_model()
    pairs = [[req.query, doc] for doc in req.documents]
    scores = model.predict(pairs)

    ranked = sorted(
        enumerate(scores), key=lambda x: x[1], reverse=True
    )[:req.top_n]

    results = [
        RerankResult(index=idx, score=float(score), text=req.documents[idx])
        for idx, score in ranked
    ]
    return RerankResponse(results=results)
