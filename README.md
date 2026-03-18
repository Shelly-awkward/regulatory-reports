# 📋 監理報告週報系統

自動抓取 IOSCO、IESBA、FSB、IFAC 最新出版品，每週日晚上 8 點（台灣時間）自動執行，產出中英對照三欄報告清單。

---

## 設定步驟（只需做一次）

### 第一步：建立 GitHub Repository

1. 登入 GitHub，點右上角 **+** → **New repository**
2. Repository name 填：`regulatory-reports`（或任何名稱）
3. 選 **Public**（GitHub Pages 免費版需要 Public）
4. 按 **Create repository**

---

### 第二步：上傳所有檔案

把以下資料夾結構完整上傳到 repository：

```
regulatory-reports/
├── .github/
│   └── workflows/
│       └── weekly_scraper.yml
├── scraper/
│   ├── scraper.py
│   └── requirements.txt
├── data/
│   ├── reports.json
│   └── custom_sources.json
├── index.html
└── README.md
```

上傳方式（選一種）：
- **網頁上傳**：在 GitHub repository 頁面點 **Add file** → **Upload files**，把整個資料夾拖進去
- **Git 指令**：
  ```bash
  git clone https://github.com/你的帳號/regulatory-reports.git
  # 把檔案複製進去
  git add .
  git commit -m "初始建立"
  git push
  ```

---

### 第三步：設定 API Key Secret

1. 在 repository 頁面點上方 **Settings**
2. 左側選單點 **Secrets and variables** → **Actions**
3. 點 **New repository secret**
4. Name 填：`ANTHROPIC_API_KEY`
5. Secret 填：你的 Anthropic API Key（`sk-ant-...`）
6. 按 **Add secret**

---

### 第四步：開啟 GitHub Pages

1. 在 repository 頁面點上方 **Settings**
2. 左側選單點 **Pages**
3. Source 選 **Deploy from a branch**
4. Branch 選 **main**，資料夾選 **/ (root)**
5. 按 **Save**
6. 等約 1 分鐘，頁面會顯示你的網址：`https://你的帳號.github.io/regulatory-reports`

---

### 第五步：手動執行第一次

1. 點上方選單 **Actions**
2. 左側點 **每週監理報告自動抓取**
3. 右側點 **Run workflow** → **Run workflow**
4. 等約 3～5 分鐘
5. 重新整理你的 GitHub Pages 網址，即可看到結果

---

## 自動排程

設定完成後，每週日台灣時間 20:00 會自動執行，無需任何手動操作。

---

## 新增自訂搜尋網站

在網頁左側「自訂搜尋網站」區塊填入網址並新增即可。自訂網站清單儲存在你的瀏覽器，下次手動觸發時會自動納入搜尋。

> **注意**：若要讓自訂網站納入自動排程，需將資料新增到 `data/custom_sources.json` 並 push 到 GitHub。

---

## 費用估計

- GitHub：完全免費
- Anthropic API：每週約 $0.10～0.30 美元，每月不超過 $1.5

---

## 常見問題

**Q：Actions 執行失敗怎麼辦？**
點 Actions → 點那次執行 → 看 log 裡的錯誤訊息，最常見的原因是 API Key 沒設好。

**Q：某個網站抓不到資料？**
部分網站有反爬蟲保護，可在 `scraper/scraper.py` 的 `HEADERS` 調整 User-Agent，或改用 Claude web_search 方式搜尋。

**Q：想增加其他固定來源？**
在 `scraper/scraper.py` 的 `DEFAULT_SOURCES` 清單新增一筆，格式參考現有項目。
