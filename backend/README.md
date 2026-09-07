# 後端開發說明

> **注意**：以下指令都在 `backend/` 目錄內執行。
> 若要用 Docker 啟動，請回到**專案根目錄**參考 [README.md](../README.md)。

---

## 本機開發環境設定

建議使用 Python 3.11 或 3.12。

```powershell
# 在 backend/ 目錄內
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 環境變數（可選）

```powershell
copy .env.example .env
```

可調整的項目：儲存路徑、頁數上限、LLM 端點、DPI 等。

## 啟動開發伺服器

```powershell
uvicorn app.main:app --reload --port 8000
```

啟動後可開啟：
- `http://localhost:8000/` — 前端頁面
- `http://localhost:8000/docs` — Swagger API 文件

> 排程清理（每小時清除過期任務）已內建在 API 中，不需額外啟動任何服務。

## 快速自動測試

```powershell
python scripts/smoke_test.py --mode fast
python scripts/smoke_test.py --mode smart
```

輸出 `status=done` 代表核心流程正常。

---

## API 端點清單

| 方法 | 路徑 | 說明 |
|------|------|------|
| POST | `/api/compare` | 上傳兩份 PDF，建立比對任務 |
| GET | `/api/compare/{job_id}` | 查詢任務狀態與進度 |
| GET | `/api/compare/{job_id}/pages/{page_no}` | 取得單頁比對結果 |
| POST | `/api/compare/analyze` | LLM 全自動分析（直接呼叫 LLM） |
