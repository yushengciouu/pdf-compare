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

在專案根目錄執行：

```powershell
cd "C:\Users\felix_chiu\Desktop\project\pdf-compare"
docker compose up --build
```

會同時啟動：

- Redis
- API
- Celery Worker
- Celery Beat

## 啟動後如何使用

- 前端頁面：`http://127.0.0.1:8000/`
- Swagger：`http://127.0.0.1:8000/docs`
- 健康檢查：`http://127.0.0.1:8000/health`

## 常見問題

- 若頁面打不開，先確認 API 是否有啟動成功。
- 若任務停在 `queued`，通常是 Worker 沒啟動。
- 若要重建環境，只需刪除 `backend/.venv` 後重建。
