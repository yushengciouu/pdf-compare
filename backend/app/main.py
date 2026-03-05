from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.compare import router as compare_router
from app.core.config import get_settings

settings = get_settings()

app = FastAPI(title=settings.app_name)
app.include_router(compare_router, prefix=settings.api_prefix)

settings.jobs_root.mkdir(parents=True, exist_ok=True)
app.mount("/static/jobs", StaticFiles(directory=settings.jobs_root), name="jobs-static")

frontend_dir = settings.storage_root.parent.parent / "frontend"
if frontend_dir.exists():
    app.mount("/ui", StaticFiles(directory=frontend_dir, html=True), name="ui")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/", response_model=None)
def root():
    index = frontend_dir / "index.html"
    if index.exists():
        return FileResponse(index)
    return {
        "name": settings.app_name,
        "health": "/health",
        "api": f"{settings.api_prefix}/compare",
    }
