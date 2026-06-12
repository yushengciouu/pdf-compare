# 啟動指南

## 方式 A：本機開發

> 以下指令在 `backend/` 目錄內執行

### 1. 首次建立環境（日常啟動免執行此步驟）

```powershell
cd backend
# 建立虛擬環境（若預設 python 版本不穩，建議指定 python3.11）
python3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. 啟動服務（非首次安裝，日常直接執行此步驟即可）

```powershell
cd backend
.\.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload --port 8000
```

開啟 `http://localhost:8000` 即可使用。

## 方式 B：Docker（建議用於部署）

> 以下指令在**專案根目錄**執行（`docker-compose.yml` 所在位置）

```powershell
cd "C:\Users\felix_chiu\Desktop\project\pdf-compare"
docker compose up --build
```

背景執行：

```powershell
docker compose up -d --build
```

只會啟動 **1 個 container**（api），內建排程清理，不需要 Redis 或其他服務。

### 更換 LLM 伺服器

```powershell
$env:PDF_COMPARE_LLM_BASE_URL = "http://your-llm-host:8001"
docker compose up -d --build
```

或在 `backend/.env` 中設定：

```
PDF_COMPARE_LLM_BASE_URL=http://your-llm-host:8001
```

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
