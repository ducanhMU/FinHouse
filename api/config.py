"""FinHouse — Application Configuration."""

from pydantic import field_validator
from pydantic_settings import BaseSettings
from functools import lru_cache

# Sentinel values that mean "please override this in .env"
_FORBIDDEN_DEFAULTS = {
    "changeme_jwt_secret_at_least_32_chars",
    "changeme_pg_secret",
    "changeme_minio_secret",
}


class Settings(BaseSettings):
    # PostgreSQL
    POSTGRES_USER: str = "finhouse"
    POSTGRES_PASSWORD: str = "changeme_pg_secret"
    POSTGRES_DB: str = "finhouse"
    POSTGRES_HOST: str = "finhouse-postgres"
    POSTGRES_PORT: int = 5432

    # MinIO
    MINIO_ROOT_USER: str = "finhouse"
    MINIO_ROOT_PASSWORD: str = "changeme_minio_secret"
    MINIO_HOST: str = "finhouse-minio"
    MINIO_PORT: int = 9000
    MINIO_BUCKET: str = "finhouse-files"

    # JWT
    JWT_SECRET: str = "changeme_jwt_secret_at_least_32_chars"
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_EXPIRE_MINUTES: int = 30
    JWT_REFRESH_EXPIRE_DAYS: int = 7

    # Ollama (local) and OpenAI-compatible backup API
    OLLAMA_HOST: str = "http://finhouse-ollama:11434"
    DEFAULT_MODEL: str = "qwen2.5:14b"

    # Mode selector — same semantics as EMBED_MODE / RERANK_MODE:
    #   "local"  → call local Ollama only; errors propagate
    #   "backup" → call managed API only; errors propagate
    #   "auto"   → try local first; sticky-switch to API after
    #              LOCAL_FAILURE_THRESHOLD consecutive failures
    OLLAMA_MODE: str = "local"

    # OpenAI-compatible chat completions endpoint (FPT Cloud, Together, etc.).
    # Empty → API backup unavailable.
    OLLAMA_API_URL: str = ""           # e.g. https://mkp-api.fptcloud.com/v1
    OLLAMA_API_KEY: str = ""
    # If set, overrides session.model_used when calling the API. Use this
    # when local model tags (e.g. "qwen2.5:14b") don't match what the
    # managed provider exposes (e.g. "Qwen3-32B").
    OLLAMA_API_MODEL: str = ""

    # Soft ceiling on tool-calling rounds. The LLM decides when it has
    # enough data and stops emitting tool_calls — most queries finish in
    # 2–4 rounds. If we hit this ceiling, the system stops calling tools
    # and asks the user a clarifying follow-up instead of grinding more
    # (the question is probably underspecified at that point).
    # Each round = 1 sync LLM call + tool exec.
    MAX_TOOL_ROUNDS: int = 10

    # ── Query Rewriter (RAG pre-processing) ──
    # Empty → falls back to DEFAULT_MODEL for rewriting.
    # Set a smaller/faster model here if latency matters (e.g. llama3.1:8b)
    # or leave blank to reuse the main model.
    REWRITER_MODEL: str = ""

    # DEPRECATED — no longer read. Rewriter now runs on every turn
    # because it's the only place we resolve scope / time / metrics
    # before RAG and tools. Kept here so existing .env files don't
    # error. To disable rewriting, edit api/services/rewriter.py.
    REWRITER_ENABLED: bool = True

    # ── Multi-agent LLM routing (LangGraph nodes) ──
    # Each ReAct agent in the chat graph has its own brain. Choose the
    # model per-agent by complexity tier.
    #
    # Qwen series available on DashScope (Model Studio):
    #   • Qwen-Max series — flagship reasoning. Latest tags:
    #       qwen3.6-max, qwen3-max, qwen-max
    #   • Qwen-Plus series — strong general / agentic. Latest tags:
    #       qwen3.6-plus, qwen3.5-plus, qwen-plus
    #   • Qwen-Flash series — fast/cheap, good for high-throughput. Tags:
    #       qwen3.6-flash, qwen3.5-flash, qwen-flash
    #   • Qwen-Coder series — best at code & structured output (SQL, JSON):
    #       qwen3-coder-plus, qwen3-coder-flash, qwen-coder-plus,
    #       qwen2.5-coder-32b-instruct
    #   • Qwen-Turbo — legacy ultra-light tier (qwen-turbo)
    #   • Open-source dense / MoE: qwen3.6-*, qwen3.5-*, qwen3-*, qwen2.5-*
    #
    # Tier mapping (recommendation):
    #   Tier-A (heaviest reasoning — DB schema + SQL planning):
    #     DB_AGENT_LLM         → dashscope:qwen3-coder-plus
    #                            (alt: qwen3.6-max for non-coder reasoning)
    #   Tier-B (medium — orchestration, charts, synthesis):
    #     ORCHESTRATOR / VIS / COLLECTOR
    #                          → dashscope:qwen3.6-plus
    #                            (alt: qwen-plus, qwen3.5-plus)
    #   Tier-C (light — JSON extraction, lookup-only ReAct):
    #     REWRITER / WEB       → dashscope:qwen3.6-flash
    #                            (alt: qwen-turbo, qwen3.5-flash)
    #
    # Format: "<provider>:<model>" or empty to fall back to the chat
    # session's model on local Ollama (legacy single-brain mode).
    # Providers (ordered by preference):
    #   dashscope:<model>      — Alibaba DashScope (PRIMARY).
    #   ollama:<tag>           — local Ollama (FALLBACK). e.g. qwen2.5:14b
    #   gemini:<model>         — Google Gemini OpenAI-compat endpoint
    #   openai:<model>         — any OpenAI-compat endpoint via
    #                            OLLAMA_API_URL / OLLAMA_API_KEY
    REWRITER_AGENT_LLM:     str = ""  # tier-C — recommend qwen3.6-flash
    ORCHESTRATOR_AGENT_LLM: str = ""  # tier-B — recommend qwen3.6-plus
    WEB_AGENT_LLM:          str = ""  # tier-C — recommend qwen3.6-flash
    DB_AGENT_LLM:           str = ""  # tier-A — recommend qwen3-coder-plus
    VIS_AGENT_LLM:          str = ""  # tier-B — recommend qwen3.6-plus
    COLLECTOR_AGENT_LLM:    str = ""  # tier-B — recommend qwen3.6-plus

    # ── DashScope (Alibaba Model Studio) ─────────────────────
    # OpenAI-compatible. International endpoint default; switch to
    # https://dashscope.aliyuncs.com/compatible-mode/v1 for Beijing region.
    # Get API key: https://www.alibabacloud.com/help/model-studio/get-api-key
    DASHSCOPE_API_URL: str = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
    DASHSCOPE_API_KEY: str = ""
    # Toggle "thinking" mode for Qwen-3 series. When True, the model emits
    # reasoning_content streamed separately from final content (rendered
    # as italic/dim block in the UI). Set False for tier-C agents to cut
    # latency and tokens.
    DASHSCOPE_ENABLE_THINKING: bool = False

    # ── Gemini (used when any *_AGENT_LLM = "gemini:<model>") ──
    # OpenAI-compatible endpoint, see:
    # https://ai.google.dev/gemini-api/docs/openai
    GEMINI_API_URL: str = "https://generativelanguage.googleapis.com/v1beta/openai"
    GEMINI_API_KEY: str = ""

    # Per-agent ReAct loop ceiling. Each agent stops calling tools after
    # this many rounds and produces its final answer with what it has.
    AGENT_MAX_ROUNDS: int = 6

    # Tighter cap for the rewriter: it should converge on JSON in 1-2
    # rounds at most (call lookup_company once, then emit JSON).
    REWRITER_MAX_ROUNDS: int = 3

    # Embedding / Reranker — local services
    EMBED_HOST: str = "http://finhouse-bge-m3:8081"
    RERANK_HOST: str = "http://finhouse-reranker:8082"

    # Service mode selector — controls which backend is used per call.
    #   "local"  → call the EMBED_HOST / RERANK_HOST service (default)
    #   "backup" → call managed API directly (skip local entirely)
    #   "auto"   → try local first, auto-fallback to API after failures
    EMBED_MODE: str = "local"
    RERANK_MODE: str = "local"

    # Managed API credentials (used when mode is "backup" or "auto")
    # OpenAI-compatible endpoints (FPT Cloud, OpenAI, Together, etc.)
    EMBED_API_URL: str = ""            # e.g. https://mkp-api.fptcloud.com/v1
    EMBED_API_KEY: str = ""
    EMBED_API_MODEL: str = "Vietnamese_Embedding"
    EMBED_API_DIMENSIONS: int = 1024

    RERANK_API_URL: str = ""           # e.g. https://mkp-api.fptcloud.com/v1
    RERANK_API_KEY: str = ""
    RERANK_API_MODEL: str = "bge-reranker-v2-m3"

    # In "auto" mode: number of consecutive local failures before
    # switching over to the API for the rest of the process lifetime.
    LOCAL_FAILURE_THRESHOLD: int = 2

    # Milvus
    MILVUS_HOST: str = "finhouse-milvus"
    MILVUS_PORT: int = 19530

    # SearXNG
    SEARXNG_HOST: str = "http://finhouse-searxng:8080"

    # ClickHouse (OLAP database for database_query tool)
    # Empty host → database_query tool is disabled
    CLICKHOUSE_HOST: str = ""
    CLICKHOUSE_PORT: int = 8123
    CLICKHOUSE_USER: str = "finhouse"
    CLICKHOUSE_PASSWORD: str = "changeme_clickhouse"
    CLICKHOUSE_DB: str = "olap"

    # Maximum rows the database_query tool may return in one call
    DATABASE_QUERY_MAX_ROWS: int = 1000

    # Maximum characters in a single LLM-generated SQL query
    DATABASE_QUERY_MAX_SQL_LEN: int = 4000

    # Data folder (auto-scanned on startup)
    DATA_DIR: str = "/app/data"

    # Cleanup
    CLEANUP_INTERVAL_MINUTES: int = 60

    # CORS — comma-separated list of allowed origins (or "*" for dev only)
    # Default restricts to localhost where the Streamlit UI runs.
    CORS_ALLOW_ORIGINS: str = "http://localhost:8501,http://127.0.0.1:8501"

    # Environment: "dev" or "prod". Prod mode enforces strict secret checks.
    ENV: str = "dev"

    @field_validator("JWT_SECRET", "POSTGRES_PASSWORD", "MINIO_ROOT_PASSWORD")
    @classmethod
    def _reject_default_secrets(cls, v: str, info) -> str:
        """
        Reject placeholder secrets. In production these MUST be overridden
        via the .env file or environment variables.
        """
        if v in _FORBIDDEN_DEFAULTS:
            import os
            env = os.getenv("ENV", "dev").lower()
            if env in ("prod", "production"):
                raise ValueError(
                    f"{info.field_name} is still set to its default placeholder "
                    f"value. You MUST override it in .env when ENV=prod."
                )
            else:
                # In dev, just warn loudly
                import logging
                logging.warning(
                    f"⚠️  {info.field_name} is using the default placeholder "
                    f"value. DO NOT deploy to production like this."
                )
        return v

    @field_validator("JWT_SECRET")
    @classmethod
    def _check_jwt_length(cls, v: str) -> str:
        if len(v) < 32:
            raise ValueError("JWT_SECRET must be at least 32 characters long")
        return v

    @field_validator("EMBED_MODE", "RERANK_MODE", "OLLAMA_MODE")
    @classmethod
    def _check_mode(cls, v: str, info) -> str:
        v = v.lower().strip()
        valid = {"local", "backup", "auto"}
        if v not in valid:
            raise ValueError(
                f"{info.field_name} must be one of {valid}, got: {v!r}"
            )
        return v

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @property
    def database_url_sync(self) -> str:
        return (
            f"postgresql://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @property
    def cors_origins(self) -> list[str]:
        """Parse CORS_ALLOW_ORIGINS into a clean list."""
        return [o.strip() for o in self.CORS_ALLOW_ORIGINS.split(",") if o.strip()]

    class Config:
        env_file = ".env"
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
