from __future__ import annotations

import json
from pathlib import Path

import cv2
import fitz
import numpy as np

# 渲染時使用高 DPI（240）以確保 diff 精準度；
# 匯出 PDF 只需顯示用解析度，96 DPI 已足夠且檔案大幅縮小。
_RENDER_DPI: int = 240
_EXPORT_DPI: int = 96
_EXPORT_SCALE: float = _EXPORT_DPI / _RENDER_DPI  # ≈ 0.4
_JPEG_QUALITY: int = 85  # 0-100，85 為品質/大小的良好平衡點


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


def _downscale_for_export(image: np.ndarray) -> np.ndarray:
    """將影像縮放至匯出用 DPI，減少嵌入 PDF 的資料量。"""
    if _EXPORT_SCALE >= 1.0:
        return image
    new_w = max(1, int(round(image.shape[1] * _EXPORT_SCALE)))
    new_h = max(1, int(round(image.shape[0] * _EXPORT_SCALE)))
    return cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_AREA)


def _image_to_jpeg_bytes(image: np.ndarray) -> bytes:
    """以 JPEG 編碼影像，相較於 PNG 可縮小 10-20 倍。"""
    ok, buf = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, _JPEG_QUALITY])
    if not ok:
        raise RuntimeError("輸出 JPEG 失敗")
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

            # 縮放至匯出用 DPI
            display = _downscale_for_export(composed)
            jpeg_bytes = _image_to_jpeg_bytes(display)

            # 頁面尺寸使用 PDF points（1 pt = 1/72 inch）
            # 96 DPI 的像素 → pt：pixel * 72 / 96 = pixel * 0.75
            h, w = display.shape[:2]
            pt_w = w * 72.0 / _EXPORT_DPI
            pt_h = h * 72.0 / _EXPORT_DPI
            page = doc.new_page(width=pt_w, height=pt_h)
            page.insert_image(fitz.Rect(0, 0, pt_w, pt_h), stream=jpeg_bytes)

        doc.save(output_pdf, deflate=True, garbage=4, deflate_images=True)
    finally:
        doc.close()

    return output_pdf
