from __future__ import annotations

import shutil
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from tempfile import mkdtemp

import cv2

from app.core.config import Settings
from app.services.page_match import build_smart_page_map
from app.services.render import extract_page_texts, get_page_count, render_pdf_pages


@dataclass
class Thresholds:
    image: float = 0.005  # 顯著變化像素比例（>15/255），對稀疏改動（如TOC頁碼、新增表格行）靈敏
    text: float = 0.05    # 降低門檻：避免少量文字修訂被過濾
    min_candidates: int = 6
    neighbor_window: int = 1


def _image_diff_score(before_png: Path, after_png: Path) -> float:
    img_a = cv2.imread(str(before_png), cv2.IMREAD_GRAYSCALE)
    img_b = cv2.imread(str(after_png), cv2.IMREAD_GRAYSCALE)
    if img_a is None or img_b is None:
        return 1.0

    h = min(img_a.shape[0], img_b.shape[0])
    w = min(img_a.shape[1], img_b.shape[1])
    if h <= 0 or w <= 0:
        return 1.0

    img_a = cv2.resize(img_a, (w, h), interpolation=cv2.INTER_AREA)
    img_b = cv2.resize(img_b, (w, h), interpolation=cv2.INTER_AREA)
    img_a = cv2.GaussianBlur(img_a, (3, 3), 0)
    img_b = cv2.GaussianBlur(img_b, (3, 3), 0)

    diff = cv2.absdiff(img_a, img_b)
    # 使用「顯著變化像素比例」而非全頁均值
    # 原因：均值會將稀疏但明顯的改動（如 TOC 頁碼、新增表格行）稀釋到門檻值以下
    # pixel_threshold=15 對應 diff_threshold=25 的較寬鬆版本（prefilter 用低解析度圖）
    return float((diff > 15).mean())


def _text_diff_score(text_a: str, text_b: str) -> float:
    if not text_a and not text_b:
        return 0.0
    if not text_a or not text_b:
        return 1.0
    sim = SequenceMatcher(a=text_a.lower(), b=text_b.lower()).ratio()
    return float(1.0 - sim)


def build_prefilter_report(
    before_pdf: Path,
    after_pdf: Path,
    settings: Settings,
    thresholds: Thresholds | None = None,
) -> dict:
    thresholds = thresholds or Thresholds()
    temp_root = Path(mkdtemp(prefix="pdf-prefilter-"))
    before_dir = temp_root / "before"
    after_dir = temp_root / "after"

    try:
        pages_before = get_page_count(before_pdf)
        pages_after = get_page_count(after_pdf)

        render_pdf_pages(before_pdf, before_dir, max(96, settings.render_dpi // 2))
        render_pdf_pages(after_pdf, after_dir, max(96, settings.render_dpi // 2))

        before_texts = extract_page_texts(before_pdf)
        after_texts = extract_page_texts(after_pdf)

        page_map = build_smart_page_map(
            before_render_dir=before_dir,
            after_render_dir=after_dir,
            pages_before=pages_before,
            pages_after=pages_after,
            thumb_size=settings.smart_thumb_size,
            gap_penalty=settings.smart_gap_penalty,
            match_bias=settings.smart_match_bias,
            min_similarity=settings.smart_min_similarity,
            before_texts=before_texts,
            after_texts=after_texts,
            image_weight=settings.smart_image_weight,
            text_weight=settings.smart_text_weight,
        )

        entries: list[dict] = []
        for slot in page_map:
            state = slot["state"]
            before_page = slot.get("before_page")
            after_page = slot.get("after_page")

            if state == "inserted":
                entries.append(
                    {
                        "slot": slot["slot"],
                        "state": state,
                        "before_page": None,
                        "after_page": after_page,
                        "image_diff": 1.0,
                        "text_diff": 1.0,
                        "reason": "inserted_page",
                        "send_to_llm": True,
                        "combined_score": 1.0,
                    }
                )
                continue

            if state == "deleted":
                entries.append(
                    {
                        "slot": slot["slot"],
                        "state": state,
                        "before_page": before_page,
                        "after_page": None,
                        "image_diff": 1.0,
                        "text_diff": 1.0,
                        "reason": "deleted_page",
                        "send_to_llm": True,
                        "combined_score": 1.0,
                    }
                )
                continue

            assert before_page is not None and after_page is not None

            image_diff = _image_diff_score(
                before_dir / f"{int(before_page):04d}.png",
                after_dir / f"{int(after_page):04d}.png",
            )
            text_diff = _text_diff_score(
                before_texts[int(before_page) - 1],
                after_texts[int(after_page) - 1],
            )
            send = image_diff >= thresholds.image or text_diff >= thresholds.text
            reason = "none"
            if send:
                if image_diff >= thresholds.image and text_diff >= thresholds.text:
                    reason = "image_and_text_diff"
                elif image_diff >= thresholds.image:
                    reason = "image_diff"
                else:
                    reason = "text_diff"

            combined_score = max(image_diff, text_diff)
            entries.append(
                {
                    "slot": slot["slot"],
                    "state": state,
                    "before_page": before_page,
                    "after_page": after_page,
                    "image_diff": round(image_diff, 4),
                    "text_diff": round(text_diff, 4),
                    "combined_score": round(combined_score, 4),
                    "reason": reason,
                    "send_to_llm": send,
                }
            )

        slot_map = {int(e["slot"]): e for e in entries}

        def mark_candidate(slot_no: int, reason: str) -> None:
            entry = slot_map.get(slot_no)
            if not entry:
                return
            if entry["send_to_llm"]:
                return
            entry["send_to_llm"] = True
            entry["reason"] = reason

        structural_slots = [
            int(e["slot"]) for e in entries if e["state"] in {"inserted", "deleted"}
        ]

        for s in structural_slots:
            for d in range(-thresholds.neighbor_window, thresholds.neighbor_window + 1):
                if d == 0:
                    continue
                mark_candidate(s + d, "neighbor_of_structural_change")

        candidates = [e for e in entries if e["send_to_llm"]]

        if len(candidates) < thresholds.min_candidates:
            paired_rank = sorted(
                [e for e in entries if e["state"] == "paired" and not e["send_to_llm"]],
                key=lambda x: float(x.get("combined_score", 0.0)),
                reverse=True,
            )
            for e in paired_rank:
                if len(candidates) >= thresholds.min_candidates:
                    break
                e["send_to_llm"] = True
                e["reason"] = "top_rank_backup"
                candidates.append(e)

        return {
            "summary": {
                "pages_before": pages_before,
                "pages_after": pages_after,
                "total_slots": len(entries),
                "candidate_pages": len(candidates),
            },
            "thresholds": {
                "image": thresholds.image,
                "text": thresholds.text,
                "min_candidates": thresholds.min_candidates,
                "neighbor_window": thresholds.neighbor_window,
            },
            "candidates": candidates,
            "all_pages": entries,
        }
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)
