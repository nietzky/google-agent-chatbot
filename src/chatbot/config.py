from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    openai_api_key: SecretStr
    chat_model: str = "openai/gpt-4.1-mini"
    elasticsearch_url: str = "http://localhost:9200"
    elasticsearch_api_key: SecretStr | None = None
    elasticsearch_index: str = "cloudhelp-products-openai-v1"
    memory_index: str = "cloudhelp-memory-v1"
    memory_db: Path = Path("data/memory.sqlite3")
    session_db: Path = Path("data/sessions.sqlite3")
    # Changing either requires a new index and re-ingestion.
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 768
    web_user_id: str = "local-user"

    aws_diagnostics_enabled: bool = False
    aws_profile: str | None = None
    aws_region: str | None = None

    auto_memory_enabled: bool = True
    memory_extraction_model: str = "gpt-4.1-mini"
