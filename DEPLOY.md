# pdf-compare 伺服器部署與雙開並行指南

本指南適用於 Linux 伺服器上的 Docker Compose 部署，包含「日常部署步驟」與「如何安全建立第二個測試版（免怕搞壞）」的操作流程。

---

## 方案 A：日常更新與部署（原本的環境）

如果你想更新目前已經在運作的 `http://伺服器IP:8080`：

### 情況 1：直接拉取分支更新（平時最常用）
如果你的伺服器已經切換好對應的分支（預設是 `feat/text-diff-highlight`）：
1. **進入專案目錄：**
   ```bash
   cd /home/user/pro/pdf-compare
   ```
2. **拉取最新程式碼：**
   ```bash
   git pull
   ```
3. **重新建置鏡像並重啟容器：**
   ```bash
   docker compose up -d --build
   ```

### 情況 2：如果需要精準指定更新的分支
如果想在伺服器上切換特定分支或進行強制拉取（防止伺服器端代碼被意外改動導致 git 拉不下）：
1. **進入專案目錄：**
   ```bash
   cd /home/user/pro/pdf-compare
   ```
2. **強制取回並覆蓋：**
   ```bash
   # 下載最新進度但不套用
   git fetch --all
   
   # 強制將本地代碼覆蓋為 GitHub 上特定的最新進度
   git reset --hard origin/feat/text-diff-highlight
   ```
3. **重新建置與啟動：**
   ```bash
   docker compose up -d --build
   ```

---

## 方案 B：雙開並行部署（另開資料夾、另開 Port、另開容器）

如果你想測試新功能，又怕把現有運作良好的 `pdf-compare` 弄壞，你可以**在同台主機上同時開啟第二個測試環境**。容器化部署適合用來隔離這兩個環境。

### 步驟 1：建立並進入測試用的新資料夾
我們不用之前的 `pdf-compare` 資料夾，改在旁邊 clone 另一個：
```bash
cd /home/user/pro
git clone https://github.com/yushengciouu/pdf-compare.git pdf-compare-test
cd pdf-compare-test
```

### 步驟 2：啟動前準備
直接執行啟動指令即可，應用程式會自動建立所需的資料目錄。

### 步驟 3：修改測試版的 `docker-compose.yml` 通訊 Port
測試版不能再用 `8080` 通道，否則會發生 Port 衝突。我們把它改成 `8081`：

1. 用文字編輯器（如 `nano` 或 `vi`）修改測試版的 `docker-compose.yml`：
   ```bash
   nano docker-compose.yml
   ```
2. 將以下部分：
   ```yaml
       ports:
         - "8080:8000"
   ```
   **改為（使用 8081 或是其他你喜歡的 Port）：**
   ```yaml
       ports:
         - "8081:8000"
   ```
3. 存檔並離開（如果用 nano，按下 `Ctrl + O` 存檔，`Ctrl + X` 離開）。

### 步驟 4：設定 LLM 連線設定檔
為這個測試版建立獨立的本地變數：
```bash
echo "PDF_COMPARE_LLM_BASE_URL=http://host.docker.internal:8001" > backend/.env
```

### 步驟 5：啟動測試版服務
現在可以啟動測試版。因為資料夾名稱不同 (`pdf-compare-test`)，Compose 會使用不同的專案名稱，不會覆蓋原本的正式版容器。
```bash
docker compose up -d --build
```

### 步驟 6：測試體驗
* **正式版（原本的）：** 依舊在 `http://伺服器IP:8080/` 運行，完全不受影響。
* **測試版（剛開的）：** 在 `http://伺服器IP:8081/` 運行，你可以在這盡量修改甚至弄壞它！

---

## 常見問題與管理指令

### 如何查看有哪些容器正在跑？
```bash
docker ps
```
正常情況下，你應該會看到 `pdf-compare_api_1` (Port 8080) 與 `pdf-compare-test_api_1` (Port 8081) 同時顯示為 Up。

### 如何單獨關閉測試版容器？
```bash
cd /home/user/pro/pdf-compare-test
docker compose down
```
這樣只會關閉測試版，原先正式版的 Port 8080 仍然安心順暢運作！
