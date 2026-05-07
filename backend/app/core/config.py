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
    frontend_dir: Path | None = None
    jobs_dir_name: str = "jobs"
    max_pdf_mb: int = 50
    max_pages: int = 200
    retention_hours: int = 24

    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"

    render_dpi: int = 240
    diff_threshold: int = 25
    diff_min_area: int = 40
    mask_alpha: int = 220
    smart_thumb_size: int = 32
    smart_gap_penalty: float = 0.35
    smart_match_bias: float = 0.25
    smart_min_similarity: float = 0.45
    smart_text_weight: float = 0.3
    smart_image_weight: float = 0.7

    compare_task_soft_timeout_sec: int = 1200
    compare_task_hard_timeout_sec: int = 1500

    # LLM 分析相關設定
    llm_base_url: str = "http://192.168.39.143:8001"
    llm_model: str = "gemma-4:31B"
    llm_max_tokens: int = 16384
    llm_temperature: float = 0.2
    llm_analyze_dpi: int = 96  # 傳給 LLM 的縮圖解析度，越低 token 越少
    llm_timeout_sec: int = 300  # 單次 LLM 請求逾時秒數

    model_config = SettingsConfigDict(env_file=".env", env_prefix="PDF_COMPARE_")

    @property
    def jobs_root(self) -> Path:
        return self.storage_root / self.jobs_dir_name


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
