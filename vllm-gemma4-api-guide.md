# vLLM + Gemma4 API 教學（從原理到實作）

## 這份教學要解決什麼
你已經成功啟動 `vllm/vllm-openai:gemma4` 容器，但想知道：
1. API 為什麼要這樣呼叫
2. 怎麼查 API 規格
3. Postman / PowerShell / curl 實際要怎麼打
4. 怎麼排錯

---

## 1. 先懂 API 是什麼（為什麼要這樣用）

HTTP API 請求有四個核心：

- **URL（端點）**：你要用哪個功能
- **Method（方法）**：`GET` 查資料、`POST` 送資料執行
- **Headers（標頭）**：例如資料格式 `application/json`
- **Body（本文）**：你送給模型的 JSON 內容

所以：
- `/v1/models` 是查詢 => `GET`
- `/v1/chat/completions` 是送對話去推論 => `POST`

---

## 2. 你現在跑的是哪種 API

你使用的映像是：

- `vllm/vllm-openai:gemma4`

關鍵字 `openai` 代表：
這個服務提供 **OpenAI-compatible API**（OpenAI 相容介面）。

因此，你可以用 OpenAI 風格端點與格式：
- `GET /v1/models`
- `POST /v1/chat/completions`

---

## 3. 怎麼查 API 規格（實務流程）

建議照這個順序：

1. **先問服務本身**
   - `GET /v1/models`：確認服務正常、拿可用模型 ID
2. **再確認規格來源**
   - 優先看 vLLM OpenAI-compatible 文件
   - 交叉看 OpenAI Chat Completions 規格
3. **若服務有開 OpenAPI**
   - 試 `/docs` 或 `/openapi.json`（不一定每個部署都有）

你這次 `/v1/models` 回傳顯示：
- `id = google/gemma-4-31B-it`

所以呼叫聊天時 `model` 就應該用這個值。

---

## 4. 核心 API 怎麼打

## 4.1 查模型清單

- Method: `GET`
- URL: `http://192.168.46.226:7777/v1/models`

---

## 4.2 聊天 API（最小可行）

- Method: `POST`
- URL: `http://192.168.46.226:7777/v1/chat/completions`
- Header: `Content-Type: application/json`
- Body:

```json
{
  "model": "google/gemma-4-31B-it",
  "messages": [
    { "role": "user", "content": "請用繁體中文自我介紹" }
  ]
}
```

可再加常用參數：
- `max_tokens`
- `temperature`
- `top_p`

---

## 5. Postman 實作

## 5.1 `/v1/models`
- Method: `GET`
- URL: `http://192.168.46.226:7777/v1/models`
- Body: 空

## 5.2 `/v1/chat/completions`
- Method: `POST`
- URL: `http://192.168.46.226:7777/v1/chat/completions`
- Headers:
  - `Content-Type: application/json`
- Body -> raw -> JSON：

```json
{
  "model": "google/gemma-4-31B-it",
  "messages": [
    { "role": "user", "content": "請用繁體中文自我介紹" }
  ],
  "max_tokens": 128,
  "temperature": 0.7
}
```

---

## 6. 沒有 Postman 也可以

## 6.1 PowerShell（推薦）

```powershell
$body = @{
  model = "google/gemma-4-31B-it"
  messages = @(@{ role="user"; content="請用繁體中文自我介紹" })
  max_tokens = 128
  temperature = 0.7
} | ConvertTo-Json -Depth 5

Invoke-RestMethod -Method Post `
  -Uri "http://192.168.46.226:7777/v1/chat/completions" `
  -ContentType "application/json" `
  -Body $body
```

## 6.2 curl.exe（Windows）

```powershell
curl.exe -X POST "http://192.168.46.226:7777/v1/chat/completions" -H "Content-Type: application/json" -d "{\"model\":\"google/gemma-4-31B-it\",\"messages\":[{\"role\":\"user\",\"content\":\"請用繁體中文自我介紹\"}],\"max_tokens\":128,\"temperature\":0.7}"
```

---

## 7. 為什麼 `curl` 跟 `curl.exe` 顯示不同？

在 Windows PowerShell 中：

- `curl` 常是 `Invoke-WebRequest` 別名
- `curl.exe` 才是原生 cURL

所以輸出不同：

- `curl.exe`：偏向顯示 body（API 實際回應）
- `Invoke-WebRequest`：顯示物件欄位（`StatusCode`、`Headers`、`RawContent`...）

不是資料不見，而是呈現方式不同。

---

## 8. 你看到那些欄位是什麼意思

- `StatusCode`：HTTP 狀態碼（200 = 成功）
- `StatusDescription`：狀態描述（OK）
- `Content`：回應內容（JSON 字串）
- `RawContent`：原始 HTTP 回應（狀態列+Headers+Body）
- `Headers`：HTTP 標頭
- `RawContentLength`：Body 大小（bytes）

如果要完整印 JSON（避免截斷）：

```powershell
(Invoke-RestMethod "http://192.168.46.226:7777/v1/models") | ConvertTo-Json -Depth 20
```

---

## 9. 常見錯誤與修正

1. **`model` 寫錯**
   - 錯：`/models/gemma-4-31B-it`
   - 對：`google/gemma-4-31B-it`（以 `/v1/models` 回傳 `id` 為準）

2. **Method 用錯**
   - `/v1/chat/completions` 要 `POST`，不是 `GET`

3. **JSON 格式錯**
   - 漏逗號、引號不合法

4. **缺 `Content-Type`**
   - 必須是 `application/json`

5. **網路問題**
   - IP/port、防火牆、容器未啟動

---

## 10. 安全提醒（重要）

你曾貼出 Hugging Face token。建議立刻：

1. 到 Hugging Face 撤銷舊 token
2. 重新建立新 token
3. 不要再把 token 放在公開訊息、截圖、程式碼或 log

---

## 11. 一句話總結

先用 `GET /v1/models` 拿到正確 `model id`，再用 OpenAI 相容格式 `POST /v1/chat/completions`，就是最穩定的呼叫方式。
