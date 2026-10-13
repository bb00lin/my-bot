"""以專案設定檔驅動 register_sync（行為與 my-bot 的 sync_register.py 相同）。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from . import mailer, profiles
from . import register_sync as sr
from .plan_store import google_sheet_id


def build_register_config(profile: dict[str, Any], secrets: dict[str, str]) -> dict[str, Any]:
    r, j, a = profile["register"], profile["jira"], profile["atlassian"]
    sheet_url = r.get("sheet_url", "")
    sid = google_sheet_id(sheet_url)
    is_google = "docs.google.com" in sheet_url
    download = f"https://docs.google.com/spreadsheets/d/{sid}/export?format=xlsx" if is_google else sheet_url
    view = f"https://docs.google.com/spreadsheets/d/{sid}/edit" if is_google else sheet_url
    label = (profile.get("plan") or {}).get("label") or f"{profile['project']['slug']}-plan"
    return {
        "atlassian": {"site_url": a["site_url"], "cloud_id": a["cloud_id"], "email": a["email"]},
        "silent_mode": bool(a.get("silent_mode", True)),
        "notify": mailer.notify_cfg(profile, secrets),
        "sharepoint": {"download_url": download, "view_url": view, "sheet_name": r.get("sheet_name") or "Register"},
        "confluence": {
            "page_id": str(r.get("confluence_page_id", "")),
            "page_title": r.get("confluence_page_title", "mail_checking"),
            "space_key": r.get("space_key", ""),
            "web_link_title": r.get("confluence_page_title", "mail_checking"),
            "source_link_title": r.get("source_link_title", "Open Issues Register (S 表格)"),
            "version_message": r.get("version_message", "Synced from register"),
        },
        "jira": {
            "project_key": j["project_key"],
            "epic_issue_type": j.get("epic_issue_type", "大型工作"),
            "task_issue_type": j.get("task_issue_type", "任務"),
            "start_date_field": j.get("start_date_field", "customfield_10015"),
            "due_date_field": j.get("due_date_field", "duedate"),
            "default_assignee_account_id": j.get("default_assignee_account_id", ""),
            "default_assignee_query": j.get("default_assignee_query", ""),
            "exclude_labels": [label],
        },
        "confluence_columns": r.get("confluence_columns") or profiles.REGISTER_CONFLUENCE_COLUMNS,
    }


def run_register(profile: dict[str, Any], secrets: dict[str, str], *, dry_run: bool) -> sr.SyncReport:
    slug = profile["project"]["slug"]
    if not secrets.get("ATLASSIAN_API_TOKEN"):
        raise RuntimeError("缺少 ATLASSIAN_API_TOKEN（請在「憑證」頁設定）")
    work = profiles.log_dir(slug, "register")
    cfg = build_register_config(profile, secrets)
    cfg_path = work / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    profiles.apply_secrets_to_env(secrets)
    os.environ.pop("GITHUB_ACTIONS", None)
    old_dir = sr.SCRIPT_DIR
    sr.SCRIPT_DIR = Path(work)
    try:
        print(f"Register 同步：{profile['project']['name']}（{'dry-run' if dry_run else '正式'}）")
        print(f"工作目錄（差異檔/快照）：{work}")
        report = sr.run_sync(cfg_path, dry_run=dry_run)
        sr.print_report(report)
        return report
    finally:
        sr.SCRIPT_DIR = old_dir
