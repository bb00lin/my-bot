#!/usr/bin/env python3
"""Offline verification for sync_plan.py and the sync_register.py plan-collision guard.

No credentials, no network: Jira/Confluence/Sheet are replaced with in-memory fakes.
"""

from __future__ import annotations

import copy
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

os.environ.setdefault("ATLASSIAN_API_TOKEN", "dummy")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import sync_plan as sp
import sync_register as sr

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
    else:
        FAIL += 1
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"
    if detail:
        line += f" — {detail}"
    print(line)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def mk(tid: str, dur: int, preds: str = "", constraint: str = "", group: str = "AT", sync: bool = True) -> sp.PlanTask:
    return sp.PlanTask(row=0, id=tid, cells={}, group=group, name=tid, duration=dur, preds_text=preds,
                       constraint_text=constraint, sync=sync,
                       type=sp.TYPE_MILESTONE if dur == 0 else sp.TYPE_TASK)


def schedule(tasks: list[sp.PlanTask], start: date = date(2026, 10, 19), holidays=()) -> dict[str, sp.PlanTask]:
    sp.resolve_predecessors(tasks)
    cal = sp.WorkCalendar(start, {0, 1, 2, 3, 4}, set(holidays))
    sp.compute_schedule(tasks, cal)
    return {t.id: t for t in tasks}


# ---------------------------------------------------------------------------
# 1. predecessor parsing
# ---------------------------------------------------------------------------

def test_parsing() -> None:
    known = {"PLN-AT-01", "PLN-FX-02", "PLN-WZ-03", "PLN-CB1-01", "PLN-CB1-010"}
    preds, errs = sp.parse_predecessors("PLN-AT-01FS+1d; PLN-FX-02SS, PLN-WZ-03FF-2d", known)
    check("1.1 解析 FS+1d / SS / FF-2d", not errs and preds == [
        sp.Pred("PLN-AT-01", "FS", 1), sp.Pred("PLN-FX-02", "SS", 0), sp.Pred("PLN-WZ-03", "FF", -2)], str(preds))
    preds, errs = sp.parse_predecessors("PLN-AT-01", known)
    check("1.2 省略類型預設 FS、lag 0", preds == [sp.Pred("PLN-AT-01", "FS", 0)] and not errs)
    preds, errs = sp.parse_predecessors("pln-at-01sf+1w，PLN-FX-02 ss -3 days", known)
    check("1.3 大小寫、週=5d、全形逗號、空白", preds == [sp.Pred("PLN-AT-01", "SF", 5), sp.Pred("PLN-FX-02", "SS", -3)],
          str(preds) + str(errs))
    preds, errs = sp.parse_predecessors("PLN-AT-01+2d", known)
    check("1.4 只有 lag 時類型為 FS", preds == [sp.Pred("PLN-AT-01", "FS", 2)])
    preds, errs = sp.parse_predecessors("PLN-ZZ-99; PLN-AT-01XX", known)
    check("1.5 未知 ID 與語法錯誤回報", len(errs) == 2 and not preds, str(errs))
    preds, errs = sp.parse_predecessors("PLN-CB1-010SS", known)
    check("1.6 最長 ID 優先比對", preds == [sp.Pred("PLN-CB1-010", "SS", 0)], str(preds))
    preds, errs = sp.parse_predecessors("PLN-AT-01FS+1.5d", known)
    check("1.7 非整數 lag 報錯", bool(errs) and not preds)
    preds, errs = sp.parse_predecessors("PLN-AT-01FS-2d; PLN-AT-01FS-2d")
    check("1.8 無 known_ids fallback + 去重", preds == [sp.Pred("PLN-AT-01", "FS", -2)] and not errs, str(preds))
    check("1.9 format 往返", sp.format_predecessors([sp.Pred("A", "FS", 0), sp.Pred("B", "SS", 2), sp.Pred("C", "FF", -1)])
          == "A; BSS+2d; CFF-1d")


# ---------------------------------------------------------------------------
# 2. calendar & scheduler
# ---------------------------------------------------------------------------

def test_scheduler() -> None:
    cal = sp.WorkCalendar(date(2026, 10, 17), {0, 1, 2, 3, 4}, {date(2026, 10, 26)})
    check("2.1 開始日落在週六 → 順延週一", cal.base == date(2026, 10, 19))
    check("2.2 假日跳過", cal.date_of(5) == date(2026, 10, 27), str(cal.date_of(5)))
    check("2.3 index_of 往返", cal.index_of(date(2026, 10, 27)) == 5 and cal.index_of(date(2026, 10, 26)) == 5)
    check("2.4 workdays_between 含頭尾", cal.workdays_between(date(2026, 10, 19), date(2026, 10, 30)) == 9)
    check("2.5 負索引", cal.date_of(-1) == date(2026, 10, 16))

    t = schedule([mk("A", 3), mk("B", 2, "AFS")])
    check("2.6 FS：B 在 A 完成次日開始", t["A"].finish == date(2026, 10, 21) and t["B"].start == date(2026, 10, 22))
    t = schedule([mk("A", 3), mk("B", 2, "AFS+2d")])
    check("2.7 FS+2d", t["B"].start == date(2026, 10, 26))
    t = schedule([mk("A", 3), mk("B", 2, "AFS-1d")])
    check("2.8 FS-1d（重疊一天）", t["B"].start == date(2026, 10, 21))
    t = schedule([mk("A", 5), mk("B", 2, "ASS+1d")])
    check("2.9 SS+1d", t["B"].start == date(2026, 10, 20))
    t = schedule([mk("A", 5), mk("B", 2, "AFF")])
    check("2.10 FF：同日完成", t["B"].finish == t["A"].finish == date(2026, 10, 23))
    t = schedule([mk("A", 1, "", "2026-10-26"), mk("B", 3, "ASF")])
    check("2.11 SF：B 在 A 開始前完成", t["B"].finish < t["A"].start or t["B"].finish == date(2026, 10, 23),
          f"A={t['A'].start} B={t['B'].start}~{t['B'].finish}")
    t = schedule([mk("A", 2), mk("B", 2, "A", "2026-11-02")])
    check("2.12 SNET 限制日晚於相依", t["B"].start == date(2026, 11, 2))
    t = schedule([mk("A", 10), mk("B", 2, "A", "MSO 2026-10-20")])
    check("2.13 MSO 強制開始日", t["B"].start == date(2026, 10, 20))
    t = schedule([mk("A", 5), mk("M", 0, "A"), mk("B", 1, "M")])
    check("2.14 里程碑顯示在前置完成日、後續次日開始",
          t["M"].finish == t["A"].finish and t["B"].start == date(2026, 10, 26), f"M={t['M'].finish} B={t['B'].start}")
    t = schedule([mk("M", 0, "", "2026-10-21"), mk("B", 1, "M")])
    check("2.15 限制日里程碑顯示在限制日", t["M"].finish == date(2026, 10, 21) and t["B"].start == date(2026, 10, 21))
    t = schedule([mk("A", 3), mk("B", 3, "A")], holidays=[date(2026, 10, 22)])
    check("2.16 假日延後", t["B"].start == date(2026, 10, 23) and t["B"].finish == date(2026, 10, 27))
    try:
        schedule([mk("A", 1, "C"), mk("B", 1, "A"), mk("C", 1, "B")])
        check("2.17 循環相依丟出 ScheduleError", False)
    except sp.ScheduleError as exc:
        check("2.17 循環相依丟出 ScheduleError", "A" in str(exc) and "C" in str(exc), str(exc))
    tasks = [mk("A", 1, "A")]
    schedule(tasks)
    check("2.18 自我參照記錄為錯誤", any("自己" in e for e in tasks[0].errors))
    check("2.19 Working Days 解析", sp.parse_working_days("Mon-Fri") == {0, 1, 2, 3, 4}
          and sp.parse_working_days("Mon,Tue,Wed,Thu,Fri,Sat") == {0, 1, 2, 3, 4, 5})


# ---------------------------------------------------------------------------
# 3. seed plan
# ---------------------------------------------------------------------------

def test_seed() -> None:
    tabs = sp.seed_tabs()
    data = sp.compute_seed_schedule(tabs)
    totals = {c: (e, a, ok) for c, e, a, ok in sp.validate_group_totals(data.tasks, data.groups)}
    want = {"AT": 25, "PT": 15, "FX": 40, "WZ": 20}
    check("3.1 AT=5W PT=3W FX=8W WZ=4W 工期合計不變",
          all(totals.get(k, (0, 0, False))[1] == v and totals[k][2] for k, v in want.items()), str(totals))
    cal = sp.project_calendar(data)
    spans = sp.group_spans(data.tasks)
    span_ok = all(cal.workdays_between(*spans[k]) == v for k, v in want.items())
    check("3.2 四組依序不重疊、群組跨度 = 承諾工期", span_ok,
          str({k: cal.workdays_between(*spans[k]) for k in want}))
    order = [spans[k] for k in ("AT", "PT", "FX", "WZ")]
    check("3.3 AT→PT→FX→WZ 不重疊", all(order[i][1] < order[i + 1][0] for i in range(3)))
    t0 = next(t for t in data.tasks if t.id == "PLN-G-05")
    at1 = next(t for t in data.tasks if t.id == "PLN-AT-01")
    check("3.4 AT-01 在 T0 次一工作日開始", cal.index_of(at1.start) == cal.index_of(t0.finish) + 1,
          f"T0={t0.finish} AT-01={at1.start}")
    ids = [t.id for t in data.tasks]
    check("3.5 所有 ID 符合 PLN 格式", all(sp.PLAN_ID_RE.match(i) for i in ids))
    check("3.6 新增群組全部標 Estimate=Y",
          all(t.estimate for t in data.tasks if t.group in ("PR", "DOE", "BT", "CB")))
    check("3.7 無前置任務錯誤", not any(t.errors for t in data.tasks), str([t.errors for t in data.tasks if t.errors]))


# ---------------------------------------------------------------------------
# 4. register collision guard
# ---------------------------------------------------------------------------

class FakeSearchClient:
    def __init__(self, issues):
        self.issues = issues

    def jira_search(self, jql, *, max_results=100, fields=None, next_page_token=None):
        return {"issues": copy.deepcopy(self.issues), "isLast": True}


def test_register_guard() -> None:
    import plan_seed_c27 as seed

    ids = [r["ID"] for r in seed.build_seed_tasks()]
    leaked = [i for i in ids if sr.parse_register_id_from_summary(f"{i}_x")]
    check("4.1 計畫 ID 前綴不符合 Register regex", not leaked, str(leaked))
    check("4.2 PLN-AT-01 不符合 ^[A-Z][A-Z0-9]*-\\d+$", not sr.REGISTER_ID_RE.match("PLN-AT-01"))
    issues = [
        {"key": "PMWC-10", "fields": {"summary": "RF-03_XO trim", "status": {"name": "待辦事項"}, "labels": []}},
        {"key": "PMWC-11", "fields": {"summary": "AT-01_plan item with register-like id", "status": {"name": "待辦事項"},
                                      "labels": ["c27-plan"]}},
        {"key": "PMWC-12", "fields": {"summary": "PLN-01_short plan id", "status": {"name": "待辦事項"}, "labels": []}},
        {"key": "PMWC-13", "fields": {"summary": "EVT2-01_DOE", "status": {"name": "完成"}, "labels": ["other"]}},
    ]
    index = sr.load_jira_register_index(FakeSearchClient(issues), "PMWC", "任務")
    check("4.3 label c27-plan 的單不進 Register 索引（不會被刪）", "AT-01" not in index, str(sorted(index)))
    check("4.4 PLN- 前綴的單不進 Register 索引", "PLN-01" not in index)
    check("4.5 Register 單仍正常索引", set(index) == {"RF-03", "EVT2-01"}, str(sorted(index)))
    index2 = sr.load_jira_register_index(FakeSearchClient(issues), "PMWC", "任務", exclude_labels=["other"])
    check("4.6 exclude_labels 可設定", "EVT2-01" not in index2 and "RF-03" in index2)
    v = sp.JiraIssueView("PMWC-10", "RF-03_XO trim", "", "RF-03_XO trim", "", [], "", "", "", "", "", "", [], False)
    check("4.7 計畫同步辨識 Register 單並略過", sp.looks_like_register_issue(v))
    v2 = sp.JiraIssueView("PMWC-90", "PLN-AT-01_x", "PLN-AT-01", "x", "", ["c27-plan"], "", "", "", "", "", "", [], False)
    check("4.8 計畫單不被誤判為 Register", not sp.looks_like_register_issue(v2))
    try:
        sp.validate_ids([mk("AT-01", 1)])
        check("4.9 Sheet 中 Register 格式 ID 被拒", False)
    except sp.PlanError:
        check("4.9 Sheet 中 Register 格式 ID 被拒", True)


# ---------------------------------------------------------------------------
# 5. merge rules
# ---------------------------------------------------------------------------

def test_merge() -> None:
    m = sp.merge_scalar
    check("5.1 相同 → same", m("name", "a", "a", "x") == "same")
    check("5.2 只有 Sheet 改 → push", m("name", "new", "old", "old") == "push")
    check("5.3 只有 Jira 改 → pull", m("name", "old", "new", "old") == "pull")
    check("5.4 兩邊都改 → conflict", m("name", "s", "j", "old") == "conflict")
    check("5.5 無快照：計畫欄位以 Sheet 為主", m("start", "a", "b", None) == "push")
    check("5.6 無快照：Status/Owner 以 Jira 為主", m("status", "a", "b", None) == "pull" and m("owner", "a", "b", None) == "pull")
    check("5.7 use sheet / use jira 解決衝突", m("name", "s", "j", "o", "sheet") == "push" and m("name", "s", "j", "o", "jira") == "pull")
    res, add, rm = sp.merge_sets({"A", "B"}, {"A", "C"}, {"A"})
    check("5.8 相依集合：兩邊各自新增都保留", res == {"A", "B", "C"} and add == {"C"} and not rm)
    res, add, rm = sp.merge_sets({"A"}, {"A", "B"}, {"A", "B"})
    check("5.9 Sheet 刪除相依 → 推到 Jira 刪除", res == {"A"} and not add and not rm)
    res, add, rm = sp.merge_sets({"A", "B"}, {"A"}, {"A", "B"})
    check("5.10 Jira 刪除相依 → 從 Sheet 移除", res == {"A"} and rm == {"B"})
    t = sp.PlanTask(row=2, id="PLN-AT-01", cells={}, conflict_text="use jira")
    check("5.11 Sync Conflict 欄 resolution 解析", t.resolution() == "jira")


# ---------------------------------------------------------------------------
# 6. end-to-end with fakes
# ---------------------------------------------------------------------------

class MemoryBackend(sp.SheetBackend):
    writable = True
    url = "https://docs.google.com/spreadsheets/d/TEST/edit"

    def __init__(self, tabs):
        self.tabs = tabs
        self.state = None

    def read_tabs(self):
        out = copy.deepcopy(self.tabs)
        if self.state is not None:
            out[sp.STATE_TAB] = [["key", "json"], ["__meta__", sp.json.dumps(self.state.get("meta", {}))]] + [
                [k, sp.json.dumps(v)] for k, v in self.state["items"].items()]
        return out

    def write_cells(self, tab, updates, *, user_entered_cols):
        rows = self.tabs[tab]
        for r, c, v in updates:
            while len(rows) < r:
                rows.append([])
            row = rows[r - 1]
            while len(row) < c:
                row.append("")
            row[c - 1] = sp.norm_text(v)

    def append_rows(self, tab, start_row, rows):
        for i, row in enumerate(rows):
            self.write_cells(tab, [(start_row + i, j + 1, v) for j, v in enumerate(row)], user_entered_cols=set())

    def ensure_headers(self, tab, headers):
        self.tabs[tab][0] = list(headers)

    def write_state(self, state):
        self.state = copy.deepcopy(state)

    def write_settings(self, values):
        pass


class FakeJira:
    """Minimal in-memory Jira: issues, Blocks links, transitions, Confluence pages."""

    def __init__(self):
        self.issues: dict[str, dict] = {}
        self.links: dict[str, tuple[str, str]] = {}  # id -> (blocker, blocked)
        self.n = 100
        self.ln = 0
        self.pages: dict[str, dict] = {}
        self.calls: list[str] = []

    def _view(self, key):
        i = self.issues[key]
        f = dict(i["fields"])
        links = []
        for lid, (a, b) in self.links.items():
            if b == key:
                links.append({"id": lid, "type": {"name": "Blocks"}, "inwardIssue": {"key": a}})
            if a == key:
                links.append({"id": lid, "type": {"name": "Blocks"}, "outwardIssue": {"key": b}})
        f["issuelinks"] = links
        return {"key": key, "fields": f}

    def jira_search(self, jql, *, max_results=100, fields=None, next_page_token=None):
        out = []
        for key in sorted(self.issues):
            f = self.issues[key]["fields"]
            if "labels =" in jql and "c27-plan" not in f.get("labels", []):
                continue
            if "parent in" in jql:
                parents = jql.split("parent in (")[1].split(")")[0].split(", ")
                if (f.get("parent") or {}).get("key") not in parents or "c27-plan" in f.get("labels", []):
                    continue
            out.append(self._view(key))
        return {"issues": out, "isLast": True}

    def jira_post(self, path, payload):
        self.calls.append(f"POST {path}")
        if path.startswith("/issueLink"):
            self.ln += 1
            self.links[str(self.ln)] = (payload["inwardIssue"]["key"], payload["outwardIssue"]["key"])
            return {}
        if path.startswith("/issue/") and path.endswith("/transitions") or "/transitions" in path:
            key = path.split("/")[2]
            self.issues[key]["fields"]["status"] = {"name": {"31": "完成", "21": "進行中", "11": "待辦事項"}[payload["transition"]["id"]]}
            return {}
        if path.startswith("/issue"):
            self.n += 1
            key = f"PMWC-{self.n}"
            f = copy.deepcopy(payload["fields"])
            is_epic = f["issuetype"]["name"] == "大型工作"
            f["issuetype"] = {"name": f["issuetype"]["name"], "hierarchyLevel": 1 if is_epic else 0}
            f["status"] = {"name": "待辦事項"}
            if "assignee" in f:
                f["assignee"] = {"accountId": f["assignee"]["accountId"], "displayName": "X"}
            self.issues[key] = {"fields": f}
            return {"key": key}
        raise AssertionError(path)

    def jira_put(self, path, payload):
        self.calls.append(f"PUT {path}")
        key = path.split("/")[2].split("?")[0]
        f = self.issues[key]["fields"]
        for k, v in payload["fields"].items():
            if k == "assignee":
                f[k] = {"accountId": v["accountId"], "displayName": "X"}
            else:
                f[k] = copy.deepcopy(v)

    def jira_get(self, path, **params):
        if path.endswith("/transitions"):
            return {"transitions": [{"id": "11", "to": {"name": "待辦事項"}}, {"id": "21", "to": {"name": "進行中"}},
                                    {"id": "31", "to": {"name": "完成"}}]}
        if path.startswith("/issue/"):
            return self._view(path.split("/")[2])
        if path.startswith("/user/search"):
            return []
        raise AssertionError(path)

    def jira_delete(self, path):
        self.calls.append(f"DELETE {path}")
        if path.startswith("/issueLink/"):
            self.links.pop(path.split("/")[2], None)
        else:
            self.issues.pop(path.split("/")[2].split("?")[0], None)

    def confluence_get(self, path, **params):
        if path.endswith("/children"):
            return {"results": [{"id": pid, "title": p["title"]} for pid, p in self.pages.items()]}
        pid = path.split("/")[2]
        return {"id": pid, "title": self.pages[pid]["title"], "version": {"number": self.pages[pid]["v"]}}

    def confluence_put(self, path, payload):
        pid = path.split("/")[2]
        assert payload["version"]["minorEdit"] is True
        self.pages[pid].update(v=payload["version"]["number"], body=payload["body"]["value"])

    def confluence_post(self, path, payload):
        pid = str(500 + len(self.pages))
        self.pages[pid] = {"title": payload["title"], "v": 1, "body": payload["body"]["value"], "parent": payload["parentId"]}
        return {"id": pid}


def small_tabs() -> dict:
    tabs = sp.seed_tabs()
    hdr = tabs[sp.TASKS_TAB][0]
    keep = {"PLN-G-04", "PLN-G-05", "PLN-AT-01", "PLN-AT-02", "PLN-AT-03"}
    rows = [hdr]
    for r in tabs[sp.TASKS_TAB][1:]:
        if r[0] in keep:
            r = list(r)
            if r[0] == "PLN-G-05":
                r[hdr.index("Predecessors")] = "PLN-G-04"
            rows.append(r)
    draft = [""] * len(hdr)
    draft[hdr.index("Group")] = "AT"
    draft[hdr.index("Task Name")] = "提案中的新工作"
    draft[hdr.index("Duration (d)")] = "2"
    draft[hdr.index("Predecessors")] = "PLN-AT-03"
    draft[hdr.index("Sync to Jira")] = "N"
    rows.append(draft)
    tabs[sp.TASKS_TAB] = rows
    return tabs


def run_sync(backend, jira, direction="both", dry_run=False, tmp=None):
    cfg = sp.load_plan_config(Path("__none__"), None, need_token=False)
    s = sp.PlanSync(cfg, backend, jira, direction=direction, dry_run=dry_run,
                    state_file=Path(tmp) / f"state_{direction}.json")
    return s.run()


def col(tabs, name):
    return tabs[sp.TASKS_TAB][0].index(name)


def row_of(tabs, tid):
    return next(r for r in tabs[sp.TASKS_TAB][1:] if r and r[0] == tid)


def test_end_to_end() -> None:
    tmp = tempfile.mkdtemp()
    backend = MemoryBackend(small_tabs())
    jira = FakeJira()

    rep = run_sync(backend, jira, dry_run=True, tmp=tmp)
    check("6.1 dry-run 不寫 Jira", not jira.issues and not jira.calls and len(rep.created_issues) == 5, str(rep.created_issues))

    rep = run_sync(backend, jira, tmp=tmp)
    tabs = backend.tabs
    tasks = {k: v for k, v in jira.issues.items() if v["fields"]["issuetype"]["hierarchyLevel"] == 0}
    epics = {k: v for k, v in jira.issues.items() if v["fields"]["issuetype"]["hierarchyLevel"] == 1}
    check("6.2 建立 2 個 Epic + 5 張任務（提案列不建）", len(epics) == 2 and len(tasks) == 5,
          f"epics={len(epics)} tasks={len(tasks)}")
    draft = next(r for r in tabs[sp.TASKS_TAB][1:] if r[col(tabs, "Task Name")] == "提案中的新工作")
    check("6.3 提案列自動配發 ID、無 Jira Key", draft[0] == "PLN-AT-04" and not draft[col(tabs, "Jira Key")], draft[0])
    at2 = row_of(tabs, "PLN-AT-02")
    key2 = at2[col(tabs, "Jira Key")]
    f2 = jira.issues[key2]["fields"]
    check("6.4 Jira Start/Due = Sheet Start/Finish",
          f2["customfield_10015"] == at2[col(tabs, "Start")] and f2["duedate"] == at2[col(tabs, "Finish")],
          f"{f2['customfield_10015']}~{f2['duedate']} vs {at2[col(tabs, 'Start')]}~{at2[col(tabs, 'Finish')]}")
    check("6.5 summary = {ID}_{Task Name}、label、parent",
          f2["summary"].startswith("PLN-AT-02_") and "c27-plan" in f2["labels"] and "prio-p1" in f2["labels"]
          and f2["parent"]["key"] in epics)
    key1 = row_of(tabs, "PLN-AT-01")[col(tabs, "Jira Key")]
    check("6.6 Blocks 相依：AT-01 blocks AT-02", (key1, key2) in jira.links.values(), str(jira.links))
    check("6.7 Confluence 頁面建立在父頁下", len(jira.pages) == 1 and "提案/討論中" in next(iter(jira.pages.values()))["body"])
    check("6.8 快照寫入 _sync_state", backend.state and len(backend.state["items"]) == 5)

    calls_before = len(jira.calls)
    rep = run_sync(backend, jira, tmp=tmp)
    writes = [c for c in jira.calls[calls_before:] if not c.startswith("GET")]
    check("6.9 第二次同步無變更（冪等）", not writes and not rep.has_changes(), str(writes) + rep.render()[-300:])

    # Jira 端拖曳日期 + 改狀態 + 新增相依；Sheet 端改名稱
    key3 = row_of(tabs, "PLN-AT-03")[col(tabs, "Jira Key")]
    f3 = jira.issues[key3]["fields"]
    new_start = "2026-10-27"  # 10-26 為光復節補假
    f3["customfield_10015"], f3["duedate"] = new_start, "2026-11-03"
    f3["status"] = {"name": "進行中"}
    jira.ln += 1
    keyg4 = row_of(tabs, "PLN-G-04")[col(tabs, "Jira Key")]
    jira.links[str(jira.ln)] = (keyg4, key3)
    row_of(tabs, "PLN-AT-01")[col(tabs, "Task Name")] = "Test tree 審閱（改名）"
    rep = run_sync(backend, jira, tmp=tmp)
    at3 = row_of(tabs, "PLN-AT-03")
    check("6.10 Jira 拖曳 → Start Constraint 與工期寫回 Sheet",
          at3[col(tabs, "Start Constraint")] == new_start and at3[col(tabs, "Duration (d)")] == "6",
          f"{at3[col(tabs, 'Start Constraint')]} dur={at3[col(tabs, 'Duration (d)')]}")
    check("6.11 Jira 狀態 → Sheet Status", at3[col(tabs, "Status")] == "In Progress")
    check("6.12 Jira 新增相依 → Predecessors", "PLN-G-04" in at3[col(tabs, "Predecessors")], at3[col(tabs, "Predecessors")])
    check("6.13 Sheet 改名 → Jira summary", jira.issues[key1]["fields"]["summary"] == "PLN-AT-01_Test tree 審閱（改名）")
    check("6.14 拖曳後 Jira 日期 = 重算後的 Sheet 日期",
          jira.issues[key3]["fields"]["customfield_10015"] == at3[col(tabs, "Start")])

    # 兩邊同時改名稱 → 衝突，不覆寫
    row_of(tabs, "PLN-AT-02")[col(tabs, "Task Name")] = "Sheet 版名稱"
    jira.issues[key2]["fields"]["summary"] = "PLN-AT-02_Jira 版名稱"
    rep = run_sync(backend, jira, tmp=tmp)
    at2 = row_of(tabs, "PLN-AT-02")
    check("6.15 兩邊都改 → 衝突，兩邊都不覆寫",
          at2[col(tabs, "Task Name")] == "Sheet 版名稱" and jira.issues[key2]["fields"]["summary"] == "PLN-AT-02_Jira 版名稱"
          and at2[col(tabs, "Sync Conflict")] and rep.conflicts, at2[col(tabs, "Sync Conflict")])
    at2[col(tabs, "Sync Conflict")] = "use sheet"
    rep = run_sync(backend, jira, tmp=tmp)
    check("6.16 use sheet 解決衝突", jira.issues[key2]["fields"]["summary"] == "PLN-AT-02_Sheet 版名稱"
          and not row_of(tabs, "PLN-AT-02")[col(tabs, "Sync Conflict")])

    # Jira 在計畫 Epic 下新建單 → Sheet 新列
    epic_at = jira.issues[key2]["fields"]["parent"]["key"]
    jira.n += 1
    newkey = f"PMWC-{jira.n}"
    jira.issues[newkey] = {"fields": {"summary": "客戶追加：EHT320 測項", "labels": [], "parent": {"key": epic_at},
                                      "issuetype": {"name": "任務", "hierarchyLevel": 0}, "status": {"name": "待辦事項"},
                                      "customfield_10015": "2026-11-09", "duedate": "2026-11-11"}}
    rep = run_sync(backend, jira, tmp=tmp)
    new_row = next((r for r in tabs[sp.TASKS_TAB][1:] if r and len(r) > col(tabs, "Jira Key") and r[col(tabs, "Jira Key")] == newkey), None)
    check("6.17 Jira 新建 → Sheet 新列 + 配發 ID + Jira 補 label/summary",
          new_row is not None and new_row[0] == "PLN-AT-05" and new_row[col(tabs, "Duration (d)")] == "3"
          and "c27-plan" in jira.issues[newkey]["fields"]["labels"]
          and jira.issues[newkey]["fields"]["summary"].startswith("PLN-AT-05_"), str(new_row))

    # 從 Sheet 刪除一列 → Jira 關閉 + plan-removed
    tabs[sp.TASKS_TAB] = [r for r in tabs[sp.TASKS_TAB] if not r or r[0] != "PLN-AT-05"]
    rep = run_sync(backend, jira, tmp=tmp)
    f_new = jira.issues[newkey]["fields"]
    check("6.18 Sheet 刪列 → Jira 轉完成 + label plan-removed（不刪除）",
          f_new["status"]["name"] == "完成" and "plan-removed" in f_new["labels"] and rep.closed_issues)
    rep = run_sync(backend, jira, tmp=tmp)
    check("6.19 已關閉的單不會被重新加回 Sheet", not rep.new_from_jira and not any(
        r and len(r) > col(tabs, "Jira Key") and r[col(tabs, "Jira Key")] == newkey for r in tabs[sp.TASKS_TAB][1:]))

    # Register 單混在 PMWC：計畫同步不碰
    jira.n += 1
    regkey = f"PMWC-{jira.n}"
    jira.issues[regkey] = {"fields": {"summary": "RF-03_XO trim", "labels": [], "parent": {"key": "PMWC-119"},
                                      "issuetype": {"name": "任務", "hierarchyLevel": 0}, "status": {"name": "待辦事項"}}}
    before = copy.deepcopy(jira.issues[regkey])
    run_sync(backend, jira, tmp=tmp)
    check("6.20 計畫同步不碰 Register 單", jira.issues.get(regkey) == before)

    # push-only：Jira 端修改不拉回、不覆寫
    jira.issues[key2]["fields"]["summary"] = "PLN-AT-02_Jira 再改"
    rep = run_sync(backend, jira, direction="push", tmp=tmp)
    check("6.21 push-only 不覆寫 Jira 端的變更",
          jira.issues[key2]["fields"]["summary"] == "PLN-AT-02_Jira 再改"
          and row_of(tabs, "PLN-AT-02")[col(tabs, "Task Name")] == "Sheet 版名稱")
    rep = run_sync(backend, jira, direction="pull", tmp=tmp)
    check("6.22 pull 把 Jira 變更拉回 Sheet", row_of(tabs, "PLN-AT-02")[col(tabs, "Task Name")] == "Jira 再改")

    # mass removal guard
    backend2 = MemoryBackend(copy.deepcopy(backend.tabs))
    backend2.state = copy.deepcopy(backend.state)
    hdr = backend2.tabs[sp.TASKS_TAB][0]
    backend2.tabs[sp.TASKS_TAB] = [hdr, row_of(backend2.tabs, "PLN-G-04")]
    cfg = sp.load_plan_config(Path("__none__"), None, need_token=False)
    cfg["plan"]["jira"]["max_removals"] = 2
    s = sp.PlanSync(cfg, backend2, jira, state_file=Path(tmp) / "s2.json")
    rep = s.run()
    check("6.23 大量刪除保護：超過上限不關閉", rep.errors and not rep.closed_issues, str(rep.errors))


def test_config_guard() -> None:
    tmp = Path(tempfile.mkdtemp())
    p = tmp / "plan.yaml"
    p.write_text("plan:\n  confluence:\n    page_id: '189300768'\n", encoding="utf-8")
    try:
        sp.load_plan_config(Path("__none__"), p, need_token=False)
        check("7.1 拒絕把計畫頁設成 mail_checking", False)
    except sp.PlanError:
        check("7.1 拒絕把計畫頁設成 mail_checking", True)
    base = tmp / "config.yaml"
    base.write_text("confluence:\n  page_id: '189300768'\njira:\n  project_key: PMWC\n", encoding="utf-8")
    cfg = sp.load_plan_config(base, None, need_token=False)
    check("7.2 Register 的 confluence.page_id 不會被計畫同步沿用", cfg["plan"]["confluence"]["page_id"] == ""
          and cfg["plan"]["confluence"]["parent_page_id"] == "14057511")
    adf = sp.build_issue_description(mk("PLN-AT-01", 2), None, "")
    check("7.3 描述 ADF 往返（同步標記以下不算使用者描述）", sp.adf_to_text(adf) == "")


def main() -> int:
    print("=" * 60)
    print("sync_plan.py / Register 防護 離線驗證")
    print("=" * 60)
    test_parsing()
    test_scheduler()
    test_seed()
    test_register_guard()
    test_merge()
    test_end_to_end()
    test_config_guard()
    print("=" * 60)
    print(f"PASS={PASS} FAIL={FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
