# PDF 比對系統實作藍圖

## 1) 目標與範圍

建立一個接近 Draftable 體驗的 PDF 比對系統，採用後端運算、前端展示。

必備兩種模式：

- `fast`：第 N 頁對第 N 頁（速度快、成本低）
- `smart`：自動頁面配對（可處理插頁/刪頁）

初始限制：

- 單一 PDF 上限：50 MB
- 單一 PDF 頁數上限：200 頁
- 任務資料保留：24 小時後自動清除

儲存位置：

- `C:\Users\felix_chiu\Desktop\project\var\compare\jobs\<job_id>\...`

## 2) 建議技術棧

後端：

- Python 3.11+
- FastAPI（REST API）
- Celery + Redis（背景任務與佇列）
- PyMuPDF（PDF 渲染）
- OpenCV + NumPy（對齊、差異計算）

前端：

- React + TypeScript
- PDF.js（頁面顯示）

基礎設施：

- MVP 先用本機檔案系統儲存產物
- Redis 作為 queue 與任務狀態後端

## 3) 專案目錄規劃

```text
project/
  backend/
    app/
      api/
        compare.py
      core/
        config.py
      models/
        schemas.py
      services/
        render.py
        align.py
        diff_fast.py
        page_match.py
        diff_smart.py
        storage.py
      workers/
        celery_app.py
        tasks.py
      main.py
    requirements.txt
  frontend/
    src/
      pages/ComparePage.tsx
      components/ViewerPane.tsx
      components/DiffOverlay.tsx
      components/DiffList.tsx
      api/client.ts
  docs/
    implementation-blueprint.md
  var/
    compare/
      jobs/
        <job_id>/
          input/
            before.pdf
            after.pdf
          render/
            before/0001.png
            after/0001.png
          diff/
            mask/0001.png
            boxes/0001.json
          meta.json
```

## 4) 儲存結構與檔案契約

每個 `job_id` 使用獨立資料夾：

```text
var/compare/jobs/<job_id>/
  input/
    before.pdf
    after.pdf
  render/
    before/<page_no>.png
    after/<page_no>.png
  diff/
    mask/<page_no>.png
    boxes/<page_no>.json
  page_map.json
  meta.json
  error.json (僅失敗時存在)
```

命名規則：

- 頁碼檔名固定用補零格式：`0001`、`0002`...
- 儲存路徑不保留使用者原始檔名（避免衝突與風險）

## 5) API 設計

### `POST /api/compare`

建立比對任務。

`multipart/form-data` 欄位：

- `before`（PDF）
- `after`（PDF）
- `mode`（`fast` 或 `smart`，預設 `fast`）

回傳：

```json
{
  "job_id": "uuid",
  "status": "queued",
  "mode": "fast"
}
```

驗證規則：

- 副檔名需為 `.pdf`
- MIME 應為 `application/pdf`
- 每個檔案不得超過 50 MB

### `GET /api/compare/{job_id}`

查詢任務狀態與統計。

```json
{
  "job_id": "uuid",
  "status": "queued|running|done|failed",
  "progress": { "current": 12, "total": 80 },
  "mode": "fast|smart",
  "stats": {
    "pages_before": 100,
    "pages_after": 101,
    "paired_pages": 99,
    "inserted_pages": 1,
    "deleted_pages": 0,
    "total_diff_boxes": 324
  },
  "created_at": "ISO-8601",
  "expires_at": "ISO-8601"
}
```

### `GET /api/compare/{job_id}/pages/{page_no}`

查詢單頁比對結果（給前端 viewer 使用）。

```json
{
  "page_no": 4,
  "mapping": {
    "before_page": 4,
    "after_page": 5,
    "state": "paired|inserted|deleted"
  },
  "assets": {
    "before_image": "/static/jobs/<job_id>/render/before/0004.png",
    "after_image": "/static/jobs/<job_id>/render/after/0005.png",
    "mask_image": "/static/jobs/<job_id>/diff/mask/0004.png"
  },
  "boxes": [
    { "x": 120, "y": 380, "w": 240, "h": 38, "score": 0.92, "type": "content_change" }
  ],
  "width": 1654,
  "height": 2339
}
```

### （可選）`DELETE /api/compare/{job_id}`

手動立即刪除任務產物。

## 6) 後端處理流程

### A. 共用流程

1. 建立 `job_id` 與目錄
2. 儲存上傳檔至 `input/`
3. 讀取頁數並驗證上限
4. 送入 Celery 背景任務

### B. `fast` 模式（`N -> N`）

對每個 `i = 1..min(pages_before, pages_after)`：

1. 固定 DPI（建議 240）渲染兩邊頁面
2. 轉灰階
3. 輕度 Gaussian blur 降低抗鋸齒雜訊
4. 進行小範圍平移/縮放對齊
5. 計算 `absdiff`
6. 閾值化成二值遮罩
7. 開閉運算去噪
8. Connected components 轉差異框
9. 輸出 `mask` PNG 與 `boxes` JSON

超出頁數的部分：

- `before` 多出頁面標記為 `deleted`
- `after` 多出頁面標記為 `inserted`

### C. `smart` 模式（自動頁面配對）

1. 先渲染所有頁低解析縮圖
2. 建立頁面特徵：
   - 影像 hash（pHash 或 dHash）
   - 可選：文字層摘要特徵
3. 建立相似度矩陣 `S(i, j)`
4. 以動態規劃做序列對齊：
   - 配對成本依相似度
   - 插入/刪除使用 gap penalty
5. 產出 `page_map`（paired/inserted/deleted）
6. 對 `paired` 的頁面套用 `fast` 差異流程

## 7) 演算法預設參數

- 渲染 DPI：240
- 差異閾值：25（灰階 0-255）
- 最小差異區塊面積：40 px
- morphology kernel：3x3 或 5x5
- 對齊策略：
  - 第一層：phase correlation（平移）
  - 第二層：ECC affine（少量迭代）

以上參數需用實際樣本再調校。

## 8) 資料格式

`meta.json`：

```json
{
  "job_id": "uuid",
  "mode": "fast|smart",
  "status": "running|done|failed",
  "created_at": "ISO-8601",
  "expires_at": "ISO-8601",
  "pages_before": 0,
  "pages_after": 0,
  "stats": {
    "paired_pages": 0,
    "inserted_pages": 0,
    "deleted_pages": 0,
    "total_diff_boxes": 0
  }
}
```

`page_map.json`：

```json
[
  { "slot": 1, "before_page": 1, "after_page": 1, "state": "paired" },
  { "slot": 2, "before_page": null, "after_page": 2, "state": "inserted" },
  { "slot": 3, "before_page": 2, "after_page": 3, "state": "paired" }
]
```

`boxes/<page_no>.json`：

```json
[
  { "x": 100, "y": 220, "w": 180, "h": 36, "score": 0.88, "type": "content_change" }
]
```

## 9) 前端互動規格

- 左右雙欄 viewer，同步捲動與縮放
- 差異遮罩可調透明度
- 差異清單可點擊跳頁與定位
- 模式切換（`fast` / `smart`）會重建新任務
- `smart` 模式顯示新增頁/刪除頁標籤

## 10) 安全與穩定性

- 僅允許 PDF 上傳
- 先做大小與頁數限制，超限立即拒絕
- 任務逾時（例如 10 分鐘）
- 建議 worker 容器化隔離
- 不執行 PDF 內嵌腳本
- 任務失敗寫入 `error.json`（回傳安全錯誤訊息）

## 11) 清理策略

- 在 `meta.json` 紀錄 `expires_at`
- 每小時排程清理一次：刪除過期 `jobs/<job_id>`
- 提供 API 手動刪除單筆任務

## 12) 分階段交付計畫

第 1 階段（MVP）：

1. 完成 FastAPI 上傳與狀態 API
2. 串 Celery + Redis
3. 完成 `fast` 模式渲染與差異產物
4. 前端完成雙欄顯示與遮罩疊圖

第 2 階段（Smart）：

1. 頁面特徵抽取
2. 動態規劃頁面配對
3. 前端新增頁/刪除頁呈現

第 3 階段（強化）：

1. 對齊與閾值調參
2. 失敗重試與錯誤診斷
3. 清理排程與保留策略驗證
4. 整合測試與效能測試

## 13) 建議先實作的檔案

先把設定與 API 契約打穩：

- `backend/app/core/config.py`
- `backend/app/models/schemas.py`
- `backend/app/api/compare.py`

這樣後續接渲染與比對服務會比較順。
