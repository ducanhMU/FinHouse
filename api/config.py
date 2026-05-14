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
    # Browser-reachable MinIO endpoint used when *signing* presigned URLs
    # (e.g. chart images returned by the visualize tool). MINIO_HOST is
    # the Docker-internal name and is unreachable from the user's browser
    # — set this to the host:port that the browser can hit.
    # Examples:
    #   Local dev:    http://localhost:19000
    #   Remote IP:    http://112.137.129.161:19000
    # Empty → falls back to MINIO_HOST:MINIO_PORT (only correct when the
    # browser shares the Docker network, i.e. essentially never).
    MINIO_PUBLIC_URL: str = ""

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
    # Each ReAct agent in the chat graph has its own brain. Each value is
    # a COMMA-SEPARATED CHAIN of specs: primary first, then fallbacks.
    # The router rotates to the next entry on quota / rate-limit (HTTP
    # 429) or transient 5xx / network errors. Each DashScope model has
    # its own ~1M token daily budget, so spreading agents across distinct
    # primaries multiplies effective capacity by N.
    #
    # Format per entry: "<provider>:<model>"
    #   dashscope:<model>      — Alibaba DashScope (PRIMARY).
    #   ollama:<tag>           — local Ollama (FALLBACK). e.g. qwen2.5:14b
    #   gemini:<model>         — Google Gemini OpenAI-compat endpoint
    #   openai:<model>         — any OpenAI-compat endpoint via
    #                            OLLAMA_API_URL / OLLAMA_API_KEY
    # Empty string → single-brain mode = chat session's selected model
    # on local Ollama (legacy behavior).
    #
    # Qwen tiers on DashScope (Model Studio):
    #   • Max series — flagship reasoning:
    #       qwen3.6-max, qwen3-max, qwen-max
    #   • Plus series — strong general / agentic:
    #       qwen3.6-plus, qwen3.5-plus, qwen-plus
    #   • Flash series — fast/cheap, high-throughput:
    #       qwen3.6-flash, qwen3.5-flash, qwen-flash
    #   • Coder series — best at SQL / JSON / structured output:
    #       qwen3-coder-plus, qwen3-coder-flash, qwen2.5-coder-32b-instruct
    #   • Turbo — legacy ultra-light tier (qwen-turbo)
    #   • Open-source dense: qwen2.5-7b-instruct, qwen2.5-14b-instruct, ...
    #
    # Chain allocation (recommendation — high-complexity tier prioritises
    # 3.6/3.5; light tier uses qwen3-* / qwen2.5-*):
    #   Tier-A (heaviest — DB schema + SQL planning):
    #     DB           → qwen3-coder-plus → qwen3.6-plus → qwen2.5-coder-32b-instruct
    #   Tier-B (medium — orchestration / charts / synthesis):
    #     ORCHESTRATOR → qwen3.6-plus  → qwen3.5-plus       → qwen-plus
    #     VIS          → qwen3.5-plus  → qwen3-coder-flash  → qwen2.5-coder-32b-instruct
    #     COLLECTOR    → qwen3.6-flash → qwen3.5-flash      → qwen-plus
    #   Tier-C (light — JSON extraction / simple ReAct):
    #     REWRITER     → qwen2.5-7b-instruct  → qwen-turbo            → qwen2.5-14b-instruct
    #     WEB          → qwen3-coder-flash    → qwen2.5-14b-instruct  → qwen-flash
    REWRITER_AGENT_LLM:     str = ""
    ORCHESTRATOR_AGENT_LLM: str = ""
    WEB_AGENT_LLM:          str = ""
    DB_AGENT_LLM:           str = ""
    VIS_AGENT_LLM:          str = ""
    COLLECTOR_AGENT_LLM:    str = ""

    # Per-agent thinking flag for DashScope Qwen-3 reasoning models.
    # When True, the model emits `reasoning_content` (rendered as a dim
    # block in the UI). Adds ~2-4s latency and burns more tokens, so
    # only enable on agents that benefit from explicit reasoning:
    #   • DB           — schema + SQL planning over multiple tables
    #   • ORCHESTRATOR — multi-task plan with non-trivial routing
    # Light-tier agents (REWRITER, WEB) and pure-formatting agents (VIS,
    # COLLECTOR) should keep thinking OFF — wastes tokens with no win.
    REWRITER_AGENT_THINKING:     bool = False
    ORCHESTRATOR_AGENT_THINKING: bool = False
    WEB_AGENT_THINKING:          bool = False
    DB_AGENT_THINKING:           bool = False
    VIS_AGENT_THINKING:          bool = False
    COLLECTOR_AGENT_THINKING:    bool = False

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

    # Optional fallback embed servers (comma-separated). The runtime
    # tries EMBED_HOST first, then each entry here in order. Only after
    # EVERY local host fails does it consider the API fallback.
    # Example: EMBED_HOST_FALLBACKS=http://server2:8081,http://server3:8081
    # NOTE: put the host with FlagEmbedding (sparse-capable) first —
    # the chain stops at the first successful response, so a dense-only
    # backend earlier in the chain will mask sparse on later ones.
    EMBED_HOST_FALLBACKS: str = ""

    # Same idea for rerank: chain of fallback rerank servers, tried in
    # order after RERANK_HOST. After ALL local hosts fail enough times
    # the runtime sticky-switches to RERANK_API_URL (DashScope by default).
    # Example: RERANK_HOST_FALLBACKS=http://server2-reranker:8082
    RERANK_HOST_FALLBACKS: str = ""

    # Service mode selector — controls which backend is used per call.
    #   "local"  → call the EMBED_HOST / RERANK_HOST service (default)
    #   "backup" → call managed API directly (skip local entirely)
    #   "auto"   → try local first, auto-fallback to API after failures
    EMBED_MODE: str = "local"
    RERANK_MODE: str = "local"

    # Managed API credentials (used when mode is "backup" or "auto").
    # Default is Alibaba DashScope (OpenAI-compatible). To switch back
    # to FPT Cloud or another provider, override URL / MODEL in .env.
    #
    # Auth: if EMBED_API_KEY / RERANK_API_KEY is empty, the runtime
    # falls back to DASHSCOPE_API_KEY (defined further up). So you only
    # need ONE key for embed + rerank + agent LLMs when all on DashScope.
    #
    # URL pattern: pass the API base WITHOUT the resource path. The code
    # appends `/embeddings` / `/reranks` automatically. If you already
    # included the resource (e.g. `.../v1/embeddings`), the code detects
    # that and uses the URL as-is.
    EMBED_API_URL: str = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
    EMBED_API_KEY: str = ""
    EMBED_API_MODEL: str = "text-embedding-v4"
    EMBED_API_DIMENSIONS: int = 1024   # must match BGE-M3 collection schema

    # NOTE the path here: DashScope rerank lives at `/compatible-api/v1`
    # (NOT `/compatible-mode/v1`). The resource is `/reranks` (plural).
    RERANK_API_URL: str = "https://dashscope-intl.aliyuncs.com/compatible-api/v1"
    RERANK_API_KEY: str = ""
    RERANK_API_MODEL: str = "qwen3-rerank"

    # In "auto" mode: number of consecutive local failures before
    # switching over to the API for the rest of the process lifetime.
    LOCAL_FAILURE_THRESHOLD: int = 2

    # Milvus
    MILVUS_HOST: str = "finhouse-milvus"
    MILVUS_PORT: int = 19530

    # ── RAG v2 feature flags ──
    # Collection routing — leave as legacy name to use the old IVF_FLAT
    # dense-only collection; switch to `finhouse_chunks_v2` to use the new
    # HNSW + hybrid-capable collection. Both collections coexist, so a
    # rollback is just flipping this back to "finhouse_chunks".
    RAG_COLLECTION: str = "finhouse_chunks_v2"
    # Vector index type for the v2 collection. HNSW is the recommended
    # default; IVF_FLAT kept as an escape hatch.
    RAG_INDEX_TYPE: str = "HNSW"
    # Use BGE-M3 sparse (lexical) embeddings alongside dense, fused via
    # RRF. Requires the embed service to expose /embed_hybrid AND the
    # active collection to have a sparse_embedding field (v2 only).
    RAG_HYBRID_ENABLED: bool = True
    # HyDE — let the rewriter emit 2-3 hypothetical passages that are
    # embedded alongside the rewritten query. Off → embed only the
    # rewritten query (legacy behavior).
    RAG_HYDE_ENABLED: bool = True
    RAG_HYDE_N_PASSAGES: int = 3
    # Reciprocal Rank Fusion constant. 60 is the canonical paper value;
    # higher values flatten the contribution of top ranks.
    RAG_RRF_K: int = 60
    # Semantic chunking at ingest time (embedding-based topic boundary
    # detection). Off by default — slower + costlier ingest. Per-file
    # fallback to rule-based chunking on any error.
    RAG_SEMANTIC_CHUNKING: bool = False
    # Percentile of pairwise embedding distance used as the split
    # threshold for semantic chunking. Lower → more chunks; higher →
    # fewer, larger chunks. 90-95 is the LangChain default range.
    RAG_SEMANTIC_THRESHOLD_PCT: float = 92.0
    # When True, the startup data-folder scanner treats files marked
    # `ready` in Postgres as needing re-ingest (instead of skipping
    # them). Use this **once** after flipping RAG_COLLECTION to v2 so a
    # plain `docker compose restart finhouse-api` re-feeds every base-
    # knowledge file into the new collection. Flip back to False after
    # the scan log says all files re-processed, otherwise every restart
    # will pointlessly re-embed everything.
    RAG_FORCE_RESCAN: bool = False

    # SearXNG
    SEARXNG_HOST: str = "http://finhouse-searxng:8080"

    # ── Web agent: optional extra tools ──
    # Each flag adds one tool to the web_search agent's toolbox. All
    # default ON; flip individually to False to remove a tool from the
    # ReAct loop without breaking anything else (no change to ToolType
    # / orchestrator). If a tool's underlying lib isn't installed, the
    # import fails silently and the tool is skipped — agent still runs
    # with whatever loaded.
    WEB_TOOL_URL_FETCH_ENABLED: bool = True
    WEB_TOOL_VN_MARKET_ENABLED: bool = True       # vnstock 3.x — VN tickers
    WEB_TOOL_WORLD_MARKET_ENABLED: bool = True    # yfinance — FX, commodities, indices
    WEB_TOOL_WIKIPEDIA_ENABLED: bool = True

    # url_fetch caps. Cap is a defense against runaway content + token cost,
    # not a security boundary. Timeout is per-request.
    URL_FETCH_MAX_CHARS: int = 10000
    URL_FETCH_TIMEOUT_SEC: int = 10

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
