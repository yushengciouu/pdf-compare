from __future__ import annotations

import base64
import html
import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from uuid import uuid4

from app.core.config import Settings

logger = logging.getLogger(__name__)


def get_log_dir(settings: Settings) -> Path:
    """取得並確保 log 目錄存在。"""
    log_dir = settings.log_dir
    if not log_dir.is_absolute():
        candidates = [
            Path(__file__).resolve().parents[3] / log_dir,  # 本機開發：pdf-compare/log
            Path.cwd() / log_dir,
            Path("/app") / log_dir,  # Docker 容器內
        ]
        resolved = None
        for candidate in candidates:
            if candidate.parent.exists():
                resolved = candidate
                break
        log_dir = resolved if resolved is not None else Path.cwd() / log_dir

    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def _load_image_base64(img_path: Path) -> str | None:
    """讀取本機 PNG 圖檔並轉換為 Base64 Data URI。"""
    try:
        if img_path.exists() and img_path.is_file():
            raw = img_path.read_bytes()
            encoded = base64.b64encode(raw).decode("ascii")
            return f"data:image/png;base64,{encoded}"
    except Exception as e:
        logger.warning("Failed to read image %s as base64: %s", img_path, e)
    return None


def generate_html_report(
    before_filename: str,
    after_filename: str,
    report_data: dict,
    model_name: str | None = None,
    generated_at: datetime | None = None,
    settings: Settings | None = None,
) -> str:
    """
    產生排版精美、完全獨立（含內嵌 CSS、JS、Base64 預覽截圖與 SVG 差異標記）的靜態 HTML 分析報告。
    可在任何瀏覽器離線打開，完整保留「文字分析清單」與「前後版本對照預覽區域」。
    """
    if generated_at is None:
        generated_at = datetime.now()

    time_str = generated_at.strftime("%Y-%m-%d %H:%M:%S")
    summary = report_data.get("summary", {})
    overall_summary = report_data.get("overall_summary", "").strip()
    pages = report_data.get("pages", [])
    all_slots = report_data.get("all_slots", [])
    render_id = report_data.get("render_id")
    model = model_name or summary.get("model", "預設模型")

    # 統計數據
    total_slots = summary.get("total_slots", len(pages))
    candidate_pages = summary.get("candidate_pages", len(pages))
    total_changes = sum(len(p.get("changes", [])) for p in pages)

    state_labels = {
        "paired": "配對頁",
        "inserted": "新增頁",
        "deleted": "刪除頁",
    }

    # 1. 建立「各頁變更條目清單」HTML
    pages_html_list = []
    for p in pages:
        slot = p.get("slot", "-")
        state = p.get("state", "paired")
        state_text = state_labels.get(state, state)
        state_class = f"badge-{state}"

        bp = p.get("before_page")
        ap = p.get("after_page")
        page_info = f"舊版 p.{bp} → 新版 p.{ap}" if (bp is not None or ap is not None) else ""
        if bp is not None and ap is None:
            page_info = f"舊版 p.{bp}（已刪除）"
        elif bp is None and ap is not None:
            page_info = f"新版 p.{ap}（新增加）"

        page_summary = html.escape(p.get("summary", "").strip() or "無重大變更說明")
        changes = p.get("changes", [])

        changes_html_list = []
        for ch in changes:
            ch_type = ch.get("type", "modified")
            desc = html.escape(ch.get("description", ""))

            type_label = "修改"
            if ch_type == "added":
                type_label = "新增"
            elif ch_type == "removed":
                type_label = "刪除"

            changes_html_list.append(
                f'<li class="change-item change-{ch_type}">'
                f'<span class="change-tag change-tag-{ch_type}">{type_label}</span>'
                f'<span class="change-text">{desc}</span>'
                f'</li>'
            )

        changes_content = (
            f'<ul class="changes-list">{"".join(changes_html_list)}</ul>'
            if changes_html_list
            else '<div class="no-changes-text">無具體條目變更</div>'
        )

        image_diff = p.get("image_diff", 0.0)
        text_diff = p.get("text_diff", 0.0)
        diff_badge = f'<span class="diff-scores">視覺: {image_diff:.3f} · 文字: {text_diff:.3f}</span>'

        pages_html_list.append(
            f'''
            <div class="page-card" id="page-item-slot-{slot}">
              <div class="page-card-header">
                <div class="page-title-group">
                  <span class="slot-number">槽位 #{slot}</span>
                  <span class="state-badge {state_class}">{state_text}</span>
                  <span class="page-mapping">{page_info}</span>
                </div>
                <div style="display:flex;align-items:center;gap:12px;">
                  {diff_badge}
                  <button type="button" class="jump-preview-btn" onclick="jumpToSlot({slot})">↓ 跳至預覽</button>
                </div>
              </div>
              <div class="page-summary">{page_summary}</div>
              {changes_content}
            </div>
            '''
        )

    pages_content = (
        "".join(pages_html_list)
        if pages_html_list
        else '<div class="empty-state">本次分析未偵測到實質差異頁面。</div>'
    )

    overall_section = ""
    if overall_summary:
        overall_section = f"""
        <section class="card overall-card">
          <div class="card-title">📌 總體變更摘要</div>
          <div class="overall-text">{html.escape(overall_summary)}</div>
        </section>
        """

    # 2. 建立「前後版本對照預覽」HTML（嵌合 Base64 截圖與差異框資料）
    browser_before_cards = []
    browser_after_cards = []
    slots_box_data: dict[str, dict] = {}

    for entry in all_slots:
        slot_no = entry.get("slot")
        state = entry.get("state", "paired")
        state_text = state_labels.get(state, state)
        bp = entry.get("before_page")
        ap = entry.get("after_page")

        # 優先使用 entry 內已轉換好的 Base64 Data URI
        before_b64 = entry.get("before_image")
        after_b64 = entry.get("after_image")
        if before_b64 and not before_b64.startswith("data:"):
            before_b64 = None
        if after_b64 and not after_b64.startswith("data:"):
            after_b64 = None

        # 若未提供 base64，才嘗試從磁碟載入
        if not before_b64 and settings is not None and render_id and bp is not None:
            p_path = settings.jobs_root / render_id / "render" / "before" / f"{int(bp):04d}.png"
            before_b64 = _load_image_base64(p_path)
        if not after_b64 and settings is not None and render_id and ap is not None:
            p_path = settings.jobs_root / render_id / "render" / "after" / f"{int(ap):04d}.png"
            after_b64 = _load_image_base64(p_path)

        before_boxes = entry.get("before_text_boxes") or []
        after_boxes = entry.get("after_text_boxes") or []

        slots_box_data[f"before-{slot_no}"] = before_boxes
        slots_box_data[f"after-{slot_no}"] = after_boxes

        # Before Card
        before_img_tag = (
            f'<div class="diff-img-wrap" id="wrap-before-{slot_no}">'
            f'<img src="{before_b64}" alt="before slot {slot_no}" onload="drawBoxes(\'before\', {slot_no})" />'
            f'<svg class="diff-svg-overlay" id="svg-before-{slot_no}"></svg>'
            f'</div>'
            if before_b64
            else '<div class="placeholder-box">無對應頁面</div>'
        )
        browser_before_cards.append(
            f'''
            <div class="analyze-page-card" id="analyze-before-slot-{slot_no}">
              <div class="analyze-page-head">
                <span>Slot {slot_no}</span>
                <span>{state_text}{" · p." + str(bp) if bp is not None else ""}</span>
              </div>
              {before_img_tag}
            </div>
            '''
        )

        # After Card
        after_img_tag = (
            f'<div class="diff-img-wrap" id="wrap-after-{slot_no}">'
            f'<img src="{after_b64}" alt="after slot {slot_no}" onload="drawBoxes(\'after\', {slot_no})" />'
            f'<svg class="diff-svg-overlay" id="svg-after-{slot_no}"></svg>'
            f'</div>'
            if after_b64
            else '<div class="placeholder-box">無對應頁面</div>'
        )
        browser_after_cards.append(
            f'''
            <div class="analyze-page-card" id="analyze-after-slot-{slot_no}">
              <div class="analyze-page-head">
                <span>Slot {slot_no}</span>
                <span>{state_text}{" · p." + str(ap) if ap is not None else ""}</span>
              </div>
              {after_img_tag}
            </div>
            '''
        )

    browser_section = ""
    if all_slots:
        browser_section = f"""
        <div class="section-title">🔍 前後版本對照預覽 (Before vs After)</div>
        <div class="analyze-browser" id="analyzeBrowser">
          <div class="analyze-browser-grid">
            <div class="analyze-browser-pane" id="analyzeBrowserBefore">
              <h4>Before (舊版)</h4>
              <div id="analyzeBrowserBeforePages">
                {"".join(browser_before_cards)}
              </div>
            </div>
            <div class="analyze-browser-pane" id="analyzeBrowserAfter">
              <h4>After (新版)</h4>
              <div id="analyzeBrowserAfterPages">
                {"".join(browser_after_cards)}
              </div>
            </div>
          </div>
        </div>
        """

    slots_box_json = json.dumps(slots_box_data, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>PDF 比對分析報告 - {time_str}</title>
  <style>
    :root {{
      --primary: #1769a8;
      --primary-dark: #103d62;
      --bg: #f8fafc;
      --card-bg: #ffffff;
      --text: #1e293b;
      --muted: #64748b;
      --border: #e2e8f0;
      --success: #16a34a;
      --success-bg: #f0fdf4;
      --danger: #dc2626;
      --danger-bg: #fef2f2;
      --warning: #d97706;
      --warning-bg: #fffbeb;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      padding: 32px 20px;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Noto Sans TC", sans-serif;
      color: var(--text);
      background-color: var(--bg);
      line-height: 1.6;
    }}
    .container {{
      max-width: 1380px;
      margin: 0 auto;
    }}
    .header {{
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 28px 32px;
      margin-bottom: 24px;
      box-shadow: 0 4px 12px rgba(0, 0, 0, 0.04);
      position: relative;
      overflow: hidden;
    }}
    .header::before {{
      content: "";
      position: absolute;
      top: 0;
      left: 0;
      right: 0;
      height: 4px;
      background: linear-gradient(90deg, var(--primary-dark), var(--primary), #38bdf8);
    }}
    .title-row {{
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      flex-wrap: wrap;
      gap: 12px;
      margin-bottom: 20px;
      border-bottom: 1px solid var(--border);
      padding-bottom: 16px;
    }}
    h1 {{
      margin: 0;
      font-size: 26px;
      color: var(--primary-dark);
    }}
    .report-time {{
      font-size: 13px;
      color: var(--muted);
    }}
    .meta-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 16px;
      font-size: 14px;
    }}
    .meta-item {{
      background: #f8fafc;
      padding: 10px 14px;
      border-radius: 8px;
      border: 1px solid var(--border);
    }}
    .meta-label {{
      font-size: 12px;
      color: var(--muted);
      margin-bottom: 4px;
      font-weight: 600;
    }}
    .meta-value {{
      font-weight: 500;
      word-break: break-all;
      color: var(--text);
    }}
    .stats-row {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
      gap: 14px;
      margin-bottom: 24px;
    }}
    .stat-card {{
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 16px 20px;
      text-align: center;
      box-shadow: 0 2px 6px rgba(0, 0, 0, 0.02);
    }}
    .stat-num {{
      font-size: 28px;
      font-weight: 700;
      color: var(--primary);
      margin-bottom: 4px;
    }}
    .stat-desc {{
      font-size: 13px;
      color: var(--muted);
    }}
    .card {{
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 24px 28px;
      margin-bottom: 24px;
      box-shadow: 0 4px 12px rgba(0, 0, 0, 0.03);
    }}
    .card-title {{
      font-size: 17px;
      font-weight: 700;
      color: var(--primary-dark);
      margin-bottom: 12px;
    }}
    .overall-text {{
      font-size: 15px;
      line-height: 1.7;
      color: #334155;
      white-space: pre-line;
    }}
    .section-title {{
      font-size: 19px;
      font-weight: 700;
      color: var(--primary-dark);
      margin: 28px 0 16px;
      display: flex;
      align-items: center;
      gap: 8px;
    }}
    .page-card {{
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 18px 22px;
      margin-bottom: 16px;
      box-shadow: 0 2px 6px rgba(0, 0, 0, 0.02);
    }}
    .page-card-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 10px;
      margin-bottom: 12px;
      padding-bottom: 10px;
      border-bottom: 1px solid #f1f5f9;
    }}
    .page-title-group {{
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }}
    .slot-number {{
      font-size: 15px;
      font-weight: 700;
      color: var(--primary-dark);
    }}
    .state-badge {{
      font-size: 12px;
      font-weight: 600;
      padding: 2px 8px;
      border-radius: 4px;
    }}
    .badge-paired {{ background: #e0f2fe; color: #0369a1; }}
    .badge-inserted {{ background: var(--success-bg); color: var(--success); }}
    .badge-deleted {{ background: var(--danger-bg); color: var(--danger); }}
    .page-mapping {{
      font-size: 13px;
      color: var(--muted);
    }}
    .diff-scores {{
      font-size: 12px;
      color: #94a3b8;
      font-family: monospace;
    }}
    .jump-preview-btn {{
      background: #eff6ff;
      color: #1d4ed8;
      border: 1px solid #bfdbfe;
      border-radius: 6px;
      padding: 4px 10px;
      font-size: 12px;
      cursor: pointer;
      font-weight: 600;
      transition: all 0.15s ease;
    }}
    .jump-preview-btn:hover {{
      background: #dbeafe;
      color: #1e40af;
    }}
    .page-summary {{
      font-size: 14px;
      color: #475569;
      margin-bottom: 12px;
      font-weight: 500;
    }}
    .changes-list {{
      list-style: none;
      padding: 0;
      margin: 0;
      display: grid;
      gap: 8px;
    }}
    .change-item {{
      display: flex;
      align-items: baseline;
      gap: 10px;
      font-size: 13px;
      padding: 8px 12px;
      border-radius: 6px;
      line-height: 1.5;
    }}
    .change-tag {{
      font-size: 11px;
      font-weight: 700;
      padding: 1px 6px;
      border-radius: 4px;
      white-space: nowrap;
    }}
    .change-added {{
      background: var(--success-bg);
      color: #14532d;
      border-left: 3px solid var(--success);
    }}
    .change-tag-added {{
      background: #bbf7d0;
      color: #15803d;
    }}
    .change-removed {{
      background: var(--danger-bg);
      color: #7f1d1d;
      border-left: 3px solid var(--danger);
    }}
    .change-tag-removed {{
      background: #fecaca;
      color: #b91c1c;
    }}
    .change-modified {{
      background: var(--warning-bg);
      color: #78350f;
      border-left: 3px solid var(--warning);
    }}
    .change-tag-modified {{
      background: #fde68a;
      color: #b45309;
    }}
    .no-changes-text {{
      font-size: 13px;
      color: var(--muted);
      font-style: italic;
    }}
    .empty-state {{
      text-align: center;
      padding: 48px;
      color: var(--muted);
      background: var(--card-bg);
      border-radius: 12px;
      border: 1px dashed var(--border);
    }}

    /* 預覽瀏覽器區塊 */
    .analyze-browser {{
      margin-top: 16px;
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 16px;
      box-shadow: 0 4px 12px rgba(0, 0, 0, 0.03);
    }}
    .analyze-browser-grid {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 14px;
    }}
    .analyze-browser-pane {{
      position: relative;
      background: #f8fafc;
      border: 1px solid var(--border);
      border-radius: 8px;
      height: 760px;
      overflow-y: auto;
      padding: 12px;
    }}
    .analyze-browser-pane h4 {{
      margin: -12px -12px 12px;
      font-size: 14px;
      color: var(--primary-dark);
      position: sticky;
      top: -12px;
      background: #f1f5f9;
      padding: 10px 14px;
      border-bottom: 1px solid var(--border);
      z-index: 2;
    }}
    .analyze-page-card {{
      display: flex;
      flex-direction: column;
      border: 1px solid var(--border);
      background: #fff;
      border-radius: 8px;
      margin-bottom: 12px;
      padding: 8px;
      box-shadow: 0 1px 3px rgba(0, 0, 0, 0.03);
      transition: outline 0.2s ease;
      box-sizing: border-box;
    }}
    .analyze-page-head {{
      display: flex;
      justify-content: space-between;
      font-size: 12px;
      color: #334155;
      font-weight: 600;
      margin-bottom: 6px;
      padding-bottom: 4px;
      border-bottom: 1px solid #f1f5f9;
    }}
    .diff-img-wrap {{
      position: relative;
      display: block;
      background: #f1f5f9;
      border-radius: 4px;
      overflow: hidden;
    }}
    .diff-img-wrap img {{
      width: 100%;
      display: block;
      border-radius: 4px;
      aspect-ratio: 3/4;
      object-fit: contain;
    }}
    .diff-svg-overlay {{
      position: absolute;
      inset: 0;
      width: 100%;
      height: 100%;
      pointer-events: none;
    }}
    .diff-svg-overlay rect {{
      pointer-events: all;
    }}
    .placeholder-box {{
      flex: 1;
      width: 100%;
      min-height: 240px;
      display: flex;
      align-items: center;
      justify-content: center;
      background: #f8fafc;
      color: #94a3b8;
      border: 2px dashed #cbd5e1;
      border-radius: 6px;
      font-size: 14px;
      font-weight: 500;
      box-sizing: border-box;
    }}
    .footer {{
      text-align: center;
      font-size: 12px;
      color: #94a3b8;
      margin-top: 40px;
      padding-top: 20px;
      border-top: 1px solid var(--border);
    }}
    @media print {{
      body {{ background: #fff; padding: 0; }}
      .header, .card, .page-card, .analyze-browser {{ box-shadow: none; border: 1px solid #ccc; }}
      .analyze-browser-pane {{ height: auto; overflow: visible; }}
    }}
    @media (max-width: 860px) {{
      .analyze-browser-grid {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <div class="container">
    <header class="header">
      <div class="title-row">
        <h1>📊 PDF 比對分析報告</h1>
        <div class="report-time">報告生成時間：{time_str}</div>
      </div>
      <div class="meta-grid">
        <div class="meta-item">
          <div class="meta-label">舊版檔案 (Before)</div>
          <div class="meta-value">{html.escape(before_filename)}</div>
        </div>
        <div class="meta-item">
          <div class="meta-label">新版檔案 (After)</div>
          <div class="meta-value">{html.escape(after_filename)}</div>
        </div>
        <div class="meta-item">
          <div class="meta-label">分析模型</div>
          <div class="meta-value">{html.escape(model)}</div>
        </div>
      </div>
    </header>

    <div class="stats-row">
      <div class="stat-card">
        <div class="stat-num">{total_slots}</div>
        <div class="stat-desc">總比對槽位數</div>
      </div>
      <div class="stat-card">
        <div class="stat-num">{candidate_pages}</div>
        <div class="stat-desc">分析差異頁數</div>
      </div>
      <div class="stat-card">
        <div class="stat-num">{total_changes}</div>
        <div class="stat-desc">詳細變更點項目</div>
      </div>
    </div>

    {overall_section}

    <div class="section-title">📑 差異槽位詳細清單</div>
    {pages_content}

    {browser_section}

    <footer class="footer">
      Generated by PDF Compare Viewer · 本報告支援離線檢視與列印 PDF
    </footer>
  </div>

  <script>
    const slotsBoxes = {slots_box_json};
    const BOX_COLORS = {{
      removed:  {{ fill: "rgba(220,38,38,0.18)",  stroke: "#dc2626" }},
      added:    {{ fill: "rgba(22,163,74,0.18)",   stroke: "#16a34a" }},
      replaced: {{ fill: "rgba(202,138,4,0.18)",   stroke: "#ca8a04" }},
    }};

    function drawBoxes(side, slotNo) {{
      const key = `${{side}}-${{slotNo}}`;
      const boxes = slotsBoxes[key];
      const wrap = document.getElementById(`wrap-${{side}}-${{slotNo}}`);
      const svg = document.getElementById(`svg-${{side}}-${{slotNo}}`);
      if (!wrap || !svg) return;
      const img = wrap.querySelector("img");
      if (!img || !img.naturalWidth) return;

      svg.setAttribute("viewBox", `0 0 ${{img.naturalWidth}} ${{img.naturalHeight}}`);
      svg.innerHTML = "";
      if (!boxes || !boxes.length) return;

      for (const box of boxes) {{
        const color = BOX_COLORS[box.type] || BOX_COLORS.replaced;
        const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
        rect.setAttribute("x", box.x);
        rect.setAttribute("y", box.y);
        rect.setAttribute("width", box.w);
        rect.setAttribute("height", box.h);
        rect.setAttribute("fill", color.fill);
        rect.setAttribute("stroke", color.stroke);
        rect.setAttribute("stroke-width", "2");
        rect.setAttribute("rx", "2");
        if (box.text_before || box.text_after) {{
          const title = document.createElementNS("http://www.w3.org/2000/svg", "title");
          if (box.type === "replaced") {{
            title.textContent = `${{box.text_before}} → ${{box.text_after}}`;
          }} else if (box.type === "removed") {{
            title.textContent = `已刪除：${{box.text_before}}`;
          }} else {{
            title.textContent = `新增：${{box.text_after}}`;
          }}
          rect.appendChild(title);
        }}
        svg.appendChild(rect);
      }}
      scheduleSyncHeights();
    }}

    let syncHeightTimer = null;
    function scheduleSyncHeights() {{
      if (syncHeightTimer) clearTimeout(syncHeightTimer);
      syncHeightTimer = setTimeout(syncCardHeights, 40);
    }}

    function syncCardHeights() {{
      const beforeCards = document.querySelectorAll("#analyzeBrowserBeforePages .analyze-page-card");
      beforeCards.forEach((beforeCard) => {{
        const slotId = beforeCard.id.replace("analyze-before-slot-", "");
        const afterCard = document.getElementById(`analyze-after-slot-${{slotId}}`);
        if (!afterCard) return;
        beforeCard.style.minHeight = "";
        afterCard.style.minHeight = "";
        const maxH = Math.max(beforeCard.offsetHeight, afterCard.offsetHeight);
        beforeCard.style.minHeight = `${{maxH}}px`;
        afterCard.style.minHeight = `${{maxH}}px`;
      }});
    }}

    window.addEventListener("load", () => {{
      syncCardHeights();
      setTimeout(syncCardHeights, 150);
      setTimeout(syncCardHeights, 600);
      setTimeout(syncCardHeights, 1500);
    }});
    window.addEventListener("resize", scheduleSyncHeights);

    function jumpToSlot(slotId) {{
      const browser = document.getElementById("analyzeBrowser");
      if (!browser) return;
      browser.scrollIntoView({{ behavior: "smooth", block: "start" }});

      const beforeCard = document.getElementById(`analyze-before-slot-${{slotId}}`);
      const afterCard = document.getElementById(`analyze-after-slot-${{slotId}}`);
      const beforePane = document.getElementById("analyzeBrowserBefore");
      const afterPane = document.getElementById("analyzeBrowserAfter");

      if (beforeCard && beforePane) {{
        beforePane.scrollTop = beforeCard.offsetTop - 40;
        beforeCard.style.outline = "2px solid #3b82f6";
        setTimeout(() => {{ beforeCard.style.outline = ""; }}, 2000);
      }}
      if (afterCard && afterPane) {{
        afterPane.scrollTop = afterCard.offsetTop - 40;
        afterCard.style.outline = "2px solid #3b82f6";
        setTimeout(() => {{ afterCard.style.outline = ""; }}, 2000);
      }}
    }}

    // 左右同步捲動
    const beforePane = document.getElementById("analyzeBrowserBefore");
    const afterPane = document.getElementById("analyzeBrowserAfter");
    let isSyncing = false;
    if (beforePane && afterPane) {{
      beforePane.addEventListener("scroll", () => {{
        if (isSyncing) return;
        isSyncing = true;
        afterPane.scrollTop = beforePane.scrollTop;
        requestAnimationFrame(() => {{ isSyncing = false; }});
      }});
      afterPane.addEventListener("scroll", () => {{
        if (isSyncing) return;
        isSyncing = true;
        beforePane.scrollTop = afterPane.scrollTop;
        requestAnimationFrame(() => {{ isSyncing = false; }});
      }});
    }}
  </script>
</body>
</html>"""


def save_html_report(
    settings: Settings,
    html_content: str,
    prefix: str = "report",
) -> tuple[str, Path]:
    """將生成的 HTML 報告儲存至 settings.log_dir，並回傳 (檔名, 完整路徑)。"""
    log_dir = get_log_dir(settings)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{prefix}_{ts}_{uuid4().hex[:6]}.html"
    file_path = log_dir / filename
    file_path.write_text(html_content, encoding="utf-8")
    logger.info("Saved HTML analysis report to: %s", file_path)
    return filename, file_path


def cleanup_expired_reports(settings: Settings) -> dict:
    """清理 log 目錄中超過 settings.log_retention_days 天（預設 14 天/兩週）的 HTML 報告。"""
    log_dir = get_log_dir(settings)
    if not log_dir.exists():
        return {"scanned": 0, "deleted": 0, "failed": 0}

    retain_days = getattr(settings, "log_retention_days", 14)
    cutoff = datetime.now(timezone.utc) - timedelta(days=retain_days)
    scanned = deleted = failed = 0

    for file in log_dir.glob("*.html"):
        scanned += 1
        try:
            mtime = datetime.fromtimestamp(file.stat().st_mtime, tz=timezone.utc)
            if mtime <= cutoff:
                file.unlink(missing_ok=True)
                deleted += 1
                logger.info("Deleted expired report: %s (age > %d days)", file.name, retain_days)
        except Exception as e:
            logger.error("Failed to delete expired report %s: %s", file.name, e)
            failed += 1

    return {"scanned": scanned, "deleted": deleted, "failed": failed}
