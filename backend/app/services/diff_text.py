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

# 匹配「從/由 X 修改為/移至/遞移至 Y」格式
_FROM_TO_RE = re.compile(
    r"(?:從|由)\s*(.+?)\s*(?:修改為|變更為|更新為|改為|調整為|移至|遞移至|頁移至|調整至)\s*(.+)"
)

# 最短搜尋字串長度（太短的詞搜出來會太多）
_MIN_SEARCH_LEN = 3

# 可直接在 PDF 文字層搜尋到的實體模式
_ENTITY_RE = re.compile(
    r'\bF-\d{3,4}\b'                     # 表單 ID：F-180, F-181
    r'|Table\s+\d+[-.]?\d*'              # 表格參照：Table 5-2, Table 4-1
    r'|\b\d+\.\d+(?:\.\d+)+'             # 多層節號：5.5.6, 5.4.2
    r'|\b[A-Z]{2,}[-_]\d{3,}'            # 料號/代號：W-317, MTK-123
    r'|\b[A-Z]{2,4}\b(?=\s*\()'          # 大寫縮寫後面緊跟括號說明：AST (Assembly test)
    , re.IGNORECASE
)


def _clean_term(term: str) -> str:
    """
    清理提取出的詞：
    - 去掉開頭的非 ASCII / 非中文意義字元，直到遇到 ASCII 英數字或已知模式
    - 去掉結尾殘留的單個中文字
    """
    # 若開頭有非 ASCII，嘗試找第一個英文開頭的有用片段
    m = _ENTITY_RE.search(term)
    if m and m.start() > 0:
        # 開頭有雜訊，用 entity 取代
        return m.group().strip()
    # 去掉結尾孤立的非 ASCII 字（1~2 個中文字）
    term = re.sub(r'[\u4e00-\u9fff\uff00-\uffef]{1,2}$', '', term).strip()
    return term


def _extract_search_terms(change: dict) -> tuple[list[str], list[str]]:
    """
    從 change dict 提取 (before_terms, after_terms)。
    回傳空 list 表示無法提取。每個 list 可含多個搜尋詞。

    支援的 description 格式：
    - "從 X 修改為 Y" / "從 X 變更為 Y"
    - "X → Y"
    - added/removed 類型：取引號內容或 entity 列表
    """
    desc = change.get("description", "")
    change_type = change.get("type", "modified")

    # 1. 優先嘗試「從/由 X 修改為/移至 Y」格式（LLM 最常用）
    m = _FROM_TO_RE.search(desc)
    if m:
        before = m.group(1).strip()
        after = m.group(2).strip()
        before = re.split(r"[：:]", before)[-1].strip().strip("'\"")
        after = re.split(r"[,，。嚗]", after)[0].strip().strip("'\"")
        after = re.sub(r"^(?:列出|新增|改為|更新為|調整為)\s*", "", after).strip()
        is_page_num = re.fullmatch(r"\d{1,3}\s*頁?", before) and re.fullmatch(r"\d{1,3}\s*頁?", after)
        if before and after and not is_page_num:
            return [before], [after]

    # 2. 嘗試箭頭格式 "X → Y"
    m = _ARROW_RE.search(desc)
    if m:
        before = re.split(r"[：:]", m.group(1).strip())[-1].strip()
        after = re.split(r"[,，。嚗]", m.group(2).strip())[0].strip()
        if before and after:
            return [before], [after]

    # 3. 嘗試雙引號 "X" 格式（LLM 常用於強調新值）
    double_quotes = re.findall(r'"([^"]{3,80})"', desc)
    if double_quotes:
        if change_type == "added":
            return [], double_quotes
        elif change_type == "removed":
            return double_quotes, []
        else:
            return [double_quotes[0]], [double_quotes[-1]]

    # 4. 嘗試單引號 'X' 格式（LLM 描述 added 時常用）
    single_quotes = re.findall(r"'([^']{3,80})'", desc)
    if single_quotes:
        if change_type == "added":
            return [], single_quotes
        elif change_type == "removed":
            return single_quotes, []
        else:
            return [single_quotes[0]], [single_quotes[-1]]

    # 5. 對 added/removed，先用 entity findall 抓取所有可搜尋實體
    entities = _ENTITY_RE.findall(desc)
    if entities:
        # 過濾掉太短的（3 字以下純英文縮寫容易誤標）
        entities = [e.strip() for e in entities if len(e.strip()) >= _MIN_SEARCH_LEN]
    if entities:
        if change_type == "added":
            return [], entities
        elif change_type == "removed":
            return entities, []
        elif change_type in ("modified", "replaced"):
            return entities, entities

    # 6. 對 added/removed，從描述提取關鍵詞（去掉「新增」「刪除」等動詞前綴）
    clean = re.sub(
        r"^(?:新增了?|刪除了?|移除了?|增加了?|添加了?|補充了?)[：:：\s]*",
        "", desc
    ).strip()
    clean = re.split(r"[（(,，：:嚗]", clean)[0].strip()
    clean = re.sub(r"\s*(?:規範|說明|定義|內容|資訊|流程|程序|標準|要求|設定)$", "", clean).strip()
    snippet = _clean_term(clean[:60].strip())
    if len(snippet) >= _MIN_SEARCH_LEN:
        if change_type == "removed":
            return [snippet], []
        elif change_type == "added":
            return [], [snippet]

    return [], []


def _search_in_page(pdf_path: Path, page_index: int, text: str, dpi: float) -> list[dict]:
    """
    在 PDF 指定頁面（0-based）搜尋 text，回傳像素座標框列表。
    搜尋策略：
    1. 先用原始字串搜尋
    2. 找不到時，正規化空白後再試
    3. 還找不到時，用 TEXT_INHIBIT_SPACES flag（忽略空白）再試
    """
    if not text or len(text) < _MIN_SEARCH_LEN:
        return []
    scale = dpi / 72.0
    # 正規化空白（多個空格合為一個）
    normalized = re.sub(r"\s+", " ", text).strip()
    doc = fitz.open(str(pdf_path))
    try:
        if page_index < 0 or page_index >= len(doc):
            return []
        page = doc[page_index]
        # 嘗試 1：原始搜尋
        rects = page.search_for(normalized)
        # 嘗試 2：忽略空白差異
        if not rects:
            try:
                rects = page.search_for(normalized, flags=fitz.TEXT_INHIBIT_SPACES)
            except Exception:
                pass
        # 嘗試 3：只取前幾個詞（針對 LLM 描述比 PDF 原文多字的情況）
        # 例如 "5.12.1 Automotive Product 規範" → 先試 "5.12.1 Automotive Product"
        if not rects and len(normalized) > 10:
            words = normalized.split()
            for n_words in range(len(words) - 1, 1, -1):
                shorter = " ".join(words[:n_words])
                if len(shorter) >= _MIN_SEARCH_LEN:
                    rects = page.search_for(shorter)
                    if rects:
                        break
        # 嘗試 4：只取前 30 字（針對超長描述）
        if not rects and len(normalized) > 30:
            rects = page.search_for(normalized[:30].strip())
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
    state: str = "paired",
) -> dict:
    """
    根據 LLM changes 清單，在 before/after PDF 頁面搜尋差異位置。

    state:
      - "paired"  : before + after 都搜尋
      - "inserted": 只搜尋 after（新增頁，before 不存在）
      - "deleted" : 只搜尋 before（刪除頁，after 不存在）

    回傳格式：
    {
        "before_boxes": [...],
        "after_boxes":  [...]
    }
    座標單位：像素（依 dpi 換算自 PDF points）。
    """
    before_boxes: list[dict] = []
    after_boxes: list[dict] = []

    can_search_before = (state != "inserted") and (before_page_index >= 0)
    can_search_after  = (state != "deleted")  and (after_page_index  >= 0)

    for change in changes:
        change_type = change.get("type", "modified")
        before_terms, after_terms = _extract_search_terms(change)

        if change_type in ("modified", "replaced"):
            if can_search_before:
                for before_term in before_terms:
                    for b in _search_in_page(before_pdf, before_page_index, before_term, dpi):
                        before_boxes.append({**b, "type": "replaced",
                                             "text_before": before_term, "text_after": ", ".join(after_terms)})
            if can_search_after:
                for after_term in after_terms:
                    for b in _search_in_page(after_pdf, after_page_index, after_term, dpi):
                        after_boxes.append({**b, "type": "replaced",
                                            "text_before": ", ".join(before_terms), "text_after": after_term})

        elif change_type == "removed":
            if can_search_before:
                for before_term in before_terms:
                    for b in _search_in_page(before_pdf, before_page_index, before_term, dpi):
                        before_boxes.append({**b, "type": "removed",
                                             "text_before": before_term, "text_after": ""})

        elif change_type == "added":
            if can_search_after:
                for after_term in after_terms:
                    for b in _search_in_page(after_pdf, after_page_index, after_term, dpi):
                        after_boxes.append({**b, "type": "added",
                                            "text_before": "", "text_after": after_term})

    return {
        "before_boxes": before_boxes,
        "after_boxes": after_boxes,
    }
