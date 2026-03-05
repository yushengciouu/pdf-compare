import json
import os
import shutil
import time
from datetime import datetime, timedelta, timezone
from json import JSONDecodeError
from pathlib import Path
from uuid import uuid4

from app.core.config import Settings


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat()


def ensure_job_dirs(settings: Settings, job_id: str) -> Path:
    job_root = settings.jobs_root / job_id
    (job_root / "input").mkdir(parents=True, exist_ok=True)
    (job_root / "render" / "before").mkdir(parents=True, exist_ok=True)
    (job_root / "render" / "after").mkdir(parents=True, exist_ok=True)
    (job_root / "diff" / "mask").mkdir(parents=True, exist_ok=True)
    (job_root / "diff" / "boxes").mkdir(parents=True, exist_ok=True)
    return job_root


def new_job(settings: Settings, mode: str) -> tuple[str, Path, dict]:
    settings.jobs_root.mkdir(parents=True, exist_ok=True)
    job_id = str(uuid4())
    job_root = ensure_job_dirs(settings, job_id)
    created_at = utc_now()
    expires_at = created_at + timedelta(hours=settings.retention_hours)
    meta = {
        "job_id": job_id,
        "mode": mode,
        "status": "queued",
        "created_at": iso(created_at),
        "expires_at": iso(expires_at),
        "progress": {"current": 0, "total": 0},
        "stats": {
            "pages_before": 0,
            "pages_after": 0,
            "paired_pages": 0,
            "inserted_pages": 0,
            "deleted_pages": 0,
            "total_diff_boxes": 0,
        },
        "message": None,
    }
    write_json(job_root / "meta.json", meta)
    return job_id, job_root, meta


def read_json(file_path: Path) -> dict:
    if not file_path.exists():
        return {}
    attempts = 5
    for i in range(attempts):
        try:
            with file_path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except JSONDecodeError:
            if i == attempts - 1:
                return {}
            time.sleep(0.02)
    return {}


def write_json(file_path: Path, payload: dict) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = file_path.with_suffix(file_path.suffix + f".{os.getpid()}.tmp")
    with temp_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())

    attempts = 20
    for i in range(attempts):
        try:
            os.replace(temp_path, file_path)
            return
        except PermissionError:
            if i == attempts - 1:
                break
            time.sleep(0.01)

    with file_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())

    if temp_path.exists():
        temp_path.unlink(missing_ok=True)


def load_meta(settings: Settings, job_id: str) -> dict:
    return read_json(settings.jobs_root / job_id / "meta.json")


def save_meta(settings: Settings, job_id: str, meta: dict) -> None:
    write_json(settings.jobs_root / job_id / "meta.json", meta)


def delete_job_root(job_root: Path) -> None:
    if job_root.exists() and job_root.is_dir():
        shutil.rmtree(job_root)


def cleanup_expired_jobs(settings: Settings) -> dict:
    now = utc_now()
    scanned = 0
    deleted = 0
    failed = 0

    if not settings.jobs_root.exists():
        return {"scanned": scanned, "deleted": deleted, "failed": failed}

    for job_root in settings.jobs_root.iterdir():
        if not job_root.is_dir():
            continue
        scanned += 1
        try:
            meta = read_json(job_root / "meta.json")
            expires_text = meta.get("expires_at")
            if not expires_text:
                continue
            expires_at = datetime.fromisoformat(expires_text)
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at <= now:
                delete_job_root(job_root)
                deleted += 1
        except Exception:
            failed += 1

    return {"scanned": scanned, "deleted": deleted, "failed": failed}
