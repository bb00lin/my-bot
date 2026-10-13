# C27 Project Plan 同步（Google Sheet ⇄ Jira → Confluence）

`sync_plan.py` 以 Google Sheet `C27_Project_Plan` 為計畫主檔，雙向同步到 Jira PMWC，並產生
Confluence 頁面 `C27 Project Plan`（`C27_project_module_wifi7_QCC2072` 底下）。

## 觸發

| 方式 | 說明 |
|---|---|
| `run_sync_plan.bat` | `gh workflow run sync-plan.yml -f direction=both` |
| `run_pull_plan_from_jira.bat` | `-f direction=pull`，只把 Jira 變更拉回 Sheet |
| GitHub Actions → **Sync Plan** | 可選 `direction`（both/push/pull）、`dry_run`、`mode`（sync/init-sheet） |
| 本機 | `python sync_plan.py --dry-run --xlsx C27_Project_Plan.xlsx`（唯讀預覽） |

## Secrets

| Secret | 用途 |
|---|---|
| `GOOGLE_SHEETS_JSON` | service account（與其他 bot 共用）；Sheet 需分享給它（編輯者） |
| `PLAN_CONFIG_YAML` | 計畫設定（sheet id、Confluence 父頁、Jira label…），範例見 `plan_config.example.yaml` |
| `CONFIG_YAML` / `ATLASSIAN_API_TOKEN` | 與 Sync Register 共用（atlassian、notify） |
| `MAIL_USERNAME` / `MAIL_PASSWORD` | 結果郵件 |

## Sheet 分頁

`Tasks`（任務）、`Groups`（群組 = Jira Epic、承諾工期）、`People`（Owner ↔ Jira accountId）、
`Holidays`（台灣國定假日）、`Settings`（Project Start、工作日）、`Gantt`（公式產生的週甘特圖）、
`說明`、`_sync_state`（隱藏，上次同步快照）。

Predecessors 採 Microsoft Project 語法：`PLN-AT-01FS+1d; PLN-FX-02SS; PLN-WZ-03FF-2d`。

## 欄位歸屬與衝突

- 每個欄位做三方比對（Sheet / Jira / 上次同步快照）：只有一邊改 → 同步到另一邊；兩邊都改 → 不覆寫，
  寫入 `Sync Conflict` 欄，填 `use sheet` / `use jira` 決定。
- 無快照時：計畫欄位（Task Name、Description、日期/工期、Predecessors、Priority、Group）以 Sheet 為主；
  Status、Owner 以 Jira 為主。
- Jira timeline 拖曳日期 → Sheet `Start Constraint` + `Duration`；Jira 新增 Blocks → `Predecessors`；
  計畫 Epic 底下新建的單 → Sheet 新列。
- `Sync to Jira` ≠ Y 的列只顯示在 Confluence（提案/討論中），不建 Jira 單。
- Sheet 刪列 → Jira 轉「完成」+ label `plan-removed`（`on_removed: delete` 可改為刪除）。

## 與 Sync Register 的隔離

計畫單 ID 為 `PLN-<群組>-<數字>`（兩個連字號，不符合 Register 的 `^[A-Z][A-Z0-9]*-\d+$`），且全部帶
label `c27-plan`。`sync_register.py` 的索引會排除 `c27-plan` label 與 `PLN-` 前綴的單（`jira.exclude_labels`
可設定），所以 Register 同步不會刪除或更新計畫單；計畫同步也只處理 `c27-plan` / 計畫 Epic 底下的單，
並會略過任何 summary 符合 Register ID 格式的單。

## 驗證

```bash
python test_sync_plan_verify.py   # 不需憑證、不連網
```
