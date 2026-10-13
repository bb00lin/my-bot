#!/usr/bin/env python3
"""C27 (WiFi7 QCC2072 / WCN7750) 專案計畫的初始內容（建立 Google Sheet 時寫入）。

只在 `sync_plan.py --init-sheet` / `--export-xlsx` 時使用；之後 Google Sheet 才是唯一資料來源，
修改計畫請直接改 Sheet，不要改這個檔案。

AT / PT / FX / WZ 四組工期來自 Bob 2026-10-12 給 Addison 的估算信（5W / 3W / 8W / 4W，
1W = 5 工作天、彼此不可重疊），總數不可更動。其餘群組（採購、DOE、BT Channel Sounding、
Carrier Board）為草擬估計值，Estimate 欄標 Y，待使用者確認。
"""

from __future__ import annotations

PROJECT_START_PLACEHOLDER = "2026-10-19"

TASK_HEADERS = [
    "ID",
    "WBS",
    "Group",
    "Task Name",
    "Type",
    "Priority",
    "Owner",
    "Duration (d)",
    "Predecessors",
    "Start Constraint",
    "Start",
    "Finish",
    "Status",
    "% Complete",
    "Sync to Jira",
    "Jira Key",
    "Description / 工作內容",
    "Discussion / Comments",
    "Proposed By",
    "Estimate",
    "Notes",
    "Sync Conflict",
    "Last Sync",
]

# code, epic summary, locked total (working days; blank = 不鎖定), description
GROUPS = [
    ("G", "PLAN-前置條件 Gates", "", "電腦週邊、儀器、耗材、客戶 XTT test tree 就緒；全部完成即 T0。"),
    ("PR", "PLAN-採購需求 Procurement", "", "儀器、耗材、socket、治具 PCB/機構件、DOE 與 Carrier Board 物料。"),
    ("AT", "PLAN-自動化測試程式", 25, "C27 自動化測試程式（Follow Test Tree from Voxmicro）5W。"),
    ("PT", "PLAN-產測腳本與Golden Sample", 15, "C27 產測 & 腳本 & Golden Sample 3W。"),
    ("FX", "PLAN-手動生產治具(含Socket)", 40, "C27 手動生產治具，包含 socket 8W。"),
    ("WZ", "PLAN-Welzek儀器Buy-off(405 DOC)", 20, "Welzek 儀器 buy off（405 DOC）4W。"),
    ("DOE", "PLAN-EVT2 DOE", "", "EVT2 DOE：6 runs x 6 pcs，QSI 台灣（Register EVT2-01..10）。"),
    ("BT", "PLAN-BT Channel Sounding", "", "BT Channel Sounding 測試（不含在 20W 估算內）。"),
    ("CB", "PLAN-Carrier Boards", "", "三塊 Carrier Board：M.2 E-key、M.2 B+M-key、mPCIe。"),
]

PEOPLE = [
    # name, email, Jira accountId, role
    ("Bob Lin", "bob.lin@qsitw.com", "712020:89d94f5e-f512-48b6-843f-4f0a1a5bbed5", "EE / 測試"),
    ("Shannon Chang", "shannon.chang@qsitw.com", "712020:a50a0ee0-1928-47a7-a135-a07c21c233ce", "預設受託人"),
    ("Vic Wu", "vic.wu@qsitw.com", "712020:3d3e75ce-84a5-4a1f-a5c3-b3acb395d183", ""),
    ("Challo Chen", "", "712020:8b27ff60-21f3-4a31-889b-a9445881588c", ""),
    ("Sam Chang", "", "712020:5fd11ab1-45fc-44ad-bfdf-cd5756c06d2a", ""),
    ("SF Hsieh", "", "712020:8cec33fb-2f64-4fea-875e-8c6ac84bf679", ""),
    ("Ryan Chen", "", "712020:d68a214f-e790-4295-8281-d488fe772840", ""),
    ("Hank", "", "712020:ad741709-4f13-4135-b781-ca3248c0d8bf", ""),
    ("Chung Tien Shih", "", "712020:4eae1fb9-7d4e-40f5-9091-112535b2096a", ""),
]

# 台灣國定假日（工作日 Mon–Fri 中的放假日）。2026 依行政院人事行政總處公告；
# 2027 為依「紀念日及節日實施條例」推算，正式公告後請在 Holidays 分頁核對。
HOLIDAYS = [
    ("2026-01-01", "元旦", ""),
    ("2026-02-16", "春節（除夕）", ""),
    ("2026-02-17", "春節", ""),
    ("2026-02-18", "春節", ""),
    ("2026-02-19", "春節", ""),
    ("2026-02-20", "春節補假", ""),
    ("2026-02-27", "和平紀念日補假", ""),
    ("2026-04-03", "兒童節補假", ""),
    ("2026-04-06", "清明節補假", ""),
    ("2026-05-01", "勞動節", ""),
    ("2026-06-19", "端午節", ""),
    ("2026-09-25", "中秋節", ""),
    ("2026-09-28", "教師節", ""),
    ("2026-10-09", "國慶日補假", ""),
    ("2026-10-26", "臺灣光復暨金門古寧頭大捷紀念日補假", ""),
    ("2026-12-25", "行憲紀念日", ""),
    ("2027-01-01", "元旦", "推算，待公告"),
    ("2027-02-04", "春節（除夕前一日）", "推算，待公告"),
    ("2027-02-05", "春節（除夕）", "推算，待公告"),
    ("2027-02-08", "春節", "推算，待公告"),
    ("2027-02-09", "春節補假", "推算，待公告"),
    ("2027-02-10", "春節補假", "推算，待公告"),
    ("2027-03-01", "和平紀念日補假", "推算，待公告"),
    ("2027-04-05", "清明節", "推算，待公告"),
    ("2027-04-06", "兒童節補假", "推算，待公告"),
    ("2027-04-30", "勞動節補假", "推算，待公告"),
    ("2027-06-09", "端午節", "推算，待公告"),
    ("2027-09-15", "中秋節", "推算，待公告"),
    ("2027-09-28", "教師節", "推算，待公告"),
    ("2027-10-11", "國慶日補假", "推算，待公告"),
    ("2027-10-25", "臺灣光復暨金門古寧頭大捷紀念日", "推算，待公告"),
    ("2027-12-24", "行憲紀念日補假", "推算，待公告"),
]

EMAIL_SRC = "Bob Lin（2026-10-12 估算信）"
DRAFT_SRC = "草擬（待 Bob 確認）"


def _t(
    tid: str,
    wbs: str,
    group: str,
    name: str,
    ttype: str,
    prio: str,
    dur: int,
    preds: str = "",
    desc: str = "",
    *,
    constraint: str = "",
    estimate: bool = False,
    notes: str = "",
    proposed: str = "",
) -> dict[str, str]:
    return {
        "ID": tid,
        "WBS": wbs,
        "Group": group,
        "Task Name": name,
        "Type": ttype,
        "Priority": prio,
        "Owner": "",
        "Duration (d)": str(dur),
        "Predecessors": preds,
        "Start Constraint": constraint,
        "Status": "Not Started",
        "% Complete": "",
        "Sync to Jira": "Y",
        "Description / 工作內容": desc,
        "Discussion / Comments": "",
        "Proposed By": proposed or (DRAFT_SRC if estimate else EMAIL_SRC),
        "Estimate": "Y" if estimate else "N",
        "Notes": notes,
    }


def _chain(prefix: str, wbs_major: int, group: str, items: list[tuple], first_preds: str) -> list[dict]:
    """依序串接（前一項 FS 下一項）。items: (name, dur, desc[, extra_preds])."""
    rows = []
    prev = first_preds
    for i, item in enumerate(items, start=1):
        name, dur, desc = item[0], item[1], item[2]
        extra = item[3] if len(item) > 3 else ""
        tid = f"PLN-{prefix}-{i:02d}"
        preds = "; ".join(p for p in (prev, extra) if p)
        rows.append(_t(tid, f"{wbs_major}.{i}", group, name, "Task", "P1", dur, preds, desc))
        prev = tid
    return rows


def build_seed_tasks() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []

    # 1. Gates（milestone，0 工期）
    rows += [
        _t("PLN-G-01", "1.1", "G", "G-01 電腦/週邊設備就緒", "Milestone", "P1", 0, "PLN-PR-01",
           "測試 PC、螢幕、USB hub、條碼槍等週邊安裝完成可使用。", estimate=True,
           notes="日期由 PLN-PR-01 推算；實際到位後可改 Start Constraint。"),
        _t("PLN-G-02", "1.2", "G", "G-02 儀器就緒", "Milestone", "P1", 0, "PLN-PR-02",
           "Welzek T6290F 與選配、屏蔽箱、RF switch、電源供應器到位並可開機。", estimate=True),
        _t("PLN-G-03", "1.3", "G", "G-03 耗材就緒", "Milestone", "P1", 0, "PLN-PR-03",
           "RF cable、衰減器、轉接頭、pogo pin、ESD 用品到位。", estimate=True),
        _t("PLN-G-04", "1.4", "G", "G-04 客戶 XTT test tree 文件交付", "Milestone", "P1", 0, "",
           "Voxmicro 提供 C27 XTT test tree（測項、limit、流程）。", constraint=PROJECT_START_PLACEHOLDER,
           estimate=True, notes="Start Constraint 為佔位日期：請填入客戶承諾交付日。"),
        _t("PLN-G-05", "1.5", "G", "T0：前置條件全部就緒", "Milestone", "P1", 0,
           "PLN-G-01; PLN-G-02; PLN-G-03; PLN-G-04",
           "電腦週邊、儀器、耗材、XTT test tree 全部就緒；AT/PT/FX/WZ 工期從 T0 起算。"),
    ]

    # 2. 採購
    pr = [
        ("PLN-PR-01", "測試 PC 與週邊採購", "P1", 10, "",
         "測試用 PC、螢幕、USB hub、條碼槍、UPS 請購到到貨。"),
        ("PLN-PR-02", "測試儀器與選配採購", "P1", 20, "",
         "Welzek T6290F 選配/授權（EHT320、SCPI 版本）、屏蔽箱、RF switch、電源供應器。"),
        ("PLN-PR-03", "測試耗材採購", "P1", 10, "",
         "RF cable、衰減器、SMA/IPEX 轉接頭、pogo pin、ESD 用品。"),
        ("PLN-PR-04", "LGA test socket 採購", "P1", 20, "PLN-FX-02",
         "依 FX-02 選定的 socket 下單到到貨（含備品）。"),
        ("PLN-PR-05", "治具轉接板 PCB 製作與 SMT", "P1", 10, "PLN-FX-05",
         "FX-04 設計的 adapter PCB 製板、SMT、出貨。"),
        ("PLN-PR-06", "治具機構件加工", "P1", 12, "PLN-FX-05",
         "CNC 底座、壓板、夾具/氣缸等機構件加工到貨。"),
        ("PLN-PR-07", "BT Channel Sounding 量測選配/授權", "P2", 15, "PLN-BT-02",
         "依 BT-02 評估結果採購 Channel Sounding 量測選配或授權（若現有儀器不支援）。"),
        ("PLN-PR-08", "EVT2 DOE 物料", "P1", 30, "PLN-DOE-01",
         "LGA PCB、BOM、consign IC、laser-cut shield。注意 EVT2-10：PCB 原物料報價 150 天，可能成為關鍵路徑。"),
        ("PLN-PR-09", "Carrier Board 共用零件備料", "P2", 15, "PLN-CB1-03",
         "三塊 Carrier Board 的連接器、被動元件、金手指相關零件備料。"),
    ]
    for i, (tid, name, prio, dur, preds, desc) in enumerate(pr, start=1):
        rows.append(_t(tid, f"2.{i}", "PR", name, "Procurement", prio, dur, preds, desc, estimate=True))

    # 3–6. 原始估算四組（工期不可變動；組與組之間不重疊）
    rows += _chain("AT", 3, "AT", [
        ("Test tree 審閱與 limit 確認", 3, "審閱 Voxmicro XTT test tree，確認測項、limit、判定條件。"),
        ("測試程式框架架構", 3, "測試程式架構、儀器抽象層、設定檔格式。"),
        ("DUT 控制模組（FTM、BDF/NVM、chain/channel）", 4, "FTM 模式控制、BDF/NVM 寫入、chain/channel 切換。"),
        ("WLAN TX 測項", 4, "WLAN TX power/EVM/mask/頻偏等測項實作。"),
        ("WLAN RX 測項", 3, "WLAN RX sensitivity/PER 測項實作。"),
        ("BT TX/RX（BR/EDR/LE）測項", 3, "BT BR/EDR/LE TX/RX 測項實作。"),
        ("判定、報表與 SN 追溯", 2, "Pass/Fail 判定、測試報表、SN 追溯紀錄。"),
        ("整合測試與 Voxmicro 審查", 3, "整合測試、交 Voxmicro 審查並修正。"),
    ], "PLN-G-05")
    rows += _chain("PT", 4, "PT", [
        ("產測流程、takt 與測項子集", 2, "定義產線流程、takt time、產測測項子集。"),
        ("Path loss / 校正腳本", 3, "線損量測與校正腳本。"),
        ("一鍵產測腳本", 3, "操作員一鍵執行的產測腳本與 UI。"),
        ("Golden sample 挑選與 baseline", 3, "挑選 golden sample 並建立 baseline（可對應 Register EVT2-09）。"),
        ("Golden correlation / GR&R", 2, "Golden 相關性與 GR&R 驗證。"),
        ("SOP 與教育訓練", 2, "產測 SOP 撰寫與操作員訓練。"),
    ], "PLN-AT-08")
    rows += _chain("FX", 5, "FX", [
        ("治具規格", 3, "手動生產治具規格（DUT 定位、RF 介面、屏蔽）。"),
        ("Socket 選型與報價", 4, "LGA socket 選型、詢價、樣品評估。"),
        ("機構設計", 5, "治具機構設計（底座、壓板、屏蔽）。"),
        ("轉接板 PCB 設計", 6, "Adapter PCB 線路與 layout。"),
        ("DFM 審查與下單", 2, "機構/PCB DFM 審查並下單。"),
        ("製作/採購 lead time", 12, "治具加工與零件採購等待期（採購細項見 PLN-PR-04~06）。"),
        ("組裝與檢查", 3, "治具組裝與基本檢查。", "PLN-PR-04; PLN-PR-05; PLN-PR-06"),
        ("治具驗證", 4, "治具功能、重複性、線損驗證。"),
        ("緩衝", 1, "排程緩衝。"),
    ], "PLN-PT-06")
    rows += _chain("WZ", 6, "WZ", [
        ("405 DOC 需求確認", 2, "確認 405 DOC 驗收項目與格式。"),
        ("安裝、設定與選配（SCPI 版本、EHT320）", 2, "儀器安裝設定，確認 SCPI 版本支援 EHT320/OFDMA（Register RF-14）。"),
        ("系統校正與 path loss", 3, "系統校正、線損量測。"),
        ("與參考分析儀相關性", 5, "與參考分析儀比對相關性。"),
        ("重複性/穩定性", 3, "重複性與長時間穩定性測試。"),
        ("撰寫 405 DOC", 3, "撰寫 405 DOC 報告。"),
        ("客戶審查與簽核", 2, "客戶審查並簽核 405 DOC。"),
    ], "PLN-FX-09")

    # 7. EVT2 DOE（估計）
    doe = [
        ("EVT2 DOE 計畫與矩陣凍結", 3, "", "6 runs x 6 pcs；7x7 49-up 面板、每面板 6 個位置（Register EVT2-01/02/03）。"),
        ("SMT 程式、鋼板與面板準備", 5, "PLN-DOE-01", "SMT 程式、鋼板、面板與治具準備。"),
        ("SMT 試產 6 runs（QSI 台灣）", 3, "PLN-DOE-02; PLN-PR-08", "依 DOE 矩陣完成 6 runs 試產。"),
        ("外觀/X-ray/共面度檢驗", 5, "PLN-DOE-03", "skirt 對位、lid 共面度、X-ray void（Register EVT2-04）。"),
        ("36 pcs Wi-Fi/BT 功能測試", 3, "PLN-DOE-03", "全部 DOE 樣品 Wi-Fi/BT 功能測試。"),
        ("EVT2 RF delta-check", 4, "PLN-DOE-05", "5 EVT2 + 1 EVT1 control 同站比對（Register EVT2-08）。"),
        ("破壞性分析", 5, "PLN-DOE-04", "~25 pcs shear、cross-section、dye-and-pry、RF seal。"),
        ("DVT golden sample 選定", 2, "PLN-DOE-06", "選 5 pcs nominal-cell golden（3 泰國 + 2 台灣，Register EVT2-09）。"),
        ("DOE 報告與客戶審查", 3, "PLN-DOE-06; PLN-DOE-07; PLN-DOE-08", "DOE 結果報告並與客戶審查。"),
    ]
    for i, (name, dur, preds, desc) in enumerate(doe, start=1):
        rows.append(_t(f"PLN-DOE-{i:02d}", f"7.{i}", "DOE", name, "Task", "P1", dur, preds, desc, estimate=True))

    # 8. BT Channel Sounding（估計）
    bt = [
        ("Channel Sounding 規格與測試方法確認", 3, "PLN-G-05", "BT 6.0 Channel Sounding（PBR/RTT）需求、測項與判定方法確認。"),
        ("量測儀器能力評估", 3, "PLN-BT-01", "確認 Welzek/現有儀器是否支援 CS 量測，決定是否需採購（PLN-PR-07）。"),
        ("CS 測試設定與腳本開發", 5, "PLN-BT-02; PLN-AT-06", "沿用 AT-06 BT 模組開發 CS 測試設定與腳本。"),
        ("CS 量測與特性分析", 5, "PLN-BT-03; PLN-PR-07", "距離精度、相位/RTT 特性量測。"),
        ("CS 測試報告與客戶審查", 3, "PLN-BT-04", "測試報告並與客戶審查。"),
    ]
    for i, (name, dur, preds, desc) in enumerate(bt, start=1):
        rows.append(_t(f"PLN-BT-{i:02d}", f"8.{i}", "BT", name, "Task", "P2", dur, preds, desc, estimate=True))

    # 9. Carrier Boards（估計）
    boards = [(1, "M.2 E-key"), (2, "M.2 B+M-key"), (3, "mPCIe")]
    wbs = 0
    for n, board in boards:
        p = f"PLN-CB{n}"
        prev_board = f"PLN-CB{n - 1}" if n > 1 else ""
        steps = [
            ("01", f"{board} 線路圖定版", "Task", 3,
             f"{prev_board}-01" if prev_board else "",
             "納入 ECO-09/15/16 等修正並定版線路圖。"),
            ("02", f"{board} Layout", "Task", 5,
             "; ".join(x for x in (f"{p}-01", f"{prev_board}-02" if prev_board else "") if x),
             "PCB layout（同一位 layout 工程師依序進行）。"),
            ("03", f"{board} DFM 審查與 Gerber 釋出", "Task", 2, f"{p}-02", "DFM 審查並釋出 Gerber。"),
            ("04", f"{board} PCB 製板", "Procurement", 12, f"{p}-03", "PCB 製板到貨。"),
            ("05", f"{board} SMT 組裝", "Task", 3,
             f"{p}-04; PLN-PR-09; PLN-DOE-05", "SMT 組裝（需 DOE 功能測試通過的 LGA 模組）。"),
            ("06", f"{board} Bring-up 與功能測試", "Task", 5, f"{p}-05", "上電 bring-up、Wi-Fi/BT 功能測試。"),
        ]
        for suffix, name, ttype, dur, preds, desc in steps:
            wbs += 1
            rows.append(_t(f"{p}-{suffix}", f"9.{wbs}", "CB", name, ttype, "P2", dur, preds, desc, estimate=True))
    return rows
