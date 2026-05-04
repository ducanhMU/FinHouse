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
    # try:
    #     from services.data_scanner import kick_off_background_scan
    #     await kick_off_background_scan()
    # except Exception as e:
    #     print(f"⚠️  Data scan kick-off error (non-fatal): {e}")

    # Start the cleanup worker (purges soft-deleted files, GC incognito)
    cleanup_task = None
    try:
        from services.cleanup import start_cleanup_task
        cleanup_task = start_cleanup_task()
        print(f"🧹 Cleanup worker started (interval: {settings.CLEANUP_INTERVAL_MINUTES}m)")
    except Exception as e:
        print(f"⚠️  Cleanup worker start failed (non-fatal): {e}")

    yield

    # Shutdown: cancel cleanup worker
    if cleanup_task and not cleanup_task.done():
        cleanup_task.cancel()
        try:
            await cleanup_task
        except Exception:
            pass

    # Close singleton httpx clients cleanly
    try:
        from services.ingest import close_http_clients
        await close_http_clients()
    except Exception as e:
        print(f"⚠️  Error closing HTTP clients: {e}")

    try:
        from tools.database_query import close_client as close_ch_client
        await close_ch_client()
    except Exception as e:
        print(f"⚠️  Error closing ClickHouse client: {e}")

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


# ── Request logging middleware ──────────────────────────────
# Log every request with timing + status so you can see what's happening
# when the UI "freezes" — most likely a slow downstream call (Ollama, embed).
@app.middleware("http")
async def log_requests(request, call_next):
    import time
    import logging
    log = logging.getLogger("finhouse.http")

    start = time.monotonic()
    path = request.url.path
    method = request.method

    # Don't spam logs for health checks
    if path != "/health":
        log.info(f"→ {method} {path}")

    try:
        response = await call_next(request)
        elapsed_ms = int((time.monotonic() - start) * 1000)
        if path != "/health":
            status = response.status_code
            log.info(f"← {method} {path} {status} ({elapsed_ms}ms)")
        return response
    except Exception as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        log.error(f"✗ {method} {path} EXCEPTION ({elapsed_ms}ms): {e}", exc_info=True)
        raise

# Routers
app.include_router(auth.router)
app.include_router(projects.router)
app.include_router(sessions.router)
app.include_router(chat.router)
app.include_router(files.router)


# ── Health Check ────────────────────────────────────────────

async def _check_postgres() -> str:
    try:
        from database import engine
        from sqlalchemy import text
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return "ok"
    except Exception as e:
        return f"error: {e}"


async def _check_ollama() -> str:
    return "ok" if await ollama_health() else "error: unreachable"


async def _check_http(name: str, url: str, ok_codes: tuple[int, ...] = (200,)) -> str:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(url)
            return "ok" if r.status_code in ok_codes else f"error: {r.status_code}"
    except Exception as e:
        return f"error: {e}"


@app.get("/health", tags=["system"])
async def health_check():
    """Check connectivity to all backend services concurrently."""
    import asyncio

    minio_url = f"http://{settings.MINIO_HOST}:{settings.MINIO_PORT}/minio/health/live"
    milvus_url = f"http://{settings.MILVUS_HOST}:9091/healthz"
    searxng_url = f"{settings.SEARXNG_HOST}/"

    pg, ol, mn, mv, sx = await asyncio.gather(
        _check_postgres(),
        _check_ollama(),
        _check_http("minio", minio_url),
        _check_http("milvus", milvus_url),
        _check_http("searxng", searxng_url),
    )
    status = {
        "postgres": pg,
        "ollama": ol,
        "minio": mn,
        "milvus": mv,
        "searxng": sx,
    }

    all_ok = all(v == "ok" for v in status.values())
    return {
        "status": "ok" if all_ok else "degraded",
        "services": status,
    }


@app.get("/models", tags=["system"])
async def get_models():
    """List available Ollama models (legacy — UI uses /agents now)."""
    return await list_models()


@app.get("/agents", tags=["system"])
async def get_agents_config():
    """Read-only snapshot of the per-agent brain configuration.

    The UI consumes this to display which LLM each ReAct agent is
    bound to. Brains are configured via *_AGENT_LLM env vars (read at
    process start); the UI does NOT let the user override them here.

    Empty `spec` means the agent is using fallback = session model on
    local Ollama. The frontend should label that as "Ollama (fallback)".
    """
    from graph.llm_router import _AGENT_ENV, parse_spec

    fallback = settings.DEFAULT_MODEL or "qwen2.5:14b"
    agents = []
    for name, env_name in _AGENT_ENV.items():
        raw = (getattr(settings, env_name, "") or "").strip()
        spec = parse_spec(raw, fallback)
        agents.append({
            "name": name,
            "env_var": env_name,
            "raw": raw,
            "provider": spec.provider,
            "model": spec.model,
            "label": f"{spec.provider}:{spec.model}",
            "is_fallback": not raw,
        })

    providers = {
        "dashscope": bool(settings.DASHSCOPE_API_KEY),
        "gemini":    bool(settings.GEMINI_API_KEY),
        "openai":    bool(settings.OLLAMA_API_KEY and settings.OLLAMA_API_URL),
        "ollama":    bool(settings.OLLAMA_HOST),
    }

    return {
        "agents": agents,
        "fallback_model": fallback,
        "providers": providers,
    }
