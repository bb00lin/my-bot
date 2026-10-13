"""計畫同步：計畫表（xlsx / Google Sheet）⇄ Jira，並輸出 Confluence 計畫頁。

流程（direction=both）：讀表 → 排程 → 讀 Jira → 三方合併（快照 = 上次同步一致的值）
→ 套用 Jira 端變更到表 → 重新排程 → 推送到 Jira（Epic / 任務 / 日期 / 相依）
→ 處理已刪除列 → 回寫表（Start/Finish、Jira Key、衝突、快照）→ 更新 Confluence → 報表 / 寄信。
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

from . import plan_core as pc
from . import plan_store as ps
from . import register_sync as sr

DIRECTIONS = ("both", "push", "pull")
PROTECTED_CONFLUENCE_TITLES = {"mail_checking"}


@dataclass
class PlanReport:
    direction: str = "both"
    dry_run: bool = False
    created_epics: list[str] = field(default_factory=list)
    updated_epics: list[str] = field(default_factory=list)
    created_issues: list[str] = field(default_factory=list)
    updated_issues: list[str] = field(default_factory=list)
    transitioned: list[str] = field(default_factory=list)
    links_added: list[str] = field(default_factory=list)
    links_removed: list[str] = field(default_factory=list)
    removed_issues: list[str] = field(default_factory=list)
    pulled: list[str] = field(default_factory=list)
    new_from_jira: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    schedule_changes: list[str] = field(default_factory=list)
    sheet_cells_written: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    confluence_url: str = ""
    confluence_page_id: str = ""
    confluence_action: str = ""
    sheet_link: str = ""
    schedule_summary: list[str] = field(default_factory=list)
    group_totals: list[tuple[str, int, int, bool]] = field(default_factory=list)

    def has_changes(self) -> bool:
        return any([
            self.created_epics, self.updated_epics, self.created_issues, self.updated_issues, self.transitioned,
            self.links_added, self.links_removed, self.removed_issues, self.pulled, self.new_from_jira,
            self.conflicts, self.schedule_changes,
        ])

    def text(self) -> str:
        mode = f"{self.direction}{'（dry-run，未寫入）' if self.dry_run else ''}"
        lines = [f"========== 計畫同步結果：{mode} =========="]
        lines += self.schedule_summary
        if self.group_totals:
            lines.append("鎖定工期檢查：" + "、".join(
                f"{c}={a}d{'✓' if ok else f'（應為 {e}d ⚠）'}" for c, e, a, ok in self.group_totals))

        def sec(title: str, items: list[str]) -> None:
            if items:
                lines.append(f"{title}（{len(items)}）：")
                lines.extend(f"  - {x}" for x in items)

        sec("新建 Epic", self.created_epics)
        sec("更新 Epic", self.updated_epics)
        sec("新建任務", self.created_issues)
        sec("更新任務", self.updated_issues)
        sec("狀態轉換", self.transitioned)
        sec("新增相依", self.links_added)
        sec("移除相依", self.links_removed)
        sec("已刪除列的 Jira 處理", self.removed_issues)
        sec("Jira → Sheet 拉回", self.pulled)
        sec("Jira 新增 → Sheet 新列", self.new_from_jira)
        sec("衝突（兩邊都改，未覆寫）", self.conflicts)
        sec("待處理（此方向未套用）", self.pending)
        sec("排程變動", self.schedule_changes)
        if self.confluence_url:
            lines.append(f"Confluence：{self.confluence_action} {self.confluence_url}")
        if self.sheet_link:
            lines.append(f"計畫表：{self.sheet_link}")
        lines.append(f"回寫計畫表儲存格：{self.sheet_cells_written}")
        sec("警告", self.warnings)
        sec("錯誤", self.errors)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Profile helpers
# ---------------------------------------------------------------------------


def plan_cfg(profile: dict[str, Any]) -> dict[str, Any]:
    return profile.get("plan") or {}


def plan_label(profile: dict[str, Any]) -> str:
    slug = (profile.get("project") or {}).get("slug", "proj")
    return str(plan_cfg(profile).get("label") or f"{slug}-plan").strip()


def id_prefix(profile: dict[str, Any]) -> str:
    return str(plan_cfg(profile).get("id_prefix") or "PLN").strip().upper()


def plan_id_regex(prefix: str) -> re.Pattern[str]:
    return re.compile(rf"^{re.escape(prefix)}-[A-Z][A-Z0-9]*-\d{{2,3}}$")


def build_client(profile: dict[str, Any], token: str) -> sr.AtlassianClient:
    a = profile["atlassian"]
    return sr.AtlassianClient(a["site_url"], a["cloud_id"], a["email"], token,
                              silent_mode=bool(a.get("silent_mode", True)))


def open_store(profile: dict[str, Any], app_dir: Path, secrets: dict[str, str]):
    p = plan_cfg(profile)
    source = (p.get("source") or "xlsx").lower()
    sa = (secrets.get("GOOGLE_SERVICE_ACCOUNT_FILE") or "").strip()
    url = p.get("google_sheet_url") or ""
    if source == "google":
        if sa and Path(sa).exists():
            return ps.GoogleStore(url, sa)
        print("警告：未設定 Google service account，改用公開匯出（唯讀）")
        return ps.ExportStore(url)
    if source == "export":
        return ps.ExportStore(url)
    path = Path(p.get("xlsx_path") or "")
    if not path.is_absolute():
        path = app_dir / path
    return ps.XlsxStore(path)


def sheet_link(profile: dict[str, Any], app_dir: Path) -> str:
    p = plan_cfg(profile)
    if (p.get("source") or "xlsx") in ("google", "export") and p.get("google_sheet_url"):
        return p["google_sheet_url"]
    path = Path(p.get("xlsx_path") or "")
    return str(path if path.is_absolute() else app_dir / path)


# ---------------------------------------------------------------------------
# Sheet → tasks
# ---------------------------------------------------------------------------

_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
             "一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6}


def parse_workdays(text: str) -> list[int]:
    out = []
    for tok in re.split(r"[,\s、/]+", (text or "").strip().lower()):
        tok = tok.replace("週", "").replace("星期", "")
        if tok[:3] in _WEEKDAYS:
            out.append(_WEEKDAYS[tok[:3]])
        elif tok in _WEEKDAYS:
            out.append(_WEEKDAYS[tok])
    return sorted(set(out)) or [0, 1, 2, 3, 4]


def tasks_from_book(book: ps.PlanBook, prefix: str) -> tuple[list[pc.PlanTask], list[str]]:
    tasks: list[pc.PlanTask] = []
    warnings: list[str] = []
    for row_num, d in book.row_dicts():
        tid = (d.get("ID") or "").strip().upper()
        name = (d.get("Task Name") or "").strip()
        if not tid and not name:
            continue
        dur_text = (d.get("Duration (d)") or "").strip()
        errors = []
        try:
            duration = int(round(float(dur_text))) if dur_text else 1
        except ValueError:
            duration, errors = 1, [f"Duration 不是數字：{dur_text!r}"]
        duration = max(0, duration)
        t = pc.PlanTask(
            id=tid, name=name, group=(d.get("Group") or "").strip().upper(),
            type=pc.norm_type(d.get("Type", ""), duration), duration=duration,
            preds_text=d.get("Predecessors", ""), constraint_text=d.get("Start Constraint", ""),
            priority=pc.norm_priority(d.get("Priority", "")), owner=(d.get("Owner") or "").strip(),
            status=pc.norm_status(d.get("Status", "")), sync=pc.norm_bool(d.get("Sync to Jira")),
            jira_key=(d.get("Jira Key") or "").strip().upper(), description=d.get("Description / 工作內容", ""),
            discussion=d.get("Discussion / Comments", ""), estimate=pc.norm_bool(d.get("Estimate")),
            row=row_num, cells=d, errors=errors,
        )
        if t.type == "Milestone":
            t.duration = 0
        tasks.append(t)
    # 自動編號（ID 空白的新列）
    rx = plan_id_regex(prefix)
    used: dict[str, int] = {}
    for t in tasks:
        m = re.match(rf"^{re.escape(prefix)}-([A-Z][A-Z0-9]*)-(\d+)$", t.id)
        if m:
            used[m.group(1)] = max(used.get(m.group(1), 0), int(m.group(2)))
    for t in tasks:
        if not t.id:
            g = t.group or "GEN"
            used[g] = used.get(g, 0) + 1
            t.id = f"{prefix}-{g}-{used[g]:02d}"
            t.cells["__new_id__"] = t.id
            warnings.append(f"第 {t.row} 列自動編號 {t.id}")
    for t in tasks:
        if pc.REGISTER_ID_RE.match(t.id):
            t.errors.append(f"ID {t.id} 與 Register ID 格式衝突（會被 Register 同步刪除），不同步到 Jira")
            t.sync = False
        elif not rx.match(t.id):
            warnings.append(f"{t.id}: ID 建議格式為 {prefix}-<群組>-<序號>")
    return tasks, warnings


def build_calendar(book: ps.PlanBook, profile: dict[str, Any]) -> tuple[pc.WorkCalendar, date]:
    start = pc.parse_date_any(book.settings.get("Project Start")) or pc.parse_date_any(
        (profile.get("project") or {}).get("project_start")) or date.today()
    cal = pc.WorkCalendar(start, parse_workdays(book.settings.get("Working Days", "")), book.holidays)
    return cal, start


# ---------------------------------------------------------------------------
# Jira side
# ---------------------------------------------------------------------------


@dataclass
class JiraIssue:
    key: str
    summary: str
    status: str
    labels: list[str]
    parent_key: str
    is_epic: bool
    assignee_id: str
    assignee_name: str
    start: str
    due: str
    description_adf: Any
    links: list[dict[str, Any]]
    reporter: str

    @classmethod
    def from_api(cls, issue: dict[str, Any], start_field: str, epic_type: str) -> "JiraIssue":
        f = issue.get("fields") or {}
        it = f.get("issuetype") or {}
        assignee = f.get("assignee") or {}
        return cls(
            key=issue["key"], summary=(f.get("summary") or "").strip(),
            status=pc.norm_jira_status((f.get("status") or {}).get("name", "")),
            labels=list(f.get("labels") or []), parent_key=(f.get("parent") or {}).get("key", ""),
            is_epic=it.get("hierarchyLevel") == 1 or it.get("name") == epic_type,
            assignee_id=assignee.get("accountId", ""), assignee_name=assignee.get("displayName", ""),
            start=(f.get(start_field) or "")[:10], due=(f.get("duedate") or "")[:10],
            description_adf=f.get("description"), links=list(f.get("issuelinks") or []),
            reporter=(f.get("reporter") or {}).get("displayName", ""),
        )


def adf_plain(adf: Any) -> str:
    if not isinstance(adf, dict):
        return ""
    return "\n".join(pc._adf_text(b) for b in adf.get("content", []) or [])


class JiraPlan:
    def __init__(self, client: sr.AtlassianClient, profile: dict[str, Any], dry_run: bool, report: PlanReport):
        j = profile["jira"]
        self.client, self.dry_run, self.report = client, dry_run, report
        self.project = j["project_key"]
        self.epic_type = sr.resolve_issuetype_for_api(j.get("epic_issue_type", "大型工作"), role="epic")
        self.task_type = sr.resolve_issuetype_for_api(j.get("task_issue_type", "任務"), role="task")
        self.start_field = j.get("start_date_field", "customfield_10015")
        self.due_field = j.get("due_date_field", "duedate")
        self.default_assignee = j.get("default_assignee_account_id", "")
        self.site = profile["atlassian"]["site_url"].rstrip("/")
        self.label = plan_label(profile)
        self.removed_label = f"{self.label}-removed"
        self.link_flip = False
        self.link_verified = False
        self._fake = 0
        self._user_cache: dict[str, str] = {}

    @property
    def fields(self) -> list[str]:
        return ["summary", "status", "labels", "parent", "issuetype", "assignee", self.due_field,
                self.start_field, "description", "issuelinks", "reporter"]

    def epic_label(self, code: str) -> str:
        return f"{self.label}-epic-{code.lower()}"

    def fetch(self) -> tuple[dict[str, JiraIssue], dict[str, JiraIssue]]:
        jql = f'project = {self.project} AND labels = "{self.label}"'
        raw = sr._paginate_jira_search(self.client, jql, fields=self.fields)
        epics: dict[str, JiraIssue] = {}
        tasks: dict[str, JiraIssue] = {}
        for it in raw:
            ji = JiraIssue.from_api(it, self.start_field, self.epic_type)
            if ji.is_epic:
                code = next((lb[len(self.label) + 6:].upper() for lb in ji.labels
                             if lb.startswith(f"{self.label}-epic-")), "")
                if code:
                    epics[code] = ji
            else:
                tasks[ji.key] = ji
        if epics:
            keys = ",".join(e.key for e in epics.values())
            jql2 = (f'project = {self.project} AND parent in ({keys}) '
                    f'AND (labels is EMPTY OR labels != "{self.label}")')
            for it in sr._paginate_jira_search(self.client, jql2, fields=self.fields):
                ji = JiraIssue.from_api(it, self.start_field, self.epic_type)
                if not ji.is_epic:
                    tasks[ji.key] = ji
        return epics, tasks

    def get_issue(self, key: str) -> JiraIssue | None:
        try:
            data = self.client.jira_get(f"/issue/{key}", fields=",".join(self.fields))
        except RuntimeError as exc:
            if " 404 " in str(exc) or "404" in str(exc)[:40]:
                return None
            raise
        return JiraIssue.from_api(data, self.start_field, self.epic_type)

    def fake_key(self) -> str:
        self._fake += 1
        return f"NEW-{self._fake}"

    def resolve_user(self, query: str) -> str:
        q = (query or "").strip()
        if not q:
            return ""
        if q in self._user_cache:
            return self._user_cache[q]
        acc = ""
        try:
            users = self.client.jira_get("/user/search", query=q)
            for u in users if isinstance(users, list) else []:
                if u.get("active", True) and u.get("accountType", "atlassian") == "atlassian":
                    acc = u.get("accountId", "")
                    break
        except Exception as exc:  # noqa: BLE001
            print(f"警告：查詢 Jira 使用者 {q!r} 失敗：{exc}")
        self._user_cache[q] = acc
        return acc

    # --- writes -----------------------------------------------------------
    def create(self, fields: dict[str, Any]) -> str:
        if self.dry_run:
            return self.fake_key()
        return self.client.jira_post("/issue", {"fields": fields})["key"]

    def update(self, key: str, fields: dict[str, Any]) -> None:
        if not self.dry_run and fields:
            self.client.jira_put(f"/issue/{key}", {"fields": fields})

    def transition(self, key: str, target: str) -> None:
        sr.transition_issue(self.client, key, target, self.dry_run)

    def add_link(self, pred_key: str, succ_key: str) -> None:
        if self.dry_run:
            return
        inward, outward = (pred_key, succ_key) if self.link_flip else (succ_key, pred_key)
        self.client.jira_post("/issueLink", {
            "type": {"name": "Blocks"}, "inwardIssue": {"key": inward}, "outwardIssue": {"key": outward}})
        if not self.link_verified:
            self.link_verified = True
            succ = self.get_issue(succ_key)
            preds = blocking_keys(succ) if succ else {}
            if pred_key not in preds:
                print("偵測到 Jira link 方向相反，改用反向寫法重建")
                self.link_flip = True
                pred = self.get_issue(pred_key)
                for lk in (pred.links if pred else []):
                    other = (lk.get("inwardIssue") or lk.get("outwardIssue") or {}).get("key")
                    if other == succ_key and (lk.get("type") or {}).get("name") == "Blocks":
                        self.client.jira_delete(f"/issueLink/{lk['id']}")
                self.add_link(pred_key, succ_key)

    def delete_link(self, link_id: str) -> None:
        if not self.dry_run:
            self.client.jira_delete(f"/issueLink/{link_id}")

    def delete_issue(self, key: str) -> None:
        sr.delete_jira_issue(self.client, key, self.dry_run)


def blocking_keys(issue: JiraIssue | None) -> dict[str, str]:
    """回傳 {阻擋此單的 issue key: link id}（= 前置任務）。"""
    out: dict[str, str] = {}
    if not issue:
        return out
    for lk in issue.links:
        if (lk.get("type") or {}).get("name") != "Blocks":
            continue
        other = lk.get("inwardIssue")
        if other:  # 此單 "is blocked by" other
            out[other["key"]] = lk["id"]
    return out


# ---------------------------------------------------------------------------
# Field value extraction (both sides normalised to comparable strings)
# ---------------------------------------------------------------------------


class Resolver:
    def __init__(self, book: ps.PlanBook, jp: JiraPlan):
        self.jp = jp
        self.by_name = {p["name"].strip().casefold(): p for p in book.people}
        self.by_account = {p["account_id"].strip(): p["name"] for p in book.people if p.get("account_id")}

    def owner_account(self, owner: str) -> str | None:
        if not owner.strip():
            return self.jp.default_assignee
        p = self.by_name.get(owner.strip().casefold())
        if p and p.get("account_id"):
            return p["account_id"].strip()
        query = (p or {}).get("email") or owner
        acc = self.jp.resolve_user(query)
        return acc or None

    def owner_name(self, account_id: str, display: str) -> str:
        if not account_id:
            return ""
        return self.by_account.get(account_id, display)


def sheet_values(t: pc.PlanTask, res: Resolver, synced_ids: set[str], jira_value_owner: str) -> dict[str, str]:
    acc = res.owner_account(t.owner)
    return {
        "name": t.name.strip(),
        "description": pc.norm_text(t.description),
        "priority": t.priority,
        "group": t.group,
        "status": pc.SHEET_TO_JIRA_STATUS.get(t.status, "待辦事項"),
        "owner": acc if acc is not None else jira_value_owner,
        "start": t.start.isoformat() if t.start else "",
        "finish": t.finish.isoformat() if t.finish else "",
        "preds": ",".join(sorted({p.id for p in t.preds if p.id in synced_ids})),
    }


def jira_values(ji: JiraIssue, plan_id: str, epic_code_by_key: dict[str, str], key_to_id: dict[str, str],
                label: str) -> dict[str, str]:
    name = ji.summary
    if plan_id and name.upper().startswith(plan_id.upper() + "_"):
        name = name[len(plan_id) + 1:]
    prio = next((lb[5:].upper() for lb in ji.labels if lb.lower().startswith("prio-")), "")
    preds = sorted({key_to_id[k] for k in blocking_keys(ji) if k in key_to_id})
    return {
        "name": name.strip(),
        "description": pc.description_from_adf(ji.description_adf),
        "priority": prio,
        "group": epic_code_by_key.get(ji.parent_key, ""),
        "status": ji.status,
        "owner": ji.assignee_id,
        "start": ji.start,
        "finish": ji.due,
        "preds": ",".join(preds),
    }


def summary_plan_id(summary: str, rx: re.Pattern[str]) -> str:
    prefix = (summary or "").split("_", 1)[0].strip().upper()
    return prefix if rx.match(prefix) else ""


def is_register_summary(summary: str, rx: re.Pattern[str]) -> bool:
    prefix = (summary or "").split("_", 1)[0].strip()
    return bool(sr.REGISTER_ID_RE.match(prefix)) and not rx.match(prefix.upper())


# ---------------------------------------------------------------------------
# Confluence
# ---------------------------------------------------------------------------

_STATUS_COLOUR = {"Not Started": "Grey", "In Progress": "Blue", "Blocked": "Red", "Done": "Green",
                  "Cancelled": "Grey"}


def _lozenge(title: str, colour: str) -> str:
    return (f'<ac:structured-macro ac:name="status"><ac:parameter ac:name="colour">{colour}</ac:parameter>'
            f'<ac:parameter ac:name="title">{html.escape(title)}</ac:parameter></ac:structured-macro>')


def _cell(text: Any) -> str:
    return "<td>" + html.escape(str(text or "")).replace("\n", "<br/>") + "</td>"


def build_confluence_html(
    tasks: list[pc.PlanTask], groups: list[dict[str, str]], epic_keys: dict[str, str], site: str,
    sheet_url: str, synced_at: str, summary_lines: list[str], warnings: list[str], conflicts: list[str],
    show_discussion: bool,
) -> str:
    esc = html.escape
    out = []
    link = (f'<a href="{esc(sheet_url)}">{esc(sheet_url)}</a>' if sheet_url.startswith("http")
            else esc(sheet_url))
    out.append('<ac:structured-macro ac:name="info"><ac:rich-text-body>'
               f"<p><strong>資料來源（唯一來源）：</strong>{link}</p>"
               f"<p><strong>最後同步：</strong>{esc(synced_at)}（由 ProjectSync 自動產生，請勿手動編輯本頁）</p>"
               + "".join(f"<p>{esc(x)}</p>" for x in summary_lines)
               + "</ac:rich-text-body></ac:structured-macro>")
    out.append("<h2>群組摘要</h2><table><tbody><tr><th>Group</th><th>Epic</th><th>Jira</th><th>Start</th>"
               "<th>Finish</th><th>工期合計 (d)</th><th>鎖定</th></tr>")
    for g in groups:
        gt = [t for t in tasks if t.group == g["code"]]
        if not gt:
            continue
        starts = [t.start for t in gt if t.start]
        fins = [t.finish for t in gt if t.finish]
        total = sum(t.duration for t in gt if t.type == "Task" and t.status != "Cancelled")
        locked = g.get("locked", "")
        lock_txt = ""
        if str(locked).strip():
            lock_txt = _lozenge(f"{locked}d ✓", "Green") if str(total) == str(locked).strip() else _lozenge(
                f"應 {locked}d", "Red")
        ek = epic_keys.get(g["code"], "")
        out.append(
            f"<tr>{_cell(g['code'])}{_cell(g['epic'])}"
            f"<td>{f'<a href={chr(34)}{site}/browse/{ek}{chr(34)}>{ek}</a>' if ek else ''}</td>"
            f"{_cell(min(starts) if starts else '')}{_cell(max(fins) if fins else '')}{_cell(total)}"
            f"<td>{lock_txt}</td></tr>")
    out.append("</tbody></table>")
    if conflicts or warnings:
        out.append('<ac:structured-macro ac:name="warning"><ac:rich-text-body>')
        for c in conflicts:
            out.append(f"<p>衝突：{esc(c)}</p>")
        for w in warnings[:40]:
            out.append(f"<p>{esc(w)}</p>")
        out.append("</ac:rich-text-body></ac:structured-macro>")
    out.append("<h2>工作清單</h2><p>灰色「提案/討論中」= Sync to Jira 為 N 的草稿，尚未建立 Jira。"
               "工期旁「(估)」= 估計值待確認。</p>")
    heads = ["ID", "Group", "Task", "Type", "Priority", "Owner", "Duration (d)", "Predecessors", "Start", "Finish",
             "Status", "Jira", "Description / 工作內容"]
    if show_discussion:
        heads.append("Discussion / Comments")
    out.append("<table><tbody><tr>" + "".join(f"<th>{esc(h)}</th>" for h in heads) + "</tr>")
    for t in tasks:
        jira = (f'<a href="{site}/browse/{t.jira_key}">{t.jira_key}</a>' if t.jira_key and t.sync
                else _lozenge("提案/討論中", "Grey") if not t.sync else "")
        dur = f"{t.duration}{' (估)' if t.estimate else ''}"
        row = (f"{_cell(t.id)}{_cell(t.group)}{_cell(t.name)}{_cell(t.type)}{_cell(t.priority)}{_cell(t.owner)}"
               f"{_cell(dur)}{_cell(pc.format_predecessors(t.preds))}"
               f"{_cell(t.start.isoformat() if t.start else '')}{_cell(t.finish.isoformat() if t.finish else '')}"
               f"<td>{_lozenge(t.status, _STATUS_COLOUR.get(t.status, 'Grey'))}</td><td>{jira}</td>"
               f"{_cell(t.description)}")
        if show_discussion:
            row += _cell(t.discussion)
        out.append(f"<tr>{row}</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def confluence_post(client: sr.AtlassianClient, path: str, payload: dict) -> Any:
    r = client.session.post(f"{client.confluence_v2}{path}", json=payload, timeout=60)
    if not r.ok:
        raise RuntimeError(f"Confluence POST {path} failed: {r.status_code} {r.text[:300]}")
    return r.json()


def find_child_page(client: sr.AtlassianClient, parent_id: str, title: str) -> dict | None:
    cursor = None
    for _ in range(20):
        params: dict[str, Any] = {"limit": 100}
        if cursor:
            params["cursor"] = cursor
        data = client.confluence_get(f"/pages/{parent_id}/children", **params)
        for p in data.get("results", []):
            if p.get("title", "").strip() == title.strip():
                return p
        nxt = (data.get("_links") or {}).get("next", "")
        m = re.search(r"cursor=([^&]+)", nxt)
        if not m:
            return None
        cursor = m.group(1)
    return None


def upsert_confluence_page(
    client: sr.AtlassianClient, *, space_id: str, parent_id: str, title: str, page_id: str, body: str,
    dry_run: bool, message: str = "Synced by ProjectSync",
) -> tuple[str, str]:
    """回傳 (page_id, action)。第一次建立、之後更新（minorEdit 靜默）。"""
    if title.strip() in PROTECTED_CONFLUENCE_TITLES or page_id == "189300768":
        raise RuntimeError(f"拒絕寫入受保護頁面（{title} / {page_id}）——那是 Register 的 C 表")
    page = None
    if page_id:
        try:
            page = client.confluence_get(f"/pages/{page_id}", **{"body-format": "storage"})
        except Exception:  # noqa: BLE001
            page = None
    if page is None and parent_id:
        found = find_child_page(client, parent_id, title)
        if found:
            page = client.confluence_get(f"/pages/{found['id']}", **{"body-format": "storage"})
    if page is None:
        if dry_run:
            return "", "將建立"
        created = confluence_post(client, "/pages", {
            "spaceId": space_id, "status": "current", "title": title, "parentId": parent_id,
            "body": {"representation": "storage", "value": body}})
        return created["id"], "已建立"
    if dry_run:
        return page["id"], "將更新"
    client.confluence_put(f"/pages/{page['id']}", {
        "id": page["id"], "status": "current", "title": title,
        "body": {"representation": "storage", "value": body},
        "version": {"number": page["version"]["number"] + 1, "message": message, "minorEdit": True}})
    return page["id"], "已更新"


def confluence_page_url(site: str, space_key: str, page_id: str) -> str:
    return f"{site.rstrip('/')}/wiki/spaces/{space_key}/pages/{page_id}" if page_id else ""


# ---------------------------------------------------------------------------
# Main sync
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def run_plan_sync(
    profile: dict[str, Any],
    secrets: dict[str, str],
    app_dir: Path,
    *,
    direction: str = "both",
    dry_run: bool = False,
    allow_mass_removal: bool = False,
    skip_confluence: bool = False,
    send_mail: bool | None = None,
    log_dir: Path | None = None,
    on_profile_update: Callable[[dict[str, Any]], None] | None = None,
) -> PlanReport:
    if direction not in DIRECTIONS:
        raise ValueError(f"direction 必須是 {DIRECTIONS}")
    report = PlanReport(direction=direction, dry_run=dry_run)
    do_push, do_pull = direction in ("both", "push"), direction in ("both", "pull")
    p = plan_cfg(profile)
    prefix = id_prefix(profile)
    rx = plan_id_regex(prefix)
    token = (secrets.get("ATLASSIAN_API_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("缺少 ATLASSIAN_API_TOKEN（請在「憑證」頁設定）")

    store = open_store(profile, app_dir, secrets)
    print(f"讀取計畫表：{store.describe()}")
    book = store.read()
    report.sheet_link = sheet_link(profile, app_dir)
    tasks, warns = tasks_from_book(book, prefix)
    report.warnings += warns
    print(f"計畫表任務：{len(tasks)}（Sync=Y：{sum(t.sync for t in tasks)}）")
    groups = list(book.groups)
    known_groups = {g["code"] for g in groups}
    for t in tasks:
        if t.group and t.group not in known_groups:
            groups.append({"code": t.group, "epic": f"PLAN-{t.group}", "locked": "", "description": ""})
            known_groups.add(t.group)
            report.warnings.append(f"Group {t.group} 不在 Groups 分頁，暫以 PLAN-{t.group} 為 Epic 名稱")
    group_by_code = {g["code"]: g for g in groups}

    cal, project_start = build_calendar(book, profile)
    pc.resolve_preds(tasks)
    report.warnings += pc.compute_schedule(tasks, cal)
    pre_dates = {t.id: (t.start, t.finish) for t in tasks}

    client = build_client(profile, token)
    jp = JiraPlan(client, profile, dry_run, report)
    print(f"讀取 Jira（label={jp.label}）…")
    epics, jira_tasks = jp.fetch()
    print(f"Jira 計畫 Epic：{len(epics)}、任務：{len(jira_tasks)}")
    epic_code_by_key = {e.key: code for code, e in epics.items()}

    state = dict(book.state or {})
    if not state and log_dir and (log_dir / "plan_state.json").exists():
        state = json.loads((log_dir / "plan_state.json").read_text(encoding="utf-8"))
    meta = dict(state.get("__meta__") or {})
    snaps = {k: v for k, v in state.items() if k != "__meta__"}

    # --- map rows ↔ issues --------------------------------------------------
    by_id = {t.id: t for t in tasks}
    mapped: dict[str, JiraIssue] = {}
    used_keys: set[str] = set()
    for t in tasks:
        if t.jira_key:
            ji = jira_tasks.get(t.jira_key) or jp.get_issue(t.jira_key)
            if ji is None:
                report.warnings.append(f"{t.id}: Jira {t.jira_key} 不存在，將重新建立")
                t.cells["__clear_key__"] = "1"
                t.jira_key = ""
                continue
            if is_register_summary(ji.summary, rx):
                report.errors.append(f"{t.id}: Jira Key {t.jira_key} 是 Register 單，已忽略（請清除該儲存格）")
                t.jira_key = ""
                continue
            mapped[t.id] = ji
            used_keys.add(ji.key)
    for key, ji in jira_tasks.items():
        if key in used_keys or jp.removed_label in ji.labels:
            continue
        pid = summary_plan_id(ji.summary, rx)
        t = by_id.get(pid) if pid else None
        if t and t.id not in mapped:
            mapped[t.id] = ji
            used_keys.add(key)
            t.jira_key = key
            t.cells["__key_found__"] = key
    key_to_id = {ji.key: tid for tid, ji in mapped.items()}

    # --- unmapped Jira issues: removed rows vs created in Jira ---------------
    removed_keys: list[str] = []
    new_rows: list[dict[str, Any]] = []
    for key, ji in jira_tasks.items():
        if key in used_keys or jp.removed_label in ji.labels:
            continue
        if is_register_summary(ji.summary, rx):
            report.warnings.append(f"略過 Register 單 {key}（{ji.summary[:40]}）")
            continue
        if key in snaps:
            removed_keys.append(key)
            continue
        dup_id = summary_plan_id(ji.summary, rx)
        if dup_id and dup_id in by_id:
            other = mapped.get(dup_id)
            report.warnings.append(
                f"重複的計畫單 {key}（ID {dup_id} 已對應 {other.key if other else '表上的列'}），未處理；"
                "請確認後在 Jira 刪除或移除 " + jp.label + " 標籤")
            continue
        if not do_pull:
            report.pending.append(f"{key} 在 Jira 新建，尚未拉回表（請用 pull/both）")
            continue
        code = epic_code_by_key.get(ji.parent_key, "") or "GEN"
        n = max([int(m.group(1)) for x in by_id if (m := re.match(rf"^{re.escape(prefix)}-{code}-(\d+)$", x))]
                + [0]) + 1
        new_id = summary_plan_id(ji.summary, rx) or f"{prefix}-{code}-{n:02d}"
        name = ji.summary.split("_", 1)[1] if summary_plan_id(ji.summary, rx) else ji.summary
        dur = 1
        if ji.start and ji.due:
            dur = max(0, cal.workdays_between(pc.parse_date_any(ji.start), pc.parse_date_any(ji.due)))
        t = pc.PlanTask(
            id=new_id, name=name, group=code if code != "GEN" else "", type="Task", duration=dur,
            constraint_text=ji.start, priority=next((lb[5:].upper() for lb in ji.labels
                                                     if lb.lower().startswith("prio-")), "P2"),
            owner=Resolver(book, jp).owner_name(ji.assignee_id, ji.assignee_name),
            status=pc.JIRA_TO_SHEET_STATUS.get(ji.status, "Not Started"), sync=True, jira_key=key,
            description=pc.description_from_adf(ji.description_adf), row=0,
            cells={"Proposed By": f"Jira：{ji.reporter}"},
        )
        tasks.append(t)
        by_id[t.id] = t
        mapped[t.id] = ji
        key_to_id[key] = t.id
        used_keys.add(key)
        new_rows.append({"task": t})
        report.new_from_jira.append(f"{key} → {t.id} {name}")
    # new rows' Jira links → preds
    for nr in new_rows:
        t = nr["task"]
        t.preds_text = "; ".join(key_to_id[k] for k in blocking_keys(mapped[t.id]) if k in key_to_id)
    if new_rows:
        pc.resolve_preds(tasks)
        report.warnings += pc.compute_schedule(tasks, cal)
        pre_dates.update({nr["task"].id: (nr["task"].start, nr["task"].finish) for nr in new_rows})

    # --- three-way merge ----------------------------------------------------
    res = Resolver(book, jp)
    synced_ids = {t.id for t in tasks if t.sync}
    decisions: dict[str, dict[str, str]] = {}
    conflict_text: dict[str, str] = {}
    pulled_fields: dict[str, list[str]] = {}
    jvals_all: dict[str, dict[str, str]] = {}
    svals_pre: dict[str, dict[str, str]] = {}
    for t in tasks:
        if not t.sync or t.id not in mapped:
            continue
        ji = mapped[t.id]
        jv = jira_values(ji, t.id, epic_code_by_key, key_to_id, jp.label)
        sv = sheet_values(t, res, synced_ids, jv["owner"])
        jvals_all[t.id], svals_pre[t.id] = jv, sv
        snap = (snaps.get(ji.key) or {}).get("fields")
        resolution = pc.parse_resolution(t.cells.get("Sync Conflict", ""))
        dec: dict[str, str] = {}
        notes = []
        for f in pc.MERGE_FIELDS:
            base = snap.get(f) if snap else None
            if f == "preds":
                continue
            d = pc.merge_scalar(f, sv[f], jv[f], base, resolution)
            dec[f] = d
            if d == "conflict":
                notes.append(f"{pc.FIELD_LABELS[f]}: Sheet={sv[f][:40]!r} / Jira={jv[f][:40]!r}")
        decisions[t.id] = dec
        if notes:
            conflict_text[t.id] = "；".join(notes) + "（輸入 use sheet 或 use jira 解決）"
            report.conflicts.append(f"{t.id} {ji.key}: " + "；".join(notes))
        # preds：集合合併
        s_set = set(filter(None, sv["preds"].split(",")))
        j_set = set(filter(None, jv["preds"].split(",")))
        b_set = set(filter(None, snap["preds"].split(","))) if snap and "preds" in snap else None
        result, after_push, after_pull = pc.merge_sets(s_set, j_set, b_set)
        target_sheet = result if do_pull else s_set
        if do_pull and target_sheet != s_set:
            add, rem = target_sheet - s_set, s_set - target_sheet
            preds = [pr for pr in t.preds if pr.id not in rem] + [pc.Pred(x) for x in sorted(add)]
            t.preds_text = pc.format_predecessors(preds)
            t.cells["__set__Predecessors"] = t.preds_text
            pulled_fields.setdefault(t.id, []).append("Predecessors")
        t.cells["__preds_target_jira__"] = ",".join(sorted(result if do_push and do_pull else
                                                          after_push if do_push else j_set))
        t.cells["__preds_snap__"] = ",".join(sorted(result if direction == "both" else
                                                   after_push if do_push else after_pull))

    # 套用 Jira → Sheet（pull）
    for t in tasks:
        dec = decisions.get(t.id)
        if not dec:
            continue
        jv = jvals_all[t.id]
        ji = mapped[t.id]
        for f, d in dec.items():
            if d != "pull":
                continue
            if not do_pull:
                report.pending.append(f"{t.id} {pc.FIELD_LABELS[f]}：Jira 有變更未拉回")
                continue
            if f == "name":
                t.name = jv["name"]
                t.cells["__set__Task Name"] = t.name
            elif f == "description":
                t.description = jv["description"]
                t.cells["__set__Description / 工作內容"] = t.description
            elif f == "priority" and jv["priority"]:
                t.priority = jv["priority"]
                t.cells["__set__Priority"] = t.priority
            elif f == "group" and jv["group"]:
                t.group = jv["group"]
                t.cells["__set__Group"] = t.group
            elif f == "status":
                t.status = pc.JIRA_TO_SHEET_STATUS.get(jv["status"], t.status)
                t.cells["__set__Status"] = t.status
            elif f == "owner":
                t.owner = res.owner_name(ji.assignee_id, ji.assignee_name)
                t.cells["__set__Owner"] = t.owner
            elif f in ("start", "finish"):
                continue
            else:
                continue
            pulled_fields.setdefault(t.id, []).append(pc.FIELD_LABELS[f])
        if do_pull and (dec.get("start") == "pull" or dec.get("finish") == "pull"):
            js, jf = pc.parse_date_any(jv["start"]), pc.parse_date_any(jv["finish"])
            changed = []
            if dec.get("start") == "pull" and js:
                t.constraint_text = js.isoformat()
                t.cells["__set__Start Constraint"] = js.isoformat()
                changed.append("Start→Start Constraint")
            begin = js or t.start
            if jf and begin:
                nd = cal.workdays_between(begin, jf)
                nd = 0 if (t.type == "Milestone" and nd <= 1) else max(1, nd)
                if nd != t.duration:
                    t.cells["__set__Duration (d)"] = str(nd)
                    changed.append(f"Duration {t.duration}→{nd}")
                    t.duration = nd
            if changed:
                pulled_fields.setdefault(t.id, []).append("、".join(changed))
    for tid, fl in pulled_fields.items():
        report.pulled.append(f"{tid} {mapped[tid].key if tid in mapped else ''}: {', '.join(fl)}")

    # 重新排程
    pc.resolve_preds(tasks)
    report.warnings += pc.compute_schedule(tasks, cal)
    for t in tasks:
        if t.errors:
            report.warnings += [f"{t.id}: {e}" for e in t.errors]
        old = (t.cells.get("Start", ""), t.cells.get("Finish", ""))
        new = (t.start.isoformat() if t.start else "", t.finish.isoformat() if t.finish else "")
        if t.row and old != new and any(old):
            report.schedule_changes.append(f"{t.id}: {old[0]}~{old[1]} → {new[0]}~{new[1]}")
    locked = {g["code"]: int(float(g["locked"])) for g in groups if str(g.get("locked", "")).strip()}
    report.group_totals = pc.validate_group_totals(tasks, locked)
    for code, exp, act, ok in report.group_totals:
        if not ok:
            report.warnings.append(f"群組 {code} 工期合計 {act}d ≠ 鎖定值 {exp}d（客戶承諾的估算被改動）")
    report.schedule_summary = schedule_summary(tasks, groups, project_start, profile)

    # --- push to Jira ---------------------------------------------------------
    epic_keys = {code: e.key for code, e in epics.items()}
    if do_push:
        push_epics(jp, tasks, group_by_code, epics, epic_keys, report)
        push_tasks(jp, tasks, mapped, decisions, res, epic_keys, group_by_code, report, synced_ids, profile)
        push_links(jp, tasks, mapped, report)
        handle_removed(jp, removed_keys, jira_tasks, snaps, profile, report, allow_mass_removal, len(jira_tasks))
    elif removed_keys:
        report.pending += [f"{k} 已從表中刪除（push/both 時才處理）" for k in removed_keys]

    # --- snapshot -------------------------------------------------------------
    new_snaps: dict[str, Any] = {}
    for t in tasks:
        if not t.sync or not t.jira_key or t.jira_key.startswith("NEW-"):
            continue
        sv = sheet_values(t, res, synced_ids, (jvals_all.get(t.id) or {}).get("owner", ""))
        dec = decisions.get(t.id)
        old = ((snaps.get(t.jira_key) or {}).get("fields")) or {}
        jv = jvals_all.get(t.id)
        fields_snap = {}
        for f in pc.MERGE_FIELDS:
            if f == "preds":
                fields_snap[f] = t.cells.get("__preds_snap__", sv["preds"]) if dec else sv["preds"]
                continue
            if not dec:  # 新建
                fields_snap[f] = sv[f]
                continue
            d = dec.get(f, "same")
            if d == "same":
                fields_snap[f] = sv[f] if (do_push or sv[f] == jv[f]) else old.get(f, jv[f])
            elif d == "push":
                fields_snap[f] = sv[f] if do_push else old.get(f, jv[f])
            elif d == "pull":
                fields_snap[f] = jv[f] if do_pull else old.get(f, sv[f])
            else:
                fields_snap[f] = old.get(f, "")
            if do_push and d in ("same", "pull") and f in ("start", "finish"):
                fields_snap[f] = sv[f]
        new_snaps[t.jira_key] = {"id": t.id, "fields": fields_snap}
    for k in snaps:
        if k not in new_snaps and k not in removed_keys and k in used_keys:
            new_snaps[k] = snaps[k]
    if not do_push:
        for k in removed_keys:
            new_snaps[k] = snaps[k]

    # --- Confluence -------------------------------------------------------------
    stamp = _now()
    pcfg = plan_cfg(profile)
    body = build_confluence_html(
        tasks, groups, epic_keys, jp.site, report.sheet_link, stamp, report.schedule_summary,
        report.warnings, report.conflicts, bool(pcfg.get("show_discussion_on_confluence", True)))
    body_hash = hashlib.sha256(body.replace(stamp, "").encode("utf-8")).hexdigest()
    if not skip_confluence and do_push:
        try:
            if body_hash == meta.get("confluence_hash") and (pcfg.get("confluence_page_id") or meta.get("page_id")):
                report.confluence_page_id = pcfg.get("confluence_page_id") or meta.get("page_id")
                report.confluence_action = "內容無變更，未更新"
            else:
                pid, action = upsert_confluence_page(
                    client, space_id=str(pcfg.get("confluence_space_id", "")),
                    parent_id=str(pcfg.get("confluence_parent_page_id", "")),
                    title=pcfg.get("confluence_page_title", "Project Plan"),
                    page_id=str(pcfg.get("confluence_page_id") or meta.get("page_id") or ""),
                    body=body, dry_run=dry_run)
                report.confluence_page_id, report.confluence_action = pid, action
                if pid and not dry_run:
                    meta["confluence_hash"] = body_hash
            if report.confluence_page_id:
                meta["page_id"] = report.confluence_page_id
                report.confluence_url = confluence_page_url(
                    jp.site, pcfg.get("confluence_space_key", ""), report.confluence_page_id)
                if not dry_run and pcfg.get("confluence_page_id") != report.confluence_page_id and on_profile_update:
                    pcfg["confluence_page_id"] = report.confluence_page_id
                    on_profile_update(profile)
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"Confluence：{exc}")

    # --- write back to sheet -------------------------------------------------------
    changes = ps.SheetChanges()
    for t in tasks:
        rec: dict[str, Any] = {}
        for k, v in list(t.cells.items()):
            if k.startswith("__set__"):
                rec[k[7:]] = v
        if "__new_id__" in t.cells:
            rec["ID"] = t.id
        new_start = t.start.isoformat() if t.start else ""
        new_fin = t.finish.isoformat() if t.finish else ""
        if t.cells.get("Start", "") != new_start:
            rec["Start"] = new_start
        if t.cells.get("Finish", "") != new_fin:
            rec["Finish"] = new_fin
        key = t.jira_key if not t.jira_key.startswith("NEW-") else ""
        if t.sync and (t.cells.get("Jira Key", "") or "").upper() != key and (key or "__clear_key__" in t.cells):
            rec["Jira Key"] = key
        conflict = conflict_text.get(t.id, "")
        if (t.cells.get("Sync Conflict", "") or "") != conflict:
            rec["Sync Conflict"] = conflict
        touched = t.id in pulled_fields or any(t.jira_key and t.jira_key in x for x in
                                                report.created_issues + report.updated_issues)
        if touched:
            rec["Last Sync"] = stamp
        if t.row == 0:
            row = {h: "" for h in ps.TASK_HEADERS}
            row.update({
                "ID": t.id, "Group": t.group, "Task Name": t.name, "Type": t.type, "Priority": t.priority,
                "Owner": t.owner, "Duration (d)": str(t.duration), "Predecessors": t.preds_text,
                "Start Constraint": t.constraint_text, "Start": new_start, "Finish": new_fin,
                "Status": t.status, "Sync to Jira": "Y", "Jira Key": key,
                "Description / 工作內容": t.description, "Proposed By": t.cells.get("Proposed By", ""),
                "Estimate": "N", "Last Sync": stamp,
            })
            changes.appends.append(row)
        else:
            for h, v in rec.items():
                changes.cells.append((t.row, h, v))
    if do_push or do_pull:
        new_meta = dict(meta)
        new_meta["last_sync"] = stamp
        new_meta["direction"] = direction
        new_snaps["__meta__"] = new_meta
        changes.state = new_snaps
    changes.settings = {"Last Sync": f"{stamp}（{direction}）"}
    if report.confluence_url:
        changes.settings["Confluence Page"] = report.confluence_url
    report.sheet_cells_written = len(changes.cells) + sum(len(a) for a in changes.appends)
    if dry_run:
        print(f"[dry-run] 計畫表將回寫 {len(changes.cells)} 格、新增 {len(changes.appends)} 列")
        for r, h, v in changes.cells[:30]:
            print(f"  [dry-run] 第 {r} 列 {h} = {str(v)[:60]}")
    elif not book.writable:
        report.warnings.append("計畫表為唯讀來源（公開匯出），未回寫；快照存在本機 logs")
    else:
        try:
            store.write(book, changes)
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"回寫計畫表失敗：{exc}")
    if log_dir and not dry_run and changes.state is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "plan_state.json").write_text(
            json.dumps(changes.state, ensure_ascii=False, indent=1), encoding="utf-8")

    # --- log / mail -------------------------------------------------------------------
    text = report.text()
    print(text)
    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp_f = datetime.now().strftime("%Y%m%d_%H%M%S")
        (log_dir / f"plan_{stamp_f}{'_dryrun' if dry_run else ''}.log").write_text(text, encoding="utf-8")
        (log_dir / "最新計畫同步結果.txt").write_text(text, encoding="utf-8")
    mail_cfg = profile.get("mail") or {}
    if send_mail is None:
        send_mail = bool(mail_cfg.get("send_on_real_run", True)) and not dry_run
    has_smtp = bool(secrets.get("MAIL_USERNAME") and secrets.get("MAIL_PASSWORD"))
    has_graph = bool(secrets.get("GRAPH_TENANT_ID") and secrets.get("GRAPH_CLIENT_ID") and secrets.get("GRAPH_CLIENT_SECRET"))
    if send_mail and not dry_run and not (has_smtp or has_graph):
        print("⚠ 未設定寄件憑證（MAIL_USERNAME / MAIL_PASSWORD），略過寄送報告")
        send_mail = False
    if send_mail and not dry_run:
        from . import mailer

        verdict = "有差異" if report.has_changes() else "無差異"
        name = (profile.get("project") or {}).get("name", "")
        try:
            mailer.send_report(profile, secrets, subject=f"[{name} 計畫同步] {verdict} {stamp}",
                               body=text, verdict=verdict)
            print("計畫同步報告已寄出")
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"寄信失敗：{exc}")
    return report


def schedule_summary(tasks: list[pc.PlanTask], groups: list[dict[str, str]], project_start: date,
                     profile: dict[str, Any]) -> list[str]:
    if not tasks:
        return []
    t0_id = plan_cfg(profile).get("t0_task_id", "")
    t0 = next((t for t in tasks if t.id == t0_id), None)
    finish = max(t.finish for t in tasks if t.finish)
    lines = [f"Project Start：{project_start}" + (f"；T0（{t0.id}）：{t0.finish}" if t0 else "")
             + f"；整體完成：{finish}"]
    parts = []
    for g in groups:
        gt = [t for t in tasks if t.group == g["code"] and t.finish]
        if gt:
            parts.append(f"{g['code']} {min(t.start for t in gt)}~{max(t.finish for t in gt)}")
    if parts:
        lines.append("各群組：" + "；".join(parts))
    return lines


# ---------------------------------------------------------------------------
# Push helpers
# ---------------------------------------------------------------------------


def push_epics(jp: JiraPlan, tasks, group_by_code, epics, epic_keys, report: PlanReport) -> None:
    for code, g in group_by_code.items():
        gt = [t for t in tasks if t.group == code and t.sync and t.start]
        if not gt:
            continue
        start = min(t.start for t in gt).isoformat()
        due = max(t.finish for t in gt).isoformat()
        summary = g["epic"]
        labels = [jp.label, jp.epic_label(code)]
        ex = epics.get(code)
        fields: dict[str, Any] = {}
        if ex is None:
            fields = {"project": {"key": jp.project}, "issuetype": {"name": jp.epic_type}, "summary": summary,
                      "labels": labels, jp.start_field: start, jp.due_field: due,
                      "description": pc.build_description_adf(g.get("description", ""), [f"Group: {code}"])}
            try:
                key = jp.create(fields)
                epic_keys[code] = key
                report.created_epics.append(f"{key} {summary}")
            except Exception as exc:  # noqa: BLE001
                report.errors.append(f"建立 Epic {summary}：{exc}")
            continue
        if ex.summary != summary:
            fields["summary"] = summary
        if ex.start != start:
            fields[jp.start_field] = start
        if ex.due != due:
            fields[jp.due_field] = due
        if fields:
            try:
                jp.update(ex.key, fields)
                report.updated_epics.append(f"{ex.key} {', '.join(fields)}")
            except Exception as exc:  # noqa: BLE001
                report.errors.append(f"更新 Epic {ex.key}：{exc}")


def desired_labels(jp: JiraPlan, t: pc.PlanTask, existing: list[str]) -> list[str]:
    keep = [lb for lb in existing if not (lb == jp.label or lb.startswith(jp.label + "-")
                                          or lb.lower().startswith("prio-"))]
    managed = [jp.label, f"{jp.label}-{t.group.lower()}" if t.group else "", f"prio-{t.priority}"]
    if t.type == "Milestone":
        managed.append(f"{jp.label}-milestone")
    if t.type == "Procurement":
        managed.append(f"{jp.label}-procurement")
    if t.estimate:
        managed.append(f"{jp.label}-estimate")
    return sorted(set(keep + [m for m in managed if m]))


def footer_lines(t: pc.PlanTask, profile: dict[str, Any]) -> list[str]:
    return [
        f"Plan ID: {t.id}　Group: {t.group}　Type: {t.type}　Priority: {t.priority}",
        f"Duration: {t.duration} 工作天{'（估計值，待確認）' if t.estimate else ''}",
        f"Predecessors: {pc.format_predecessors(t.preds) or '-'}",
        f"Start Constraint: {t.constraint_text or '-'}",
        "由 ProjectSync 依計畫表同步；名稱/描述/工期/前置請在計畫表修改。",
    ]


def push_tasks(jp: JiraPlan, tasks, mapped, decisions, res: Resolver, epic_keys, group_by_code,
               report: PlanReport, synced_ids: set[str], profile) -> None:
    for t in tasks:
        if not t.sync:
            continue
        dec = decisions.get(t.id, {})

        def allowed(f: str) -> bool:
            return dec.get(f, "push") in ("push", "same") or (f in ("start", "finish") and dec.get(f) == "pull")

        acc = res.owner_account(t.owner)
        if acc is None:
            report.warnings.append(f"{t.id}: Owner {t.owner!r} 找不到 Jira 帳號（請在 People 分頁填 Jira Account ID）")
        epic_key = epic_keys.get(t.group, "")
        desc = pc.build_description_adf(t.description, footer_lines(t, profile))
        summary = f"{t.id}_{t.name}".replace("\n", " ")[:255]
        start = t.start.isoformat() if t.start else None
        due = t.finish.isoformat() if t.finish else None
        ji = mapped.get(t.id)
        if ji is None:
            fields: dict[str, Any] = {
                "project": {"key": jp.project}, "issuetype": {"name": jp.task_type}, "summary": summary,
                "labels": desired_labels(jp, t, []), "description": desc,
                jp.start_field: start, jp.due_field: due,
            }
            if epic_key and not epic_key.startswith("NEW-"):
                fields["parent"] = {"key": epic_key}
            if acc:
                fields["assignee"] = {"accountId": acc}
            try:
                key = jp.create(fields)
                t.jira_key = key
                report.created_issues.append(f"{key} {summary}")
                target = pc.SHEET_TO_JIRA_STATUS.get(t.status, "待辦事項")
                if target != "待辦事項":
                    jp.transition(key, target)
                    report.transitioned.append(f"{key} → {target}")
                mapped[t.id] = JiraIssue(key, summary, target, fields["labels"], epic_key, False, acc or "", "",
                                         start or "", due or "", desc, [], "")
            except Exception as exc:  # noqa: BLE001
                report.errors.append(f"建立 {t.id}：{exc}")
            continue
        fields = {}
        if allowed("name") and ji.summary != summary:
            fields["summary"] = summary
        if allowed("description") and adf_plain(ji.description_adf) != adf_plain(desc):
            fields["description"] = desc
        labels = desired_labels(jp, t, ji.labels)
        if jp.removed_label in labels:
            labels.remove(jp.removed_label)
        if allowed("priority") and sorted(ji.labels) != labels:
            fields["labels"] = labels
        if allowed("start") and ji.start != (start or ""):
            fields[jp.start_field] = start
        if allowed("finish") and ji.due != (due or ""):
            fields[jp.due_field] = due
        if allowed("group") and epic_key and not epic_key.startswith("NEW-") and ji.parent_key != epic_key:
            fields["parent"] = {"key": epic_key}
        if allowed("owner") and acc is not None and ji.assignee_id != acc:
            fields["assignee"] = {"accountId": acc} if acc else None
        if fields:
            try:
                jp.update(ji.key, fields)
                report.updated_issues.append(f"{ji.key} {t.id}: {', '.join(sorted(fields))}")
            except Exception as exc:  # noqa: BLE001
                report.errors.append(f"更新 {ji.key}：{exc}")
        target = pc.SHEET_TO_JIRA_STATUS.get(t.status, "待辦事項")
        if allowed("status") and dec.get("status") == "push" and ji.status != target:
            try:
                jp.transition(ji.key, target)
                report.transitioned.append(f"{ji.key} {ji.status} → {target}")
            except Exception as exc:  # noqa: BLE001
                report.errors.append(f"狀態 {ji.key}：{exc}")


def push_links(jp: JiraPlan, tasks, mapped, report: PlanReport) -> None:
    id_to_key = {tid: ji.key for tid, ji in mapped.items()}
    for t in tasks:
        if not t.sync or t.id not in mapped:
            continue
        ji = mapped[t.id]
        target_ids = t.cells.get("__preds_target_jira__")
        if target_ids is None:  # 新建：用表上的前置
            target = {id_to_key[p.id] for p in t.preds if p.id in id_to_key and
                      next((x for x in tasks if x.id == p.id and x.sync), None)}
        else:
            target = {id_to_key[x] for x in filter(None, target_ids.split(",")) if x in id_to_key}
        current = blocking_keys(ji)
        plan_keys = set(id_to_key.values())
        for pk in sorted(target - set(current)):
            try:
                jp.add_link(pk, ji.key)
                report.links_added.append(f"{pk} blocks {ji.key}")
            except Exception as exc:  # noqa: BLE001
                report.errors.append(f"建立相依 {pk}→{ji.key}：{exc}")
        for pk, link_id in current.items():
            if pk in plan_keys and pk not in target:
                try:
                    jp.delete_link(link_id)
                    report.links_removed.append(f"{pk} blocks {ji.key}")
                except Exception as exc:  # noqa: BLE001
                    report.errors.append(f"移除相依 {pk}→{ji.key}：{exc}")


def handle_removed(jp: JiraPlan, removed_keys: list[str], jira_tasks, snaps, profile, report: PlanReport,
                   allow_mass: bool, total: int) -> None:
    if not removed_keys:
        return
    limit = max(5, int(total * 0.3))
    if len(removed_keys) > limit and not allow_mass:
        report.errors.append(
            f"有 {len(removed_keys)} 張計畫單在表中已不存在（超過安全上限 {limit}），本次不處理。"
            "若確定是刪除，請勾選「允許大量移除」再執行。")
        return
    mode = (plan_cfg(profile).get("on_removed") or "close").lower()
    for key in removed_keys:
        ji = jira_tasks[key]
        try:
            if mode == "delete":
                jp.delete_issue(key)
                report.removed_issues.append(f"{key} 已刪除（{ji.summary[:40]}）")
            else:
                labels = sorted(set(ji.labels) | {jp.removed_label})
                jp.update(key, {"labels": labels})
                if ji.status != "完成":
                    jp.transition(key, "完成")
                report.removed_issues.append(f"{key} 已關閉並加標籤 {jp.removed_label}（{ji.summary[:40]}）")
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"處理已刪除 {key}：{exc}")
