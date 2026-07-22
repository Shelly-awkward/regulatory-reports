# 用 GitHub Copilot 重建監理報告週報系統：操作指南與評估標準

> 目的：組織指定使用 GitHub Copilot。本文件說明如何指揮 Copilot 從零重建與本 repo 同等級的系統，並提供評估 Copilot 產出品質的驗收標準。

---

## 一、先選對 Copilot 模式（這一步決定成敗）

Copilot 有好幾種形態，能力差距極大。要做這種「多檔案、多步驟、自主規劃」的任務，只有兩種模式可用：

| 模式 | 怎麼用 | 適合度 |
|---|---|---|
| **Copilot Chat「Agent mode」**（VS Code） | VS Code 左側 Copilot Chat 面板 → 模式切到 **Agent** → 貼 prompt | ⭐ 首選，最接近 Claude Code 的體驗，會自己建檔案、跑指令、迭代 |
| **Copilot coding agent**（GitHub 網站） | 在 GitHub 開 issue → 指派給 **Copilot** → 它在雲端自己開 PR | 適合驗收明確的單一階段任務 |
| Inline 補全（打字時的灰字建議） | — | ❌ 完全不適合，別用這個做架構 |
| Copilot Chat「Ask mode」 | — | ❌ 只會回答問題不會動手 |

模型選擇：Agent mode 右下角可切換模型，選最強的可用模型（如 Claude Sonnet 或 GPT 系列最高階），不要用預設的快速模型跑架構任務。

---

## 二、關鍵心法：Copilot 需要「規格＋拆階段＋驗收標準」

和 Claude Code 一口氣丟大目標的用法不同，Copilot 在以下情況表現最好：

1. **一次一個階段**：每個 prompt 只做一件事，做完驗收再給下一個。
2. **把規格寫死**：資料 schema、檔案結構、函式清單都明講，不留給它發揮。
3. **給驗收標準**：每個 prompt 結尾附「完成的定義」，並要求它自己驗證後回報。
4. **先放 `copilot-instructions.md`**：Copilot 每次對話都會自動讀 `.github/copilot-instructions.md`，把整體架構和不變的規則放在那裡，省得每次重講。

---

## 三、第一步：在新 repo 放入 `.github/copilot-instructions.md`

建好空 repo 後，先建立這個檔案（可直接複製）：

```markdown
# 專案：監理報告週報系統

## 目標
自動抓取 IOSCO、FSB、IFIAR、IESBA、IAASB、IFAC、PCAOB 最新出版品，
每週日 20:00（台灣時間，UTC 12:00）由 GitHub Actions 執行，
產出中英對照報告清單，以 GitHub Pages 靜態網頁呈現。

## 架構（不可更動）
- `scraper/scraper.py`：單一 Python 爬蟲腳本，每個機構一個 adapter 函式
- `scraper/requirements.txt`：requests、beautifulsoup4、lxml、playwright、（LLM SDK）
- `data/reports.json`：唯一資料儲存，前端直接 fetch 這個檔案，無後端無資料庫
- `index.html`：單檔前端，純 vanilla JS，無框架、無 build step
- `.github/workflows/weekly_scraper.yml`：排程 + workflow_dispatch，跑完 commit 回 repo

## 資料 schema（reports.json 內每筆報告）
{ "id": "<source>-<url的md5前12碼>", "source": "IOSCO", "title": "英文原標題",
  "title_zh": "中文翻譯", "summary": "英文真實摘要", "summary_zh": "中文翻譯",
  "date": "YYYY-MM-DD 或 YYYY-MM 或 YYYY", "url": "報告詳細頁完整網址" }
頂層另有 last_updated（ISO 時間）與 sources 中繼資料。

## 鐵律
1. 摘要必須是從報告詳細頁抓到的真實內容（og:description 或內文首段）。
   抓不到就留空字串，絕對禁止用 LLM 憑標題編造摘要。
2. LLM 只做「翻譯」，不做「創作」。已翻譯過的項目要快取，不重翻。
3. 所有連結進驗證閘門：過濾導覽連結（home/about/contact…）、按鈕文字
   （View Report/Read More/Download…）、分類目錄頁。既有資料每次執行也重新驗證。
4. 日期一律正規化為 YYYY-MM-DD（不確定時允許 YYYY-MM 或 YYYY），排序新→舊。
5. 網路請求都要有 timeout 與例外處理，單一來源失敗不能讓整個流程掛掉。
```

---

## 四、分階段 Prompt（依序貼給 Agent mode，一次一個）

### Phase 0：骨架

> 建立專案骨架：`scraper/scraper.py`（先只放來源設定與空的 main）、`scraper/requirements.txt`、`data/reports.json`（空結構）、`index.html`（先放標題與載入中畫面）、`.github/workflows/weekly_scraper.yml`。
> workflow 需求：cron `0 12 * * 0` 加 `workflow_dispatch`，Python 3.11，裝 requirements 後執行 scraper，若 `data/reports.json` 有變更就以 github-actions[bot] 身分 commit push（需要 `permissions: contents: write`）。
> 完成標準：workflow 檔 YAML 語法正確、reports.json 是合法 JSON、目錄結構與 copilot-instructions.md 一致。

### Phase 1：共用基礎設施

> 在 `scraper/scraper.py` 實作共用層，先不寫任何機構的 adapter：
> 1. `fetch(url)`：requests 抓頁面，帶真實瀏覽器 User-Agent 與 Accept-Language 標頭，timeout 25 秒。
> 2. `normalize_date(text)`：把各種格式（"12 May 2026"、"May 12, 2026"、"2026/05/12"、只有年月等）正規化為 YYYY-MM-DD／YYYY-MM／YYYY，並提供穩定排序用的 `sort_key`。
> 3. `make_id(source, url)`：`<source>-<url md5 前 12 碼>`。
> 4. 驗證閘門 `is_valid_report(title, url)`：過濾（a）標題等於或結尾是按鈕文字如 View Report、Read More、Download；（b）導覽關鍵字如 home、about、contact、login、cookie；（c）過短標題；（d）明顯是分類目錄頁而非單篇報告的 URL。
> 5. `load_existing()` / 存檔函式：讀寫 `data/reports.json`，以 id 去重。
> 完成標準：為 normalize_date 與 is_valid_report 各寫至少 8 個測試案例（可用 `if __name__` 區塊或 pytest），全部通過。

### Phase 2：RSS 來源（FSB、IFIAR，最簡單，先建立信心）

> 實作 `scrape_wordpress_rss(src)`：讀 WordPress RSS feed（FSB `https://www.fsb.org/feed/`、IFIAR `https://www.ifiar.org/feed/`），解析 title/link/pubDate，過驗證閘門，回傳統一 schema（title_zh/summary 等留空，翻譯是後面的階段）。RSS 失敗時 fallback 到 HTML 列表頁解析。每來源上限 40 筆。
> 完成標準：實際執行後兩個來源各抓到至少 5 筆，逐筆檢查 URL 都是單篇報告頁而非目錄頁，日期都已正規化。把抓到的前 5 筆印出來給我看。

### Phase 3：HTML 解析來源（IOSCO）

> 實作 `scrape_iosco(src)`：解析 `https://www.iosco.org/v2/publications/?subsection=public_reports` 的出版品表格，逐列取「真實標題」與文件編號、日期、PDF/詳細頁連結。注意：不要抓到導覽區或 footer 的連結，鎖定主內容區的表格。
> 完成標準：抓到至少 10 筆，標題是完整報告名稱（不是 "View Report"），列出前 5 筆讓我人工核對。

### Phase 4：JS 動態渲染來源（IESBA／IAASB／IFAC 共用 + PCAOB）

> 這是最難的階段，分兩步：
> （a）實作 `scrape_ifac_platform(src)`：IESBA、IAASB、IFAC 三站共用同一個 Drupal 平台，出版品列表由 JS 動態渲染。用 Playwright（chromium，headless）渲染後再用 BeautifulSoup 解析。三站共用一個函式，用 src 設定區分。
> （b）實作 `scrape_pcaob(src)`：抓 `https://pcaobus.org/resources/staff-publications` 主列表；另外用 Playwright 渲染 `https://pcaobus.org/news-events/news-releases`（JS 動態頁）抓新聞發布。列表上沒有完整日期的項目，要進詳細頁補抓真實發布日期。
> workflow 記得加 `python -m playwright install --with-deps chromium` 步驟。
> 完成標準：四個來源各抓到至少 5 筆真實出版品，PCAOB 的日期精確到日。

### Phase 5：真實摘要 + 翻譯 + 快取

> 1. `fetch_summary(url)`：進每篇報告詳細頁，優先取 `og:description`，否則取主內文第一段。抓不到就回空字串——禁止編造。
> 2. `translate_batch(reports)`：呼叫 LLM API 把英文 title/summary 翻成繁體中文（台灣金融監理術語體例，如 IOSCO=國際證券管理機構組織）。只翻譯、不添加內容；summary 為空就不產生 summary_zh。
> 3. 快取：既有資料裡已有 title_zh 的項目跳過不重翻。API key 從環境變數讀取，workflow 用 repository secret 傳入。
> 完成標準：跑一次完整流程，隨機抽 5 筆核對中文翻譯忠實於英文原文、沒有無中生有的摘要；跑第二次確認已翻譯項目沒有再次呼叫 API。

### Phase 6：前端

> 用單檔 `index.html`（vanilla JS，無框架）呈現 `data/reports.json`：
> 1. 卡片／表格兩種檢視切換；2. 關鍵字搜尋（標題＋摘要，中英都要能搜）；3. 機構篩選按鈕（含各機構筆數、專屬顏色）；4. 日期區間篩選；5. 按日期新→舊排序並分組（本週／本月／更早）；6. 顯示最後更新時間；7. 中英對照顯示（中文標題為主、英文原標題為輔）；8. 手機響應式。
> 完成標準：本機開靜態伺服器實測所有篩選組合，JSON 載入失敗時顯示友善錯誤而非空白頁。

### Phase 7：整合驗收

> 完整跑一次 end-to-end：手動觸發 GitHub Actions workflow，確認七個機構都有資料進 `data/reports.json`、自動 commit 成功、GitHub Pages 頁面正確顯示。把每個來源的抓取筆數與失敗原因整理成表格回報。

---

## 五、翻譯 API 的注意事項

Copilot 是「開發工具」，但系統執行期（runtime）翻譯仍需呼叫一個 LLM API，這與用什麼工具開發無關：

- 若組織**只限制開發工具**：runtime 照舊用 Anthropic API（現行版本，成本每月 < US$1.5）。
- 若組織**連 runtime API 都限制**：可在 Phase 5 的 prompt 中把 LLM 換成
  **GitHub Models**（`https://models.github.ai/inference`，用 GITHUB_TOKEN 認證，與 GitHub 生態整合最順）或 **Azure OpenAI**。翻譯 prompt 邏輯不變，只換 SDK 與 endpoint。

---

## 六、評估記分表（Copilot vs 現行系統）

拿這張表逐項核對 Copilot 的產出，就能客觀回答「它有沒有能力架出同樣的報告」：

| # | 驗收項目 | 現行系統 | Copilot 產出 |
|---|---|---|---|
| 1 | 七個來源都抓得到資料 | ✅ | |
| 2 | RSS 優先、HTML fallback 的分層策略 | ✅ | |
| 3 | JS 動態頁（IFAC 平台、PCAOB 新聞）用 headless 瀏覽器成功渲染 | ✅ | |
| 4 | 驗證閘門：無導覽連結、無 "View Report" 假標題混入 | ✅ | |
| 5 | 摘要是詳細頁真實內容，抓不到就留空（不編造） | ✅ | |
| 6 | 翻譯快取：重跑不重翻 | ✅ | |
| 7 | 日期正規化 + 穩定排序 | ✅ | |
| 8 | 單一來源失敗不影響其他來源 | ✅ | |
| 9 | Actions 排程 + 自動 commit 正常運作 | ✅ | |
| 10 | 前端：雙檢視、搜尋、機構／日期篩選、分組、響應式 | ✅ | |

**預期的難點**（Copilot 最容易失敗、需要人工追加提示的地方）：

1. **#3 Playwright 渲染**：Copilot 常會先寫純 requests 版本然後抓到空頁面。發現時直接說「這個頁面是 JS 動態渲染，改用 Playwright headless chromium 渲染後再解析」。
2. **#4 驗證閘門**：它傾向把列表頁所有 `<a>` 都當報告。用 Phase 3 的「列出前 5 筆讓我人工核對」逼它面對髒資料。
3. **#5 不編造摘要**：LLM 工具天生愛「補完」。鐵律已寫進 copilot-instructions.md，驗收時務必抽查。
4. **反爬蟲**：部分網站擋預設 UA，提醒它帶真實瀏覽器 User-Agent。

---

## 七、如果用 GitHub 網站的 Copilot coding agent

把上面每個 Phase 開成一張 issue（標題 = Phase 名稱，內文 = 該 Phase 的 prompt 全文），指派給 Copilot，它會自己開 PR。注意：

- coding agent 無法在它的沙箱裡實測外部網站的抓取結果（網路受限），所以 Phase 2–4 的「實抓驗收」要靠你 merge 後手動觸發 workflow 驗證，或改在 VS Code Agent mode 做這幾個階段。
- 一樣要先把 `.github/copilot-instructions.md` merge 進 main，coding agent 才讀得到。
