from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from app.api.compare import router as compare_router
from app.core.config import get_settings

settings = get_settings()

app = FastAPI(title=settings.app_name)
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
