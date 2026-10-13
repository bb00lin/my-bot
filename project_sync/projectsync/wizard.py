"""開案精靈步驟與連線測試（GUI / CLI 共用；每一步皆可重跑、冪等）。"""

from __future__ import annotations

import re
import smtplib
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

import requests

from . import plan_core as pc
from . import plan_store as ps
from . import plan_sync
from . import profiles
from . import register_sync as sr


def next_monday(today: date | None = None) -> date:
    d = today or date.today()
    return d + timedelta(days=(7 - d.weekday()) or 7)


def page_id_from(text: str) -> str:
    m = re.search(r"/pages/(\d+)", text or "")
    if m:
        return m.group(1)
    m = re.search(r"pageId=(\d+)", text or "")
    return m.group(1) if m else (text or "").strip()


def client_for(profile: dict[str, Any], secrets: dict[str, str]) -> sr.AtlassianClient:
    token = secrets.get("ATLASSIAN_API_TOKEN", "")
    if not token:
        raise RuntimeError("缺少 ATLASSIAN_API_TOKEN")
    return plan_sync.build_client(profile, token)


# ---------------------------------------------------------------------------
# Connection tests
# ---------------------------------------------------------------------------


def test_atlassian(profile: dict[str, Any], secrets: dict[str, str]) -> str:
    c = client_for(profile, secrets)
    me = c.jira_get("/myself")
    msg = [f"Jira 登入：{me.get('displayName')}"]
    key = profile["jira"].get("project_key")
    if key:
        proj = c.jira_get(f"/project/{key}")
        msg.append(f"專案 {key}：{proj.get('name')}（{len(proj.get('issueTypes', []))} 種類型）")
    sk = profile["plan"].get("confluence_space_key") or profile["register"].get("space_key")
    if sk:
        sp = c.confluence_get("/spaces", keys=sk)
        res = sp.get("results", [])
        msg.append(f"Confluence 空間 {sk}：{res[0]['name'] if res else '找不到'}")
    return "；".join(msg)


def test_google(secrets: dict[str, str]) -> str:
    sa = secrets.get("GOOGLE_SERVICE_ACCOUNT_FILE", "")
    if not sa or not Path(sa).exists():
        raise RuntimeError("未設定 service account JSON 檔（GOOGLE_SERVICE_ACCOUNT_FILE）")
    from google.auth.transport.requests import Request

    creds = ps.google_credentials(sa)
    creds.refresh(Request())
    return f"Google 憑證有效：{creds.service_account_email}（請把計畫表分享給此帳號為編輯者）"


def test_smtp(secrets: dict[str, str]) -> str:
    user, pw = secrets.get("MAIL_USERNAME", ""), secrets.get("MAIL_PASSWORD", "")
    if not user or not pw:
        raise RuntimeError("缺少 MAIL_USERNAME / MAIL_PASSWORD")
    host = secrets.get("SMTP_HOST") or sr.infer_smtp_host(user)
    port = int(secrets.get("SMTP_PORT") or 587)
    with smtplib.SMTP(host, port, timeout=30) as s:
        s.ehlo()
        s.starttls()
        s.ehlo()
        s.login(user, pw)
    return f"SMTP 登入成功：{host}:{port}（{user}）"


def test_register_sheet(profile: dict[str, Any]) -> str:
    url = profile["register"].get("sheet_url", "")
    sid = ps.google_sheet_id(url)
    r = requests.get(f"https://docs.google.com/spreadsheets/d/{sid}/export?format=xlsx", timeout=60)
    if r.status_code != 200 or not r.content.startswith(b"PK"):
        raise RuntimeError(f"S 表下載失敗（HTTP {r.status_code}），請確認「知道連結者可檢視」")
    return f"S 表可下載（{len(r.content)} bytes）"


# ---------------------------------------------------------------------------
# Template
# ---------------------------------------------------------------------------


def template_content(profile: dict[str, Any], use_c27: bool) -> dict[str, Any]:
    proj = profile["project"]
    start = proj.get("project_start") or next_monday().isoformat()
    label = plan_sync.plan_label(profile)
    if use_c27:
        from . import plan_seed_c27 as seed

        tasks, groups, people, holidays = seed.build_seed_tasks(), seed.GROUPS, seed.PEOPLE, seed.HOLIDAYS
        if not proj.get("project_start"):
            start = seed.PROJECT_START_PLACEHOLDER
    else:
        from . import plan_seed_c27 as seed

        tasks = [
            {"ID": "PLN-G-01", "WBS": "1.1", "Group": "G", "Task Name": "專案開始 Kickoff", "Type": "Milestone",
             "Priority": "P1", "Duration (d)": "0", "Status": "Not Started", "Sync to Jira": "Y",
             "Estimate": "N", "Description / 工作內容": "專案啟動"},
            {"ID": "PLN-G-02", "WBS": "1.2", "Group": "G", "Task Name": "（範例）需求確認", "Type": "Task",
             "Priority": "P2", "Duration (d)": "5", "Predecessors": "PLN-G-01", "Status": "Not Started",
             "Sync to Jira": "N", "Estimate": "Y", "Description / 工作內容": "範例草稿列，Sync=N 不會建 Jira",
             "Proposed By": "ProjectSync 範本"},
        ]
        groups = [("G", f"PLAN-{proj.get('name', '')} 里程碑", "", "專案里程碑")]
        people, holidays = seed.PEOPLE, seed.HOLIDAYS
    settings = [
        ("Project Name", proj.get("name", ""), ""),
        ("Project Start", start, "T0 之前的專案開始日（佔位值，請改成實際日期）"),
        ("Working Days", "Mon,Tue,Wed,Thu,Fri", "工作日"),
        ("Plan Label", label, "顯示用；Jira 標籤"),
        ("Jira Project", profile["jira"].get("project_key", ""), "顯示用"),
        ("Confluence Page", "", "程式回寫"),
        ("Last Sync", "", "程式回寫"),
        ("On Removed", profile["plan"].get("on_removed", "close"), "刪除列時 Jira 處理方式（實際設定在 ProjectSync）"),
    ]
    return {"tasks": tasks, "groups": groups, "people": people, "holidays": holidays, "settings": settings}


def xlsx_path(profile: dict[str, Any]) -> Path:
    p = Path(profile["plan"].get("xlsx_path") or f"projects/{profile['project']['slug']}/plan.xlsx")
    return p if p.is_absolute() else profiles.APP_DIR / p


# ---------------------------------------------------------------------------
# Wizard steps — each returns a status message, raises on failure
# ---------------------------------------------------------------------------


def step_plan_sheet(profile: dict[str, Any], secrets: dict[str, str], *, use_c27: bool = False,
                    overwrite: bool = False) -> str:
    path = xlsx_path(profile)
    msgs = []
    if path.exists() and not overwrite:
        msgs.append(f"計畫表已存在：{path}")
    else:
        content = template_content(profile, use_c27)
        ps.build_template_xlsx(path, content["tasks"], content["groups"], content["people"],
                               content["holidays"], content["settings"])
        msgs.append(f"已產生計畫表 xlsx：{path}")
    if use_c27 and not profile["plan"].get("t0_task_id"):
        profile["plan"]["t0_task_id"] = "PLN-G-05"
    sa = secrets.get("GOOGLE_SERVICE_ACCOUNT_FILE", "")
    if profile["plan"].get("google_sheet_url"):
        msgs.append(f"已有 Google Sheet：{profile['plan']['google_sheet_url']}")
    elif sa and Path(sa).exists():
        share = profile["plan"].get("share_with") or [profile["atlassian"]["email"]]
        title = f"{profile['project']['name']}_Project_Plan"
        url = ps.upload_xlsx_as_google_sheet(path, title, sa, share)
        profile["plan"]["google_sheet_url"] = url
        profile["plan"]["source"] = "google"
        msgs.append(f"已建立 Google Sheet 並分享給 {', '.join(share)}：{url}")
    else:
        msgs.append("未設定 Google service account：目前以本機 xlsx 為計畫來源（可讀寫）。"
                    "要改用 Google Sheet，請見 README「Google Sheet 設定」。")
    profiles.save_profile(profile)
    return "\n".join(msgs)


def resolve_space_id(client: sr.AtlassianClient, space_key: str) -> str:
    res = client.confluence_get("/spaces", keys=space_key).get("results", [])
    if not res:
        raise RuntimeError(f"找不到 Confluence 空間 {space_key}")
    return str(res[0]["id"])


def step_confluence_plan_page(profile: dict[str, Any], secrets: dict[str, str]) -> str:
    p = profile["plan"]
    client = client_for(profile, secrets)
    if not p.get("confluence_space_id") and p.get("confluence_space_key"):
        p["confluence_space_id"] = resolve_space_id(client, p["confluence_space_key"])
    p["confluence_parent_page_id"] = page_id_from(str(p.get("confluence_parent_page_id", "")))
    body = ("<p>此頁由 ProjectSync 依專案計畫表自動產生，第一次「同步排程」後會顯示完整排程。</p>")
    pid, action = plan_sync.upsert_confluence_page(
        client, space_id=p["confluence_space_id"], parent_id=p["confluence_parent_page_id"],
        title=p.get("confluence_page_title", "Project Plan"), page_id=str(p.get("confluence_page_id") or ""),
        body=body, dry_run=False) if not p.get("confluence_page_id") else (p["confluence_page_id"], "已存在")
    p["confluence_page_id"] = pid
    profiles.save_profile(profile)
    url = plan_sync.confluence_page_url(profile["atlassian"]["site_url"], p.get("confluence_space_key", ""), pid)
    return f"Confluence 計畫頁{action}：{url}"


def step_jira_epics(profile: dict[str, Any], secrets: dict[str, str]) -> str:
    store = plan_sync.open_store(profile, profiles.APP_DIR, secrets)
    book = store.read()
    client = client_for(profile, secrets)
    report = plan_sync.PlanReport()
    jp = plan_sync.JiraPlan(client, profile, False, report)
    epics, _ = jp.fetch()
    created, existing = [], []
    for g in book.groups:
        code = g["code"]
        if code in epics:
            existing.append(f"{epics[code].key} {g['epic']}")
            continue
        key = jp.create({
            "project": {"key": jp.project}, "issuetype": {"name": jp.epic_type}, "summary": g["epic"],
            "labels": [jp.label, jp.epic_label(code)],
            "description": pc.build_description_adf(g.get("description", ""), [f"Group: {code}"]),
        })
        created.append(f"{key} {g['epic']}")
    return (f"Jira 標籤 {jp.label}；Epic 新建 {len(created)}、已存在 {len(existing)}"
            + ("".join(f"\n  + {x}" for x in created)))


def step_register(profile: dict[str, Any], secrets: dict[str, str], *, create_c_page: bool = False) -> str:
    r = profile["register"]
    if not r.get("sheet_url"):
        return "未設定 S 表，略過 Register（之後可在 Register 分頁補）"
    msgs = [test_register_sheet(profile)]
    if not r.get("confluence_page_id") and create_c_page:
        client = client_for(profile, secrets)
        p = profile["plan"]
        space_key = r.get("space_key") or p.get("confluence_space_key")
        space_id = p.get("confluence_space_id") or resolve_space_id(client, space_key)
        title = f"{profile['project']['name']} mail_checking"
        pid, action = plan_sync.upsert_confluence_page(
            client, space_id=space_id, parent_id=page_id_from(str(p.get("confluence_parent_page_id", ""))),
            title=title, page_id="", body="<p>Register C 表（由 ProjectSync 同步）</p>", dry_run=False)
        r["confluence_page_id"], r["confluence_page_title"], r["space_key"] = pid, title, space_key
        msgs.append(f"C 表頁面{action}：{pid}")
    r["enabled"] = bool(r.get("confluence_page_id"))
    profiles.save_profile(profile)
    msgs.append(f"Register 同步{'已啟用' if r['enabled'] else '未啟用（缺 C 表 page id）'}")
    return "；".join(msgs)


def step_mail(profile: dict[str, Any], secrets: dict[str, str], *, send_test: bool = False) -> str:
    m = profile["mail"]
    profiles.save_profile(profile)
    msg = f"收件人 To={m.get('to')} Cc={', '.join(m.get('cc') or []) or '(none)'}"
    if send_test:
        from . import mailer

        msg += "；" + mailer.send_test_mail(profile, secrets)
    return msg


WIZARD_STEPS: list[tuple[str, str]] = [
    ("plan_sheet", "建立計畫表（Google Sheet / xlsx）"),
    ("confluence", "建立 Confluence 計畫頁"),
    ("jira", "建立 Jira 標籤與 Epic"),
    ("register", "Register 同步設定"),
    ("mail", "郵件收件人"),
]


def run_step(name: str, profile: dict[str, Any], secrets: dict[str, str], options: dict[str, Any]) -> str:
    if name == "plan_sheet":
        return step_plan_sheet(profile, secrets, use_c27=options.get("use_c27", False),
                               overwrite=options.get("overwrite", False))
    if name == "confluence":
        return step_confluence_plan_page(profile, secrets)
    if name == "jira":
        return step_jira_epics(profile, secrets)
    if name == "register":
        return step_register(profile, secrets, create_c_page=options.get("create_c_page", False))
    if name == "mail":
        return step_mail(profile, secrets, send_test=options.get("send_test", False))
    raise ValueError(name)


def preview_schedule(profile: dict[str, Any], secrets: dict[str, str]) -> tuple[list[pc.PlanTask], list[str]]:
    """只讀計畫表並排程（不連 Jira），供 GUI 預覽。"""
    store = plan_sync.open_store(profile, profiles.APP_DIR, secrets)
    book = store.read()
    tasks, warns = plan_sync.tasks_from_book(book, plan_sync.id_prefix(profile))
    cal, start = plan_sync.build_calendar(book, profile)
    pc.resolve_preds(tasks)
    warns += pc.compute_schedule(tasks, cal)
    locked = {g["code"]: int(float(g["locked"])) for g in book.groups if str(g.get("locked", "")).strip()}
    for code, exp, act, ok in pc.validate_group_totals(tasks, locked):
        warns.append(f"{code}: 工期合計 {act}d / 鎖定 {exp}d {'✓' if ok else '⚠'}")
    warns = plan_sync.schedule_summary(tasks, book.groups, start, profile) + warns
    return tasks, warns


ProgressFn = Callable[[str, str, str], None]
