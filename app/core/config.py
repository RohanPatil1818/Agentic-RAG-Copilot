from functools import lru_cache
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parents[2]

class Settings(BaseSettings):
    app_name: str = "Enterprise IT Support Agentic RAG Copilot"
    app_env: str = "development"
    groq_api_key: str = ""
    tavily_api_key: str = ""
    pinecone_api_key: str = ""
    pinecone_index_name: str = "fde-it-support-rag"
    pinecone_namespace: str = "company-it-kb"
    embeddings_model: str = "BAAI/bge-small-en-v1.5"
    hf_api_key: str = ""
    groq_model: str = "openai/gpt-oss-20b"
    top_k: int = 4
    max_retries: int = 1
    admin_api_key: str = ""
    audit_db_path: str = str(BASE_DIR / "data" / "audit.db")
    upload_dir: str = str(BASE_DIR / "uploads")
    sample_kb_dir: str = str(BASE_DIR / "data" / "sample_kb")

    model_config = SettingsConfigDict(env_file=str(BASE_DIR / ".env"), extra="ignore")

    # Setting up semantic cashe setup
    redis_url: str = "redis://localhost:6379"
    cache_enabled: bool = True
    cache_distance_threshold: float = 0.08
    cache_ttl_kb_seconds: int = 86400     # 24hr
    cache_ttl_web_seconds: int = 3600     # 1hr
    cache_breaker_seconds: int = 30


@lru_cache
def get_settings() -> Settings:
    return Settings()
