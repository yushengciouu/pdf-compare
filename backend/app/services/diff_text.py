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
_ARROW_RE = re.compile(r"(.+?)\s*(?:→|->)\s*(.+)")

# 匹配「從 X 修改為/變更為/更新為/改為 Y」格式
_FROM_TO_RE = re.compile(r"從\s*(.+?)\s*(?:修改為|變更為|更新為|改為|調整為)\s*(.+)")

# 匹配單引號或雙引號內的文字：'...' 或 "..." 或 「...」
_SINGLE_QUOTE_RE = re.compile(r"['\u2018\u2019](.+?)['\u2018\u2019]|[\"](. +?)[\"]|\u300c(.+?)\u300d")

# 匹配最短搜尋字串長度（太短的詞搜出來會太多）
_MIN_SEARCH_LEN = 3


def _extract_search_terms(change: dict) -> tuple[str, str]:
    """
    從 change dict 提取 (before_term, after_term)。
    回傳空字串表示無法提取。

    支援的 description 格式：
    - "從 X 修改為 Y" / "從 X 變更為 Y"
    - "X → Y"
    - added 類型：取單引號 'X' 或英文名稱作為搜尋詞
    """
    desc = change.get("description", "")
    change_type = change.get("type", "modified")

    # 1. 優先嘗試「從 X 修改為/變更為 Y」格式（LLM 最常用）
    m = _FROM_TO_RE.search(desc)
    if m:
        before = m.group(1).strip()
        after = m.group(2).strip()
        # 取最後一段（去掉前綴說明）
        before = re.split(r"[：:]", before)[-1].strip()
        after = re.split(r"[,，。]", after)[0].strip()  # 取逗號前的部分
        if before and after:
            return before, after

    # 2. 嘗試箭頭格式 "X → Y"
    m = _ARROW_RE.search(desc)
    if m:
        before = re.split(r"[：:]", m.group(1).strip())[-1].strip()
        after = re.split(r"[,，。]", m.group(2).strip())[0].strip()
        if before and after:
            return before, after

    # 3. 嘗試單引號 'X' 格式（LLM 描述 added 時常用）
    single_quotes = re.findall(r"'([^']{3,80})'", desc)
    if single_quotes:
        if change_type == "added":
            return "", single_quotes[0]
        elif change_type == "removed":
            return single_quotes[0], ""
        elif len(single_quotes) >= 2:
            return single_quotes[0], single_quotes[1]
        else:
            return single_quotes[0], single_quotes[0]

    # 4. 對 added/removed，從描述提取關鍵詞（去掉「新增」「刪除」等動詞前綴）
    clean = re.sub(
        r"^(?:新增了?|刪除了?|移除了?|增加了?)[：:：\s]*",
        "", desc
    ).strip()
    # 取括號前的部分（去掉說明）
    clean = re.split(r"[（(,，：:]", clean)[0].strip()
    # 截取合理長度（3~60 字）
    snippet = clean[:60].strip()
    if len(snippet) >= _MIN_SEARCH_LEN:
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
