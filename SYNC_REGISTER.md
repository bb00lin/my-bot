# Register 同步工具

SharePoint Excel（**S 表格**）→ Confluence `mail_checking`（**C 表格**）→ Jira PMWC 的三向同步腳本。

## 資料流

```
S 表格（主資料源）
    ↓ 覆寫
C 表格（Confluence，S 欄位 + LINK 欄）
    ↓ 建立/更新
Jira PMWC（P1/P2/P3… 大型工作底下的任務）
```

## 同步規則

| 項目 | 行為 |
|------|------|
| 資料來源 | **S 表格為唯一主資料** |
| C 表格 | 完全取代（HTML storage），只保留 S 有的 ID，**不插入空白列** |
| C 欄位結構 | **與 S 表完全相同**（同順序、同表頭），最後加一欄 **LINK** |
| LINK 欄位 | 同步後寫入可點擊的 PMWC 超連結（**不是** S 表的 JIRA 欄） |
| Priority → Epic | S 表 **Ball with** 欄的 P1/P2/P3… 掛到對應「大型工作」Epic |
| Status → Jira | S 表 **Priority** 欄的 Open/Closed 等文字對應 Jira 狀態 |
| Jira 任務標題 | `{ID}_{Workstream}`（S 表 Workstream 欄） |
| Jira 父子關係 | 任務透過 `parent` 欄位掛在 Priority Epic 下（僅 Jira 麵包屑，**不**建立 issue link） |
| S 已刪除的 ID | 對應 Jira 任務一併**刪除** |
| Jira 任務名稱 | `ID_Title`（例：`RF-05_BT Rx max-input-level sweep to +10 dBm`） |
| Closed 狀態 | 建立 Jira 並設為「完成」，LINK 欄仍回寫 PMWC 連結 |

### Status 對應（子字串優先順序）

讀取 S 表 **Priority** 欄（非 Status 欄）：

| S 表 Priority 含… | Jira 狀態 |
|-------------------|-----------|
| `Blocked` | BLOCKED |
| `Closed` | 完成 |
| `In progress` | 進行中 |
| `Open` | 待辦事項 |
| 其他 | 待辦事項 |

### Timeline 日期（Jira 時間軸）

| S 表儲存格 | Jira 欄位 | 行為 |
|------------|-----------|------|
| **空白／空字串** | Start（Opened）或 Due（Target close） | **不變更** Jira 既有值（payload 省略該欄） |
| **有內容但無法解析為日期** | 同上 | **忽略**，不變更 Jira |
| **成功解析為日期** | Opened → Start、Target close → Due | 寫入解析後的 `YYYY-MM-DD` |
| **兩者皆成功解析且 due < start** | Due | 將 due **年 +1**（僅兩邊都自本 run 表格解析時） |

補充：

- 絕不使用 `sync_date` 回填 Start，也不以 `null`/`None` 清空日期欄
- 僅有 Opened 可解析：只更新 Start，**不碰** Due
- 僅有 Target close 可解析：只更新 Due，**不碰** Start
- create／update 皆只 `update` 有成功解析的欄位

支援日期格式：`2026-07-30`、`7/30`、`7/30/2026`、`7/30/26`、`7月31日`、`2026年7月31日`、Excel 序號等。

可在 `config.yaml` 的 `jira.start_date_field` / `jira.due_date_field` 調整。

### Confluence C 表格 LINK 超連結

「連結」指的是 **C 表格最右側 LINK 欄**的可點擊超連結，**不是** Jira 工作項目頁面的「已連結工作項目」區塊。腳本**不會**呼叫 `POST /issueLink`。

同步完成後，LINK 欄會輸出 HTML storage 格式：

```html
<a href="https://qsiaiot.atlassian.net/browse/PMWC-121">PMWC-121</a>
```

等同於 Markdown 的 `[PMWC-121](https://qsiaiot.atlassian.net/browse/PMWC-121)`。Closed 項目（如 RF-13）同樣會建立 Jira 任務並回寫連結。

讀取既有 Confluence 頁面時，腳本優先從 **LINK 欄（最後一欄）** 解析 PMWC key；亦相容舊版將 JIRA 放在第 2 欄的頁面。

Jira 端僅設定 `parent` 父子階層（Priority Epic 麵包屑），不額外建立 issue link。

## C 表格欄位

C 表 = S 表全部欄位（同順序）+ **LINK**（最右欄）：

```
ID, JIRA, Workstream, Title, Description / current state, Next action,
Ball with, Priority, Status, Opened, Target close, Next milestone / due,
Source / reference, QSI Comment, LINK
```

- **JIRA** 欄：保留 S 表原始內容（如 `EVT1 RF testing`），**不是** PMWC 連結
- **LINK** 欄：同步後填入 PMWC 超連結

## 安裝

```powershell
cd "D:\MEGA\Project\C27(MWF70LC1B2)\WCN7750_QCC2072\Issue"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 設定

1. 複製設定檔：

```powershell
copy config.example.yaml config.yaml
copy .env.example .env
```

2. 編輯 `config.yaml`：
   - `atlassian.email`：你的 Atlassian 帳號
   - `sharepoint.download_url`：SharePoint 分享下載連結

3. 編輯 `.env`：

```
ATLASSIAN_API_TOKEN=你的_API_Token
```

API Token 建立方式：[Atlassian Account Security](https://id.atlassian.com/manage-profile/security/api-tokens)

## 使用方式

### 預覽（不寫入）

```powershell
python sync_register.py --dry-run
```

### 正式同步

```powershell
python sync_register.py
```

### 指定設定檔

```powershell
python sync_register.py --config .\config.yaml
```

## 變更紀錄（Diff）

每次執行同步後，腳本會比對本次 S 表格與上次成功同步的快照（`.register_cache/last_snapshot.json`），並在終端機顯示摘要、寫入日誌：

| 輸出 | 說明 |
|------|------|
| 終端機摘要 | 新增／移除 ID、狀態變更、欄位變更 |
| `logs/sync_YYYYMMDD_HHMMSS.log` | 完整變更與同步結果 |
| `logs/sync_log.txt` | 每次執行的一行摘要（append） |

比對欄位與 S 表相同（JIRA、Workstream、Title、Description、Next action、Ball with、Priority、Status、Opened、Target close、Next milestone、Source、QSI Comment）。

- **首次執行**：無前次快照，僅列出目前 S 表格所有 ID，不視為「新增」。
- **dry-run**：仍會產生變更紀錄，但**不更新**快照。
- **正式同步成功後**：更新 `last_snapshot.json` 供下次比對。
- **差異郵件主旨**：括號內使用 S 表 Excel **真實檔名**（去 `.xlsx`），例如  
  `[C27_VOX-QSI_Open_Issues_Register_20260702] 有差異 — 2026-07-14 11:00`  
  （優先 Content-Disposition；抓不到時退回 `sharepoint.register_filename` / 來源標題）。

## 檔案說明

| 檔案 | 說明 |
|------|------|
| `sync_register.py` | 主程式 |
| `config.yaml` | 實際設定（勿提交） |
| `config.example.yaml` | 設定範本 |
| `.env` | API Token（勿提交） |
| `.github/workflows/sync-register.yml` | GitHub Actions 同步 workflow |
| `.register_cache/` | 下載的 S 表格快取與上次同步快照 |
| `logs/` | 每次同步的變更紀錄（`sync_YYYYMMDD_HHMMSS.log`） |

## GitHub Actions 遠端同步（**主要自動排程**）

**建議用 GitHub Actions 做每日自動同步**（雲端執行，不必本機開機）。手動同步請到 Actions 頁面按 **Run workflow**。

### 自動排程（每日 09:00／17:00 台灣時間）

`.github/workflows/sync-register.yml` 已設定為**主要排程**：

| 台灣時間 | cron（UTC） |
|----------|-------------|
| 09:00 | `0 1 * * *` |
| 17:00 | `0 9 * * *` |

推送到 GitHub 並設好 Secrets 後即可無人值守執行。亦可手動 **Run workflow**。實際觸發可能有數分鐘延遲。

> **為何不用 Windows 排程當主方案？** Windows 工作排程器需在設定時間**開機**（或允許喚醒）才會跑。GitHub Actions 在雲端執行，電腦關機也不影響。  
> `scripts/install_windows_schedule.ps1` 僅作可選備援（**不必安裝**）。若已裝過 `PMWC_Sync_Register`，可保留或移除：  
> `Unregister-ScheduledTask -TaskName PMWC_Sync_Register -Confirm:$false`

### Jira 狀態同步

S／C 表 **Status** 預期為**英文**（**大小寫不拘**）；腳本對應到 Jira 狀態：

| 表內英文（例） | Jira 狀態 |
|----------------|-----------|
| Done / Closed / Completed | `完成` |
| In progress / Doing / Working | `進行中` |
| Open / Todo / Backlog / New | `待辦事項` |
| Blocked / Block | `BLOCKED` |
| Waiting / Wait | `WAITING` |
| Candidate | `CANDIDATE` |
| Resume | `RESUME` |
| Abort / Cancelled | `ABORT` |

優先順序：① Jira 英文狀態名完全符合（`blocked`／`BLOCKED` 相同）→ ② 英文同義詞（`Todo`／`TODO`／`todo` → `待辦事項`）→ ③ 子字串 fallback（如 `In progress - testing`）。

### 前置：建立／推送 GitHub 儲存庫

```powershell
cd "D:\MEGA\Project\C27(MWF70LC1B2)\WCN7750_QCC2072\Issue"
git init
git add .github sync_register.py requirements.txt config.example.yaml README.md .gitignore scripts
git commit -m "Add register sync and GitHub Actions schedule"
git remote add origin https://github.com/<OWNER>/<REPO>.git
git push -u origin main
```

勿提交 `.env` 或含密鑰的 `config.yaml`；Token 只放 GitHub Secrets。

### 必要 Secrets

在 GitHub → **Settings → Secrets and variables → Actions** 新增：

| Secret | 說明 |
|--------|------|
| `ATLASSIAN_API_TOKEN` | Atlassian API Token（**必要**） |
| `CONFIG_YAML` | 完整 `config.yaml` 文字內容（**建議**；必須以 **UTF-8** 寫入，否則中文會變 `?`） |

設定 `CONFIG_YAML` 時（PowerShell，避免編碼破壞中文）：

```powershell
# 以 UTF-8 檔案內容寫入 secret（勿用 Get-Content 預設編碼再貼上）
gh secret set CONFIG_YAML --repo bb00lin/my-bot < config.yaml
```

### 頁首 S 表連結標題

```yaml
confluence:
  source_link_title: "C27 Open Issues Register (S 表格)"
```

若 secret 編碼損壞導致標題出現 `??`，腳本會自動改用內建 UTF-8 預設標題。

### 啟用確認

1. 開啟：`https://github.com/<OWNER>/<REPO>/actions/workflows/sync-register.yml`
2. 先按 **Run workflow** 驗證
3. 之後依 cron 於台灣時間 09:00、17:00 自動跑

### Windows 本機排程（可選備援，需開機）

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_windows_schedule.ps1
```

## 疑難排解

- **找不到 config.yaml**：複製 `config.example.yaml`
- **缺少 ATLASSIAN_API_TOKEN**：在 `.env` 設定
- **SharePoint 下載失敗**：確認分享連結仍有效、可公開下載
- **Jira 狀態轉換失敗**：檢查目標狀態名稱是否與 PMWC 專案一致
- **Timeline 沒顯示**：確認 Opened 或 Target close 有值，且 Jira Timeline 檢視已啟用
- **LINK 欄位空白**：確認已執行正式同步（非 `--dry-run`）；Closed 項目也會建立 Jira 並設為「完成」
- **C 表格有空白列**：舊版手動表格可能含 spacer row；新版以 HTML 整頁覆寫，只輸出 S 表格筆數
- **GitHub Actions 失敗：缺少 CONFIG_YAML**：在 Secrets 貼上完整 config.yaml（UTF-8），或提交不含 Token 的 config.yaml
- **Confluence 頁首標題出現 ??**：`CONFIG_YAML` 中文編碼損壞；以 UTF-8 重新 `gh secret set CONFIG_YAML < config.yaml` 後再跑同步
