"""
文字層差異服務

使用 PyMuPDF 取得 PDF 頁面的每個單字及其座標，
再用 difflib.SequenceMatcher 比對 before/after 的文字序列，
找出「內容真正有變化」的單字，回傳附有像素座標的差異框列表。

此方法的優點：純位移（文字相同）不會被標記，只標記內容真正改變的部分。
"""

from __future__ import annotations

from pathlib import Path

import fitz  # PyMuPDF


def _get_words(pdf_path: Path, page_index: int) -> list[dict]:
    """
    取得指定頁面（0-based）的所有單字及其 PDF 座標（unit: points, 72dpi）。
    回傳格式：[{"text": str, "x0": float, "y0": float, "x1": float, "y1": float}]
    """
    doc = fitz.open(str(pdf_path))
    try:
        if page_index < 0 or page_index >= len(doc):
            return []
        page = doc[page_index]
        # get_text("words") 回傳 (x0, y0, x1, y1, word, block_no, line_no, word_no)
        raw_words = page.get_text("words")
        return [
            {"text": w[4], "x0": w[0], "y0": w[1], "x1": w[2], "y1": w[3]}
            for w in raw_words
        ]
    finally:
        doc.close()


def _merge_nearby_boxes(boxes: list[dict], gap: float = 8.0) -> list[dict]:
    """
    將水平方向相鄰（同行）的框合併，減少框數量，讓視覺更乾淨。
    gap: 允許合併的最大水平間距（points）
    """
    if not boxes:
        return []

    # 先依 y0 排序（行），再依 x0 排序（左到右）
    sorted_boxes = sorted(boxes, key=lambda b: (round(b["y0"] / 4), b["x0"]))
    merged: list[dict] = []
    current = dict(sorted_boxes[0])

    for box in sorted_boxes[1:]:
        # 同一行（y 範圍有重疊）且水平距離夠近 → 合併
        same_row = not (box["y0"] > current["y1"] or box["y1"] < current["y0"])
        close_enough = box["x0"] - current["x1"] <= gap
        if same_row and close_enough and box["type"] == current["type"]:
            current["x1"] = max(current["x1"], box["x1"])
            current["y0"] = min(current["y0"], box["y0"])
            current["y1"] = max(current["y1"], box["y1"])
            if box.get("text_before"):
                current["text_before"] = (current.get("text_before", "") + " " + box["text_before"]).strip()
            if box.get("text_after"):
                current["text_after"] = (current.get("text_after", "") + " " + box["text_after"]).strip()
        else:
            merged.append(current)
            current = dict(box)

    merged.append(current)
    return merged


def compute_text_diff_boxes(
    before_pdf: Path,
    after_pdf: Path,
    before_page_index: int,
    after_page_index: int,
    dpi: float = 96.0,
) -> dict:
    """
    比對 before/after 兩頁的文字層，回傳差異框列表。

    回傳格式：
    {
        "before_boxes": [
            {"type": "removed"|"replaced", "x": int, "y": int, "w": int, "h": int,
             "text_before": str, "text_after": str}
        ],
        "after_boxes": [
            {"type": "added"|"replaced", "x": int, "y": int, "w": int, "h": int,
             "text_before": str, "text_after": str}
        ]
    }

    座標單位：像素（依 dpi 換算自 PDF points）。
    """
    scale = dpi / 72.0

    before_words = _get_words(before_pdf, before_page_index)
    after_words = _get_words(after_pdf, after_page_index)

    before_texts = [w["text"] for w in before_words]
    after_texts = [w["text"] for w in after_words]

    matcher = __import__("difflib").SequenceMatcher(
        None, before_texts, after_texts, autojunk=False
    )

    before_boxes_raw: list[dict] = []
    after_boxes_raw: list[dict] = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue

        if tag in ("replace", "delete"):
            # before 的單字被改或被刪 → 標在 before 圖片上
            for word in before_words[i1:i2]:
                box_type = "replaced" if tag == "replace" else "removed"
                text_after = " ".join(w["text"] for w in after_words[j1:j2]) if tag == "replace" else ""
                before_boxes_raw.append(
                    {
                        "type": box_type,
                        "x0": word["x0"],
                        "y0": word["y0"],
                        "x1": word["x1"],
                        "y1": word["y1"],
                        "text_before": word["text"],
                        "text_after": text_after,
                    }
                )

        if tag in ("replace", "insert"):
            # after 的單字是新增或替換 → 標在 after 圖片上
            for word in after_words[j1:j2]:
                box_type = "replaced" if tag == "replace" else "added"
                text_before = " ".join(w["text"] for w in before_words[i1:i2]) if tag == "replace" else ""
                after_boxes_raw.append(
                    {
                        "type": box_type,
                        "x0": word["x0"],
                        "y0": word["y0"],
                        "x1": word["x1"],
                        "y1": word["y1"],
                        "text_before": text_before,
                        "text_after": word["text"],
                    }
                )

    # 合併相鄰框，換算成像素座標
    def _to_pixel_box(b: dict) -> dict:
        x = int(b["x0"] * scale)
        y = int(b["y0"] * scale)
        w = max(4, int((b["x1"] - b["x0"]) * scale))
        h = max(4, int((b["y1"] - b["y0"]) * scale))
        return {
            "type": b["type"],
            "x": x,
            "y": y,
            "w": w,
            "h": h,
            "text_before": b.get("text_before", ""),
            "text_after": b.get("text_after", ""),
        }

    before_merged = _merge_nearby_boxes(before_boxes_raw)
    after_merged = _merge_nearby_boxes(after_boxes_raw)

    return {
        "before_boxes": [_to_pixel_box(b) for b in before_merged],
        "after_boxes": [_to_pixel_box(b) for b in after_merged],
    }
