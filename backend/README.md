# 後端啟動說明

## 1. 安裝依賴

建議使用 Python 3.11 或 3.12。Python 3.14 目前部分套件（如 NumPy/OpenCV）在 Windows 可能沒有預編譯輪子，會導致安裝失敗。

```bash
pip install -r requirements.txt
```

## 2. 準備環境變數

```bash
copy .env.example .env
```

如需調整儲存路徑、頁數限制等，可修改 `.env`。

## 3. 啟動 API

```bash
uvicorn app.main:app --reload --port 8000
```

> 排程清理（每小時清除過期任務、每日清除 LLM debug 檔）已內建在 API 中，不需要額外啟動任何服務。

## 4. 測試頁面

API 跑起來後可開啟：

- `http://localhost:8000/`（最小測試前端）
- `http://localhost:8000/docs`（Swagger）

## 5. 快速自動測試

```bash
python scripts/smoke_test.py --mode fast
python scripts/smoke_test.py --mode smart
```

若輸出 `status=done`，代表核心流程正常。

## 6. 重要 API 補充

- `POST /api/compare/prefilter`：LLM 前處理，快速挑出候選差異頁
- `GET /api/compare/{job_id}/pages`：一次取回頁面清單（前端 lazy 顯示）
- `POST /api/compare/{job_id}/cancel`：請求取消任務
- `POST /api/compare/{job_id}/export`：背景排程產生匯出 PDF
- `GET /api/compare/{job_id}/export`：查詢匯出狀態
- `GET /api/compare/{job_id}/export/download`：下載匯出 PDF

### `POST /api/compare/prefilter` 說明

用途：在送 LLM 之前，先做頁級快速篩選。

- 輸入：`before`、`after`（PDF）
- 可選參數：`image_threshold`、`text_threshold`、`min_candidates`、`neighbor_window`
- 輸出：
  - `candidates`：建議送 LLM 的頁面
  - `all_pages`：全部頁面的分數與原因

前處理主要看兩種分數：

- `image_diff`：影像差異（0~1）
- `text_diff`：文字差異（0~1）

新增頁 / 刪除頁會直接列入候選。

為什麼快：只做頁級打分，不做完整框選與遮罩輸出。

## 7. Docker Compose（可選）

在專案根目錄執行：

```bash
docker compose up --build
```

只會啟動 `api` 一個 container。
