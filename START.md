# 啟動指南

這份文件給專案使用者快速啟動系統。

## 方式 A：本機啟動（開發模式）

### 1) 進入後端目錄

```powershell
cd "C:\Users\felix_chiu\Desktop\project\pdf-compare\backend"
```

### 2) 建立虛擬環境並安裝套件

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 3) 準備環境變數

```powershell
copy .env.example .env
```

### 4) 啟動 Redis

如果有 Docker：

```powershell
docker run -d --name pdf-compare-redis -p 6379:6379 redis:7
```

若容器已存在：

```powershell
docker start pdf-compare-redis
```

### 5) 開三個終端機啟動服務

每個終端機都要先進到 `backend` 並啟用 `.venv`。

終端機 1（API）：

```powershell
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

終端機 2（Worker）：

```powershell
celery -A app.workers.celery_app.celery_app worker --loglevel=info --pool=solo
```

終端機 3（Beat，排程清理）：

```powershell
celery -A app.workers.celery_app.celery_app beat --loglevel=info
```

## 方式 B：Docker Compose 一鍵啟動

### 1) 準備環境變數（可選）

複製範本並依需求修改（尤其是 LLM 設定）：

```powershell
Copy-Item backend\.env.example backend\.env
```

> Docker 部署時，路徑相關設定（`STORAGE_ROOT`、`FRONTEND_DIR`、Redis 連線）會由 `docker-compose.yml` 自動覆蓋，無需手動修改。

若要更換 LLM 伺服器，可在 `.env` 修改，或直接帶入環境變數：

```powershell
$env:PDF_COMPARE_LLM_BASE_URL = "http://your-llm-host:8001"
docker compose up -d --build
```

### 2) 建置並啟動

```powershell
cd "C:\Users\felix_chiu\Desktop\project\pdf-compare"
docker compose up --build
```

若想背景執行：

```powershell
docker compose up -d --build
```

會同時啟動：

- Redis（含 health check）
- API（等 Redis 健康後啟動，2 workers）
- Celery Worker（並行度 2）
- Celery Beat（排程清理）

### 關閉 Docker Compose

前景模式可在執行中的終端按 `Ctrl+C`，或開新終端執行：

```powershell
docker compose down
```

- `docker compose down`：停止所有容器並刪除
- `docker compose stop`：只停止容器，保留資料

### 下次啟動

```powershell
cd "C:\Users\felix_chiu\Desktop\project\pdf-compare"
docker compose up
```

- 若沒改動程式碼，直接 `docker compose up` 即可
- 若有改動程式碼且想重建，執行 `docker compose up --build`

### 查看服務狀態與日誌

```powershell
# 查看所有服務狀態
docker compose ps

# 查看即時日誌
docker compose logs -f

# 只看特定服務
docker compose logs -f worker
```

## 啟動後如何使用

- 前端頁面：`http://127.0.0.1:8000/`
- Swagger：`http://127.0.0.1:8000/docs`
- 健康檢查：`http://127.0.0.1:8000/health`
- 預設比對模式：`smart`（會先做頁面配對，可處理插頁 / 刪頁）

## LLM 前處理（新功能）

在前端頁面上傳兩份 PDF 後，可以直接按 `LLM 前處理`。

- 目的：先挑出「值得送 LLM」的頁面，避免整份長文件直接丟給 LLM 造成 token 過大
- 輸出：候選頁清單（slot、before/after 頁碼、原因、影像分數、文字分數）

### 前處理判定邏輯（摘要）

1. 先做 smart 頁面配對（能處理插頁 / 刪頁）
2. 每個配對頁計算：
   - `image_diff`（影像差異，0~1）
   - `text_diff`（文字差異，0~1；只有抽得到文字層時才會發揮作用）
3. 新增頁 / 刪除頁會直接列入候選
4. 達到閾值的頁面列入候選；若候選數不足，會用高分頁補齊（`top_rank_backup`）

### 為什麼比完整 smart 比對快很多

- 前處理只做頁級打分，不做完整遮罩與框選輸出
- 不跑全量高成本的視覺差異流程（morphology + components）
- 只回傳候選頁資訊，資料量小、處理快

## 常見問題

- 若頁面打不開，先確認 API 是否有啟動成功。
- 若任務停在 `queued`，通常是 Worker 沒啟動。
- 若要重建環境，只需刪除 `backend/.venv` 後重建。
