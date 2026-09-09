import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from app.api.compare import router as compare_router
from app.core.config import get_settings
from app.services.storage import cleanup_expired_jobs, cleanup_llm_debug, load_meta, save_meta
from app.services.html_report import cleanup_expired_reports

logger = logging.getLogger(__name__)


def _recover_stuck_jobs(settings) -> None:
    """啟動時將殘留的 'running' 狀態任務標記為 failed（前次進程意外中斷）"""
    jobs_root = settings.jobs_root
    if not jobs_root.exists():
        return
    for job_dir in jobs_root.iterdir():
        if not job_dir.is_dir():
            continue
        try:
            meta = load_meta(settings, job_dir.name)
            if meta and meta.get("status") == "running":
                meta["status"] = "failed"
                meta["message"] = "服務重啟，任務中斷"
                save_meta(settings, job_dir.name, meta)
                logger.warning("Recovered stuck job: %s", job_dir.name)
        except Exception:
            pass


async def _periodic_cleanup(settings) -> None:
    """每小時清除過期任務；每 24 小時清除 LLM debug 檔案"""
    hours_since_llm_cleanup = 0
    while True:
        await asyncio.sleep(3600)
        try:
            cleanup_expired_jobs(settings)
        except Exception as e:
            logger.error("cleanup_expired_jobs error: %s", e)
        hours_since_llm_cleanup += 1
        if hours_since_llm_cleanup >= 24:
            try:
                cleanup_llm_debug(settings)
            except Exception as e:
                logger.error("cleanup_llm_debug error: %s", e)
            try:
                cleanup_expired_reports(settings)
            except Exception as e:
                logger.error("cleanup_expired_reports error: %s", e)
            hours_since_llm_cleanup = 0


@asynccontextmanager
async def lifespan(app: FastAPI):
    _settings = get_settings()
    _recover_stuck_jobs(_settings)
    try:
        cleanup_expired_reports(_settings)
    except Exception as e:
        logger.error("Startup cleanup_expired_reports error: %s", e)
    cleanup_task = asyncio.create_task(_periodic_cleanup(_settings))
    yield
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass


settings = get_settings()

app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.include_router(compare_router, prefix=settings.api_prefix)

settings.jobs_root.mkdir(parents=True, exist_ok=True)
app.mount("/static/jobs", StaticFiles(directory=settings.jobs_root), name="jobs-static")

frontend_dir = None
if settings.frontend_dir is not None:
    frontend_dir = Path(settings.frontend_dir)
else:
    candidates = [
        settings.storage_root.parent.parent / "frontend",
        Path.cwd().parent / "frontend",
        Path(__file__).resolve().parents[3] / "frontend",
    ]
    for candidate in candidates:
        if candidate.exists():
            frontend_dir = candidate
            break

if frontend_dir is not None and frontend_dir.exists():
    app.mount("/ui", StaticFiles(directory=frontend_dir, html=True), name="ui")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/", response_model=None)
def root():
    if frontend_dir is not None:
        index = frontend_dir / "index.html"
        if index.exists():
            return FileResponse(index)
    return {
        "name": settings.app_name,
        "health": "/health",
        "api": f"{settings.api_prefix}/compare",
    }
