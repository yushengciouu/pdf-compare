from app.core.config import get_settings
from app.services.diff_fast import compare_images
from app.services.diff_smart import plan_smart_mapping
from app.services.render import extract_page_texts, get_page_count, render_pdf_pages
from app.services.storage import cleanup_expired_jobs, cleanup_llm_debug, load_meta, save_meta, write_json
from app.workers.celery_app import celery_app


@celery_app.task(name="app.workers.tasks.run_compare_job")
def run_compare_job(job_id: str, mode: str) -> None:
    settings = get_settings()
    job_root = settings.jobs_root / job_id
    before_pdf = job_root / "input" / "before.pdf"
    after_pdf = job_root / "input" / "after.pdf"

    def safe_save() -> None:
        try:
            save_meta(settings, job_id, meta)
        except Exception:
            pass

    def is_cancel_requested() -> bool:
        latest = load_meta(settings, job_id)
        return bool(latest.get("cancel_requested")) if latest else False

    meta = load_meta(settings, job_id)
    meta["cancel_requested"] = False
    meta["status"] = "running"
    meta["message"] = "比對中"
    safe_save()

    try:
        pages_before = get_page_count(before_pdf)
        pages_after = get_page_count(after_pdf)

        if pages_before > settings.max_pages or pages_after > settings.max_pages:
            raise RuntimeError(f"PDF 頁數超過限制（上限 {settings.max_pages} 頁）")

        render_pdf_pages(
            before_pdf, job_root / "render" / "before", settings.render_dpi
        )
        render_pdf_pages(after_pdf, job_root / "render" / "after", settings.render_dpi)

        before_texts = extract_page_texts(before_pdf)
        after_texts = extract_page_texts(after_pdf)

        meta["stats"]["pages_before"] = pages_before
        meta["stats"]["pages_after"] = pages_after

        if mode == "smart":
            page_map = plan_smart_mapping(
                settings=settings,
                before_render_dir=job_root / "render" / "before",
                after_render_dir=job_root / "render" / "after",
                pages_before=pages_before,
                pages_after=pages_after,
                before_texts=before_texts,
                after_texts=after_texts,
            )
        else:
            page_map = []
            paired = min(pages_before, pages_after)
            for i in range(1, paired + 1):
                page_map.append(
                    {"slot": i, "before_page": i, "after_page": i, "state": "paired"}
                )
            for i in range(paired + 1, pages_before + 1):
                page_map.append(
                    {
                        "slot": len(page_map) + 1,
                        "before_page": i,
                        "after_page": None,
                        "state": "deleted",
                    }
                )
            for i in range(paired + 1, pages_after + 1):
                page_map.append(
                    {
                        "slot": len(page_map) + 1,
                        "before_page": None,
                        "after_page": i,
                        "state": "inserted",
                    }
                )

        write_json(job_root / "page_map.json", page_map)

        total_boxes = 0
        paired_count = 0
        inserted_count = 0
        deleted_count = 0

        total_slots = len(page_map)
        meta["progress"] = {"current": 0, "total": total_slots}
        safe_save()

        for index, slot in enumerate(page_map, start=1):
            if is_cancel_requested():
                meta["status"] = "failed"
                meta["message"] = "任務已取消"
                safe_save()
                return

            state = slot["state"]
            if state == "paired":
                paired_count += 1
                before_page = int(slot["before_page"])
                after_page = int(slot["after_page"])
                before_png = job_root / "render" / "before" / f"{before_page:04d}.png"
                after_png = job_root / "render" / "after" / f"{after_page:04d}.png"
                mask_out = job_root / "diff" / "mask" / f"{index:04d}.png"
                boxes_out = job_root / "diff" / "boxes" / f"{index:04d}.json"
                box_count, width, height = compare_images(
                    before_png,
                    after_png,
                    mask_out,
                    boxes_out,
                    threshold=settings.diff_threshold,
                    min_area=settings.diff_min_area,
                    mask_alpha=settings.mask_alpha,
                )
                total_boxes += box_count
                slot["width"] = width
                slot["height"] = height
            elif state == "inserted":
                inserted_count += 1
                write_json(job_root / "diff" / "boxes" / f"{index:04d}.json", [])
            elif state == "deleted":
                deleted_count += 1
                write_json(job_root / "diff" / "boxes" / f"{index:04d}.json", [])

            meta["progress"] = {"current": index, "total": total_slots}
            safe_save()

        write_json(job_root / "page_map.json", page_map)
        meta["stats"]["paired_pages"] = paired_count
        meta["stats"]["inserted_pages"] = inserted_count
        meta["stats"]["deleted_pages"] = deleted_count
        meta["stats"]["total_diff_boxes"] = total_boxes
        meta["status"] = "done"
        meta["message"] = "比對完成"
        safe_save()

    except Exception as exc:
        meta["status"] = "failed"
        meta["message"] = str(exc)
        safe_save()
        try:
            write_json(job_root / "error.json", {"message": str(exc)})
        except Exception:
            pass
        raise



        save_meta(settings, job_id, meta)
        raise


@celery_app.task(name="app.workers.tasks.cleanup_expired_jobs_task")
def cleanup_expired_jobs_task() -> dict:
    settings = get_settings()
    return cleanup_expired_jobs(settings)


@celery_app.task(name="app.workers.tasks.cleanup_llm_debug_task")
def cleanup_llm_debug_task() -> dict:
    settings = get_settings()
    return cleanup_llm_debug(settings)
