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

    # Ollama
    OLLAMA_HOST: str = "http://finhouse-ollama:11434"
    DEFAULT_MODEL: str = "qwen2.5:14b"

    # Embedding / Reranker
    EMBED_HOST: str = "http://finhouse-bge-m3:8081"
    RERANK_HOST: str = "http://finhouse-reranker:8082"

    # Milvus
    MILVUS_HOST: str = "finhouse-milvus"
    MILVUS_PORT: int = 19530

    # SearXNG
    SEARXNG_HOST: str = "http://finhouse-searxng:8080"

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