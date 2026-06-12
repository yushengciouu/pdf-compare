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

# 支援各種括號/引號/書名號包裹的修改格式 (例如：將「A」修改為「B」、將『A』改成『B』)
_QUOTED_CHANGE_RE = re.compile(
    r'(?:[「『"\'【［（\(《])(.+?)(?:[」』"\'】］）\)》])'
    r'\s*(?:修改為|變更為|更新為|改為|調整為|取代為|替換為|更換為|修正為|調整至|遞移至|頁移至|改成|改成了|更名為|變更成|寫成|→|->)\s*'
    r'(?:[「『"\'【［（\(《])(.+?)(?:[」』"\'】］）\)裝\]〉》])'
)

# 更廣譜的「A 修改為 B」格式（不限前綴）
_DIRECT_CHANGE_RE = re.compile(
    r'(.+?)\s*(?:修改為|變更為|更新為|改為|調整為|取代為|替換為|更換為|修正為|調整至|遞移至|頁移至|改成|改成了|更名為|變更成|寫成)\s*(.+)'
)

# 抓取單側任何引號內的內容（適用於 added / removed 格式，如：新增「安全手冊」）
_ANY_QUOTES_RE = re.compile(r'["\'「』『」【】《》(（]([^"\'「』『」【】《》)）]{2,80})["\'「』『」【】《》)）]')

# 最短搜尋字串長度（設定為 3，但對中文字另外放寬）
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


def _is_valid_search_term(term: str) -> bool:
    """
    判斷搜尋詞是否有效。
    為了防止中文字詞（通常為 2 個字，如「金額」、「變更」）被 _MIN_SEARCH_LEN = 3 阻擋而漏標，
    如果含有中文字（CJK 字符），只要字數大於等於 2 即視為有效；英文等其他字串仍維持大於等於 3。
    """
    if not term:
        return False
    term = term.strip()
    # 檢查是否包含中文字 (CJK Unified Ideographs)
    has_cjk = any('\u4e00' <= char <= '\u9fff' for char in term)
    if has_cjk:
        return len(term) >= 2
    return len(term) >= _MIN_SEARCH_LEN


def _clean_extracted_term(term: str) -> str:
    """
    清理提取出的 before/after 搜尋詞：
    - 移除常見引號及括號
    - 移除冒號等前綴
    - 移除「從」、「由」、「將」等語意引導詞
    """
    if not term:
        return ""
    term = term.strip()
    term = term.strip("'\"`「」『』【】（）()［］[]{}<>《》 ")
    if "：" in term:
        term = term.split("：")[-1].strip()
    if ":" in term:
        term = term.split(":")[-1].strip()
    
    # 移除開頭可能多餘的介詞
    prepositions = re.split(r"^(?:從|由|將)\s*", term)
    if len(prepositions) > 1:
        term = prepositions[-1].strip()
        
    return term.strip("'\"`「」『』【】（）()［］[]{}<>《》 ")


def _clean_extracted_after(term: str) -> str:
    """
    進一步清理 after 關鍵詞：
    - 去除結尾多餘的描述或標點
    - 移除常見被誤納入的動詞
    """
    term = _clean_extracted_term(term)
    # 取點、逗、分號、中英文括號前面的主字串
    term = re.split(r"[,，。;；（(嚗\n]", term)[0].strip()
    # 移除可能被誤分到裡面的動詞首語
    term = re.sub(r"^(?:列出|新增|改為|更新為|調整為|修正為|變更為)\s*", "", term).strip()
    return term.strip("'\"`「」『』【】（）()［］[]{}<>《》 ")


def _clean_term(term: str) -> str:
    """
    對 added/removed 單側詞進行兜底清理：
    - 若包含 Entity，且有少數無關跟隨字，只取實體
    - 絕對不對純中文進行截斷 (保留完整字串)
    """
    if not term:
        return ""
    term = term.strip("'\"`「」『』【】（）()［］[]{}<>《》 ")
    m = _ENTITY_RE.search(term)
    if m:
        if len(term) < 15:
            return m.group().strip()
    return term


def _extract_list_terms(desc: str) -> list[str]:
    """
    從並列清單中提取每個搜尋項目 (避免使用中文角引號注釋，防止 py_compile 失敗)。
    """
    terms: list[str] = []
    
    # 1. 尋找列表開頭引導詞
    list_parts = re.split(r"(?:包含|包括|像是|分別為|例如|：|:)", desc, maxsplit=1)
    target_str = list_parts[1] if len(list_parts) > 1 else list_parts[0]
    
    # 2. 先處理斜線分隔的一到多個章節號/數字情況 (例如 5.3/5.4/5.5)
    # 針對斜線數字做獨立抓取
    slash_nums = re.findall(r"\b\d+\.\d+(?:\.\d+)*\b", target_str)
    for num in slash_nums:
        if len(num) >= 2:
            terms.append(num)
            
    # 3. 依據中文頓號、英文 AND/及/與、以及逗號進行多重切分
    raw_splits = re.split(r"[、，,;；]|(?:(?:\s+and\s+)|(?:\s+or\s+)|及|與|以及|\s+&\s+)", target_str)
    
    for split_item in raw_splits:
        split_item = split_item.strip()
        if not split_item:
            continue
            
        # 清洗此項
        cleaned = _clean_extracted_after(split_item)
        
        # 移除可能殘留在結尾的贅詞
        cleaned = re.sub(r"\s*(?:描述修改|描述|說明|報告|內容|修訂|變更|修改|更名|紀錄|記錄)$", "", cleaned).strip()
        
        # 再次過濾括號與引號
        cleaned = _clean_extracted_term(cleaned)
        
        # 如果該項中還藏有單獨的 Entity，例如 F-180
        m = _ENTITY_RE.search(cleaned)
        if m:
            entity_val = m.group().strip()
            if entity_val not in terms:
                terms.append(entity_val)
        
        if _is_valid_search_term(cleaned) and cleaned not in terms:
            terms.append(cleaned)
            
    return terms


def _extract_search_terms(change: dict) -> tuple[list[str], list[str]]:
    """
    從 change dict 提取 (before_terms, after_terms)。
    回傳空 list 表示無法提取。每個 list 可含多個搜尋詞。

    支援多種靈活的 description 格式：
    1. 雙側有引號/括號的修改：「A」修改為「B」
    2. 從/由 A 修改為 B
    3. A 修改為 B
    4. A → B
    5. 並列清單式 (例如包含 A、B、C 等並列項目，可提取出全部的多個條件)
    6. added/removed 類型之引號內容或 entity 列表
    """
    desc = change.get("description", "")
    change_type = change.get("type", "modified")

    # A. 針對並列清單式 (例如包含 A、B、C)
    # 若在文字中發現頓號、包含及多重並列，且為 added/removed，優先抽取多項
    if "、" in desc or "包含" in desc or "包括" in desc or "and" in desc or " & " in desc:
        items = _extract_list_terms(desc)
        if items:
            if change_type == "added":
                return [], items
            elif change_type == "removed":
                return items, []
            else:
                # modified 形式，若能用特定格式切分則用特化解析，否則將 items 作為兩側備用
                pass

    # A. 針對 modified / replaced 先嘗試進行雙側對比提取
    if change_type in ("modified", "replaced", "version", "reorder"):
        # 1. 優先匹配：前後皆包含中文引號/英文引號包裹的情況 (如 「A」修改為「B」)
        m = _QUOTED_CHANGE_RE.search(desc)
        if m:
            before = _clean_extracted_term(m.group(1))
            after = _clean_extracted_after(m.group(2))
            if _is_valid_search_term(before) or _is_valid_search_term(after):
                return [before] if before else [], [after] if after else []

        # 2. 嘗試「從/由 X 修改為 Y」格式
        m = _FROM_TO_RE.search(desc)
        if m:
            before = _clean_extracted_term(m.group(1))
            after = _clean_extracted_after(m.group(2))
            is_page_num = re.fullmatch(r"\d{1,3}\s*頁?", before) and re.fullmatch(r"\d{1,3}\s*頁?", after)
            if not is_page_num:
                if _is_valid_search_term(before) or _is_valid_search_term(after):
                    return [before] if before else [], [after] if after else []

        # 3. 嘗試廣譜「X 修改為 Y」格式
        m = _DIRECT_CHANGE_RE.search(desc)
        if m:
            before = _clean_extracted_term(m.group(1))
            after = _clean_extracted_after(m.group(2))
            is_page_num = re.fullmatch(r"\d{1,3}\s*頁?", before) and re.fullmatch(r"\d{1,3}\s*頁?", after)
            if not is_page_num:
                if _is_valid_search_term(before) or _is_valid_search_term(after):
                    return [before] if before else [], [after] if after else []

        # 4. 嘗試帶箭頭格式 "X → Y" 或 "X -> Y"
        m = _ARROW_RE.search(desc)
        if m:
            before = _clean_extracted_term(m.group(1))
            after = _clean_extracted_after(m.group(2))
            if _is_valid_search_term(before) or _is_valid_search_term(after):
                return [before] if before else [], [after] if after else []

    # B. 嘗試使用引號 findall（例如：新增 "A"、刪除 「B」）
    quoted_matches = _ANY_QUOTES_RE.findall(desc)
    if quoted_matches:
        cleaned_quotes = [q.strip() for q in quoted_matches if _is_valid_search_term(q)]
        if cleaned_quotes:
            if change_type == "added":
                return [], cleaned_quotes
            elif change_type == "removed":
                return cleaned_quotes, []
            else:
                return [cleaned_quotes[0]], [cleaned_quotes[-1]]

    # C. 嘗試使用經典 ASCII 單引號 / 雙引號備用 matcher
    double_quotes = re.findall(r'"([^"]{2,80})"', desc)
    if double_quotes:
        cleaned_dq = [q.strip() for q in double_quotes if _is_valid_search_term(q)]
        if cleaned_dq:
            if change_type == "added":
                return [], cleaned_dq
            elif change_type == "removed":
                return cleaned_dq, []
            else:
                return [cleaned_dq[0]], [cleaned_dq[-1]]

    single_quotes = re.findall(r"'([^']{2,80})'", desc)
    if single_quotes:
        cleaned_sq = [q.strip() for q in single_quotes if _is_valid_search_term(q)]
        if cleaned_sq:
            if change_type == "added":
                return [], cleaned_sq
            elif change_type == "removed":
                return cleaned_sq, []
            else:
                return [cleaned_sq[0]], [cleaned_sq[-1]]

    # D. 嘗試從 Entity regex 抓取所有特殊實體
    entities = _ENTITY_RE.findall(desc)
    if entities:
        entities = [e.strip() for e in entities if _is_valid_search_term(e)]
    if entities:
        if change_type == "added":
            return [], entities
        elif change_type == "removed":
            return entities, []
        elif change_type in ("modified", "replaced", "version", "reorder"):
            return entities, entities

    # E. 經典兜底：從描述提取單一關鍵詞
    clean = re.sub(
        r"^(?:新增了?|刪除了?|移受到了?|增加了?|添加了?|補充了?)[：:：\s]*",
        "", desc
    ).strip()
    clean = re.split(r"[（(,，：:嚗]", clean)[0].strip()
    clean = re.sub(r"\s*(?:規範|說明|定義|內容|資訊|流程|程序|標準|要求|設定)$", "", clean).strip()
    snippet = _clean_term(clean[:60].strip())
    if _is_valid_search_term(snippet):
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
    if not text or not _is_valid_search_term(text):
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
                if _is_valid_search_term(shorter):
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
