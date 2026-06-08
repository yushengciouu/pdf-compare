"""
文字層差異服務 — Plan B：根據 LLM changes 在 PDF 文字層搜尋位置

流程：
1. LLM 分析完後，changes 清單已包含語意上有意義的差異描述
   例如：{"type": "modified", "description": "壹佰萬元 → 貳佰萬元"}
2. 用 regex 從 description 提取「舊文字」和「新文字」
3. 用 page.search_for() 在 before/after PDF 文字層搜尋這些字串
4. 回傳像素座標，前端畫 SVG 框

優點：只標記 LLM 認定有語意意義的差異，不會誤標純位移。
"""

from __future__ import annotations

import re
from pathlib import Path

import fitz  # PyMuPDF

# 匹配 "舊文字 → 新文字" 格式（支援中文箭頭 → 和 ASCII ->）
_ARROW_RE = re.compile(r"(.+?)\s*(?:→|->|→)\s*(.+)")

# 匹配「：」後面的引號內文字，例如 '第二條金額：「壹佰萬元」'
_COLON_QUOTE_RE = re.compile(r"[：:][「『""](.+?)[」』""]")

# 最短搜尋字串長度（太短的詞搜出來會太多）
_MIN_SEARCH_LEN = 2


def _extract_search_terms(change: dict) -> tuple[str, str]:
    """
    從 change dict 提取 (before_term, after_term)。
    回傳空字串表示無法提取。
    """
    desc = change.get("description", "")
    change_type = change.get("type", "modified")

    # 優先嘗試箭頭格式 "X → Y"
    m = _ARROW_RE.search(desc)
    if m:
        before = m.group(1).strip()
        after = m.group(2).strip()
        # 去除前綴說明文字，只取最後一個冒號後面的部分
        before = re.split(r"[：:]", before)[-1].strip().strip("\u300c\u300e\u201c\u2018\u300d\u300f\u201d\u2019")
        after = re.split(r"[：:]", after)[-1].strip().strip("\u300c\u300e\u201c\u2018\u300d\u300f\u201d\u2019")
        return before, after

    # 嘗試「：引號」格式
    quotes = _COLON_QUOTE_RE.findall(desc)
    if len(quotes) >= 2:
        return quotes[0], quotes[1]
    if len(quotes) == 1:
        if change_type == "removed":
            return quotes[0], ""
        elif change_type == "added":
            return "", quotes[0]

    # 對 added/removed，取描述的前半段作為搜尋詞（最多 30 字）
    clean_desc = re.split(r"[（(]", desc)[0].strip()  # 去掉括號說明
    clean_desc = re.sub(r"^(?:新增|刪除|增加|移除|新增了?|刪除了?)[：:]?\s*", "", clean_desc)
    snippet = clean_desc[:30].strip()
    if change_type == "removed":
        return snippet, ""
    elif change_type == "added":
        return "", snippet

    return "", ""


def _search_in_page(pdf_path: Path, page_index: int, text: str, dpi: float) -> list[dict]:
    """
    在 PDF 指定頁面（0-based）搜尋 text，回傳像素座標框列表。
    """
    if not text or len(text) < _MIN_SEARCH_LEN:
        return []
    scale = dpi / 72.0
    doc = fitz.open(str(pdf_path))
    try:
        if page_index < 0 or page_index >= len(doc):
            return []
        page = doc[page_index]
        rects = page.search_for(text)
        boxes = []
        for r in rects:
            boxes.append({
                "x": int(r.x0 * scale),
                "y": int(r.y0 * scale),
                "w": max(4, int((r.x1 - r.x0) * scale)),
                "h": max(4, int((r.y1 - r.y0) * scale)),
            })
        return boxes
    finally:
        doc.close()


def search_changes_boxes(
    before_pdf: Path,
    after_pdf: Path,
    before_page_index: int,
    after_page_index: int,
    changes: list[dict],
    dpi: float = 96.0,
) -> dict:
    """
    根據 LLM changes 清單，在 before/after PDF 頁面搜尋差異位置。

    回傳格式：
    {
        "before_boxes": [{"type": "removed"|"replaced", "x", "y", "w", "h",
                          "text_before", "text_after"}],
        "after_boxes":  [{"type": "added"|"replaced",   "x", "y", "w", "h",
                          "text_before", "text_after"}]
    }
    座標單位：像素（依 dpi 換算自 PDF points）。
    """
    before_boxes: list[dict] = []
    after_boxes: list[dict] = []

    for change in changes:
        change_type = change.get("type", "modified")
        before_term, after_term = _extract_search_terms(change)

        if change_type in ("modified", "replaced"):
            # 在 before 找舊文字（黃框）
            if before_term:
                for b in _search_in_page(before_pdf, before_page_index, before_term, dpi):
                    before_boxes.append({**b, "type": "replaced",
                                         "text_before": before_term, "text_after": after_term})
            # 在 after 找新文字（黃框）
            if after_term:
                for b in _search_in_page(after_pdf, after_page_index, after_term, dpi):
                    after_boxes.append({**b, "type": "replaced",
                                        "text_before": before_term, "text_after": after_term})

        elif change_type == "removed":
            if before_term:
                for b in _search_in_page(before_pdf, before_page_index, before_term, dpi):
                    before_boxes.append({**b, "type": "removed",
                                         "text_before": before_term, "text_after": ""})

        elif change_type == "added":
            if after_term:
                for b in _search_in_page(after_pdf, after_page_index, after_term, dpi):
                    after_boxes.append({**b, "type": "added",
                                        "text_before": "", "text_after": after_term})

    return {
        "before_boxes": before_boxes,
        "after_boxes": after_boxes,
    }
