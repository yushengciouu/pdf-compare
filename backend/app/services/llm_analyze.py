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

    # 限制總長度，避免 token 爆炸
    MAX_CHARS = 3500
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

    # --- 配對頁大幅偏移：附加舊版鄰頁文字供跨頁比對 ---
    # 若 after_page - before_page >= 3 且此槽位有名義上的差異（防止對很多 low-diff 頁浪費 token）
    if state == "paired" and before_page is not None and after_page is not None:
        offset = int(after_page) - int(before_page)
        if offset >= 3 and (image_diff >= 0.05 or text_diff >= 0.05):
            from app.services.page_match import _strip_boilerplate
            all_bt = before_texts + after_texts
            common_skip = _detect_common_prefix_len(all_bt)

            # 只提供鄰頁 +1 （防止訊息過大）
            nb_idx = int(before_page)  # 0-based index = before_page+1 - 1
            if nb_idx < len(before_texts):
                raw_nb = before_texts[nb_idx]
                stripped_nb = raw_nb[common_skip:].strip()[:1500]
                if stripped_nb:
                    # 偵測鄰頁與 after 頁的相似度
                    from difflib import SequenceMatcher
                    after_page_text = after_texts[int(after_page) - 1] if after_page else ""
                    sim = SequenceMatcher(None, stripped_nb.lower(), after_page_text[common_skip:].lower()).ratio()

                    reflow_hint = ""
                    if sim >= 0.25:
                        reflow_hint = (
                            f"⚠️ 【頁面重排偵測警告】"
                            f"舊版第 {int(before_page)+1} 頁與新版第 {after_page} 頁文字相似度 {sim:.2f}\n"
                            f"→ diff 中 '+' 出現的章節內容，若已存在於下方舊版鄰頁文字，則為頁面重排，不得列為 added。\n"
                        )
                        content.insert(1, {"type": "text", "text": reflow_hint})

                    content.append({
                        "type": "text",
                        "text": (
                            f"【舊版鄰頁文字（第 {int(before_page)+1} 頁，供跨頁位移比對參考）】\n"
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
1. 查閱 user 訊息開頭的「全文頁面摘要索引」（B###=舊版各頁摘要，A###=新版各頁摘要）
2. 若槽位頭部出現【頁面重排偵測警告】，務必優先執行下述步驟 3
3. 【關鍵步驟】對 diff 中每一行以「+」開頭的段落（包含章節號碼如 5.2.5），逐一在【舊版鄰頁文字】中搜尋：
   - 若該章節號碼或段落內容已出現在任何一個【舊版鄰頁文字（第 N 頁）】中 → 該段落是「頁面重排」，不是新增，**禁止**列為 added，改列為 modified（描述：頁面重排，內容源自舊版第 N 頁）或直接忽略
   - 只有當該章節號碼、段落首句在所有【舊版鄰頁文字】中都找不到時，才能列為 added
4. 對「新增頁」或「配對頁中的 added 內容」：搜尋其關鍵文字（章節號碼、段落首句）是否已出現在舊版索引（B###）中的某頁 → 若有，則該內容極可能是「頁面位移」而非真正的新增，summary 中應說明「內容源自舊版第 N 頁，可能為頁面位移」，importance 降為 low
5. 對「刪除頁」或「配對頁中的 removed 內容」：搜尋其關鍵文字是否已出現在新版索引（A###）中的某頁 → 若有，則該內容極可能是「頁面位移」而非真正的刪除，summary 中應說明「內容仍存在於新版第 N 頁，可能為頁面位移」，importance 降為 low
6. 只有在確認整份文件的另一版本中完全找不到相似內容時，才能判斷為真正的新增或刪除

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
      "summary": "（這個槽位的主要差異是什麼，一到兩句話）",
      "changes": [
        {"type": "added", "description": "（新增了什麼）"},
        {"type": "removed", "description": "（刪除了什麼）"},
        {"type": "modified", "description": "（修改了什麼，從「舊內容」改為「新內容」）"}
      ]
    }
  ]
}

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
    structure_context = _build_structure_context(candidates)

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
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
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
    payload: dict[str, Any] = {
        "model": settings.llm_model,
        "messages": messages,
        "max_tokens": settings.llm_max_tokens,
        "temperature": settings.llm_temperature,
    }

    with httpx.Client(timeout=settings.llm_timeout_sec) as client:
        resp = client.post(url, json=payload)

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
) -> tuple[str, list[dict]]:
    """
    將 before/after 已渲染的 PNG 複製到永久目錄，
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
        all_slots.append(
            {
                "slot": int(entry["slot"]),
                "state": entry["state"],
                "before_page": bp,
                "after_page": ap,
                "before_image": before_image,
                "after_image": after_image,
            }
        )

    return render_id, all_slots


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
                settings, before_render_dir, after_render_dir, all_pages
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

        # Step 3.5：預分類「高可信度頁面重排」
        # 對於偏移頁（offset >= 3），使用 text_diff 作為主要判斷基準（image_diff 會因版頭頁碼改變而號跬）：
        # 條件1（純偏移）: text_diff < 0.05 → 內容幾乎相同，對循環 token 筆數，直接自動判定
        # 條件2（帶鄰頁驗證）: 0.05 <= text_diff < 0.15 + 鄰頁相似度 >= 0.5
        from difflib import SequenceMatcher as _SM
        common_skip = _detect_common_prefix_len(before_texts + after_texts)

        auto_reflow_results: list[dict] = []   # 自動回答的重排頁
        llm_candidates: list[dict] = []        # 真正需要 LLM 分析的頁

        for cand in candidates:
            state = cand["state"]
            bp = cand.get("before_page")
            ap = cand.get("after_page")
            text_diff_val = cand.get("text_diff", 0.0)

            is_high_conf_reflow = False
            if (
                state == "paired"
                and bp is not None and ap is not None
                and int(ap) - int(bp) >= 3
            ):
                if text_diff_val < 0.05:
                    # 文字內容幾乎相同，直接判定為純頁面偏移
                    is_high_conf_reflow = True
                elif text_diff_val < 0.15:
                    # 中等文字差異，需要鄰頁相似度確認
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

        # Step 4：組裝 prompt（只用需要 LLM 分析的候選頁）
        messages = _build_prompt(
            llm_candidates,
            before_render_dir,
            after_render_dir,
            before_texts,
            after_texts,
        )

        # Step 5：呼叫 LLM（並 dump debug 資料）
        if llm_candidates:
            _dump_llm_debug(messages, settings)  # 送出前先存 messages
            raw_response = _call_llm(messages, settings)
            _dump_llm_debug(messages, settings, raw_response)  # 收到回應後補存 response
            llm_result = _parse_llm_response(raw_response, llm_candidates)
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
                        "summary": "頁面重排（版面調整導致頁面邊界位移，內容無實質變更）",
                        "changes": [
                            {"type": "modified", "description": f"頁碼從 {candidate.get('before_page')} 變更為 {candidate.get('after_page')}（頁面重排）"}
                        ],
                    }
                )
            else:
                llm_page = slot_to_llm.get(slot_no, {})
                merged_pages.append(
                    {
                        "slot": slot_no,
                        "state": candidate["state"],
                        "before_page": candidate.get("before_page"),
                        "after_page": candidate.get("after_page"),
                        "image_diff": candidate.get("image_diff", 0.0),
                        "text_diff": candidate.get("text_diff", 0.0),
                        "reason": candidate.get("reason", ""),
                        "importance": llm_page.get("importance", "medium"),
                        "summary": llm_page.get("summary", ""),
                        "changes": llm_page.get("changes", []),
                    }
                )

        render_id, all_slots = _persist_renders(
            settings, before_render_dir, after_render_dir, all_pages
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
