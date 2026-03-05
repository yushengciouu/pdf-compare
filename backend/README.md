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

如需調整儲存路徑、頁數限制、Redis 位置，可修改 `.env`。

## 3. 啟動 Redis

請先確保本機有 Redis，並且可用 `redis://localhost:6379` 連線。

## 4. 啟動 API

```bash
uvicorn app.main:app --reload --port 8000
```

## 5. 啟動 Celery Worker

```bash
celery -A app.workers.celery_app.celery_app worker --loglevel=info
```

## 6. 啟動 Celery Beat（排程清理）

```bash
celery -A app.workers.celery_app.celery_app beat --loglevel=info
```

## 7. 測試頁面

API 跑起來後可開啟：

- `http://localhost:8000/`（最小測試前端）
- `http://localhost:8000/docs`（Swagger）

## 8. 快速自動測試（不需啟動 worker）

可先做同步 smoke test，確認渲染與比對流程可跑通：

```bash
python scripts/smoke_test.py --mode fast
python scripts/smoke_test.py --mode smart
```

若輸出 `status=done`，代表核心流程正常。

## 9. 重要 API 補充

- `GET /api/compare/{job_id}/pages`：一次取回頁面清單（前端 lazy 顯示）
- `POST /api/compare/{job_id}/cancel`：請求取消任務
- `POST /api/compare/{job_id}/export`：背景排程產生匯出 PDF
- `GET /api/compare/{job_id}/export`：查詢匯出狀態
- `GET /api/compare/{job_id}/export/download`：下載匯出 PDF

## 10. Docker Compose（可選）

在專案根目錄執行：

```bash
docker compose up --build
```

會同時啟動 `api + worker + beat + redis`。
