"""ProjectSync 桌面介面（Tkinter / ttk）。"""

from __future__ import annotations

import contextlib
import copy
import os
import queue
import threading
import traceback
import webbrowser
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from . import profiles

FONT = ("Microsoft JhengHei", 10)
FONT_B = ("Microsoft JhengHei", 10, "bold")
FONT_H = ("Microsoft JhengHei", 14, "bold")

# (tab, path, label, kind) kind: str | bool | list | choice:a,b | file | secret
FIELDS: list[tuple[str, tuple[str, ...], str, str]] = [
    ("general", ("project", "name"), "專案名稱", "str"),
    ("general", ("project", "slug"), "專案代碼 (slug)", "str"),
    ("general", ("project", "customer"), "客戶", "str"),
    ("general", ("project", "project_start"), "專案開始日 (YYYY-MM-DD)", "str"),
    ("general", ("project", "description"), "說明", "str"),
    ("atlassian", ("atlassian", "site_url"), "Atlassian 網址", "str"),
    ("atlassian", ("atlassian", "cloud_id"), "Cloud ID", "str"),
    ("atlassian", ("atlassian", "email"), "登入 Email", "str"),
    ("atlassian", ("atlassian", "silent_mode"), "寂靜同步（不通知 / minorEdit）", "bool"),
    ("atlassian", ("jira", "project_key"), "Jira 專案 Key", "str"),
    ("atlassian", ("jira", "epic_issue_type"), "Epic 類型名稱", "str"),
    ("atlassian", ("jira", "task_issue_type"), "Task 類型名稱", "str"),
    ("atlassian", ("jira", "start_date_field"), "開始日期欄位", "str"),
    ("atlassian", ("jira", "due_date_field"), "截止日期欄位", "str"),
    ("atlassian", ("jira", "default_assignee_account_id"), "預設受託人 accountId", "str"),
    ("atlassian", ("jira", "default_assignee_query"), "預設受託人搜尋字", "str"),
    ("atlassian", ("jira", "board_url"), "Jira Board / Timeline 網址", "str"),
    ("plan", ("plan", "enabled"), "啟用排程同步", "bool"),
    ("plan", ("plan", "source"), "計畫表來源", "choice:xlsx,google,export"),
    ("plan", ("plan", "xlsx_path"), "本機 xlsx 路徑", "file"),
    ("plan", ("plan", "google_sheet_url"), "Google Sheet 網址", "str"),
    ("plan", ("plan", "share_with"), "分享給（逗號分隔）", "list"),
    ("plan", ("plan", "label"), "Jira 標籤（<slug>-plan）", "str"),
    ("plan", ("plan", "id_prefix"), "計畫 ID 前綴", "str"),
    ("plan", ("plan", "t0_task_id"), "T0 里程碑 ID", "str"),
    ("plan", ("plan", "on_removed"), "刪除列時", "choice:close,delete"),
    ("plan", ("plan", "confluence_space_key"), "Confluence 空間 Key", "str"),
    ("plan", ("plan", "confluence_space_id"), "Confluence 空間 ID", "str"),
    ("plan", ("plan", "confluence_parent_page_id"), "計畫頁父頁面 ID / URL", "str"),
    ("plan", ("plan", "confluence_page_title"), "計畫頁標題", "str"),
    ("plan", ("plan", "confluence_page_id"), "計畫頁 ID（自動回填）", "str"),
    ("plan", ("plan", "show_discussion_on_confluence"), "Confluence 顯示 Discussion 欄", "bool"),
    ("register", ("register", "enabled"), "啟用 Register 同步", "bool"),
    ("register", ("register", "sheet_url"), "S 表 Google Sheet 網址", "str"),
    ("register", ("register", "sheet_name"), "工作表名稱（找不到用第一張）", "str"),
    ("register", ("register", "confluence_page_id"), "C 表 Confluence page id", "str"),
    ("register", ("register", "confluence_page_title"), "C 表頁面標題", "str"),
    ("register", ("register", "space_key"), "Confluence 空間 Key", "str"),
    ("register", ("register", "source_link_title"), "頁首 S 表連結文字", "str"),
    ("register", ("register", "version_message"), "Confluence 版本訊息", "str"),
    ("mail", ("mail", "to"), "收件者 To", "str"),
    ("mail", ("mail", "cc"), "副本 Cc（逗號分隔）", "list"),
    ("mail", ("mail", "backend"), "寄信方式", "choice:smtp,graph,auto"),
    ("mail", ("mail", "send_on_real_run"), "正式同步後寄出報告", "bool"),
]

SECRET_FIELDS = [
    ("ATLASSIAN_API_TOKEN", "Atlassian API Token", True),
    ("MAIL_USERNAME", "寄件帳號（Gmail）", False),
    ("MAIL_PASSWORD", "Gmail 應用程式密碼", True),
    ("SMTP_HOST", "SMTP 主機（預設 smtp.gmail.com）", False),
    ("SMTP_PORT", "SMTP Port（587）", False),
    ("GOOGLE_SERVICE_ACCOUNT_FILE", "Google service account JSON 檔", False),
    ("GRAPH_TENANT_ID", "Graph Tenant ID（選用）", False),
    ("GRAPH_CLIENT_ID", "Graph Client ID（選用）", False),
    ("GRAPH_CLIENT_SECRET", "Graph Client Secret（選用）", True),
]


def _get(d: dict, path: tuple[str, ...]) -> Any:
    for k in path:
        d = d.get(k, {}) if isinstance(d, dict) else {}
    return d if d != {} else ""


def _set(d: dict, path: tuple[str, ...], value: Any) -> None:
    for k in path[:-1]:
        d = d.setdefault(k, {})
    d[path[-1]] = value


class QueueWriter:
    def __init__(self, q: queue.Queue):
        self.q = q

    def write(self, s: str) -> int:
        if s:
            self.q.put(("log", s))
        return len(s)

    def flush(self) -> None:
        pass


class App(tk.Tk):
    def __init__(self, initial: str | None = None):
        super().__init__()
        self.title("ProjectSync — Register / 排程 ⇄ Confluence / Jira")
        self.geometry("1280x860")
        self.minsize(1000, 700)
        self.option_add("*Font", FONT)
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("TNotebook.Tab", padding=(12, 4), font=FONT)
        style.configure("Accent.TButton", font=FONT_B)
        self.q: queue.Queue = queue.Queue()
        self.busy = False
        self.profile: dict[str, Any] = {}
        self.vars: dict[tuple[str, ...], tk.Variable] = {}
        self.secret_vars: dict[str, tk.StringVar] = {}
        self.direction = tk.StringVar(value="both")
        self.allow_mass = tk.BooleanVar(value=False)
        self.send_mail = tk.BooleanVar(value=True)
        self._build()
        self.reload_projects(initial)
        self.after(100, self._drain)

    # ------------------------------------------------------------------ layout
    def _build(self) -> None:
        top = ttk.Frame(self, padding=(10, 8))
        top.pack(fill="x")
        ttk.Label(top, text="ProjectSync", font=FONT_H).pack(side="left")
        ttk.Label(top, text="   專案：").pack(side="left")
        self.project_var = tk.StringVar()
        self.project_cb = ttk.Combobox(top, textvariable=self.project_var, state="readonly", width=24)
        self.project_cb.pack(side="left")
        self.project_cb.bind("<<ComboboxSelected>>", lambda e: self.load_project(self.project_var.get()))
        for text, cmd in (("開案精靈…", self.open_wizard), ("複製", self.clone_project),
                          ("刪除", self.delete_project), ("儲存設定", self.save_project)):
            ttk.Button(top, text=text, command=cmd).pack(side="left", padx=3)
        self.silent_lbl = ttk.Label(top, text="", foreground="#1e8449", font=FONT_B)
        self.silent_lbl.pack(side="right")

        paned = ttk.PanedWindow(self, orient="vertical")
        paned.pack(fill="both", expand=True, padx=8, pady=(0, 6))
        self.nb = ttk.Notebook(paned)
        paned.add(self.nb, weight=3)
        self.tabs: dict[str, ttk.Frame] = {}
        for key, title in (("actions", "執行"), ("schedule", "排程預覽"), ("general", "開案資訊"),
                           ("atlassian", "Atlassian（Confluence / Jira）"), ("plan", "Google Sheets / 排程同步"),
                           ("register", "Register 同步"), ("mail", "郵件"), ("creds", "憑證")):
            f = ttk.Frame(self.nb, padding=10)
            self.nb.add(f, text=title)
            self.tabs[key] = f
        self._build_forms()
        self._build_actions()
        self._build_schedule()
        self._build_creds()

        logf = ttk.Frame(paned)
        paned.add(logf, weight=2)
        bar = ttk.Frame(logf)
        bar.pack(fill="x")
        ttk.Label(bar, text="執行紀錄", font=FONT_B).pack(side="left")
        ttk.Button(bar, text="清除", command=lambda: self.log.delete("1.0", "end")).pack(side="right")
        ttk.Button(bar, text="開啟 logs 資料夾", command=self.open_logs).pack(side="right", padx=4)
        self.log = ScrolledText(logf, height=12, font=("Consolas", 10), wrap="word")
        self.log.pack(fill="both", expand=True)
        self.log.tag_config("ok", foreground="#1e8449")
        self.log.tag_config("err", foreground="#c0392b")
        self.status = tk.StringVar(value="就緒")
        ttk.Label(self, textvariable=self.status, anchor="w", padding=(10, 2)).pack(fill="x")

    def _form_row(self, parent, r: int, label: str, kind: str, var: tk.Variable) -> None:
        ttk.Label(parent, text=label).grid(row=r, column=0, sticky="w", pady=3, padx=(0, 10))
        if kind == "bool":
            ttk.Checkbutton(parent, variable=var).grid(row=r, column=1, sticky="w")
        elif kind.startswith("choice:"):
            ttk.Combobox(parent, textvariable=var, values=kind[7:].split(","), state="readonly", width=20).grid(
                row=r, column=1, sticky="w")
        else:
            show = "•" if kind == "secret" else ""
            ttk.Entry(parent, textvariable=var, width=90, show=show).grid(row=r, column=1, sticky="we")
            if kind == "file":
                ttk.Button(parent, text="瀏覽…", command=lambda v=var: self._browse(v)).grid(row=r, column=2, padx=4)
        parent.columnconfigure(1, weight=1)

    def _build_forms(self) -> None:
        rows: dict[str, int] = {}
        frames: dict[str, ttk.Frame] = {}
        for tab, path, label, kind in FIELDS:
            if tab not in frames:
                frames[tab] = ttk.Frame(self.tabs[tab])
                frames[tab].pack(fill="x", anchor="n")
            var: tk.Variable = tk.BooleanVar() if kind == "bool" else tk.StringVar()
            self.vars[path] = var
            self._form_row(frames[tab], rows.get(tab, 0), label, kind, var)
            rows[tab] = rows.get(tab, 0) + 1
        btns = {
            "atlassian": [("測試 Atlassian 連線", self.test_atlassian)],
            "plan": [("測試 Google 憑證", self.test_google), ("產生計畫表 xlsx（不覆寫）", self.init_plan_xlsx),
                     ("開啟計畫表", self.open_sheet)],
            "register": [("測試讀取 S 表", self.test_register_sheet)],
            "mail": [("測試 SMTP 登入", self.test_smtp), ("寄測試信", self.test_mail)],
            "general": [("驗證設定", self.validate)],
        }
        for tab, items in btns.items():
            bar = ttk.Frame(self.tabs[tab])
            bar.pack(fill="x", pady=10, anchor="n")
            for text, cmd in items:
                ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=4)
        ttk.Label(self.tabs["plan"], foreground="#555", justify="left", text=(
            "來源 xlsx：本機檔案（可讀寫，目前預設）。google：需 service account JSON（憑證頁），表格須分享給 SA 為編輯者。\n"
            "export：Google 公開匯出唯讀（知道連結者可檢視），無法回寫 Jira Key / 拉回資料。")).pack(anchor="w")
        ttk.Label(self.tabs["mail"], foreground="#555", justify="left", text=(
            "公司 Office365 已停用 SMTP Basic Auth；請用 Gmail 帳號 + 應用程式密碼（憑證頁），寄件人顯示為該 Gmail。")).pack(anchor="w")

    def _build_actions(self) -> None:
        f = self.tabs["actions"]
        reg = ttk.LabelFrame(f, text="Register：S 表 → Confluence C 表 → Jira", padding=10)
        reg.pack(fill="x", pady=4)
        ttk.Button(reg, text="預覽（dry-run）", command=lambda: self.run_register(True)).pack(side="left", padx=4)
        ttk.Button(reg, text="正式同步", style="Accent.TButton",
                   command=lambda: self.run_register(False)).pack(side="left", padx=4)
        ttk.Button(reg, text="開啟 S 表", command=lambda: self.open_url(_get(self.profile, ("register", "sheet_url"))
                                                                     )).pack(side="left", padx=12)
        ttk.Button(reg, text="開啟 C 表頁面", command=self.open_register_page).pack(side="left")

        plan = ttk.LabelFrame(f, text="排程：計畫表 ⇄ Jira（Epic / 任務 / 日期 / 相依）→ Confluence 計畫頁", padding=10)
        plan.pack(fill="x", pady=4)
        ttk.Label(plan, text="方向：").pack(side="left")
        for val, txt in (("both", "雙向（先拉後推）"), ("push", "推送 Sheet→Jira"), ("pull", "拉回 Jira→Sheet")):
            ttk.Radiobutton(plan, text=txt, value=val, variable=self.direction).pack(side="left", padx=3)
        ttk.Checkbutton(plan, text="允許大量移除", variable=self.allow_mass).pack(side="left", padx=10)
        ttk.Checkbutton(plan, text="寄報告", variable=self.send_mail).pack(side="left")
        plan2 = ttk.Frame(plan)
        plan2.pack(side="right")
        ttk.Button(plan2, text="預覽（dry-run）", command=lambda: self.run_plan(True)).pack(side="left", padx=4)
        ttk.Button(plan2, text="正式同步", style="Accent.TButton", command=lambda: self.run_plan(False)).pack(
            side="left", padx=4)

        links = ttk.LabelFrame(f, text="連結", padding=10)
        links.pack(fill="x", pady=4)
        for text, cmd in (("計畫表", self.open_sheet), ("Confluence 計畫頁", self.open_plan_page),
                          ("Jira Timeline / Board", lambda: self.open_url(_get(self.profile, ("jira", "board_url")))),
                          ("Jira 計畫單（標籤）", self.open_jira_label), ("寄測試信", self.test_mail)):
            ttk.Button(links, text=text, command=cmd).pack(side="left", padx=4)

        self.summary = ScrolledText(f, height=10, font=("Consolas", 10), wrap="word")
        ttk.Label(f, text="上次執行摘要", font=FONT_B).pack(anchor="w", pady=(10, 0))
        self.summary.pack(fill="both", expand=True)

    def _build_schedule(self) -> None:
        f = self.tabs["schedule"]
        bar = ttk.Frame(f)
        bar.pack(fill="x")
        ttk.Button(bar, text="讀取計畫表並排程（不連 Jira）", command=self.preview).pack(side="left")
        self.sched_note = ttk.Label(bar, text="", foreground="#555")
        self.sched_note.pack(side="left", padx=10)
        pw = ttk.PanedWindow(f, orient="vertical")
        pw.pack(fill="both", expand=True, pady=6)
        cols = ("id", "group", "name", "type", "dur", "start", "finish", "status", "sync", "jira")
        self.tree = ttk.Treeview(pw, columns=cols, show="headings", height=12)
        for c, w, t in zip(cols, (110, 50, 330, 90, 50, 95, 95, 90, 45, 90),
                           ("ID", "群組", "工作", "類型", "工期", "Start", "Finish", "狀態", "Sync", "Jira")):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w")
        self.tree.tag_configure("draft", foreground="#888")
        pw.add(self.tree, weight=2)
        gf = ttk.Frame(pw)
        pw.add(gf, weight=2)
        self.gantt = tk.Canvas(gf, background="white", height=260)
        hs = ttk.Scrollbar(gf, orient="horizontal", command=self.gantt.xview)
        vs = ttk.Scrollbar(gf, orient="vertical", command=self.gantt.yview)
        self.gantt.configure(xscrollcommand=hs.set, yscrollcommand=vs.set)
        hs.pack(side="bottom", fill="x")
        vs.pack(side="right", fill="y")
        self.gantt.pack(fill="both", expand=True)

    def _build_creds(self) -> None:
        f = self.tabs["creds"]
        ttk.Label(f, text=f"憑證存於 {profiles.ENV_PATH}（本機，不在專案設定檔內，勿分享）。所有專案共用。",
                  foreground="#555").pack(anchor="w")
        form = ttk.Frame(f)
        form.pack(fill="x", pady=8)
        for r, (key, label, secret) in enumerate(SECRET_FIELDS):
            var = tk.StringVar()
            self.secret_vars[key] = var
            self._form_row(form, r, label, "secret" if secret else ("file" if "FILE" in key else "str"), var)
        bar = ttk.Frame(f)
        bar.pack(fill="x")
        for text, cmd in (("儲存憑證", self.save_secrets), ("從 jira_config.txt 匯入 Atlassian", self.import_jira),
                          ("測試 Atlassian", self.test_atlassian), ("測試 SMTP", self.test_smtp),
                          ("測試 Google", self.test_google)):
            ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=4)
        self.show_secret = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="顯示密碼", variable=self.show_secret, command=self._toggle_secret).pack(
            side="left", padx=12)
        ttk.Label(f, foreground="#555", justify="left", text=(
            "Google Sheet 設定（雙向同步需要寫入權限）：\n"
            "1. Google Cloud Console 建立專案 → 啟用 Google Sheets API 與 Google Drive API。\n"
            "2. IAM → 服務帳戶 → 建立 → 金鑰 → 新增 JSON 金鑰，下載後放在本機（例如 D:\\MEGA\\ProjectSync\\secrets\\sa.json）。\n"
            "3. 在上方選擇該 JSON 檔並儲存；按「測試 Google」會顯示 SA 的 email。\n"
            "4. 把計畫 Google Sheet 分享給該 email 為「編輯者」；在排程同步頁把來源改成 google 並貼上網址。")).pack(
            anchor="w", pady=10)
        self.secret_entries = [w for w in form.winfo_children() if isinstance(w, ttk.Entry)]

    def _toggle_secret(self) -> None:
        show = "" if self.show_secret.get() else "•"
        secret_keys = [k for k, _, s in SECRET_FIELDS if s]
        for w in self.secret_entries:
            name = str(w.cget("textvariable"))
            for k in secret_keys:
                if str(self.secret_vars[k]) == name:
                    w.configure(show=show)

    # ----------------------------------------------------------------- projects
    def reload_projects(self, select: str | None = None) -> None:
        names = profiles.list_projects()
        self.project_cb["values"] = names
        target = select if select in names else (names[0] if names else None)
        if target:
            self.project_var.set(target)
            self.load_project(target)
        secrets = profiles.load_secrets()
        for k, v in self.secret_vars.items():
            v.set(secrets.get(k, ""))

    def load_project(self, slug: str) -> None:
        try:
            self.profile = profiles.load_profile(slug)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("載入失敗", str(exc))
            return
        for path, var in self.vars.items():
            v = _get(self.profile, path)
            if isinstance(var, tk.BooleanVar):
                var.set(bool(v))
            elif isinstance(v, list):
                var.set(", ".join(str(x) for x in v))
            else:
                var.set("" if v is None else str(v))
        silent = self.profile["atlassian"].get("silent_mode", True)
        self.silent_lbl.configure(text="🔕 寂靜同步（不通知）" if silent else "🔔 會通知",
                                  foreground="#1e8449" if silent else "#c0392b")
        self.status.set(f"已載入專案 {slug}：{profiles.profile_path(slug)}")
        last = profiles.LOGS_DIR / slug / "plan" / "最新計畫同步結果.txt"
        self.summary.delete("1.0", "end")
        if last.exists():
            self.summary.insert("end", last.read_text(encoding="utf-8"))

    def collect(self) -> dict[str, Any]:
        p = copy.deepcopy(self.profile) if self.profile else profiles.default_profile("new")
        for path, var in self.vars.items():
            val = var.get()
            kind = next(k for t, pth, _, k in FIELDS if pth == path)
            if kind == "list":
                val = [x.strip() for x in str(val).split(",") if x.strip()]
            _set(p, path, val)
        return p

    def save_project(self) -> bool:
        p = self.collect()
        problems = profiles.validate_profile(p)
        old_slug = self.profile.get("project", {}).get("slug")
        path = profiles.save_profile(p)
        if old_slug and old_slug != p["project"]["slug"]:
            if messagebox.askyesno("專案代碼變更", f"已另存為 {p['project']['slug']}，是否刪除舊設定 {old_slug}？"):
                profiles.delete_profile(old_slug)
        self.profile = p
        self.reload_projects(p["project"]["slug"])
        self._log(f"✅ 已儲存 {path}\n", "ok")
        if problems:
            self._log("⚠ 設定檢查：\n" + "\n".join(f"  - {x}" for x in problems) + "\n", "err")
        return True

    def validate(self) -> None:
        problems = profiles.validate_profile(self.collect())
        messagebox.showinfo("驗證設定", "設定完整 ✅" if not problems else "\n".join(problems))

    def clone_project(self) -> None:
        if not self.profile:
            return
        slug = simple_input(self, "複製專案", "新專案代碼（slug）：", self.profile["project"]["slug"] + "-copy")
        if not slug:
            return
        p = copy.deepcopy(self.collect())
        p["project"]["slug"] = profiles.slugify(slug)
        p["project"]["name"] = p["project"]["name"] + " (copy)"
        p["plan"].update({"confluence_page_id": "", "google_sheet_url": "", "label": f"{p['project']['slug']}-plan",
                          "xlsx_path": f"projects/{p['project']['slug']}/{p['project']['slug'].upper()}_Project_Plan.xlsx"})
        profiles.save_profile(p)
        self.reload_projects(p["project"]["slug"])

    def delete_project(self) -> None:
        slug = self.project_var.get()
        if slug and messagebox.askyesno("刪除專案", f"刪除專案設定 {slug}？（不會刪除 Jira / Confluence / 計畫表）"):
            profiles.delete_profile(slug)
            self.profile = {}
            self.reload_projects()

    # ------------------------------------------------------------------ secrets
    def secrets(self) -> dict[str, str]:
        s = profiles.load_secrets()
        s.update({k: v.get().strip() for k, v in self.secret_vars.items()})
        return s

    def save_secrets(self) -> None:
        profiles.save_secrets(self.secrets())
        self._log(f"✅ 憑證已儲存到 {profiles.ENV_PATH}（值不顯示）\n", "ok")

    def import_jira(self) -> None:
        try:
            email, token = profiles.import_jira_config()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("匯入失敗", str(exc))
            return
        if not token:
            messagebox.showwarning("匯入", "jira_config.txt 沒有 JIRA_TOKEN")
            return
        self.secret_vars["ATLASSIAN_API_TOKEN"].set(token)
        if email and email != self.vars[("atlassian", "email")].get():
            self.vars[("atlassian", "email")].set(email)
        self.save_secrets()
        self._log(f"✅ 已從 jira_config.txt 匯入 {email} 的 token（{profiles.mask(token)}）\n", "ok")

    # ------------------------------------------------------------------ jobs
    def _job(self, title: str, fn: Callable[[], Any], done: Callable[[Any], None] | None = None) -> None:
        if self.busy:
            messagebox.showinfo("執行中", "已有工作執行中，請稍候")
            return
        self.busy = True
        self.status.set(f"執行中：{title}…")
        self._log(f"\n===== {datetime.now():%H:%M:%S} {title} =====\n")

        def worker():
            writer = QueueWriter(self.q)
            result, error = None, None
            with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                try:
                    result = fn()
                except Exception as exc:  # noqa: BLE001
                    error = exc
                    print(traceback.format_exc())
            self.q.put(("done", (title, result, error, done)))

        threading.Thread(target=worker, daemon=True).start()

    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "done":
                    title, result, error, done = payload
                    self.busy = False
                    if error:
                        self._log(f"❌ {title} 失敗：{error}\n", "err")
                        self.status.set(f"❌ {title} 失敗")
                    else:
                        if isinstance(result, str):
                            self._log(f"✅ {result}\n", "ok")
                        self._log(f"✅ {title} 完成\n", "ok")
                        self.status.set(f"✅ {title} 完成 {datetime.now():%H:%M:%S}")
                        if done:
                            done(result)
                elif kind == "call":
                    payload()
        except queue.Empty:
            pass
        self.after(100, self._drain)

    def _log(self, text: str, tag: str = "") -> None:
        if not tag:
            tag = "err" if ("❌" in text or "錯誤" in text[:20]) else ("ok" if text.startswith("✅") else "")
        self.log.insert("end", text, tag)
        self.log.see("end")

    def _ready(self) -> tuple[dict, dict] | None:
        if not self.profile:
            messagebox.showwarning("沒有專案", "請先建立或選擇專案")
            return None
        p = self.collect()
        profiles.save_profile(p)
        self.profile = p
        return p, self.secrets()

    def run_register(self, dry: bool) -> None:
        ready = self._ready()
        if not ready:
            return
        p, s = ready
        if not dry and not messagebox.askyesno("正式同步", "執行 Register 正式同步（會寫入 Confluence / Jira）？"):
            return
        from .register_adapter import run_register

        def done(rep):
            text = (profiles.LOGS_DIR / p["project"]["slug"] / "register" / "最新同步差異.txt")
            if text.exists():
                self.summary.delete("1.0", "end")
                self.summary.insert("end", text.read_text(encoding="utf-8"))

        self._job(f"Register {'預覽' if dry else '正式同步'}", lambda: run_register(p, s, dry_run=dry), done)

    def run_plan(self, dry: bool) -> None:
        ready = self._ready()
        if not ready:
            return
        p, s = ready
        d = self.direction.get()
        if not dry and not messagebox.askyesno("正式同步", f"執行排程正式同步（方向：{d}）？"):
            return
        from .plan_sync import run_plan_sync

        def done(rep):
            self.summary.delete("1.0", "end")
            self.summary.insert("end", rep.text())
            if rep.confluence_page_id and not dry:
                self.load_project(p["project"]["slug"])

        self._job(f"排程同步 {d} {'預覽' if dry else '正式'}", lambda: run_plan_sync(
            p, s, profiles.APP_DIR, direction=d, dry_run=dry, allow_mass_removal=self.allow_mass.get(),
            send_mail=(self.send_mail.get() and not dry), log_dir=profiles.log_dir(p["project"]["slug"], "plan"),
            on_profile_update=profiles.save_profile), done)

    def preview(self) -> None:
        ready = self._ready()
        if not ready:
            return
        p, s = ready
        from .wizard import preview_schedule

        self._job("排程預覽", lambda: preview_schedule(p, s), self._show_schedule)

    def _show_schedule(self, result) -> None:
        tasks, notes = result
        self.tree.delete(*self.tree.get_children())
        for t in tasks:
            self.tree.insert("", "end", values=(t.id, t.group, t.name, t.type, t.duration, t.start, t.finish,
                                                t.status, "Y" if t.sync else "N", t.jira_key),
                             tags=() if t.sync else ("draft",))
        self.sched_note.configure(text=notes[0] if notes else "")
        for n in notes:
            self._log(n + "\n")
        self._draw_gantt(tasks)

    def _draw_gantt(self, tasks) -> None:
        c = self.gantt
        c.delete("all")
        dated = [t for t in tasks if t.start and t.finish]
        if not dated:
            return
        start = min(t.start for t in dated)
        end = max(t.finish for t in dated)
        px, row_h, left = 4, 18, 260
        days = (end - start).days + 1
        colors = {}
        palette = ["#5b9bd5", "#ed7d31", "#70ad47", "#ffc000", "#4472c4", "#a5a5a5", "#9e480e", "#264478", "#43682b"]
        for i, t in enumerate(dated):
            colors.setdefault(t.group, palette[len(colors) % len(palette)])
        d = start
        while d <= end:
            x = left + (d - start).days * px
            if d.weekday() == 0:
                c.create_line(x, 0, x, row_h * (len(dated) + 2), fill="#eee")
                if d.day <= 7:
                    c.create_text(x + 2, 8, text=d.strftime("%Y-%m"), anchor="w", font=("Segoe UI", 8))
            d = date.fromordinal(d.toordinal() + 1)
        for i, t in enumerate(dated):
            y = row_h * (i + 1) + 4
            c.create_text(4, y + 7, text=f"{t.id} {t.name}"[:40], anchor="w", font=("Microsoft JhengHei", 8),
                          fill="#333" if t.sync else "#999")
            x1 = left + (t.start - start).days * px
            x2 = left + ((t.finish - start).days + 1) * px
            if t.duration == 0:
                c.create_polygon(x1, y + 7, x1 + 6, y + 1, x1 + 12, y + 7, x1 + 6, y + 13, fill="#c0392b")
            else:
                c.create_rectangle(x1, y + 2, x2, y + 13, fill=colors[t.group], outline="",
                                   stipple="" if t.sync else "gray50")
        c.configure(scrollregion=(0, 0, left + days * px + 40, row_h * (len(dated) + 2)))

    # ------------------------------------------------------------------ tests / misc
    def test_atlassian(self) -> None:
        ready = self._ready()
        if ready:
            from .wizard import test_atlassian

            self._job("測試 Atlassian", lambda: test_atlassian(*ready))

    def test_google(self) -> None:
        from .wizard import test_google

        s = self.secrets()
        self._job("測試 Google", lambda: test_google(s))

    def test_smtp(self) -> None:
        from .wizard import test_smtp

        s = self.secrets()
        self._job("測試 SMTP", lambda: test_smtp(s))

    def test_mail(self) -> None:
        ready = self._ready()
        if ready:
            from .mailer import send_test_mail

            self._job("寄測試信", lambda: send_test_mail(*ready))

    def test_register_sheet(self) -> None:
        ready = self._ready()
        if ready:
            from .wizard import test_register_sheet

            self._job("測試 S 表", lambda: test_register_sheet(ready[0]))

    def init_plan_xlsx(self) -> None:
        ready = self._ready()
        if ready:
            from .wizard import step_plan_sheet

            use_c27 = ready[0]["project"]["slug"] == "c27"
            self._job("產生計畫表", lambda: step_plan_sheet(*ready, use_c27=use_c27))

    def _browse(self, var: tk.StringVar) -> None:
        path = filedialog.askopenfilename(initialdir=str(profiles.APP_DIR))
        if path:
            var.set(path)

    def open_url(self, url: str) -> None:
        if not url:
            messagebox.showinfo("連結", "尚未設定")
            return
        if url.startswith("http"):
            webbrowser.open(url)
        else:
            os.startfile(url)  # noqa: S606

    def open_sheet(self) -> None:
        from .plan_sync import sheet_link

        if self.profile:
            self.open_url(sheet_link(self.collect(), profiles.APP_DIR))

    def open_plan_page(self) -> None:
        from .plan_sync import confluence_page_url

        p = self.collect()
        self.open_url(confluence_page_url(p["atlassian"]["site_url"], p["plan"].get("confluence_space_key", ""),
                                          str(p["plan"].get("confluence_page_id", ""))))

    def open_register_page(self) -> None:
        from .plan_sync import confluence_page_url

        p = self.collect()
        self.open_url(confluence_page_url(p["atlassian"]["site_url"], p["register"].get("space_key", ""),
                                          str(p["register"].get("confluence_page_id", ""))))

    def open_jira_label(self) -> None:
        from urllib.parse import quote

        p = self.collect()
        jql = f'project = {p["jira"]["project_key"]} AND labels = "{p["plan"]["label"]}" ORDER BY key'
        self.open_url(f'{p["atlassian"]["site_url"].rstrip("/")}/issues/?jql={quote(jql)}')

    def open_logs(self) -> None:
        slug = self.project_var.get()
        path = profiles.LOGS_DIR / slug if slug else profiles.LOGS_DIR
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(str(path))  # noqa: S606

    def open_wizard(self) -> None:
        WizardDialog(self)


def simple_input(parent, title: str, prompt: str, initial: str = "") -> str:
    win = tk.Toplevel(parent)
    win.title(title)
    win.transient(parent)
    var = tk.StringVar(value=initial)
    ttk.Label(win, text=prompt).pack(padx=12, pady=(12, 4), anchor="w")
    e = ttk.Entry(win, textvariable=var, width=40)
    e.pack(padx=12)
    e.focus_set()
    result = {"v": ""}

    def ok():
        result["v"] = var.get().strip()
        win.destroy()

    ttk.Button(win, text="確定", command=ok).pack(pady=10)
    win.bind("<Return>", lambda _: ok())
    win.grab_set()
    parent.wait_window(win)
    return result["v"]


class WizardDialog(tk.Toplevel):
    """開案精靈：收集專案資訊 → 依序建立計畫表 / Confluence 計畫頁 / Jira Epic / Register / 郵件。"""

    FIELDS = [
        ("name", "專案名稱", ""), ("slug", "專案代碼（英數，例 c28）", ""), ("customer", "客戶", ""),
        ("jira_key", "Jira 專案 Key", ""), ("space_key", "Confluence 空間 Key", "projectmod"),
        ("parent", "計畫頁父頁面（ID 或網址）", ""), ("page_title", "計畫頁標題（空白 = <名稱> Project Plan）", ""),
        ("start", "專案開始日 T0 前（YYYY-MM-DD）", ""), ("assignee", "預設受託人 accountId", ""),
        ("to", "郵件 To", "bob.lin@qsitw.com"), ("cc", "郵件 Cc（逗號分隔）", ""),
        ("s_url", "Register S 表網址（選填）", ""), ("c_page", "Register C 表 page id（選填）", ""),
    ]

    def __init__(self, app: App):
        super().__init__(app)
        self.app = app
        self.title("開案精靈")
        self.geometry("900x760")
        self.transient(app)
        from .wizard import WIZARD_STEPS, next_monday

        self.vars = {k: tk.StringVar(value=v) for k, _, v in self.FIELDS}
        self.vars["start"].set(next_monday().isoformat())
        form = ttk.LabelFrame(self, text="專案資訊（假日使用台灣國定假日，可在計畫表 Holidays 分頁修改）", padding=10)
        form.pack(fill="x", padx=10, pady=8)
        for r, (k, label, _) in enumerate(self.FIELDS):
            ttk.Label(form, text=label).grid(row=r, column=0, sticky="w", pady=2)
            ttk.Entry(form, textvariable=self.vars[k], width=70).grid(row=r, column=1, sticky="we")
        form.columnconfigure(1, weight=1)
        opts = ttk.Frame(self)
        opts.pack(fill="x", padx=10)
        self.use_c27 = tk.BooleanVar(value=False)
        self.create_c = tk.BooleanVar(value=False)
        self.send_test = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="使用 C27 完整 WBS 範本", variable=self.use_c27).pack(side="left")
        ttk.Checkbutton(opts, text="沒有 C 表時新建 mail_checking 式頁面", variable=self.create_c).pack(side="left", padx=10)
        ttk.Checkbutton(opts, text="寄測試信", variable=self.send_test).pack(side="left")

        steps = ttk.LabelFrame(self, text="建立項目（每一步可重跑，已存在的不會重建）", padding=10)
        steps.pack(fill="both", expand=True, padx=10, pady=8)
        self.step_lbls: dict[str, ttk.Label] = {}
        for r, (key, title) in enumerate(WIZARD_STEPS):
            ttk.Label(steps, text=title, width=28).grid(row=r, column=0, sticky="w", pady=4)
            lbl = ttk.Label(steps, text="⏳ 尚未執行", width=70, wraplength=520)
            lbl.grid(row=r, column=1, sticky="w")
            ttk.Button(steps, text="執行", command=lambda k=key: self.run_steps([k])).grid(row=r, column=2, padx=4)
            self.step_lbls[key] = lbl
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=10, pady=8)
        ttk.Button(bar, text="建立專案並執行全部步驟", style="Accent.TButton",
                   command=lambda: self.run_steps([k for k, _ in WIZARD_STEPS])).pack(side="left")
        ttk.Button(bar, text="關閉", command=self.destroy).pack(side="right")
        self.profile: dict[str, Any] | None = None

    def build_profile(self) -> dict[str, Any] | None:
        v = {k: var.get().strip() for k, var in self.vars.items()}
        if not v["name"] or not v["jira_key"]:
            messagebox.showwarning("開案精靈", "專案名稱與 Jira 專案 Key 必填", parent=self)
            return None
        slug = profiles.slugify(v["slug"] or v["name"])
        try:
            p = profiles.load_profile(slug)
        except FileNotFoundError:
            p = profiles.default_profile(slug, v["name"])
        p["project"].update({"name": v["name"], "slug": slug, "customer": v["customer"], "project_start": v["start"]})
        p["jira"].update({"project_key": v["jira_key"].upper(), "default_assignee_account_id": v["assignee"],
                          "board_url": f"{p['atlassian']['site_url']}/jira/software/projects/{v['jira_key'].upper()}/timeline"})
        from .wizard import page_id_from

        p["plan"].update({"confluence_space_key": v["space_key"], "confluence_parent_page_id": page_id_from(v["parent"]),
                          "confluence_page_title": v["page_title"] or f"{v['name']} Project Plan",
                          "share_with": [p["atlassian"]["email"]]})
        p["register"].update({"sheet_url": v["s_url"], "confluence_page_id": page_id_from(v["c_page"]),
                              "space_key": v["space_key"], "enabled": bool(v["s_url"] and v["c_page"])})
        p["mail"].update({"to": v["to"], "cc": [x.strip() for x in v["cc"].split(",") if x.strip()]})
        profiles.save_profile(p)
        return p

    def run_steps(self, keys: list[str]) -> None:
        if self.app.busy:
            messagebox.showinfo("執行中", "已有工作執行中", parent=self)
            return
        p = self.build_profile()
        if not p:
            return
        self.profile = p
        secrets = self.app.secrets()
        options = {"use_c27": self.use_c27.get(), "create_c_page": self.create_c.get(),
                   "send_test": self.send_test.get()}
        from .wizard import run_step

        def all_steps():
            results = []
            for k in keys:
                self.app.q.put(("call", lambda k=k: self.step_lbls[k].configure(text="🔄 執行中…")))
                try:
                    msg = run_step(k, p, secrets, options)
                    print(f"✅ {k}: {msg}")
                    self.app.q.put(("call", lambda k=k, m=msg: self.step_lbls[k].configure(text="✅ " + m)))
                except Exception as exc:  # noqa: BLE001
                    print(f"❌ {k}: {exc}")
                    self.app.q.put(("call", lambda k=k, e=exc: self.step_lbls[k].configure(text=f"❌ {e}")))
                results.append(k)
            return f"開案精靈完成 {len(results)} 步"

        self.app._job("開案精靈", all_steps, lambda _: self.app.reload_projects(p["project"]["slug"]))


def run_gui(project: str | None = None) -> None:
    app = App(project)
    app.mainloop()
