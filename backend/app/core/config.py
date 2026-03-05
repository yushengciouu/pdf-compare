from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "PDF Compare API"
    app_env: str = "dev"
    api_prefix: str = "/api"

    storage_root: Path = Path(
        r"C:\Users\felix_chiu\Desktop\project\pdf-compare\var\compare"
    )
    jobs_dir_name: str = "jobs"
    max_pdf_mb: int = 50
    max_pages: int = 200
    retention_hours: int = 24

    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"

    render_dpi: int = 240
    smart_thumb_size: int = 32
    smart_gap_penalty: float = 0.35
    smart_match_bias: float = 0.25
    smart_min_similarity: float = 0.45

    model_config = SettingsConfigDict(env_file=".env", env_prefix="PDF_COMPARE_")

    @property
    def jobs_root(self) -> Path:
        return self.storage_root / self.jobs_dir_name


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
