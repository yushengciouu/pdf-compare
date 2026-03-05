from __future__ import annotations

import json
from pathlib import Path

import cv2
import fitz
import numpy as np


def _compose_after_with_mask(after_path: Path, mask_path: Path | None) -> np.ndarray:
    base = cv2.imread(str(after_path), cv2.IMREAD_COLOR)
    if base is None:
        raise RuntimeError(f"無法讀取影像: {after_path}")

    if mask_path is None or not mask_path.exists():
        return base

    mask = cv2.imread(str(mask_path), cv2.IMREAD_UNCHANGED)
    if mask is None:
        return base

    if mask.ndim == 3 and mask.shape[2] == 4:
        overlay_rgb = mask[:, :, :3].astype(np.float32)
        alpha = (mask[:, :, 3].astype(np.float32) / 255.0)[:, :, None]
    else:
        overlay_rgb = np.zeros_like(base, dtype=np.float32)
        alpha = (mask.astype(np.float32) / 255.0)[:, :, None]

    base_f = base.astype(np.float32)
    composed = overlay_rgb * alpha + base_f * (1.0 - alpha)
    return np.clip(composed, 0, 255).astype(np.uint8)


def _image_to_png_bytes(image: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", image)
    if not ok:
        raise RuntimeError("輸出 PNG 失敗")
    return buf.tobytes()


def build_result_pdf(job_root: Path) -> Path:
    page_map_path = job_root / "page_map.json"
    if not page_map_path.exists():
        raise RuntimeError("尚未產生 page_map，請先完成比對")

    with page_map_path.open("r", encoding="utf-8") as f:
        page_map = json.load(f)

    export_dir = job_root / "export"
    export_dir.mkdir(parents=True, exist_ok=True)
    output_pdf = export_dir / "result_preview.pdf"

    doc = fitz.open()
    try:
        for slot_idx, slot in enumerate(page_map, start=1):
            after_page = slot.get("after_page")
            if after_page is None:
                page = doc.new_page(width=842, height=595)
                text = f"Slot {slot_idx}: After 無對應頁（deleted）"
                page.insert_text((40, 70), text, fontsize=18)
                continue

            after_png = job_root / "render" / "after" / f"{int(after_page):04d}.png"
            mask_png = job_root / "diff" / "mask" / f"{slot_idx:04d}.png"
            composed = _compose_after_with_mask(
                after_png, mask_png if mask_png.exists() else None
            )
            png_bytes = _image_to_png_bytes(composed)

            h, w = composed.shape[:2]
            page = doc.new_page(width=float(w), height=float(h))
            page.insert_image(fitz.Rect(0, 0, float(w), float(h)), stream=png_bytes)

        doc.save(output_pdf)
    finally:
        doc.close()

    return output_pdf
