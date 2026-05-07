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


def _make_text_diff(before_text: str, after_text: str) -> str:
    """
    產生可讀的 unified diff 字串。
    若兩份文字相同，回傳空字串。
    """
    if not before_text and not after_text:
        return ""
    if not before_text:
        return f"+ {after_text[:1500]}"  # 全新頁，只顯示 after 文字（限長）
    if not after_text:
        return f"- {before_text[:1500]}"  # 删除頁，只顯示 before 文字（限長）

    before_lines = before_text.splitlines(keepends=True)
    after_lines = after_text.splitlines(keepends=True)

    diff_lines = list(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile="before",
            tofile="after",
            lineterm="",
            n=2,  # 上下文行數
        )
    )

    if not diff_lines:
        return "(文字內容無差異)"

    # 限制總長度，避免 token 爆炸
    MAX_CHARS = 2500
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

    # --- 舊版（before）圖片 ---
    if before_page is not None:
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
    if after_page is not None:
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
    # 若 after_page - before_page >= 3，代表中間可能有整批頁面位移，
    # 提供 before:page+1 的文字讓 LLM 確認相似內容是否已存在於舊版鄰頁
    if state == "paired" and before_page is not None and after_page is not None:
        offset = int(after_page) - int(before_page)
        if offset >= 3:
            next_before_idx = int(before_page)  # 0-based index = page+1 - 1 = page
            if next_before_idx < len(before_texts):
                neighbor_text = before_texts[next_before_idx][:1500].strip()
                if neighbor_text:
                    content.append({
                        "type": "text",
                        "text": (
                            f"【舊版鄰頁文字（第 {int(before_page) + 1} 頁，供跨頁位移比對參考）】\n"
                            f"{neighbor_text}"
                        ),
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


def _build_full_text_index(
    before_texts: list[str],
    after_texts: list[str],
    max_excerpt: int = 120,
) -> str:
    """
    產生舊版/新版所有頁面的文字摘要索引（移除樣板行後取前 max_excerpt 字元）。
    當有 inserted/deleted 槽位時加入 prompt，
    讓 LLM 能跨頁搜尋比對，避免把頁面位移誤判為新增或刪除。
    """
    from app.services.page_match import _strip_boilerplate

    # 移除樣板行（頁腳/頁首）再擷取摘要
    all_texts = list(before_texts) + list(after_texts)
    stripped = _strip_boilerplate(all_texts)
    stripped_before = stripped[:len(before_texts)]
    stripped_after = stripped[len(before_texts):]

    lines = [
        "【全文頁面摘要索引（請在判斷新增/刪除頁前先查閱此索引）】",
        "B###=舊版頁碼  A###=新版頁碼（已移除共同頁腳樣板）",
        "─ 舊版 ─",
    ]
    for i, text in enumerate(stripped_before, 1):
        excerpt = text[:max_excerpt].replace("\n", " ").strip()
        lines.append(f"  B{i:03d}: {excerpt if excerpt else '（空白頁）'}")
    lines.append("─ 新版 ─")
    for i, text in enumerate(stripped_after, 1):
        excerpt = text[:max_excerpt].replace("\n", " ").strip()
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
代表中間有多頁被插入，該配對頁的內容可能來自舊版的「下一頁」而非真正新增。

在對任何「新增頁（inserted）」、「刪除頁（deleted）」或「配對頁中的 added/removed 變更」下結論前，請先執行以下步驟：
1. 查閱 user 訊息開頭的「全文頁面摘要索引」（B###=舊版各頁摘要，A###=新版各頁摘要）
2. 若有提供「舊版鄰頁文字（第 N+1 頁）」，務必比對其內容與 after 頁是否相似
3. 對「新增頁」或「配對頁中的 added 內容」：搜尋其關鍵文字（章節號碼、段落首句）是否已出現在舊版索引（B###）中的某頁 → 若有，則該內容極可能是「頁面位移」而非真正的新增，summary 中應說明「內容源自舊版第 N 頁，可能為頁面位移」，importance 降為 low
4. 對「刪除頁」或「配對頁中的 removed 內容」：搜尋其關鍵文字是否已出現在新版索引（A###）中的某頁 → 若有，則該內容極可能是「頁面位移」而非真正的刪除，summary 中應說明「內容仍存在於新版第 N 頁，可能為頁面位移」，importance 降為 low
5. 只有在確認整份文件的另一版本中完全找不到相似內容時，才能判斷為真正的新增或刪除

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


def _dump_llm_debug(messages: list[dict], raw_response: str | None = None) -> Path:
    """
    將送給 LLM 的 messages 存到 backend/tmp/llm_debug/<timestamp>/，
    圖片另存為 PNG 檔案，JSON 中以相對路徑取代 base64。
    若提供 raw_response，一併存為 response.txt。
    回傳 dump 目錄路徑。
    """
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    # 相對於此檔案往上兩層到 backend/，再進 tmp/llm_debug
    debug_root = Path(__file__).parent.parent.parent / "tmp" / "llm_debug" / ts
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

        if not candidates:
            return {
                "summary": prefilter_report["summary"],
                "thresholds": prefilter_report["thresholds"],
                "overall_summary": "未偵測到任何差異頁面",
                "pages": [],
            }

        # Step 3：提取文字層
        before_texts = extract_page_texts(before_pdf)
        after_texts = extract_page_texts(after_pdf)

        # Step 4：組裝 prompt
        messages = _build_prompt(
            candidates,
            before_render_dir,
            after_render_dir,
            before_texts,
            after_texts,
        )

        # Step 5：呼叫 LLM（並 dump debug 資料）
        _dump_llm_debug(messages)  # 送出前先存 messages
        raw_response = _call_llm(messages, settings)
        _dump_llm_debug(messages, raw_response)  # 收到回應後補存 response

        # Step 6：解析回應
        llm_result = _parse_llm_response(raw_response, candidates)

        # Step 7：合併 prefilter 資訊與 LLM 分析結果
        slot_to_candidate = {int(c["slot"]): c for c in candidates}
        slot_to_llm = {int(p["slot"]): p for p in llm_result.get("pages", [])}

        merged_pages = []
        for slot_no in sorted(slot_to_candidate.keys()):
            candidate = slot_to_candidate[slot_no]
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

        return {
            "summary": prefilter_report["summary"],
            "thresholds": prefilter_report["thresholds"],
            "overall_summary": llm_result.get("overall_summary", ""),
            "pages": merged_pages,
        }

    finally:
        shutil.rmtree(temp_root, ignore_errors=True)
