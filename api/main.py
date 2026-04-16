"""FinHouse — FastAPI Application Entry Point."""

from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import get_settings
from routers import auth, projects, sessions, chat, files
from services.ollama import list_models, check_health as ollama_health

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown events."""
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

    print("🚀 FinHouse API starting...")
    print(f"   PostgreSQL: {settings.POSTGRES_HOST}:{settings.POSTGRES_PORT}")
    print(f"   Ollama:     {settings.OLLAMA_HOST}")
    print(f"   MinIO:      {settings.MINIO_HOST}:{settings.MINIO_PORT}")
    print(f"   Milvus:     {settings.MILVUS_HOST}:{settings.MILVUS_PORT}")
    print(f"   Data dir:   {settings.DATA_DIR}")

    # Kick off data folder scan in the background — doesn't block startup
    try:
        from services.data_scanner import kick_off_background_scan
        await kick_off_background_scan()
    except Exception as e:
        print(f"⚠️  Data scan kick-off error (non-fatal): {e}")

    yield

    # Shutdown: close singleton httpx clients cleanly
    try:
        from services.ingest import close_http_clients
        await close_http_clients()
    except Exception as e:
        print(f"⚠️  Error closing HTTP clients: {e}")

    print("👋 FinHouse API shutting down")


app = FastAPI(
    title="FinHouse API",
    description="RAG-based AI Chat Platform — Backend API",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS — restricted to configured origins (never "*" with credentials=True)
# credentials=True requires an explicit origin list per CORS spec.
_cors_origins = settings.cors_origins
if "*" in _cors_origins:
    # Wildcard mode — must disable credentials (browser would reject anyway)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
else:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# Routers
app.include_router(auth.router)
app.include_router(projects.router)
app.include_router(sessions.router)
app.include_router(chat.router)
app.include_router(files.router)


# ── Health Check ────────────────────────────────────────────

@app.get("/health", tags=["system"])
async def health_check():
    """Check connectivity to all backend services."""
    status = {}

    # PostgreSQL
    try:
        from database import engine
        from sqlalchemy import text
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        status["postgres"] = "ok"
    except Exception as e:
        status["postgres"] = f"error: {e}"

    # Ollama
    status["ollama"] = "ok" if await ollama_health() else "error: unreachable"

    # MinIO
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(
                f"http://{settings.MINIO_HOST}:{settings.MINIO_PORT}/minio/health/live"
            )
            status["minio"] = "ok" if r.status_code == 200 else f"error: {r.status_code}"
    except Exception as e:
        status["minio"] = f"error: {e}"

    # Milvus
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(
                f"http://{settings.MILVUS_HOST}:9091/healthz"
            )
            status["milvus"] = "ok" if r.status_code == 200 else f"error: {r.status_code}"
    except Exception as e:
        status["milvus"] = f"error: {e}"

    # SearXNG
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{settings.SEARXNG_HOST}/")
            status["searxng"] = "ok" if r.status_code == 200 else f"error: {r.status_code}"
    except Exception as e:
        status["searxng"] = f"error: {e}"

    all_ok = all(v == "ok" for v in status.values())
    return {
        "status": "ok" if all_ok else "degraded",
        "services": status,
    }


@app.get("/models", tags=["system"])
async def get_models():
    """List available Ollama models."""
    return await list_models()