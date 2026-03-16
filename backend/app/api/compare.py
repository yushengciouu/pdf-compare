import json
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from app.core.config import Settings, get_settings
from app.models.schemas import (
    CompareCreateResponse,
    CompareMode,
    ComparePageResponse,
    CompareStatusResponse,
)
from app.services.storage import (
    cleanup_expired_jobs,
    delete_job_root,
    load_meta,
    new_job,
    save_meta,
)
from app.services.prefilter import Thresholds, build_prefilter_report
from app.workers.tasks import run_compare_job, run_export_job

router = APIRouter(prefix="/compare", tags=["compare"])


def _job_root(settings: Settings, job_id: str) -> Path:
    return settings.jobs_root / job_id


async def _save_upload(upload: UploadFile, out_path: Path, max_bytes: int) -> None:
    size = 0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("wb") as f:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise HTTPException(status_code=413, detail="PDF 檔案超過大小限制")
            f.write(chunk)
    await upload.close()


def _validate_pdf(upload: UploadFile) -> None:
    if not upload.filename or not upload.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="只接受 PDF 檔案")


@router.post("", response_model=CompareCreateResponse)
async def create_compare_job(
    before: Annotated[UploadFile, File(...)],
    after: Annotated[UploadFile, File(...)],
    mode: Annotated[CompareMode, Form()] = "smart",
    settings: Settings = Depends(get_settings),
) -> CompareCreateResponse:
    _validate_pdf(before)
    _validate_pdf(after)

    job_id, job_root, _meta = new_job(settings, mode)
    max_bytes = settings.max_pdf_mb * 1024 * 1024

    await _save_upload(before, job_root / "input" / "before.pdf", max_bytes)
    await _save_upload(after, job_root / "input" / "after.pdf", max_bytes)

    run_compare_job.apply_async(args=[job_id, mode])
    return CompareCreateResponse(job_id=job_id, status="queued", mode=mode)


@router.post("/prefilter")
async def run_prefilter(
    before: Annotated[UploadFile, File(...)],
    after: Annotated[UploadFile, File(...)],
    image_threshold: Annotated[float, Form()] = 0.03,
    text_threshold: Annotated[float, Form()] = 0.08,
    min_candidates: Annotated[int, Form()] = 6,
    neighbor_window: Annotated[int, Form()] = 1,
    settings: Settings = Depends(get_settings),
) -> dict:
    _validate_pdf(before)
    _validate_pdf(after)

    temp_dir = Path(tempfile.mkdtemp(prefix="pdf-prefilter-upload-"))
    try:
        max_bytes = settings.max_pdf_mb * 1024 * 1024
        before_path = temp_dir / "before.pdf"
        after_path = temp_dir / "after.pdf"
        await _save_upload(before, before_path, max_bytes)
        await _save_upload(after, after_path, max_bytes)

        thresholds = Thresholds(
            image=max(0.0, min(1.0, image_threshold)),
            text=max(0.0, min(1.0, text_threshold)),
            min_candidates=max(1, min(1000, int(min_candidates))),
            neighbor_window=max(0, min(5, int(neighbor_window))),
        )
        return build_prefilter_report(before_path, after_path, settings, thresholds)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@router.get("/{job_id}", response_model=CompareStatusResponse)
def get_compare_job(
    job_id: str, settings: Settings = Depends(get_settings)
) -> CompareStatusResponse:
    meta = load_meta(settings, job_id)
    if not meta:
        raise HTTPException(status_code=404, detail="找不到任務")

    return CompareStatusResponse(
        job_id=meta["job_id"],
        status=meta["status"],
        progress=meta.get("progress", {"current": 0, "total": 0}),
        mode=meta["mode"],
        stats=meta.get("stats", {}),
        created_at=datetime.fromisoformat(meta["created_at"]),
        expires_at=datetime.fromisoformat(meta["expires_at"]),
        message=meta.get("message"),
    )


@router.get("/{job_id}/pages/{page_no}", response_model=ComparePageResponse)
def get_compare_page(
    job_id: str, page_no: int, settings: Settings = Depends(get_settings)
) -> ComparePageResponse:
    job_root = _job_root(settings, job_id)
    if not job_root.exists():
        raise HTTPException(status_code=404, detail="找不到任務")

    page_map_path = job_root / "page_map.json"
    if not page_map_path.exists():
        raise HTTPException(status_code=400, detail="任務尚未產生頁面映射")

    with page_map_path.open("r", encoding="utf-8") as f:
        page_map = json.load(f)

    if page_no < 1 or page_no > len(page_map):
        raise HTTPException(status_code=404, detail="頁碼不存在")

    slot = page_map[page_no - 1]
    before_page = slot.get("before_page")
    after_page = slot.get("after_page")

    before_image = None
    after_image = None
    if before_page is not None:
        before_image = f"/static/jobs/{job_id}/render/before/{int(before_page):04d}.png"
    if after_page is not None:
        after_image = f"/static/jobs/{job_id}/render/after/{int(after_page):04d}.png"

    mask_path = job_root / "diff" / "mask" / f"{page_no:04d}.png"
    mask_removed_path = job_root / "diff" / "mask" / f"{page_no:04d}.removed.png"
    mask_added_path = job_root / "diff" / "mask" / f"{page_no:04d}.added.png"
    boxes_path = job_root / "diff" / "boxes" / f"{page_no:04d}.json"
    mask_image = (
        f"/static/jobs/{job_id}/diff/mask/{page_no:04d}.png"
        if mask_path.exists()
        else None
    )
    mask_removed_image = (
        f"/static/jobs/{job_id}/diff/mask/{page_no:04d}.removed.png"
        if mask_removed_path.exists()
        else None
    )
    mask_added_image = (
        f"/static/jobs/{job_id}/diff/mask/{page_no:04d}.added.png"
        if mask_added_path.exists()
        else None
    )

    boxes = []
    if boxes_path.exists():
        with boxes_path.open("r", encoding="utf-8") as f:
            boxes = json.load(f)

    width = slot.get("width")
    height = slot.get("height")

    payload = {
        "page_no": page_no,
        "mapping": {
            "before_page": before_page,
            "after_page": after_page,
            "state": slot["state"],
        },
        "assets": {
            "before_image": before_image,
            "after_image": after_image,
            "mask_image": mask_image,
            "mask_removed_image": mask_removed_image,
            "mask_added_image": mask_added_image,
        },
        "boxes": boxes,
        "width": width,
        "height": height,
    }
    return ComparePageResponse.model_validate(payload)


@router.get("/{job_id}/pages")
def list_compare_pages(job_id: str, settings: Settings = Depends(get_settings)) -> dict:
    job_root = _job_root(settings, job_id)
    if not job_root.exists():
        raise HTTPException(status_code=404, detail="找不到任務")

    page_map_path = job_root / "page_map.json"
    if not page_map_path.exists():
        raise HTTPException(status_code=400, detail="任務尚未產生頁面映射")

    with page_map_path.open("r", encoding="utf-8") as f:
        page_map = json.load(f)

    pages: list[dict] = []
    for idx, slot in enumerate(page_map, start=1):
        before_page = slot.get("before_page")
        after_page = slot.get("after_page")

        before_image = (
            f"/static/jobs/{job_id}/render/before/{int(before_page):04d}.png"
            if before_page is not None
            else None
        )
        after_image = (
            f"/static/jobs/{job_id}/render/after/{int(after_page):04d}.png"
            if after_page is not None
            else None
        )

        boxes_path = job_root / "diff" / "boxes" / f"{idx:04d}.json"
        mask_path = job_root / "diff" / "mask" / f"{idx:04d}.png"
        mask_removed_path = job_root / "diff" / "mask" / f"{idx:04d}.removed.png"
        mask_added_path = job_root / "diff" / "mask" / f"{idx:04d}.added.png"
        boxes = []
        if boxes_path.exists():
            with boxes_path.open("r", encoding="utf-8") as f:
                boxes = json.load(f)

        pages.append(
            {
                "page_no": idx,
                "mapping": {
                    "before_page": before_page,
                    "after_page": after_page,
                    "state": slot["state"],
                },
                "assets": {
                    "before_image": before_image,
                    "after_image": after_image,
                    "mask_image": f"/static/jobs/{job_id}/diff/mask/{idx:04d}.png"
                    if mask_path.exists()
                    else None,
                    "mask_removed_image": f"/static/jobs/{job_id}/diff/mask/{idx:04d}.removed.png"
                    if mask_removed_path.exists()
                    else None,
                    "mask_added_image": f"/static/jobs/{job_id}/diff/mask/{idx:04d}.added.png"
                    if mask_added_path.exists()
                    else None,
                },
                "boxes": boxes,
                "width": slot.get("width"),
                "height": slot.get("height"),
            }
        )

    return {"pages": pages, "total": len(pages)}


@router.delete("/{job_id}")
def delete_job(job_id: str, settings: Settings = Depends(get_settings)) -> dict:
    job_root = _job_root(settings, job_id)
    if not job_root.exists():
        raise HTTPException(status_code=404, detail="找不到任務")

    delete_job_root(job_root)
    return {"ok": True}


@router.post("/{job_id}/cancel")
def cancel_job(job_id: str, settings: Settings = Depends(get_settings)) -> dict:
    meta = load_meta(settings, job_id)
    if not meta:
        raise HTTPException(status_code=404, detail="找不到任務")

    if meta.get("status") in {"done", "failed"}:
        return {"ok": True, "status": meta.get("status")}

    meta["cancel_requested"] = True
    meta["message"] = "取消中"
    save_meta(settings, job_id, meta)
    return {"ok": True, "status": "cancelling"}


@router.post("/maintenance/cleanup")
def cleanup_jobs(settings: Settings = Depends(get_settings)) -> dict:
    return cleanup_expired_jobs(settings)


@router.post("/{job_id}/export")
def export_compare_result(
    job_id: str,
    force: bool = Query(False),
    settings: Settings = Depends(get_settings),
) -> dict:
    job_root = _job_root(settings, job_id)
    if not job_root.exists():
        raise HTTPException(status_code=404, detail="找不到任務")

    meta = load_meta(settings, job_id)
    if not meta:
        raise HTTPException(status_code=404, detail="找不到任務")
    if meta.get("status") != "done":
        raise HTTPException(status_code=400, detail="任務尚未完成，無法匯出")

    export_meta = meta.get("export", {})
    status = export_meta.get("status")
    if force or status not in {"queued", "running"}:
        meta["export"] = {
            "status": "queued",
            "message": "排隊中",
            "file": None,
        }
        save_meta(settings, job_id, meta)
        run_export_job.apply_async(args=[job_id])

    return {"ok": True, "status": meta.get("export", {}).get("status", "queued")}


@router.get("/{job_id}/export")
def get_export_status(job_id: str, settings: Settings = Depends(get_settings)) -> dict:
    job_root = _job_root(settings, job_id)
    if not job_root.exists():
        raise HTTPException(status_code=404, detail="找不到任務")

    meta = load_meta(settings, job_id)
    if not meta:
        raise HTTPException(status_code=404, detail="找不到任務")

    export_meta = meta.get("export", {"status": "idle", "message": "尚未匯出"})
    download_url = None
    if export_meta.get("status") == "done":
        download_url = f"{settings.api_prefix}/compare/{job_id}/export/download"
    return {
        "status": export_meta.get("status", "idle"),
        "message": export_meta.get("message"),
        "download_url": download_url,
    }


@router.get("/{job_id}/export/download")
def download_export(
    job_id: str, settings: Settings = Depends(get_settings)
) -> FileResponse:
    output_pdf = _job_root(settings, job_id) / "export" / "result_preview.pdf"
    if not output_pdf.exists():
        raise HTTPException(status_code=404, detail="尚未產生匯出檔")
    return FileResponse(
        output_pdf,
        media_type="application/pdf",
        filename=f"compare-{job_id}.pdf",
    )
