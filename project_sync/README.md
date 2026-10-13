# ProjectSync（本機桌面版）

Register 同步（S 表 → Confluence C 表 → Jira）與排程同步（計畫表 ⇄ Jira Epic/任務 → Confluence 計畫頁），多專案、全部在本機執行。

## 啟動

- 介面：雙擊 `ProjectSync.bat`（首次會自動 `pip install -r requirements.txt`，之後以 pythonw 開啟視窗）
- CLI：
  ```
  python -m projectsync --project c27 --job register [--dry-run]
  python -m projectsync --project c27 --job plan --direction both|push|pull [--dry-run] [--allow-mass-removal] [--no-mail]
  python -m projectsync --project c27 --job preview        # 只排程，不連 Jira
  python -m projectsync --project c27 --job test-mail
  python -m projectsync --project c27 --job init-plan [--c27-template] [--overwrite]
  python -m unittest discover -s tests                    # 單元測試
  ```

## 檔案

| 路徑 | 內容 |
|---|---|
| `projects/<slug>.yaml` | 專案設定（不含任何密碼） |
| `projects/<slug>/` | 本機計畫表 xlsx |
| `.env` | 憑證（ATLASSIAN_API_TOKEN、MAIL_USERNAME、MAIL_PASSWORD、SMTP_HOST/PORT、GOOGLE_SERVICE_ACCOUNT_FILE、GRAPH_*）；已列入 `.gitignore` |
| `logs/<slug>/register/` | Register 差異報告、快照 |
| `logs/<slug>/plan/` | 排程同步紀錄、`plan_state.json` |

## 開案精靈（新專案）

介面上方「開案精靈…」，填入名稱、Jira Key、Confluence 空間與父頁面、開始日、郵件收件者後，依序執行（每步可重跑，已存在的不會重建，產生的 id 自動寫回設定檔）：

1. 計畫表：產生 xlsx（Tasks / Groups / People / Holidays / Settings / 說明 / Gantt）；有 Google service account 時另上傳成 Google Sheet 並分享
2. Confluence 計畫頁：在父頁面下建立
3. Jira Epic：依 Groups 分頁建立，標籤 `<slug>-plan`
4. Register：檢查 S 表與 C 表頁面（可選新建 mail_checking 式頁面）
5. 郵件：檢查收件者 / SMTP，可寄測試信

## Google Sheet 寫入（選用）

1. Google Cloud Console 建專案 → 啟用 Sheets API、Drive API
2. 建立服務帳戶 → 新增 JSON 金鑰，存到本機（例 `secrets\sa.json`，已 gitignore）
3. 介面「憑證」頁選擇 JSON → 測試 Google（顯示 SA email）
4. 計畫 Sheet 分享給 SA 為編輯者；「Google Sheets / 排程同步」頁來源改 `google` 並貼網址

沒有 SA 時用 `xlsx`（本機檔案，可讀寫，可放 MEGA 共用）。

## 郵件

公司 Office365 已停用 SMTP Basic Auth，使用 Gmail（smtp.gmail.com:587）＋應用程式密碼；Graph 設定保留作為選項。dry-run 不寄信。

## 欄位歸屬與衝突規則（可分享給同事／客戶）

- **計畫欄位（以計畫表為準）**：Task Name、Description/工作內容、Duration、Predecessors、Group、Type、Priority、Sync to Jira。
- **執行欄位（以 Jira 為準）**：Status、Owner（受託人）、Jira Key；在 Jira 拖動的開始/截止日會寫回計畫表成 Start Constraint＋Duration，再重新排程。
- **只在計畫表**：Discussion/Comments、Proposed By（顯示在 Confluence，不送 Jira）。
- **Sync to Jira = N**：草稿／提案，只出現在 Confluence「提案/討論中」，不建 Jira。
- **新增**：在 Jira 建在計畫 Epic 下（或加 `<slug>-plan` 標籤）的任務會自動新增到計畫表；在 Jira 新增的 Blocks 連結會寫回 Predecessors。
- **衝突**：同一欄位在上次同步後兩邊都被改 → 兩邊都不覆寫，寫在 Sync Conflict 欄並列入報告；在該格輸入 `use sheet` 或 `use jira` 後下次同步套用。
- **刪除**：計畫表刪列 → Jira 單預設關閉（`on_removed: close`），一次超過 max(5, 30%) 需勾「允許大量移除」。
- **Register 單**（`XXX-123` 形式 ID）永不被排程同步動到；Register 同步也會忽略 `<slug>-plan` 標籤與 `PLN-` 開頭的單。
- 寫入一律寂靜：Jira `notifyUsers=false`、Confluence minorEdit。
