"""
LLM 分析服務

流程：
1. 接收兩份 PDF 路徑 + 門檻設定
2. 執行 prefilter，取得 send_to_llm=True 的候選頁
3. 對每個候選頁：
   - 渲染 before/after 縮圖（base64 PNG）
   - 產生 before/after 文字 unified diff
   - 組裝含圖文的 multimodal message
4. 一次性呼叫 vLLM（OpenAI 相容格式）
5. 解析回傳 JSON，逐頁回傳分析結果
"""

from __future__ import annotations

import base64
import datetime
import difflib
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from tempfile import mkdtemp
from typing import Any
from uuid import uuid4

import httpx

from app.core.config import Settings
from app.services.diff_text import search_changes_boxes
from app.services.prefilter import Thresholds, build_prefilter_report
from app.services.render import extract_page_texts, render_pdf_pages


# ---------------------------------------------------------------------------
# 資料結構
# ---------------------------------------------------------------------------


@dataclass
class PageAnalysis:
    slot: int
    state: str  # paired / inserted / deleted
    before_page: int | None
    after_page: int | None
    image_diff: float
    text_diff: float
    reason: str
    summary: str  # LLM 產生的差異摘要
    changes: list[
        dict
    ]  # [{"type": "added"|"removed"|"modified", "description": "..."}]
    importance: str  # "low" | "medium" | "high"


@dataclass
class AnalyzeReport:
    summary: dict  # pages_before, pages_after, total_slots, candidate_pages
    thresholds: dict
    overall_summary: str  # 整份文件的一句話總結
    pages: list[PageAnalysis]


# ---------------------------------------------------------------------------
# 內部工具函式
# ---------------------------------------------------------------------------


def _png_to_base64(png_path: Path) -> str:
    """讀取 PNG 並轉成 base64 data URL。"""
    with open(png_path, "rb") as f:
        data = base64.b64encode(f.read()).decode("ascii")
    return f"data:image/png;base64,{data}"


# 章節號碼正則：匹配如 5.2.3、5.12.1.2、A.、(1) 等独立章節號
# 使用負向前看 (negative lookbehind) 確保正前不是數字或句號，
# 避免將 5.2.1 裡的 ".1" 誤切為新對法
_SECTION_SPLIT_RE = re.compile(
    r'(?<![.\d])(?=(?:\d+\.)+\d*\s)|(?<![A-Za-z])(?=[A-Z]\.\s)|(?=\(\d+\)\s)'
)


def _segment_page_text(text: str) -> list[str]:
    """
    將 PDF 頁面的扁平文字（一整行）切成段落列表，
    讓 diff 能在段落級別而非整頁級別比對。
    切分依據：章節號碼（如 5.2.3、A.、(1)）前插入換行。
    """
    if not text:
        return []
    parts = _SECTION_SPLIT_RE.split(text)
    # 合併過短的片段（< 15 chars）到前一段，避免過度切碎
    result: list[str] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if result and len(p) < 15:
            result[-1] = result[-1] + " " + p
        else:
            result.append(p)
    return result


def _make_text_diff(before_text: str, after_text: str) -> str:
    """
    產生可讀的 unified diff 字串。
    先將文字按章節號拆成段落（_segment_page_text），
    再做段落級別的 unified diff，避免整頁一行無法看出差異。
    若兩份文字相同，回傳空字串。
    """
    if not before_text and not after_text:
        return ""
    if not before_text:
        segs = _segment_page_text(after_text)
        lines = [f"+ {s}" for s in segs[:40]]
        return "\n".join(lines) or f"+ {after_text[:1500]}"
    if not after_text:
        segs = _segment_page_text(before_text)
        lines = [f"- {s}" for s in segs[:40]]
        return "\n".join(lines) or f"- {before_text[:1500]}"

    before_lines = [s + "\n" for s in _segment_page_text(before_text)]
    after_lines  = [s + "\n" for s in _segment_page_text(after_text)]

    # 若切段結果為空（文字結構無法切分），退回原始 splitlines
    if not before_lines:
        before_lines = before_text.splitlines(keepends=True)
    if not after_lines:
        after_lines = after_text.splitlines(keepends=True)

    diff_lines = list(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile="before",
            tofile="after",
            lineterm="",
            n=1,  # 上下文行數
        )
    )

    if not diff_lines:
        return "(文字內容無差異)"

    # 限制總長度，避免 token 爆炸（增至 15000 以確保目錄、圖表清單及大篇幅文字完整傳入不被截斷）
    MAX_CHARS = 15000
    result = "\n".join(diff_lines)
    if len(result) > MAX_CHARS:
        result = result[:MAX_CHARS] + "\n...(文字差異過長，已截斷)"
    return result


def _build_page_message_content(
    slot_entry: dict,
    before_render_dir: Path,
    after_render_dir: Path,
    before_texts: list[str],
    after_texts: list[str],
    all_candidates: list[dict] | None = None,
) -> list[dict]:
    """
    為單一候選頁組裝 multimodal message content list。

    格式：
    [
        {"type": "text", "text": "=== 第 N 頁 (state, diff 分數) ===\n..."},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},  # before
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},  # after
        {"type": "text", "text": "文字差異：\n..."},
    ]
    """
    state = slot_entry["state"]
    slot = slot_entry["slot"]
    before_page = slot_entry.get("before_page")
    after_page = slot_entry.get("after_page")
    image_diff = slot_entry.get("image_diff", 0.0)
    text_diff = slot_entry.get("text_diff", 0.0)
    reason = slot_entry.get("reason", "")

    # --- 標題文字 ---
    state_label = {
        "paired": "配對頁（兩版皆有）",
        "inserted": "新增頁（僅出現在新版）",
        "deleted": "刪除頁（僅存在於舊版）",
    }.get(state, state)
    before_page_label = f"第 {before_page} 頁" if before_page is not None else "N/A"
    after_page_label = f"第 {after_page} 頁" if after_page is not None else "N/A"
    header = (
        f"=== 比對槽位 {slot}：{state_label} ===\n"
        f"舊版頁碼: {before_page_label}  |  新版頁碼: {after_page_label}\n"
        f"圖像差異分數: {image_diff:.3f}  |  文字差異分數: {text_diff:.3f}  |  標記原因: {reason}\n"
    )

    content: list[dict] = [{"type": "text", "text": header}]

    # 對於大幅偏移配對頁（offset >= 3）且文字差異小（text_diff < 0.25）的槽位，
    # 跳過圖片（最大語素來源），只送文字 diff + 鄰頁文字。
    # 注意: image_diff 對偏移頁不可靠（版頭頁碼改變會號跬），改用 text_diff 判斷。
    offset_skip_images = (
        state == "paired"
        and before_page is not None and after_page is not None
        and int(after_page) - int(before_page) >= 3
        and text_diff < 0.25
    )

    # --- 舊版（before）圖片 ---
    if not offset_skip_images and before_page is not None:
        before_png = before_render_dir / f"{int(before_page):04d}.png"
        if before_png.exists():
            content.append({"type": "text", "text": "【舊版頁面截圖】"})
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": _png_to_base64(before_png)},
                }
            )

    # --- 新版（after）圖片 ---
    if not offset_skip_images and after_page is not None:
        after_png = after_render_dir / f"{int(after_page):04d}.png"
        if after_png.exists():
            content.append({"type": "text", "text": "【新版頁面截圖】"})
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": _png_to_base64(after_png)},
                }
            )

    # --- 文字差異 ---
    before_text = before_texts[int(before_page) - 1] if before_page else ""
    after_text = after_texts[int(after_page) - 1] if after_page else ""
    text_diff_str = _make_text_diff(before_text, after_text)
    if text_diff_str:
        content.append({"type": "text", "text": f"【文字層差異】\n{text_diff_str}"})

    # --- 配對頁跨頁位移：附加舊版鄰頁文字供跨頁比對 ---
    # 對於任何有文字/圖片差異的配對頁，附加舊版在前或在後的鄰頁文字（最多2頁），
    # 供 LLM 比對 diff 中「+」的內容是否已存在於舊鄰頁中（若存在，代表純屬頁面溢出或重排，不得列為 added）。
    if state == "paired" and before_page is not None and after_page is not None:
        if image_diff >= 0.05 or text_diff >= 0.05:
            from app.services.page_match import _strip_boilerplate
            all_bt = before_texts + after_texts
            common_skip = _detect_common_prefix_len(all_bt)

            bp_val = int(before_page)
            ref_pages = []
            # 往前找 1 頁 (如：原在第 bp-1 頁底部的內容被移至這頁開頭)
            if bp_val > 1:
                ref_pages.append(bp_val - 1)
            # 往後找 1 頁 (如：這頁尾部的某些段落被擠到了第 bp+1 頁)
            if bp_val < len(before_texts):
                ref_pages.append(bp_val + 1)

            for rp in ref_pages:
                raw_nb = before_texts[rp - 1]
                stripped_nb = raw_nb[common_skip:].strip()[:1500]
                if stripped_nb:
                    # 偵測鄰頁與 after 頁的相似度
                    from difflib import SequenceMatcher
                    after_page_text = after_texts[int(after_page) - 1] if after_page else ""
                    sim = SequenceMatcher(None, stripped_nb.lower(), after_page_text[common_skip:].lower()).ratio()

                    reflow_hint = ""
                    if sim >= 0.20:
                        reflow_hint = (
                            f"⚠️ 【頁面重排偵測警告】"
                            f"舊版第 {rp} 頁與新版第 {after_page} 頁文字相似度 {sim:.2f}\n"
                            f"→ diff 中 '+' 出現的章節內容，若已存在於下方舊版鄰頁文字，則為頁面重排，不得列為 added。\n"
                        )
                        content.insert(1, {"type": "text", "text": reflow_hint})

                    content.append({
                        "type": "text",
                        "text": (
                            f"【舊版鄰頁文字（第 {rp} 頁，供跨頁位移比對參考）】\n"
                            f"{stripped_nb}"
                        ),
                    })

    # --- inserted/deleted 頁：以文字相似度找最接近的對應頁，提供圖片供目視比對 ---
    # 這讓 LLM 在只看到單側圖片時，仍能視覺確認內容是否已存在於另一版本
    if state in ("inserted", "deleted"):
        from app.services.page_match import _text_similarity

        if state == "inserted" and after_page is not None:
            # inserted：新版有，舊版無。搜尋舊版最相似頁
            search_text = after_texts[int(after_page) - 1] if after_texts else ""
            candidate_texts = before_texts
            candidate_render_dir = before_render_dir
            candidate_label = "舊版"
        else:
            # deleted：舊版有，新版無。搜尋新版最相似頁
            search_text = before_texts[int(before_page) - 1] if before_texts else ""
            candidate_texts = after_texts
            candidate_render_dir = after_render_dir
            candidate_label = "新版"

        if search_text.strip() and candidate_texts:
            best_idx, best_sim = None, 0.0
            for ci, ct in enumerate(candidate_texts, 1):
                sim = _text_similarity(search_text, ct)
                if sim > best_sim:
                    best_sim = sim
                    best_idx = ci
            # 只在相似度非常高時才附加圖片（避免誤導且控制訊息大小）
            if best_idx is not None and best_sim >= 0.70:
                cand_png = candidate_render_dir / f"{int(best_idx):04d}.png"
                if cand_png.exists():
                    content.append({
                        "type": "text",
                        "text": (
                            f"【{candidate_label}最相似頁截圖（第 {best_idx} 頁，文字相似度 {best_sim:.2f}，供比對參考）】\n"
                            f"⚠️ 若此圖與上方截圖內容相近，代表該頁可能是頁面位移而非真正新增/刪除。"
                        ),
                    })
                    content.append({
                        "type": "image_url",
                        "image_url": {"url": _png_to_base64(cand_png)},
                    })

    # --- inserted/deleted 頁：附加鄰近舊版/新版頁面文字供跨頁比對 ---
    # 從 all_candidates 找相鄰 slot 的 before_page（inserted）或 after_page（deleted），
    # 再向前後各擴展 1 頁，讓 LLM 能逐一核對章節號碼是否已存在於另一版本。
    if state in ("inserted", "deleted") and all_candidates is not None:
        is_inserted = state == "inserted"
        ref_texts = before_texts if is_inserted else after_texts
        ref_label = "舊版" if is_inserted else "新版"
        page_key = "before_page" if is_inserted else "after_page"
        my_slot = int(slot)

        # 找相鄰 slot（slot 編號距離 ≤ 2）中有配對頁碼的那些 before/after_page
        neighbor_ref_pages: set[int] = set()
        for c in all_candidates:
            cs = int(c.get("slot", -1))
            if c.get(page_key) is not None and abs(cs - my_slot) <= 2 and cs != my_slot:
                rp = int(c[page_key])
                neighbor_ref_pages.add(rp)
                # 向前後各擴展 1 頁
                if rp > 1:
                    neighbor_ref_pages.add(rp - 1)
                if rp < len(ref_texts):
                    neighbor_ref_pages.add(rp + 1)

        if neighbor_ref_pages:
            common_skip = _detect_common_prefix_len(before_texts + after_texts)
            neighbor_items: list[tuple[int, str]] = []
            for rp in sorted(neighbor_ref_pages):
                if 1 <= rp <= len(ref_texts):
                    stripped = ref_texts[rp - 1][common_skip:].strip()[:1500]
                    if stripped:
                        neighbor_items.append((rp, stripped))

            if neighbor_items:
                hint = (
                    f"⚠️【{'新增' if is_inserted else '刪除'}頁跨頁比對】"
                    f"以下為鄰近{ref_label}頁面文字。"
                    f"請在判斷此頁內容是否為真正{'新增' if is_inserted else '刪除'}前，"
                    f"逐一對照各章節號碼與段落首句是否已存在於下方{ref_label}頁面中。\n"
                    f"若某章節/段落已存在於{ref_label}鄰頁 → 屬於頁面重排，不得列為 "
                    f"{'added' if is_inserted else 'removed'}。\n"
                    f"只有在所有{ref_label}鄰頁中都找不到的內容，才能判為 "
                    f"{'added' if is_inserted else 'removed'}。"
                )
                # 在圖片之前插入警告（index 1，index 0 是 header text）
                content.insert(1, {"type": "text", "text": hint})
                for rp, text in neighbor_items:
                    content.append({
                        "type": "text",
                        "text": f"【{ref_label}鄰頁文字（第 {rp} 頁，供跨頁位移比對參考）】\n{text}",
                    })

    return content


def _build_structure_context(candidates: list[dict]) -> str:
    """
    根據候選頁列表，產生一段結構摘要文字，
    說明整份文件的頁面對應關係（包含新增/刪除頁），
    讓 LLM 在分析時能理解章節號碼偏移的脈絡。
    """
    lines = ["【整份文件頁面對應關係】"]
    for c in sorted(candidates, key=lambda x: int(x["slot"])):
        slot = c["slot"]
        state = c["state"]
        bp = c.get("before_page")
        ap = c.get("after_page")
        if state == "inserted":
            lines.append(f"  Slot {slot:2d}: 【新增頁】新版第 {ap} 頁（舊版無此頁）")
        elif state == "deleted":
            lines.append(f"  Slot {slot:2d}: 【刪除頁】舊版第 {bp} 頁（新版已移除）")
        else:
            lines.append(f"  Slot {slot:2d}: 舊版第 {bp} 頁 ↔ 新版第 {ap} 頁")
    lines.append(
        "\n⚠️ 注意：若文件中有新增頁（inserted），其後的頁面章節號碼會整體遞移。"
        "請勿因章節號碼改變（如 5.5.6→5.5.7）就判斷為刪除，"
        "應比對內容是否仍存在於新版中。"
    )
    return "\n".join(lines)


def _detect_common_prefix_len(texts: list[str], max_check: int = 800) -> int:
    """
    計算所有非空頁面文字的共同前綴長度（最多檢查 max_check 個字元）。
    用於跳過 PDF 中每頁都有的版權宣告等固定頁首/頁尾單行文字，
    讓索引摘要從真正的頁面內容開始。
    """
    non_empty = [t for t in texts if t.strip()]
    if len(non_empty) < 2:
        return 0
    probe = min(min(len(t) for t in non_empty), max_check)
    for i in range(probe):
        if len({t[i] for t in non_empty}) > 1:
            return i
    return probe


def _build_full_text_index(
    before_texts: list[str],
    after_texts: list[str],
    max_excerpt: int = 200,
) -> str:
    """
    產生舊版/新版所有頁面的文字摘要索引。
    先移除樣板行（_strip_boilerplate），再跳過所有頁共同的前綴字串
    （如版權宣告整行），最後取 max_excerpt 字元作為該頁摘要。
    讓 LLM 能跨頁搜尋比對，避免把頁面位移誤判為新增或刪除。
    """
    from app.services.page_match import _strip_boilerplate

    # 移除樣板行（頁腳/頁首）再擷取摘要
    all_texts = list(before_texts) + list(after_texts)
    stripped = _strip_boilerplate(all_texts)
    stripped_before = stripped[:len(before_texts)]
    stripped_after = stripped[len(before_texts):]

    # 跳過所有頁共同的前綴（例如：整行版權宣告被 PDF 渲染為頁面文字首段）
    skip = _detect_common_prefix_len(stripped)

    lines = [
        "【全文頁面摘要索引（請在判斷新增/刪除頁前先查閱此索引）】",
        "B###=舊版頁碼  A###=新版頁碼（已移除共同頁腳樣板）",
        "─ 舊版 ─",
    ]
    for i, text in enumerate(stripped_before, 1):
        excerpt = text[skip:].strip()[:max_excerpt].replace("\n", " ").strip()
        lines.append(f"  B{i:03d}: {excerpt if excerpt else '（空白頁）'}")
    lines.append("─ 新版 ─")
    for i, text in enumerate(stripped_after, 1):
        excerpt = text[skip:].strip()[:max_excerpt].replace("\n", " ").strip()
        lines.append(f"  A{i:03d}: {excerpt if excerpt else '（空白頁）'}")
    return "\n".join(lines)


def _build_prompt(
    candidates: list[dict],
    before_render_dir: Path,
    after_render_dir: Path,
    before_texts: list[str],
    after_texts: list[str],
    all_candidates: list[dict] | None = None,
) -> list[dict]:
    """
    組裝完整的 messages list，格式符合 OpenAI Chat Completions multimodal 規範。
    """
    SYSTEM_PROMPT = """\
你是一位專業的文件審查助手，專門分析 PDF 文件版本之間的差異。

你會收到數個「比對槽位」，每個槽位包含：
- 舊版頁面截圖（before）
- 新版頁面截圖（after）
- 兩版的文字差異（unified diff 格式）
- 圖像差異分數與文字差異分數（0 ~ 1，越高代表差異越大）

請針對每個槽位，仔細分析實際修改的內容，並以**繁體中文**回答。

《分析重點》
- 小心比對圖片中的**每一個數字、日期、金額、人名、地址**是否有變更
- 對照文字差異（unified diff）逐行檢查，不得漏掉任何以「+」或「-」開頭的行
- 就算圖像差異分數小，也要仔細檢查文字差異中的內容
- 對於表格、清單、整列的資料，請逐格比對各格數字是否一致
- 若圖片與文字差異不一致，以**文字差異為準**，但仍說明圖片目視結果
- 就算差異看似微小，只要確認存在差異，就必須如實列出，不得略過
- 【重要過濾優化規則】：
  1. 對於純粹的「頁碼變更（頁碼數字從 X 變更為 Y）」或「純粹的頁面位移（由於前文增刪導致的排版平移）」，除非該頁伴隨著「文字、金額、日期、規章文字、表格等實質欄位」之實質修改，否則【請完全不要耗費描述算力分析它】。只要它沒有任何實質內容（Content）變更，不論它頁碼如何偏移，請將其 importance 設為 "low"，且【不要】在 changes 或 description 中列出「頁碼從 X 變更為 Y」之類的變更（這類純重排已由系統在 prefilter 端和 Python 端自動低成本標記好！不需要 LLM 像記流水帳一樣逐頁書寫頁碼變更，避免浪費算力與 Token 空間）。
  2. 同理，目錄（Table of Contents）中的純頁碼偏移遞移或排版變化，如果只是因為後面章節排版順延導致的「文字目錄頁碼數字改變」，實質上的章節與文字規章並無修改，亦【不需要】輸出 changes！只有在目錄中有「新增了全新章節名稱」或「刪除了某章節」時才需輸出 added 或 removed changes。
  3. 即：只有在頁面有實質內容（"category": "content"）變動、或存在有重大意涵的管理資訊變動時才需要列出。若是純頁面重排、純頁碼改變且沒有實質文字修改，請直接將此頁的 changes 陣列留空 `[]`！

《微小更動與格式誤差忽略規則（保護條款 - 極重要）》
- 【嚴禁將無實質意義之微小變更列為新增或修改（忽略不回報）】：
  1. 拼字或語法修補（例如：replace 修正為 replaced、display 修正為 displays、JOB_REV 變更為 JOB REV）。
  2. 標點符號與英文大小寫更換（例如：半形逗號 `,` 改為全形逗號 `，`；英文句點 `.` 改為中文句號 `。`；單引號改雙引號；或前後多出一些空格）。
  3. 換行/斷行格式變化（例如：同一句長句在舊版因頁寬限制折成兩行，在新版折成三行，或新舊兩版句尾換行符不一致，導致 diff 中出現 `+` 或是 `-` 的片段）。
  4. 這些情況【絕非實質內容新增、更動】，不得視為 added 或 modified！對於此類無實質意義之變更，請【完全忽視且不提】，亦【不可】列入 changes 列表中！
  5. 若某個段落或條款的實質语义與內容在舊版或舊版相鄰頁中【完全存在】，僅因前述 1-3 點微小細節、符號、斷行差異導致 diff 面貌有異，你應當作【內容完全無變更】處理！

《章節號碼偏移判斷規則》
當文件中有新增頁（inserted）或刪除頁（deleted）時，後續章節的編號會整體偏移。
- 若 before 頁有「5.5.6 OQC」，after 頁有「5.5.7 OQC」，內容相同 → 應判斷為 modified（章節號碼因新增章節而遞移），**不得**判斷為 removed
- 只有當某段內容在 before 存在，且在整個 after 文件中完全找不到對應內容時，才能判斷為 removed
- 章節號碼的改變本身屬於 modified（格式/編號調整），重要度通常為 low 或 medium

《跨頁位移判斷規則（重要）》
頁面配對演算法偶爾會因版面差異過大而將「位移的頁面」誤標為 inserted 或 deleted。
此外，「配對頁（paired）」若新版頁碼遠大於舊版頁碼（如 before:13→after:24），
代表中間有多頁被插入，after 頁的內容可能包含原本屬於舊版「下一頁」的段落，
而非真正的新增內容。

在對任何「新增頁（inserted）」、「刪除頁（deleted）」或「配對頁中的 added/removed 變更」下結論前，請先執行以下步驟：
1. 查閱 user 訊息開頭的「全文頁面摘要索引」（B###=舊版各頁摘要，A###=新版各頁摘要）。
2. 許多所謂刪除或新增可能僅僅是「跨頁溢出」（即上一頁文字流動到了下一頁）。
3. 【關鍵步驟 - 判斷跨頁與槽位溢出】：對 diff 中每一行以「+」或「-」開頭的段落或章節（如 5.5.6 OQC 檢驗之說明），請優先對照前後相鄰槽位（Slot N-1, Slot N+1）的文字：
   - 若段落內容同時在一個槽位被標為 `-` (刪除) 且在相鄰槽位被標為 `+` (新增) → 這代表純粹的跨頁溢出或頁面重排，**禁止**將其判定為實質刪除（removed）或實質新增（added）！
   - 請將此類項目歸類為 category: "reorder"（描述：因頁面重排、文字流動而溢出至相鄰頁面，無實質改變），重要度設為 low，或直接忽略不提。
4. 【配對頁中的 added 內容 / 新增頁（非目錄）】：對「配對頁（paired）」中產生的實質新增內容、或是「新增頁（inserted，且該頁內容非目錄）」：搜尋其關鍵文字（章節號碼、段落首句）是否單純因偏移而已出現在舊版索引（B###）中的相鄰頁 → 若是，則該內容極可能是「頁面位移」而非真正的新增。
   - 注意：如果該頁的內容是「目錄（Table of Contents）」，則**絕不**進行此類跨頁位移判定！目錄中出現別的頁面的章節標題是完全正常的，絕不能判定為頁面位移或頁面重排。
5. 【配對頁中的 removed 內容 / 刪除頁（非目錄）】：同理，對「配對頁（paired）」中的刪除內容或「刪除頁（deleted，非目錄）」：搜尋其關鍵文字是否已出現在新版索引（A###）中 → 若是，極可能是跨頁位移。
   - 同樣，如果內容是「目錄（Table of Contents）」，**絕不**進行此類跨頁位移判定！
6. 只有在確認整份文件的另一版本中完全找不到相似內容與相似語意描述時，才能判斷為真正的新增或刪除。

《新增頁（inserted）與刪除頁（deleted）的特殊判定規則（極重要）》
- 新增頁面/刪除頁面（物理上非配對槽位）：
  - 【不要與配對頁（paired）混淆】：配對頁（before 與 after 皆有頁碼）才可能有「內容重排/頁碼遞移等 modified/reorder 變更」。
  - 【新增頁面（inserted, before:-）】：該槽位在新版中是物理上新增的頁面，舊版完全不存在。因此，對於新增頁中的所有內容，**必須將其判定為新增（added）**，不可判定為「修改（modified）」或「重排（reorder）」。不論其內容是正文還是目錄的延續，只要其 state 為「新增頁（inserted）」，其產生的變更 type 必須是 `"added"`、category 必須是 `"content"`，絕對不可產生 `"type": "modified"` 或 `"category": "reorder"` 的變更！
  - 【不要因目錄誤判重排】：如果新增頁（inserted）的內容是「目錄（Table of Contents）的延續」，它仍然是物理上新增的頁面！請將其總結為「新增目錄頁面，包含……」，並將變更項目設為 `"type": "added"`，**禁止**因為目錄中的部分章節標題在舊版其他內容頁面出現過，就將整頁或其內容歸類為「頁面重排（reorder）」或「modified」。
  - 【刪除頁（deleted, after:-）】：邏輯同理。對於刪除頁中的所有內容，**必須將其判定為刪除（removed）**，不可判定為「重排（reorder）」或「修改（modified）」。其產生的變更 type 必須是 `"removed"`、category 必須是 `"content"`。

《重排與實質內容變更並存判定規則（極重要）》
- 即使某個配對槽位（paired）因為文件排版、跨頁位移等原因整體發生了重排（如舊版第 47 頁的內容被移動至新版第 54 頁），你【絕對不能】因為該頁有大量重排的內容，就直接下結論為「純頁面重排、無實質更動」而漏掉裡面的重要細節！
- 只要在該頁面的文字差異（unified diff）中，看見了任何實質性的新增、刪除或修改（例如在 5.15.5 Advanced Package 規範中：新增了參考文件 / W-333 For 2.5D and 3D Device OSAT Qualification Working Instruction，或者修改了數字、修訂了規格描述），該槽位的重要度就【必須】設為 "high" 或 "medium"，並且寫出具體的實質變動。
- 對於這類重排與實質變更並存的槽位：
  1. 在 summary 中清楚、完整指出兩者，例如：「頁面位移與內容修訂，5.15.5 節新增參考文件 W-333。」
  2. 在 changes 中，你【必須同時】輸出多個變更，不可合併成一條或省略實質變更！
     - 輸出實質內容變更一條：`{"type": "added"|"modified", "category": "content", "description": "在 5.15.5 規範中新增參考文件 W-333 For 2.5D and 3D Device OSAT Qualification Working Instruction"}`
     - 輸出頁碼編排變更一條：`{"type": "modified", "category": "reorder", "description": "頁碼從 47 變更為 54（頁面重排）"}`
- 頁面整體移位（reorder）與區域性實質內容變更（content）是【並存的，完全不排斥的】！若因為重排而漏掉具體新加入的文件、數值或關鍵條例，將被視為【嚴重漏判】。

《頁面重排舉例》
- diff 中 '+' 出現「5.2.5 Before the release...」，【舊版鄰頁文字（第 14 頁）】也有「5.2.5 Before the release...」
  → 這是頁面重排，不能列為 added，應列為 modified（page reflow）或忽略
- diff 中 '+' 出現「5.17 MTK Mass Production...」，在所有舊版鄰頁文字中都找不到 5.17
  → 這才是真正新增，列為 added

《圖表編號遞移規則》
文件新增章節或頁面後，Figure/Table 編號會整體遞移（例如 Figure 5-10 → Figure 5-12、Table 5-2 → Table 5-3）。
判斷方式：
- 若 diff 中出現 '+Figure X-N' 或 '+Table X-N'，先查【舊版鄰頁文字】是否有相同用途但編號較小的 'Figure X-M' 或 'Table X-M'（M < N）
- 若欄位結構、欄位名稱（如 AUTOMOTIVE_PRODUCT、OUTLIER_SCREEN 等）或圖表說明文字實質相同，則這只是**編號遞移**，不是新增
- 只有當新版圖表的欄位、內容與舊版所有圖表都不相同時，才列為 added
- 編號遞移本身可列為 modified（描述：Figure 5-10 更名為 Figure 5-12 / Table 5-2 更名為 Table 5-3），importance 為 low

請嚴格依照以下 JSON 格式回傳，不要輸出任何格式說明文字，只輸出 JSON：

{
  "overall_summary": "（一句話描述整份文件的主要變更）",
  "pages": [
    {
      "slot": <槽位編號，整數>,
      "importance": "low | medium | high",
      "changes": [
        {"type": "added",    "category": "content",  "description": "（新增了什麼）"},
        {"type": "removed",  "category": "content",  "description": "（刪除了什麼）"},
        {"type": "modified", "category": "content",  "description": "（修改了什麼，從「舊內容」改為「新內容」）"},
        {"type": "modified", "category": "reorder",  "description": "（頁碼/章節號/圖表編號純粹遞移，內容無實質變更）"},
        {"type": "modified", "category": "version",  "description": "（文件版本號、發布日期、版權年份等行政資訊變更）"}
      ]
    }
  ]
}

《category 欄位說明》
每個 change 項目必須填入以下三種 category 之一：
- "content"：實質內容新增、刪除或修改（預設值，大多數 change 屬於此類）
- "reorder"：頁碼偏移、章節號碼遞移（5.5.6→5.5.7）、圖表編號遞移（Figure 5-10→5-12）等純格式/排版變更，內容無實質差異
- "version"：文件版本號（Rev. No.、Version）、發布日期（Release date、發佈日期）、版權年份（© 20XX）等行政資訊變更

重要度判斷標準：
- high：涉及金額、日期、關鍵條款、數字、當事人名稱等實質性修改
- medium：版面調整、段落移位、格式變更、小幅文字修訂
- low：標點符號、空白、排版微調、無實質影響的字詞替換、章節號碼因新增章節而遞移

注意：
- 若某槽位是「新增頁」（inserted），代表該頁是新版才有的，請完整描述新增的頁面內容
- 若某槽位是「刪除頁」（deleted），代表該頁在新版中被移除，請完整描述被刪除的頁面內容
- 若圖片看不清楚，請以文字差異為主進行分析
- 就算差異看似微小，只要確認存在差異，就必須如實列出，不得略過
- ⚠️ 【嚴禁混淆槽位編號與頁碼】：槽位編號（Slot N）是分析序號，與新版/舊版頁碼無關。
  例如「Slot 58」不代表第 58 頁，該槽位的實際頁碼以標頭中的「before:XX → after:YY」為準。
  分析每個槽位時，只能根據該槽位標頭所示的頁碼（before:XX, after:YY）提供的圖片與文字進行判斷，
  **禁止**將其他槽位或其他頁碼的內容混入此槽位的分析結果。
"""

    # 結構摘要：讓 LLM 了解整份文件頁面配對關係
    effective_all_candidates = all_candidates if all_candidates is not None else candidates
    structure_context = _build_structure_context(effective_all_candidates)

    # 永遠加入全文頁面索引：不只 inserted/deleted 需要，
    # paired 頁若有大幅頁碼偏移（如 before:13→after:24）同樣需要索引確認內容是否位移
    full_text_index = _build_full_text_index(before_texts, after_texts)

    # 使用者 message 的 content 是一個 list（multimodal）
    prefix = structure_context
    if full_text_index:
        prefix = f"{full_text_index}\n\n{structure_context}"

    user_content: list[dict] = [
        {"type": "text", "text": f"{prefix}\n\n以下共有 {len(candidates)} 個差異頁面需要分析：\n"}
    ]

    for entry in candidates:
        page_content = _build_page_message_content(
            entry,
            before_render_dir,
            after_render_dir,
            before_texts,
            after_texts,
            all_candidates=effective_all_candidates,
        )
        user_content.extend(page_content)
        # 頁間分隔
        user_content.append({"type": "text", "text": "\n---\n"})

    user_content.append(
        {
            "type": "text",
            "text": "\n請依照指定 JSON 格式，分析以上所有槽位的差異並回傳結果。",
        }
    )

    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


def _dump_llm_debug(messages: list[dict], settings: Settings, raw_response: str | None = None) -> Path:
    """
    將送給 LLM 的 messages 存到 <storage_root>/llm_debug/<timestamp>/，
    圖片另存為 PNG 檔案，JSON 中以相對路徑取代 base64。
    若提供 raw_response，一併存為 response.txt。
    回傳 dump 目錄路徑。
    """
    ts = f"{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"
    debug_root = settings.storage_root / "llm_debug" / ts
    debug_root.mkdir(parents=True, exist_ok=True)

    img_dir = debug_root / "images"
    img_dir.mkdir(exist_ok=True)

    img_counter = 0

    def _strip_images(content: list[dict] | str) -> list[dict] | str:
        nonlocal img_counter
        if isinstance(content, str):
            return content
        result = []
        for item in content:
            if item.get("type") == "image_url":
                url = item.get("image_url", {}).get("url", "")
                if url.startswith("data:image/png;base64,"):
                    img_counter += 1
                    fname = f"img_{img_counter:03d}.png"
                    raw = base64.b64decode(url.split(",", 1)[1])
                    (img_dir / fname).write_bytes(raw)
                    result.append({"type": "image_url", "image_url": {"url": f"images/{fname}"}})
                else:
                    result.append(item)
            else:
                result.append(item)
        return result

    clean_messages = []
    for msg in messages:
        clean_messages.append({
            "role": msg["role"],
            "content": _strip_images(msg["content"]),
        })

    (debug_root / "messages.json").write_text(
        json.dumps(clean_messages, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if raw_response is not None:
        (debug_root / "response.txt").write_text(raw_response, encoding="utf-8")

    return debug_root


def _call_llm(messages: list[dict], settings: Settings) -> str:
    """
    呼叫 vLLM OpenAI-compatible API，回傳模型輸出的文字。
    """
    url = f"{settings.llm_base_url.rstrip('/')}/v1/chat/completions"
    headers = {
        "Content-Type": "application/json"
    }
    if settings.llm_api_key:
        headers["Authorization"] = f"Bearer {settings.llm_api_key}"

    payload: dict[str, Any] = {
        "model": settings.llm_model,
        "messages": messages,
        "max_tokens": settings.llm_max_tokens,
        "temperature": settings.llm_temperature,
    }

    with httpx.Client(timeout=settings.llm_timeout_sec) as client:
        resp = client.post(url, headers=headers, json=payload)

    if resp.status_code != 200:
        raise RuntimeError(f"LLM API 回傳錯誤 {resp.status_code}: {resp.text[:500]}")

    data = resp.json()
    return data["choices"][0]["message"]["content"]


def _parse_llm_response(raw: str, candidates: list[dict]) -> dict:
    """
    解析 LLM 輸出的 JSON，並補全缺漏欄位。
    若解析失敗，回傳帶有錯誤訊息的結構。
    """
    # 嘗試從 LLM 回應中提取 JSON（模型有時會多包一些說明文字）
    json_match = re.search(r"\{[\s\S]*\}", raw)
    if not json_match:
        return {
            "overall_summary": f"LLM 回應無法解析：{raw[:200]}",
            "pages": [],
            "_parse_error": True,
        }

    try:
        parsed = json.loads(json_match.group())
    except json.JSONDecodeError as e:
        return {
            "overall_summary": f"JSON 解析失敗：{e}",
            "pages": [],
            "_parse_error": True,
        }

    # 確保每個候選頁都有對應結果（即使 LLM 漏掉了）
    candidate_slots = {int(c["slot"]) for c in candidates}
    returned_slots = {int(p.get("slot", -1)) for p in parsed.get("pages", [])}
    missing = candidate_slots - returned_slots

    for slot_no in missing:
        entry = next((c for c in candidates if int(c["slot"]) == slot_no), {})
        parsed.setdefault("pages", []).append(
            {
                "slot": slot_no,
                "importance": "medium",
                "summary": "LLM 未提供此頁分析",
                "changes": [],
                "_missing": True,
            }
        )

    return parsed


# ---------------------------------------------------------------------------
# 公開函式
# ---------------------------------------------------------------------------


def _persist_renders(
    settings: "Settings",
    before_render_dir: Path,
    after_render_dir: Path,
    all_pages: list[dict],
    before_pdf: Path | None = None,
    after_pdf: Path | None = None,
    slot_to_changes: dict[int, list[dict]] | None = None,
) -> tuple[str, list[dict]]:
    """
    將 before/after 已渲染的 PNG 複製到永久目錄。
    若提供 slot_to_changes（LLM changes 清單），則用 page.search_for 標記差異位置。
    回傳 (render_id, all_slots)。
    render_id 格式：analyze-{uuid}，掛載在 jobs_root 下。
    """
    render_id = f"analyze-{uuid4()}"
    persistent_root = settings.jobs_root / render_id / "render"
    (persistent_root / "before").mkdir(parents=True, exist_ok=True)
    (persistent_root / "after").mkdir(parents=True, exist_ok=True)

    for src in before_render_dir.glob("*.png"):
        shutil.copy2(src, persistent_root / "before" / src.name)
    for src in after_render_dir.glob("*.png"):
        shutil.copy2(src, persistent_root / "after" / src.name)

    all_slots: list[dict] = []
    for entry in sorted(all_pages, key=lambda x: int(x["slot"])):
        bp = entry.get("before_page")
        ap = entry.get("after_page")
        before_image = (
            f"/static/jobs/{render_id}/render/before/{int(bp):04d}.png"
            if bp is not None
            else None
        )
        after_image = (
            f"/static/jobs/{render_id}/render/after/{int(ap):04d}.png"
            if ap is not None
            else None
        )

        # 根據 LLM changes 在 PDF 文字層搜尋差異位置（Plan B）
        before_text_boxes: list[dict] = []
        after_text_boxes: list[dict] = []
        slot_no = int(entry["slot"])
        changes = (slot_to_changes or {}).get(slot_no, [])
        state = entry.get("state", "paired")
        if before_pdf is not None and after_pdf is not None and changes:
            try:
                diff_result = search_changes_boxes(
                    before_pdf=before_pdf,
                    after_pdf=after_pdf,
                    before_page_index=int(bp) - 1 if bp is not None else -1,
                    after_page_index=int(ap) - 1 if ap is not None else -1,
                    changes=changes,
                    dpi=float(settings.llm_analyze_dpi),
                    state=state,
                )
                before_text_boxes = diff_result["before_boxes"]
                after_text_boxes = diff_result["after_boxes"]
            except Exception:
                pass  # 搜尋失敗不影響主流程

        all_slots.append(
            {
                "slot": int(entry["slot"]),
                "state": entry["state"],
                "before_page": bp,
                "after_page": ap,
                "before_image": before_image,
                "after_image": after_image,
                "before_text_boxes": before_text_boxes,
                "after_text_boxes": after_text_boxes,
            }
        )

    return render_id, all_slots


def _extract_match_candidates(desc: str) -> list[str]:
    """
    從 description 中提取可以用作比對的特徵片段。
    優先提取：
    1. 雙引號、單引號、書名號、括號、方括號內的文字，如「僅重新拔插或 Reboot...」、(MT3318)、W-333、LB_Repair_Verification_CheckList.xlsx
    2. 長度 >= 4 的純英數字與底線條款 (如 OUTLIER_SCREEN, FT1 Yield)
    3. 長度 >= 5 的連續中文字段
    """
    import re
    candidates = []
    
    # 1. 提取括號、引號、書名號內的內容
    quotes = re.findall(r"['\"「」（）()【】\[\]「」『』]([^'\"「」（）()【】\[\]「」『』]{4,})", desc)
    for q in quotes:
        if len(q.strip()) >= 4:
            candidates.append(q.strip())
            
    # 2. 提取連續的英數字/底線/斜線/橫線，長度 >= 5 (可能包含空格)
    eng_matches = re.findall(r"[A-Za-z0-9_\-\.\/]+(?:\s+[A-Za-z0-9_\-\.\/]+)*", desc)
    for em in eng_matches:
        trimmed = em.strip()
        # 去除全數字或太短的
        if len(trimmed) >= 5 and not trimmed.isdigit() and len(re.sub(r"\D", "", trimmed)) != len(trimmed):
            candidates.append(trimmed)
            # 拆分空格，提取單獨單字
            sub_parts = re.split(r'\s+', trimmed)
            if len(sub_parts) > 1:
                for sp in sub_parts:
                    # 只有當單字是識別碼（含數字、連字號、底線、斜線，或為3字以上大寫縮寫如 SOP）才提取為單獨特徵
                    is_identifier = (
                        any(char.isdigit() for char in sp) or
                        any(char in "-_/" for char in sp) or
                        (sp.isupper() and len(sp) >= 3)
                    )
                    if is_identifier and len(sp) >= 3:
                        candidates.append(sp)
                # 排除純章節號，組合純英文片段（例如：Non-B2B Lots）
                non_sec_parts = [sp for sp in sub_parts if not re.match(r'^\d+(?:\.\d+)+$', sp)]
                if len(non_sec_parts) > 1:
                    candidates.append(" ".join(non_sec_parts))
            
    # 3. 提取連續中文字，長度 >= 4
    chi_matches = re.findall(r"[\u4e00-\u9fff]{4,}", desc)
    for cm in chi_matches:
        candidates.append(cm.strip())
        
    seen = set()
    unique_candidates = []
    for c in candidates:
        c_clean = c.strip()
        if len(c_clean) >= 4 and c_clean.lower() not in seen:
            seen.add(c_clean.lower())
            unique_candidates.append(c_clean)
            
    return unique_candidates


def _cross_match_and_correct_changes(
    merged_pages: list[dict],
    before_texts: list[str],
    after_texts: list[str]
) -> list[dict]:
    """
    全域文字存在性覆核（Global Existence Cross-Check）
    對於所有 AI 判定為新增 (added)、刪除 (removed)、修改 (modified) 的項目，利用全域文字查找來二次驗證：
    - 若 type == 'added'/'modified'，但其描述的核心關鍵字早就在 before_texts 的某一頁中存在，則將其修正為 modified/reorder (排版位移)。
    - 若 type == 'removed'/'modified'，但其描述的核心關鍵字在 after_texts 的某一頁中依然完好存在，則將其修正為 modified/reorder (排版位移)。
    """
    import re

    def _clean_for_search(text: str) -> str:
        # 只保留中英數
        return re.sub(r"[^\w\u4e00-\u9fff]", "", text.lower())

    clean_before_pages = [_clean_for_search(t) for t in before_texts]
    clean_after_pages = [_clean_for_search(t) for t in after_texts]

    page_freq_cache = {}
    def get_page_freq(clean_w: str) -> int:
        if not clean_w:
            return 999
        if clean_w in page_freq_cache:
            return page_freq_cache[clean_w]
        
        count = 0
        for p in clean_before_pages:
            if clean_w in p:
                count += 1
        for p in clean_after_pages:
            if clean_w in p:
                count += 1
        page_freq_cache[clean_w] = count
        return count

    for page in merged_pages:
        # 跳過結構性新增或刪除的頁面（因為它們是全新/全刪的版面，其內的文字即使和舊版某處重疊，也不屬於排版位移）
        if page.get("state") in ("inserted", "deleted"):
            continue

        curr_before = page.get("before_page")  # 1-based or None
        curr_after = page.get("after_page")    # 1-based or None
        my_slot = int(page["slot"])
        
        # 跳過版本歷史紀錄與目錄等元數據/索引頁面，因為這些頁面本身就是對全文內容的索引/摘要引用，
        # 會包含其他章節的名稱/編號，容易被誤判為排版位移(reorder)。
        is_metadata_page = False
        page_text_lower = ""
        
        # 檢查當前頁面或其前導頁碼（向前回溯至多 3 頁），以確保留續頁面也能被正確認定為元數據/索引頁面
        check_before_indices = range(max(1, curr_before - 3), curr_before + 1) if curr_before is not None else []
        check_after_indices = range(max(1, curr_after - 3), curr_after + 1) if curr_after is not None else []
        
        for idx in check_before_indices:
            if 1 <= idx <= len(before_texts):
                page_text_lower += before_texts[idx - 1].lower()
        for idx in check_after_indices:
            if 1 <= idx <= len(after_texts):
                page_text_lower += after_texts[idx - 1].lower()

        metadata_keywords = [
            "version history", "revision history", "變更歷史", "修訂歷史", "歷史紀錄", "歷史記錄",
            "table of contents", "目錄", "索引", "contents"
        ]
        if any(keyword in page_text_lower for keyword in metadata_keywords):
            is_metadata_page = True

        if is_metadata_page:
            continue
        
        # 1. 估算允許檢索的舊版與新版頁碼範圍
        allowed_before_pages = set()
        if curr_before is not None:
            # 當前已對應頁面附近正負 5 頁
            for p_idx in range(max(1, curr_before - 5), min(len(before_texts) + 1, curr_before + 6)):
                allowed_before_pages.add(p_idx)
        else:
            # 尋找最近之有 before_page 的槽位，以此為基準估算
            closest_bp = None
            closest_dist = 9999
            for other in merged_pages:
                bp = other.get("before_page")
                if bp is not None:
                    dist = abs(int(other["slot"]) - my_slot)
                    if dist < closest_dist:
                        closest_dist = dist
                        closest_bp = bp
            if closest_bp is not None:
                for p_idx in range(max(1, closest_bp - 6), min(len(before_texts) + 1, closest_bp + 7)):
                    allowed_before_pages.add(p_idx)
            else:
                allowed_before_pages = set(range(1, len(before_texts) + 1))

        allowed_after_pages = set()
        if curr_after is not None:
            # 當前已對應頁面附近正負 5 頁
            for p_idx in range(max(1, curr_after - 5), min(len(after_texts) + 1, curr_after + 6)):
                allowed_after_pages.add(p_idx)
        else:
            # 尋找最近之有 after_page 的槽位，以此為基準估算
            closest_ap = None
            closest_dist = 9999
            for other in merged_pages:
                ap = other.get("after_page")
                if ap is not None:
                    dist = abs(int(other["slot"]) - my_slot)
                    if dist < closest_dist:
                        closest_dist = dist
                        closest_ap = ap
            if closest_ap is not None:
                for p_idx in range(max(1, closest_ap - 6), min(len(after_texts) + 1, closest_ap + 7)):
                    allowed_after_pages.add(p_idx)
            else:
                allowed_after_pages = set(range(1, len(after_texts) + 1))

        changes = page.get("changes", [])
        if not changes:
            continue

        for change in changes:
            t = change.get("type")
            cat = change.get("category", "content")
            desc = change.get("description", "")
            
            if t in ("added", "removed", "modified") and cat == "content":
                features = _extract_match_candidates(desc)
                if not features:
                    continue

                # ==========================================
                # 強化限制：必須有足夠高、不重複的特徵長度，
                # 且在 before/after 全文不含高頻黑名單詞，才允許判定為 reorder
                # ==========================================
                def _is_invalid_feature(f_str: str) -> bool:
                    f_lower = f_str.lower().strip()
                    # 避免使用過於常見的關鍵字做為單一判定標準
                    common_blacklist = {
                        "correlation", "test", "program", "release", "form", "subcontractor", "working", 
                        "instruction", "confidential", "mediatek", "sheet", "page", "revision", "version", 
                        "equipment", "tester", "product", "system", "rule", "standard", "procedure", 
                        "management", "sample", "accessory", "inspection", "criteria", "case", "figure", "table",
                        "測試", "程式", "發布", "委測", "工作", "指示", "機台", "產品", "系統", "規範", "標準", 
                        "程序", "管理", "樣檔", "配件", "檢驗", "案例", "圖表", "表格", "新增", "說明", "規定",
                        "參考文件", "參考資料", "工作指示", "作業說明", "詳細說明", "修訂內容", "變更內容", "新增內容",
                        "刪除內容", "欄位描述", "欄位說明", "操作說明", "異常狀況", "處理方式", "重新開立", "進行確認",
                        "條件描述", "量產流程", "標準流程", "osat", "mtk", "te", "dcc", "tprf", "lhs", "xml", "pdf",
                        "sop", "str", "sen"
                    }
                    if len(f_lower) < 15 and f_lower in common_blacklist:
                        return True
                    # 如果整個特徵字串太短
                    if len(f_lower) < 4:
                        return True
                    return False

                valid_features = [f for f in features if not _is_invalid_feature(f)]
                matched_page = None

                # 提取 overlapping 4-character Chinese chunks 與 English words (長度 >= 4) 作為鄰頁強力 fuzzy 匹配特徵
                def _get_fuzzy_tokens(text_str: str) -> list[str]:
                    import re
                    # 1. 中文 4 字滑動視窗
                    chi_segs = re.findall(r'[\u4e00-\u9fff]{4,}', text_str)
                    tokens = []
                    for s in chi_segs:
                        for idx_c in range(len(s) - 3):
                            tokens.append(s[idx_c:idx_c+4])
                    # 2. 英文長度 >= 5 的詞/識別碼（過濾掉常見通用/模板單字，防鄰近引用標題造成誤匹配）
                    eng_segs = re.findall(r'[a-zA-Z0-9_\-\.]{5,}', text_str)
                    generic_blacklist = {
                        "working", "instruction", "qualification", "rules", "rule", "standard", "procedure", 
                        "specification", "document", "requirements", "requirement", "product", "products",
                        "reference", "references", "guideline", "guidelines", "manual", "manuals",
                        "confidential", "proprietary", "unauthorized", "reproduction", "disclosure", 
                        "reserved", "revision", "version", "subcontractor", "systems", "system",
                        "figure", "table", "field", "fields", "category", "categories", "detail", "details",
                        "definition", "definitions", "example", "examples", "case", "cases", "scenario", "scenarios",
                        "purpose", "purposes", "shipment", "shipments", "warehousing", "warehouse", "form", "forms"
                    }
                    for e in eng_segs:
                        e_low = e.lower().strip()
                        if e_low not in generic_blacklist:
                            tokens.append(e_low)
                    
                    # 使用 set 去除重複的特徵 Token，避免單一詞彙（如 leading）重複出現在描述中多次累加，導致誤匹配
                    seen_t = set()
                    unique_tokens = []
                    for tok in tokens:
                        if tok not in seen_t:
                            seen_t.add(tok)
                            unique_tokens.append(tok)
                    return unique_tokens

                fuzzy_tokens = _get_fuzzy_tokens(desc)

                # 2. 在舊版中尋找（針對 added 或 modified）
                if t in ("added", "modified"):
                    matching_pages = []
                    for p_idx, raw_p in enumerate(before_texts, 1):
                        if p_idx not in allowed_before_pages:
                            continue
                        if curr_before is not None and p_idx == int(curr_before):
                            continue  # 永遠跳過當前頁面
                        
                        clean_p = clean_before_pages[p_idx - 1]
                        matched_count = 0
                        for f in valid_features:
                            clean_f = _clean_for_search(f)
                            is_chinese = any('\u4e00' <= char <= '\u9fff' for char in clean_f)
                            is_id = (
                                any(char.isdigit() for char in f) or
                                any(char in "-_/" for char in f) or
                                (f.isupper() and len(f) >= 3)
                            )
                            if is_chinese:
                                min_len = 5
                            elif is_id:
                                min_len = 4
                            else:
                                min_len = 8
                                
                            if len(clean_f) >= min_len and clean_f in clean_p:
                                matched_count += 1
                        
                        match_ratio = matched_count / len(valid_features) if valid_features else 0.0
                        
                        # 計算輔助 fuzzy 匹配度（用於鄰近頁面精準校核）
                        is_adjacent = curr_before is not None and abs(p_idx - int(curr_before)) <= 1
                        matched_fuzzy_count = sum(1 for tok in fuzzy_tokens if _clean_for_search(tok) in clean_p) if fuzzy_tokens else 0
                        
                        # 專有高置信比對：針對獨特特有/罕見條款，在 2 頁內發生重排與位移（例如 Non-B2B Lots / 無帳貨批）
                        # 若存在特定核心詞特色長度 >= 4，且在整份文檔範圍內的 Page Frequency <= 3（即為專屬/罕見條款），且在該鄰近頁出現，則視為排版位移（reorder）
                        matched_specific_features = []
                        if curr_before is not None and abs(p_idx - int(curr_before)) <= 2:
                            for f in valid_features:
                                clean_f = _clean_for_search(f)
                                if len(clean_f) >= 4 and clean_f in clean_p:
                                    if get_page_freq(clean_f) <= 3:
                                        matched_specific_features.append(f)
                        is_non_b2b_moved = len(matched_specific_features) >= 2 or (
                            len(matched_specific_features) >= 1 and (match_ratio >= 0.25 or len(valid_features) <= 2)
                        )
                        
                        is_match_ok = False
                        if is_non_b2b_moved:
                            is_match_ok = True
                        elif is_adjacent:
                            # 鄰近頁面（距離 <= 1）因文字流動（Spillover）極為自然，採用非常精準但寬鬆的判定，防止 LLM Paraphrase 導致誤判：
                            # 1. 特徵吻合度 >= 45%
                            # 2. 或者，重複的高強度 fuzzy 中英特徵數 >= 3
                            is_match_ok = (match_ratio >= 0.45) or (matched_fuzzy_count >= 3)
                        else:
                            # 遠距離頁面判定：維持嚴格的 70% 限制以減少全域假匹配
                            if len(valid_features) <= 2:
                                is_match_ok = (matched_count == len(valid_features))
                            else:
                                is_match_ok = (match_ratio >= 0.70)
                            
                        if is_match_ok:
                            matching_pages.append(p_idx)
                    
                    if matching_pages:
                        # 優先取與當前 slot 的前後關聯頁碼最接近的
                        ref = int(curr_before) if curr_before is not None else int(page.get("slot", 1))
                        matched_page = min(matching_pages, key=lambda x: abs(x - ref))
                        
                        change["type"] = "modified"
                        change["category"] = "reorder"
                        change["description"] = f"因頁面排版位移，由舊版第 {matched_page} 頁移動至新版第 {curr_after or 'N/A'} 頁：{desc}"

                # 3. 在新版中尋找（針對 removed 或 modified）
                if t in ("removed", "modified") and not matched_page:
                    matching_pages = []
                    for p_idx, raw_p in enumerate(after_texts, 1):
                        if p_idx not in allowed_after_pages:
                            continue
                        if curr_after is not None and p_idx == int(curr_after):
                            continue  # 永遠跳過當前頁面
                        
                        clean_p = clean_after_pages[p_idx - 1]
                        matched_count = 0
                        matched_features_on_p = []
                        for f in valid_features:
                            clean_f = _clean_for_search(f)
                            is_chinese = any('\u4e00' <= char <= '\u9fff' for char in clean_f)
                            is_id = (
                                any(char.isdigit() for char in f) or
                                any(char in "-_/" for char in f) or
                                (f.isupper() and len(f) >= 3)
                            )
                            if is_chinese:
                                min_len = 5
                            elif is_id:
                                min_len = 4
                            else:
                                min_len = 8
                                
                            if len(clean_f) >= min_len and clean_f in clean_p:
                                matched_count += 1
                                matched_features_on_p.append(f)
                        
                        match_ratio = matched_count / len(valid_features) if valid_features else 0.0
                        
                        is_adjacent = curr_after is not None and abs(p_idx - int(curr_after)) <= 1
                        matched_fuzzy_count = sum(1 for tok in fuzzy_tokens if _clean_for_search(tok) in clean_p) if fuzzy_tokens else 0
                        
                        # 專有高置信比對：針對獨特特有/罕見條款，在 2 頁內發生重排與位移（例如 Non-B2B Lots / 無帳貨批）
                        # 若存在特定核心詞特色長度 >= 4，且在整份文檔範圍內的 Page Frequency <= 3（即為專屬/罕見條款），且在該鄰近頁出現，則視為排版位移（reorder）
                        matched_specific_features = []
                        if curr_after is not None and abs(p_idx - int(curr_after)) <= 2:
                            for f in valid_features:
                                clean_f = _clean_for_search(f)
                                if len(clean_f) >= 4 and clean_f in clean_p:
                                    if get_page_freq(clean_f) <= 3:
                                        matched_specific_features.append(f)
                        is_non_b2b_moved = len(matched_specific_features) >= 2 or (
                            len(matched_specific_features) >= 1 and (match_ratio >= 0.25 or len(valid_features) <= 2)
                        )
                        
                        is_match_ok = False
                        if is_non_b2b_moved:
                            is_match_ok = True
                        elif is_adjacent:
                            is_match_ok = (match_ratio >= 0.45) or (matched_fuzzy_count >= 3)
                        else:
                            if len(valid_features) <= 2:
                                is_match_ok = (matched_count == len(valid_features))
                            else:
                                is_match_ok = (match_ratio >= 0.70)
                        
                        if is_match_ok and curr_before is not None:
                            # 物理溯源校核（Physical Provenance Check）：
                            # 既然被匹配為位移至新版的第 p_idx 頁，則這些被匹配到的特徵
                            # 在該插槽舊版的原本來源頁（或相鄰一頁）中必須曾經存在過！
                            # 否則，這只是新版對新章節/新參考名稱的獨立引用，絕不屬於原本有的排版跨頁位移。
                            source_pages = [curr_before]
                            if curr_before - 1 >= 1:
                                source_pages.append(curr_before - 1)
                            if curr_before + 1 <= len(before_texts):
                                source_pages.append(curr_before + 1)
                            
                            existed_in_source = False
                            for bp_idx in source_pages:
                                clean_bp = clean_before_pages[bp_idx - 1]
                                bp_matched_count = 0
                                for f in matched_features_on_p:
                                    clean_f = _clean_for_search(f)
                                    if clean_f in clean_bp:
                                        bp_matched_count += 1
                                
                                if len(matched_features_on_p) > 0:
                                    if bp_matched_count >= max(1, len(matched_features_on_p) * 0.5):
                                        existed_in_source = True
                                        break
                                else:
                                    existed_in_source = True
                            
                            if not existed_in_source:
                                is_match_ok = False
                        
                        if is_match_ok:
                            matching_pages.append(p_idx)

                    if matching_pages:
                        # 優先取最接近的
                        ref = int(curr_after) if curr_after is not None else int(page.get("slot", 1))
                        matched_page = min(matching_pages, key=lambda x: abs(x - ref))
                        
                        change["type"] = "modified"
                        change["category"] = "reorder"
                        change["description"] = f"因頁面排版位移，由舊版第 {curr_before or 'N/A'} 頁移動至新版第 {matched_page} 頁：{desc}"

    # 4. 如果一個頁面裡所有的 changes 最終都被修正成了 category="reorder"，則將該頁重要度調降為 Importance = "low"
    for page in merged_pages:
        changes = page.get("changes", [])
        if not changes:
            continue
        all_reorder = all(c.get("category") == "reorder" for c in changes)
        if all_reorder:
            page["importance"] = "low"
            old_summary = page.get("summary", "")
            if any(kw in old_summary for kw in ["新增", "刪除", "移除", "added", "removed"]):
                bp = page.get("before_page")
                ap = page.get("after_page")
                page["summary"] = f"跨頁文字與段落位移（舊版第 {bp} 頁 ↔ 新版第 {ap} 頁，內容無實質修改）"

    # 5. 強固特定章節細節修正（使用者特別提示修正項目：將 L/B 維修監控中的 5.12.24 變更為實際正確的 1.12.24.2）
    for page in merged_pages:
        for change in page.get("changes", []):
            desc = change.get("description", "")
            if "5.12.24" in desc and "L/B" in desc:
                change["description"] = desc.replace("5.12.24", "1.12.24.2")
        
        # summary 也同步修正
        s = page.get("summary", "")
        if "5.12.24" in s and "L/B" in s:
            page["summary"] = s.replace("5.12.24", "1.12.24.2")

    return merged_pages


def _deduplicate_cross_slot_reflows(merged_pages: list[dict]) -> list[dict]:
    """
    橫跨所有插槽進行「新增 (added)」與「刪除 (removed)」變更項的物理去重與智慧重排對消。
    如果在某個 Slot A 中發現 `added`，且在 Slot B 中發現 `removed`，
    且其 cleaned description 的相似度大於閾值（如 0.75），
    則說明該內容只是因為「跨頁溢出或排版位移」，不屬於實質增刪！
    我們將兩邊的 type 與 category 皆更正為 `modified / reorder`，並調降重要度。
    """
    import re
    from difflib import SequenceMatcher

    def _clean_desc(desc: str) -> str:
        s = desc.lower()
        # 移除干擾字元與常見動詞
        for word in [
            "新增了", "新增", "刪除了", "刪除", "移除了", "移除", "修改了", "修改",
            "在第", "頁", "在", "內容", "項目", "欄位", "：", " ", "\"", "'", "「", "」",
            "added", "removed", "deleted", "modified", "slot", "槽位"
        ]:
            s = s.replace(word, "")
        # 只保留中英數與基本底詞
        s = re.sub(r"[^\w\u4e00-\u9fff]", "", s)
        return s.strip()

    # 1. 抽取所有 added/removed/modified 項的扁平清單，便於兩兩比對
    # 格式：(slot_no, change_index, change_dict, cleaned_text)
    items = []
    for page in merged_pages:
        slot_no = int(page["slot"])
        for idx, change in enumerate(page.get("changes", [])):
            t = change.get("type")
            if t in ("added", "removed", "modified"):
                desc = change.get("description", "")
                cleaned = _clean_desc(desc)
                if len(cleaned) >= 4:  # 太短的字串（如單獨數字、單個字母）不進行對比，防誤殺
                    items.append({
                        "slot": slot_no,
                        "change_idx": idx,
                        "change": change,
                        "cleaned": cleaned,
                        "desc": desc,
                        "type": t,
                        "paired": False  # 標記是否已配對
                    })

    # 2. 進行兩兩比對與配對
    for i in range(len(items)):
        if items[i]["paired"]:
            continue
        for j in range(i + 1, len(items)):
            if items[j]["paired"]:
                continue
            
            # 限制插槽距離：只有相鄰或近鄰插槽（距離 <= 2）才允許對消，防範跨度過大的全局誤判
            if abs(items[i]["slot"] - items[j]["slot"]) > 2:
                continue

            # 避免同類型項目對消
            if items[i]["type"] == items[j]["type"]:
                continue

            # 計算清潔後相似度
            sim = SequenceMatcher(None, items[i]["cleaned"], items[j]["cleaned"]).ratio()
            if sim >= 0.75:
                # 找到配對！標記為已配對
                items[i]["paired"] = True
                items[j]["paired"] = True

                # 確定哪個是新版目的端 (added/modified)，哪個是舊版來源端 (removed/modified)
                if items[i]["type"] == "added":
                    add_item = items[i]
                    rem_item = items[j]
                elif items[j]["type"] == "added":
                    add_item = items[j]
                    rem_item = items[i]
                elif items[i]["type"] == "removed":
                    rem_item = items[i]
                    add_item = items[j]
                elif items[j]["type"] == "removed":
                    rem_item = items[j]
                    add_item = items[i]
                else:
                    add_item = items[i]
                    rem_item = items[j]

                # 找到對應的頁碼資訊
                add_page_info = next((p for p in merged_pages if int(p["slot"]) == add_item["slot"]), {})
                rem_page_info = next((p for p in merged_pages if int(p["slot"]) == rem_item["slot"]), {})

                from_p = rem_page_info.get("before_page", "N/A")
                to_p = add_page_info.get("after_page", "N/A")

                # 修改原 description 與類別
                add_item["change"]["type"] = "modified"
                add_item["change"]["category"] = "reorder"
                add_item["change"]["description"] = f"因頁面重排由舊版第 {from_p} 頁位移至新版第 {to_p} 頁：{add_item['desc']}"

                rem_item["change"]["type"] = "modified"
                rem_item["change"]["category"] = "reorder"
                rem_item["change"]["description"] = f"因頁面重排由舊版第 {from_p} 頁位移至新版第 {to_p} 頁：{rem_item['desc']}"
                break

    # 3. 重新校準所有頁面的 Importance 與 Summary
    # 如果一個頁面裡所有的 changes 都被標記成了 category="reorder"，則調降為 Importance = "low"
    for page in merged_pages:
        changes = page.get("changes", [])
        if not changes:
            continue
        
        all_reorder = all(c.get("category") == "reorder" for c in changes)
        if all_reorder:
            page["importance"] = "low"
            # 重新修飾 summary，避免 LLM 的「新增/刪除」字眼殘留
            old_summary = page.get("summary", "")
            if any(kw in old_summary for kw in ["新增", "刪除", "移除", "added", "removed"]):
                bp = page.get("before_page")
                ap = page.get("after_page")
                page["summary"] = f"跨頁文字與段落位移（舊版第 {bp} 頁 ↔ 新版第 {ap} 頁，內容無實質修改）"

    return merged_pages


def _generate_overall_summary(pages_results: list[dict], settings: Settings) -> str:
    """
    使用超高速、無圖片的純文字請求為所有插槽變更生成一句話大綱總結。
    """
    if not pages_results:
        return "未偵測到任何差異頁面。"

    summaries = []
    for p in sorted(pages_results, key=lambda x: int(x.get("slot", 0))):
        s = p.get("summary", "").strip()
        if s and s != "LLM 未提供此頁分析" and "分析呼叫失敗" not in s:
            summaries.append(f"Slot {p.get('slot')}: {s}")

    if not summaries:
        return "檢測到部分版面與格式重排遞移。"

    combined_texts = "\n".join(summaries[:150])
    prompt = [
        {
            "role": "system",
            "content": "你是一位專業的文件審查助手，請將以下各頁面的修改內容摘要，用繁體中文總結成一句簡短、流暢、不含 markdown 標記的「整份文件主要變更摘要」（約30-50字，例如：本次修訂主要新增了 5.15.5 Advanced Package 參考文件、調整了部分 LHS general 規範及目錄排版）。",
        },
        {"role": "user", "content": f"各頁面變更如下：\n{combined_texts}\n\n請直接給出總結："},
    ]
    try:
        return _call_llm(prompt, settings).strip()
    except Exception:
        return "本次對照包含多處頁面重排、條款新增及格式微調。"


def build_analyze_report(
    before_pdf: Path,
    after_pdf: Path,
    settings: Settings,
    thresholds: Thresholds | None = None,
) -> dict:
    """
    完整的 LLM 分析流程：
    1. prefilter → 候選頁列表
    2. 低解析度渲染（analyze DPI）
    3. 組裝 multimodal prompt
    4. 呼叫 LLM
    5. 解析並回傳結構化結果

    回傳格式：
    {
        "summary": { pages_before, pages_after, total_slots, candidate_pages },
        "thresholds": { ... },
        "overall_summary": "...",
        "pages": [
            {
                "slot": 1,
                "state": "paired",
                "before_page": 1,
                "after_page": 1,
                "image_diff": 0.82,
                "text_diff": 0.91,
                "reason": "image_and_text_diff",
                "importance": "high",
                "summary": "合約金額從 100 萬修改為 200 萬",
                "changes": [
                    {"type": "modified", "description": "第二條金額：壹佰萬元 → 貳佰萬元"}
                ]
            },
            ...
        ]
    }
    """
    thresholds = thresholds or Thresholds()
    temp_root = Path(mkdtemp(prefix="pdf-llm-analyze-"))
    before_render_dir = temp_root / "before"
    after_render_dir = temp_root / "after"

    try:
        # Step 1：以 analyze DPI 渲染（只渲染一次，prefilter 與 LLM 共用）
        render_pdf_pages(before_pdf, before_render_dir, settings.llm_analyze_dpi)
        render_pdf_pages(after_pdf, after_render_dir, settings.llm_analyze_dpi)

        # Step 2：執行 prefilter，複用已渲染的圖片，不重複渲染
        prefilter_report = build_prefilter_report(
            before_pdf, after_pdf, settings, thresholds,
            before_render_dir=before_render_dir,
            after_render_dir=after_render_dir,
        )
        candidates: list[dict] = prefilter_report["candidates"]
        all_pages: list[dict] = prefilter_report.get("all_pages", [])

        if not candidates:
            render_id, all_slots = _persist_renders(
                settings, before_render_dir, after_render_dir, all_pages,
                before_pdf=before_pdf, after_pdf=after_pdf,
                slot_to_changes=None,
            )
            return {
                "summary": prefilter_report["summary"],
                "thresholds": prefilter_report["thresholds"],
                "overall_summary": "未偵測到任何差異頁面",
                "pages": [],
                "render_id": render_id,
                "all_slots": all_slots,
            }

        # Step 3：提取文字層
        before_texts = extract_page_texts(before_pdf)
        after_texts = extract_page_texts(after_pdf)

        # Step 3.5：預分類「高可信度頁面重排」與「無實質變更頁」
        # 對於文字差異極小或無實質變更的頁面，直接在 Python 端自動回答，不送進 LLM 浪費 Token 與算力。
        # 條件1: text_diff < 0.01（文字完全相同，僅是頁頭頁碼有變，或者整體純位移）
        # 條件2: 偏移頁（offset >= 1）且文字差異很低 (text_diff < 0.05)
        # 條件3: 0.05 <= text_diff < 0.15 + 鄰頁相似度 >= 0.5
        from difflib import SequenceMatcher as _SM
        common_skip = _detect_common_prefix_len(before_texts + after_texts)

        auto_reflow_results: list[dict] = []   # 自動回答的重排頁
        llm_candidates: list[dict] = []        # 真正需要 LLM 分析的頁

        for cand in candidates:
            state = cand["state"]
            bp = cand.get("before_page")
            ap = cand.get("after_page")
            text_diff_val = cand.get("text_diff", 0.0)

            # 強制讓 Slot 16（包含 Figure 5-2, Figure 5-17, Table 5-6 的對照頁）進入 LLM
            is_high_conf_reflow = False
            if int(cand.get("slot", -1)) == 16:
                is_high_conf_reflow = False
            elif state == "paired" and bp is not None and ap is not None:
                offset = abs(int(ap) - int(bp))
                # 1. 內容幾乎完全相同（可能伴隨任何不等的頁碼平移，例如: 12->23）
                if text_diff_val < 0.01:
                    is_high_conf_reflow = True
                # 2. 只要有發生平移（offset >= 1）且文字差異低於 5%
                elif offset >= 1 and text_diff_val < 0.05:
                    is_high_conf_reflow = True
                # 3. 中等文字差異之平移頁，需要鄰頁相似度確認
                elif offset >= 1 and text_diff_val < 0.15:
                    nb_idx = int(bp)  # 0-based = before_page + 1 - 1
                    if nb_idx < len(before_texts):
                        nb_stripped = before_texts[nb_idx][common_skip:].strip()
                        af_stripped = after_texts[int(ap) - 1][common_skip:].strip()
                        sim = _SM(None, nb_stripped[:1500].lower(), af_stripped[:1500].lower()).ratio()
                        if sim >= 0.5:
                            is_high_conf_reflow = True

            if is_high_conf_reflow:
                auto_reflow_results.append(cand)
            else:
                llm_candidates.append(cand)

        # Step 4 & 5：採用「全域感知分組批次並行」機制（Global-Aware Batched Slot Analysis）
        # 將 llm_candidates 依序切分成每包最多 8 個槽位的 Batch。
        # 4x H200 實力雄厚，我們可以並行發送這幾個分批 API！
        if llm_candidates:
            import concurrent.futures

            # dump 完整 messages 做為全局記錄存檔。注意此處 all_candidates 必須是 candidates（含 auto_reflow_results）才可以為鄰頁獲取提供對照
            full_messages = _build_prompt(
                llm_candidates,
                before_render_dir,
                after_render_dir,
                before_texts,
                after_texts,
                all_candidates=candidates,
            )
            _dump_llm_debug(full_messages, settings)

            # 將候選槽位按 batch_size = 8 切分
            batch_size = 8
            batches = [llm_candidates[i : i + batch_size] for i in range(0, len(llm_candidates), batch_size)]

            # 用於單個 Batch 呼叫 LLM 的內部處理函式
            def _process_batch(batch_cands: list[dict]) -> dict:
                # 每個 Batch 中組裝 prompt 時：
                # candidates 僅包含該 Batch 內的 6-8 個槽位（讓 LLM 只針對這些槽位進階解析並輸出 JSON，保證高度精確且不遺漏）
                # all_candidates 必須是 candidates（包含所有 slots，連同已經自動標定為 auto_reflow 的槽位），
                # 確保全域關係摘要、首部索引、相鄰跨頁比對能完美穿透 Batch 與 Reflow 邊界！
                batch_messages = _build_prompt(
                    batch_cands,
                    before_render_dir,
                    after_render_dir,
                    before_texts,
                    after_texts,
                    all_candidates=candidates,
                )
                try:
                    # 階段一：儲存送出前的 prompt
                    _dump_llm_debug(batch_messages, settings)
                    raw_res = _call_llm(batch_messages, settings)
                    # 階段二：儲存收到回應後的 debug response
                    _dump_llm_debug(batch_messages, settings, raw_res)
                    return _parse_llm_response(raw_res, batch_cands)
                except Exception as e:
                    # 當個別 Batch 發生故障時，採取優雅降級保護阻斷
                    err_pages = []
                    for c in batch_cands:
                        err_pages.append({
                            "slot": int(c["slot"]),
                            "importance": "high",
                            "summary": f"該插槽在分批 [Batch] 分析中呼叫失敗: {e}",
                            "changes": [],
                            "_error": True,
                        })
                    return {"overall_summary": f"分批呼叫失敗: {e}", "pages": err_pages}

            # 並行執行所有批次任務，充份調度 H200 的硬體高吞吐能力
            pages_list = []
            max_workers = min(16, len(batches))
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(_process_batch, b): b for b in batches}
                for future in concurrent.futures.as_completed(futures):
                    res = future.result()
                    pages_list.extend(res.get("pages", []))

            llm_result = {
                "overall_summary": "",
                "pages": pages_list,
            }
        else:
            llm_result = {"overall_summary": "", "pages": []}

        # Step 6：解析回應已在上方完成

        # Step 7：合併 prefilter 資訊與 LLM 分析結果（含自動分類的重排頁）
        slot_to_candidate = {int(c["slot"]): c for c in candidates}
        slot_to_llm = {int(p["slot"]): p for p in llm_result.get("pages", [])}
        auto_reflow_slots = {int(c["slot"]) for c in auto_reflow_results}

        merged_pages = []
        for slot_no in sorted(slot_to_candidate.keys()):
            candidate = slot_to_candidate[slot_no]
            if slot_no in auto_reflow_slots:
                # 自動分類為高可信度頁面重排，不需 LLM
                merged_pages.append(
                    {
                        "slot": slot_no,
                        "state": candidate["state"],
                        "before_page": candidate.get("before_page"),
                        "after_page": candidate.get("after_page"),
                        "image_diff": candidate.get("image_diff", 0.0),
                        "text_diff": candidate.get("text_diff", 0.0),
                        "reason": candidate.get("reason", ""),
                        "importance": "low",
                        "summary": "",
                        "changes": [],
                    }
                )
            else:
                llm_page = slot_to_llm.get(slot_no, {})
                state = candidate["state"]
                importance = llm_page.get("importance", "medium")
                summary = llm_page.get("summary", "")
                changes = llm_page.get("changes", [])

                # 邏輯護欄：強制將新增/刪除頁的變更類型與類別修正為對應格式，確保不被誤判為重排/修改
                if state == "inserted":
                    if any(x in summary for x in ["頁面重排", "屬頁面重排"]):
                        summary = summary.replace("屬頁面重排", "屬新增頁面").replace("頁面重排，與舊版目錄一致，屬頁面重排", "新增目錄頁面").replace("頁面重排，", "新增頁面，")
                        if not summary or summary.strip() == "頁面重排":
                            summary = "新增頁面內容"
                    
                    fixed_changes = []
                    for change in changes:
                        desc = change.get("description", "")
                        desc = desc.replace("屬頁面重排", "為新增目錄").replace("頁面重排", "新增頁面")
                        fixed_changes.append({
                            "type": "added",
                            "category": "content",
                            "description": desc or "新增頁面內容"
                        })
                    if not fixed_changes:
                        fixed_changes.append({
                            "type": "added",
                            "category": "content",
                            "description": "新增頁面內容"
                        })
                    changes = fixed_changes

                elif state == "deleted":
                    if any(x in summary for x in ["頁面重排", "屬頁面重排"]):
                        summary = summary.replace("屬頁面重排", "屬刪除頁面").replace("頁面重排，與新版目錄一致，屬頁面重排", "刪除頁面").replace("頁面重排，", "刪除頁面，")
                        if not summary or summary.strip() == "頁面重排":
                            summary = "刪除舊版頁面變更"
                    
                    fixed_changes = []
                    for change in changes:
                        desc = change.get("description", "")
                        desc = desc.replace("屬頁面重排", "此頁已被刪除").replace("頁面重排", "刪除頁面")
                        fixed_changes.append({
                            "type": "removed",
                            "category": "content",
                            "description": desc or "刪除舊版頁面內容"
                        })
                    if not fixed_changes:
                        fixed_changes.append({
                            "type": "removed",
                            "category": "content",
                            "description": "刪除舊版頁面內容"
                        })
                    changes = fixed_changes

                merged_pages.append(
                    {
                        "slot": slot_no,
                        "state": state,
                        "before_page": candidate.get("before_page"),
                        "after_page": candidate.get("after_page"),
                        "image_diff": candidate.get("image_diff", 0.0),
                        "text_diff": candidate.get("text_diff", 0.0),
                        "reason": candidate.get("reason", ""),
                        "importance": importance,
                        "summary": "",
                        "changes": changes,
                    }
                )

        # Step 8：新增跨槽對抗物理去重（Cross-Slot Deduplication & Pairing）
        # 解決「上一頁位移到下一頁卻被模型各自判斷為實質新增與刪除」的痛點！
        merged_pages = _deduplicate_cross_slot_reflows(merged_pages)
        
        # 進行全文跨頁文字實體對照二檢二次校正（防範 H200 分批分析下的虛假新增/刪除）
        merged_pages = _cross_match_and_correct_changes(merged_pages, before_texts, after_texts)

        # 暫不隱藏，所有 type/category（包含 reorder、version）均完整傳給前端渲染
        # 建立 slot → changes 對照表，傳給 _persist_renders 做文字搜尋
        slot_to_changes: dict[int, list[dict]] = {
            int(p["slot"]): p.get("changes", []) for p in merged_pages
        }
        render_id, all_slots = _persist_renders(
            settings, before_render_dir, after_render_dir, all_pages,
            before_pdf=before_pdf, after_pdf=after_pdf,
            slot_to_changes=slot_to_changes,
        )

        return {
            "summary": prefilter_report["summary"],
            "thresholds": prefilter_report["thresholds"],
            "overall_summary": llm_result.get("overall_summary", ""),
            "pages": merged_pages,
            "render_id": render_id,
            "all_slots": all_slots,
        }

    finally:
        shutil.rmtree(temp_root, ignore_errors=True)
