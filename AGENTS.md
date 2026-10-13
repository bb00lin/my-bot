# AGENTS.md

給 AI agent 的 repo 導覽。專有名詞、識別符、API 名稱、檔案路徑保留英文。

## 這個 repo 是什麼

個人的**自動化腳本集**：根目錄 17 支獨立的 Python 腳本，每一支都是由
`.github/workflows/` 裡的 GitHub Actions 觸發的一次性批次工作。**沒有長駐服務、
沒有 web app**（`stm32_dashboard.py` 名字有 dashboard 但其實是 CLI 腳本），
也沒有共用套件 —— 每支腳本都自己 import 需要的東西、自己讀環境變數。

## Workflow 與腳本的對應

| Workflow 名稱 | 檔案 | 腳本 | 觸發方式 |
|---|---|---|---|
| `DailyStockBot` | `main.yml` | `DailyStockBot.py` | 手動 + `repository_dispatch` (`trigger-push`) |
| `ManualStockDiagnostic` | `manual_analyze.yml` | `ManualStock.py` | 僅手動，需輸入股票代碼 |
| `Guardian Price Check Bot` | `guardian.yml` | `guardian_bot.py` | 手動 + 每日 UTC 02:00 |
| `Mix Match Check` | `mix_check.yml` | `mix_guardian_bot.py` | 手動 + 每日 UTC 02:00 |
| `CosIng Ingredient Search` | `run_check.yml` | `cosing_automation.py` | 手動 + 每日 UTC 02:00 |
| `BOM Automation Logic` | `bom_action.yml` | `bom_manager.py` | 僅手動，下拉選單選模式 |
| `Run STM32 GPIO Planner` | `RunSTM_GPIO.yml` | `stm32_dashboard.py` | 手動 + push（限 `stm32_dashboard.py` / `STM32MP133CAFx.xml` 變更） |
| `Sync Register` | `sync-register.yml` | `sync_register.py` | 手動 + `repository_dispatch` (`sync-register`) |
| `worklog_to_confluence (Auto Sync)` | `worklog_to_confluence.yml` | `daily_worklog_to_confluence.py` | 手動 + 排程 |
| `Confluence Weekly2 Report` | `Confluence Weekly2 Report.yml` | `confluence_api2.py` | 手動 + 排程 |
| `Monthly Confluence Task` | `monthly_confluence.yml` | `monthly_confluence_copy.py` | 手動 + 排程 |
| `Jira Gantt to Confluence Sync` | `gantt_sync.yml` | `github_gantt_sync.py` | 僅手動 |
| `Test Project Cleaner (Manual)` | `test.yml` | `confluence_cleaner.py` | 僅手動 |

`Guardian Price Check Bot`、`Mix Match Check`、`CosIng Ingredient Search` 的 cron
都是 `0 2 * * *`（台灣 10:00），三支會同時起跑。前兩支還留著幾條已註解的高頻 cron，
改動排程時注意不要誤啟用。

`DailyStockBot.py` 是唯一一支分階段的腳本（原本是 `DailyStockBot.py` +
`DailyStockPush.py` 兩支，已合併）：

```bash
python DailyStockBot.py --stage full   # 掃描 + 推播（預設）
python DailyStockBot.py --stage scan   # 只跑全市場掃描，寫入 WATCH_LIST
python DailyStockBot.py --stage push   # 只跑 AI 戰略推播，讀 WATCH_LIST
```

兩階段靠 Google Sheets 的 `WATCH_LIST` 串接，**掃描必須先於推播**。workflow 透過
`scripts/resolve_daily_stock_plan.py` 把觸發事件與 inputs 正規化成 `STAGE` /
`ENABLE_AI` / `AI_PROVIDER` 三個環境變數。

## 環境與執行

- repo 裡**沒有** `.cursor/environment.json`，也沒有鎖定的虛擬環境位置。相依套件在
  `requirements.txt`，但注意 `pandas-ta-classic` 是從 GitHub zip URL 原始碼安裝，
  很慢而且需要網路。實務上不必裝滿整份 `requirements.txt`。
- 各 workflow 的 Python 版本不一致（3.9 / 3.10 / 3.11），開發時用 3.10+ 都可以。
- 最輕量的「build 檢查」是 byte-compile：`python -m py_compile *.py`。

## 測試慣例：獨立的離線驗證腳本

沒有 pytest、沒有 lint 設定。這個 repo 的慣例是**獨立可執行的 Python 腳本、
印 PASS/FAIL、以 exit code 回報**。新增驗證請沿用這個風格，不要引入 pytest。

| 腳本 | 驗證對象 | 需要的套件 |
|---|---|---|
| `test_stock_pipeline_metrics_verify.py` | `DailyStockBot.py` 的指標、階段路由、Sheets 讀寫 | `pandas` |
| `test_daily_stock_workflow_verify.py` | `main.yml` 結構與 `resolve_daily_stock_plan.py` | `PyYAML` |
| `test_unit_verify.py` | `sync_register.py` | `PyYAML` |
| `scripts/test_block_media_local.py` | `daily_worklog_to_confluence.py` 的 Confluence 排版 | `beautifulsoup4` |

手法是「假環境變數 + 把需要網路的第三方模組換成假模組後 import，再 monkeypatch」。
`test_stock_pipeline_metrics_verify.py` 是最完整的範例：它把 `yfinance` / `FinMind` /
`gspread` / `google.genai` / `oauth2client` 都換成假模組，所以不需要任何憑證也不連外網。

**不要一開始就推上去用 Actions 試錯** —— 很慢（`--stage full` 要跑一小時）而且會消耗 API token。

## 憑證

全部放在 GitHub Secrets，以環境變數注入。**secrets 是單向寫入、本機讀不回來**，
所以不要規劃「在本機直連外部服務」的驗證方式。

| Secret | 用途 |
|---|---|
| `GOOGLE_SHEETS_JSON` | Google Sheets 讀寫，幾乎每支 bot 都用 |
| `LINE_ACCESS_TOKEN` / `LINE_USER_ID` | LINE 推播（缺少時會跳過，不會中斷） |
| `GEMINI_API_KEY` / `CURSOR_API_KEY` | `DailyStockBot.py` 的 AI 診斷，由 `AI_PROVIDER` 二選一 |
| `FINMIND_TOKEN` | FinMind 台股資料 API，提高頻率上限 |
| `MAIL_USERNAME` / `MAIL_PASSWORD` | Gmail SMTP，需用應用程式密碼 |
| `CONF_URL` / `CONF_USER` / `CONF_PASS` | Confluence / Jira 類腳本，缺少時直接結束 |

**這個 repo 是 public**，所以：

- 絕對不要在 log 或 debug step 裡印出 secret 值
- `GOOGLE_SHEETS_JSON` 維持「整包 JSON 當環境變數傳給腳本、腳本自己解析」的作法，
  不要退回「先寫成 .json 檔」的舊寫法
- `jira_config.txt` 與 `credentials.json` 是本機憑證檔，已列入 `.gitignore`，不要解除

## 踩坑點

### GitHub Actions

1. `schedule` 排程只會執行**預設分支（`main`）**上的 workflow 檔，功能分支改了也不算。
2. 手動觸發（`workflow_dispatch`）可以指定任意分支，對話框顯示的輸入選項讀的是
   「那個分支」的 workflow 檔；切了分支看不到新選項就重新整理頁面。
3. 排程觸發時 `github.event.inputs.*` 是**空字串**，腳本要把空值當成「使用預設值」
   處理，不能當成 `false`。`scripts/resolve_daily_stock_plan.py` 就是為此存在。

### gspread（踩過三次，都很難查）

1. `get_all_records()` 預設會把純數字字串轉成 int，`'009824'` 會變成 `9824`，
   導致 yfinance 查不到 `009824.TW`。要保留原始文字請傳 `numericise_ignore=['all']`。
2. gspread 6 把 `update()` 的第一個位置參數改成 `values`（舊版是 `range_name`），
   **一律用關鍵字參數** `update(values=..., range_name=...)`。
3. 寫入超出工作表現有欄數的儲存格，Google Sheets 會回 400
   `Range exceeds grid limits`。舊工作表可能只有 6 欄，寫第 7 欄前要先 `resize()`。

### 腳本本身

- `ManualStock.py` 在 module 層執行 `STOCK_NAME_MAP = get_stock_name_map()`
  （FinMind 網路呼叫），所以 import 它就會連網。
- `bom_manager.py:305` 與 `stm32_dashboard.py:578` 在非 CI 環境會退回互動式 `input()`，
  非互動 shell 裡會卡住；用 `EXECUTION_MODE` 或把輸入 pipe 進去。
- Selenium 爬蟲（`guardian_bot.py`、`mix_guardian_bot.py`、`cosing_automation.py`）
  需要 Chrome / Chromedriver，workflow 會在 runner 上安裝，本機環境預設沒有。
- **不要用裸 `except: pass`**。這個 repo 曾因此讓成本統計寫入失敗靜默兩個多月才被發現。
  失敗路徑請印出代號／名稱與例外訊息，`gh run view --log` 才查得到。

### 檔名陷阱

`.github/workflows/` 裡有兩個檔案因為副檔名不對而**永遠不會被 GitHub 執行**，
它們是舊版殘留，不要拿來當現行設定參考：

- `bom_action.ym`（少一個 `l`）
- `Confluence Weekly Report2`（完全沒有副檔名）

## 觸發與讀結果

```bash
gh workflow run "DailyStockBot" --ref main -f stage=push -f ai_provider=none
gh workflow run "ManualStockDiagnostic" --ref main -f stock_list="2330 2317"
gh run list --workflow="main.yml" --limit 3
gh run view <RUN_ID> --log
```

部分 bot 會寄通知信或推 LINE，但最可靠的還是 `gh run view --log`。
