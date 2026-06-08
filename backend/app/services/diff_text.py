"""
文字層差異服務

使用 PyMuPDF 取得 PDF 頁面的文字區塊（block）及其座標，
再用 difflib.SequenceMatcher 比對 before/after 的文字區塊序列，
找出「內容真正有變化」的區塊，回傳附有像素座標的差異框列表。

以區塊（段落/表格格）為單位比對，比單字層級穩定：
- 純位移（同樣文字出現在不同位置）→ SequenceMatcher 能正確識別為 equal，不標記
- 常見短字（的、了、是）在段落層級不會造成錯誤對齊
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path

import fitz  # PyMuPDF

# 區塊文字正規化：移除多餘空白，方便比對
_WHITESPACE_RE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip()


def _get_blocks(pdf_path: Path, page_index: int) -> list[dict]:
    """
    取得指定頁面（0-based）的所有文字區塊及其 PDF 座標（unit: points, 72dpi）。
    回傳格式：[{"text": str, "x0": float, "y0": float, "x1": float, "y1": float}]
    只回傳 type=0（文字）的區塊，忽略圖片區塊。
    """
    doc = fitz.open(str(pdf_path))
    try:
        if page_index < 0 or page_index >= len(doc):
            return []
        page = doc[page_index]
        # get_text("blocks") 回傳 (x0, y0, x1, y1, text, block_no, block_type)
        raw_blocks = page.get_text("blocks")
        result = []
        for b in raw_blocks:
            block_type = int(b[6])
            if block_type != 0:  # 只要文字區塊
                continue
            text = _normalize(b[4])
            if not text:
                continue
            result.append({"text": text, "x0": b[0], "y0": b[1], "x1": b[2], "y1": b[3]})
        return result
    finally:
        doc.close()


def compute_text_diff_boxes(
    before_pdf: Path,
    after_pdf: Path,
    before_page_index: int,
    after_page_index: int,
    dpi: float = 96.0,
    min_change_ratio: float = 0.15,
) -> dict:
    """
    比對 before/after 兩頁的文字層（區塊層級），回傳差異框列表。

    min_change_ratio: replace 操作中，若兩段文字相似度 > (1 - min_change_ratio)，
                      視為輕微差異（如頁碼），仍標記但可調整門檻過濾。

    回傳格式：
    {
        "before_boxes": [{"type": "removed"|"replaced", "x", "y", "w", "h",
                           "text_before", "text_after"}],
        "after_boxes":  [{"type": "added"|"replaced",   "x", "y", "w", "h",
                           "text_before", "text_after"}]
    }
    座標單位：像素（依 dpi 換算自 PDF points）。
    """
    scale = dpi / 72.0

    before_blocks = _get_blocks(before_pdf, before_page_index)
    after_blocks = _get_blocks(after_pdf, after_page_index)

    before_texts = [b["text"] for b in before_blocks]
    after_texts = [b["text"] for b in after_blocks]

    # autojunk=False：關閉「熱門元素自動視為 junk」，避免常見段落被忽略
    matcher = difflib.SequenceMatcher(None, before_texts, after_texts, autojunk=False)

    before_boxes: list[dict] = []
    after_boxes: list[dict] = []

    def _to_pixel_box(blk: dict, box_type: str, text_before: str, text_after: str) -> dict:
        return {
            "type": box_type,
            "x": int(blk["x0"] * scale),
            "y": int(blk["y0"] * scale),
            "w": max(4, int((blk["x1"] - blk["x0"]) * scale)),
            "h": max(4, int((blk["y1"] - blk["y0"]) * scale)),
            "text_before": text_before[:120],   # 截斷避免 tooltip 過長
            "text_after": text_after[:120],
        }

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue

        if tag == "replace":
            # 進一步確認：若文字內容極為相似（只差數字/頁碼），仍標記
            # 若完全相同（normalize 後），跳過（pure layout shift）
            before_chunk = " ".join(before_texts[i1:i2])
            after_chunk = " ".join(after_texts[j1:j2])
            if before_chunk == after_chunk:
                continue  # 同樣文字，純位移，不標記

            # 計算相似度，太相似（>0.95）且都很短（頁碼類）也跳過
            sim = difflib.SequenceMatcher(None, before_chunk, after_chunk).ratio()
            is_short = len(before_chunk) < 10 and len(after_chunk) < 10
            if sim > 0.95 and is_short:
                continue

            for blk in before_blocks[i1:i2]:
                before_boxes.append(_to_pixel_box(blk, "replaced", blk["text"], after_chunk))
            for blk in after_blocks[j1:j2]:
                after_boxes.append(_to_pixel_box(blk, "replaced", before_chunk, blk["text"]))

        elif tag == "delete":
            before_chunk = " ".join(before_texts[i1:i2])
            for blk in before_blocks[i1:i2]:
                before_boxes.append(_to_pixel_box(blk, "removed", blk["text"], ""))

        elif tag == "insert":
            after_chunk = " ".join(after_texts[j1:j2])
            for blk in after_blocks[j1:j2]:
                after_boxes.append(_to_pixel_box(blk, "added", "", blk["text"]))

    return {
        "before_boxes": before_boxes,
        "after_boxes": after_boxes,
    }
