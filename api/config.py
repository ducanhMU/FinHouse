"""FinHouse — Application Configuration."""

from pydantic_settings import BaseSettings
from functools import lru_cache


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

    class Config:
        env_file = ".env"
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
