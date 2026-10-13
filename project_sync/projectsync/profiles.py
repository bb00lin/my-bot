"""專案設定檔（projects/<slug>.yaml，不含密鑰）與本機憑證（app 目錄 .env）。"""

from __future__ import annotations

import copy
import os
import re
from pathlib import Path
from typing import Any

import yaml

APP_DIR = Path(__file__).resolve().parent.parent
PROJECTS_DIR = APP_DIR / "projects"
LOGS_DIR = APP_DIR / "logs"
ENV_PATH = APP_DIR / ".env"
JIRA_CONFIG_TXT = Path(r"D:\MEGA\Jira\jira_config.txt")

SECRET_KEYS = [
    "ATLASSIAN_API_TOKEN",
    "MAIL_USERNAME",
    "MAIL_PASSWORD",
    "SMTP_HOST",
    "SMTP_PORT",
    "GRAPH_TENANT_ID",
    "GRAPH_CLIENT_ID",
    "GRAPH_CLIENT_SECRET",
    "GOOGLE_SERVICE_ACCOUNT_FILE",
]
MASKED_KEYS = {"ATLASSIAN_API_TOKEN", "MAIL_PASSWORD", "GRAPH_CLIENT_SECRET"}

REGISTER_CONFLUENCE_COLUMNS = [
    "ID", "Workstream", "Title", "Description / current state", "Next action", "Ball with", "Priority",
    "Status", "Opened", "Target close", "Next milestone / due", "Source / reference", "LINK",
]


def default_profile(slug: str = "new", name: str = "") -> dict[str, Any]:
    return {
        "project": {"name": name or slug.upper(), "slug": slug, "customer": "", "project_start": ""},
        "atlassian": {
            "site_url": "https://qsiaiot.atlassian.net",
            "cloud_id": "2657b5c5-bbc7-4469-9e06-6bc58db3de73",
            "email": "bob.lin@qsitw.com",
            "silent_mode": True,
        },
        "jira": {
            "project_key": "",
            "epic_issue_type": "大型工作",
            "task_issue_type": "任務",
            "start_date_field": "customfield_10015",
            "due_date_field": "duedate",
            "default_assignee_account_id": "",
            "default_assignee_query": "",
            "board_url": "",
        },
        "register": {
            "enabled": False,
            "sheet_url": "",
            "sheet_name": "Register",
            "confluence_page_id": "",
            "confluence_page_title": "mail_checking",
            "space_key": "",
            "source_link_title": "Open Issues Register (S 表格)",
            "version_message": "Synced from register",
            "confluence_columns": list(REGISTER_CONFLUENCE_COLUMNS),
        },
        "plan": {
            "enabled": True,
            "source": "xlsx",
            "xlsx_path": f"projects/{slug}/{slug.upper()}_Project_Plan.xlsx",
            "google_sheet_url": "",
            "label": f"{slug}-plan",
            "id_prefix": "PLN",
            "t0_task_id": "",
            "on_removed": "close",
            "confluence_space_key": "",
            "confluence_space_id": "",
            "confluence_parent_page_id": "",
            "confluence_page_title": f"{slug.upper()} Project Plan",
            "confluence_page_id": "",
            "show_discussion_on_confluence": True,
            "share_with": [],
        },
        "mail": {"to": "", "cc": [], "send_on_real_run": True, "backend": "smtp"},
    }


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return s or "project"


def list_projects() -> list[str]:
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    return sorted(p.stem for p in PROJECTS_DIR.glob("*.yaml"))


def profile_path(slug: str) -> Path:
    return PROJECTS_DIR / f"{slug}.yaml"


def load_profile(slug: str) -> dict[str, Any]:
    path = profile_path(slug)
    if not path.exists():
        raise FileNotFoundError(f"找不到專案設定 {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    slug_in = (data.get("project") or {}).get("slug") or slug
    return _merge(default_profile(slug_in), data)


def save_profile(profile: dict[str, Any]) -> Path:
    slug = slugify(profile["project"]["slug"])
    profile["project"]["slug"] = slug
    path = profile_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(profile, allow_unicode=True, sort_keys=False, width=120)
    path.write_text(f"# ProjectSync 專案設定（不含密鑰；密鑰在 {ENV_PATH.name}）\n" + text, encoding="utf-8")
    return path


def delete_profile(slug: str) -> None:
    path = profile_path(slug)
    if path.exists():
        path.unlink()


def validate_profile(profile: dict[str, Any]) -> list[str]:
    problems = []
    a, j, r, p = profile["atlassian"], profile["jira"], profile["register"], profile["plan"]
    for k in ("site_url", "cloud_id", "email"):
        if not a.get(k):
            problems.append(f"Atlassian {k} 未設定")
    if not j.get("project_key"):
        problems.append("Jira project_key 未設定")
    if r.get("enabled"):
        if not r.get("sheet_url"):
            problems.append("Register：S 表 URL 未設定")
        if not r.get("confluence_page_id"):
            problems.append("Register：C 表 Confluence page id 未設定")
    if p.get("enabled"):
        if p.get("source") == "xlsx" and not p.get("xlsx_path"):
            problems.append("Plan：xlsx 路徑未設定")
        if p.get("source") in ("google", "export") and not p.get("google_sheet_url"):
            problems.append("Plan：Google Sheet URL 未設定")
        if str(p.get("confluence_page_id")) == str(r.get("confluence_page_id")) and p.get("confluence_page_id"):
            problems.append("Plan 的 Confluence 頁面不可與 Register C 表同一頁")
        if not p.get("confluence_parent_page_id") and not p.get("confluence_page_id"):
            problems.append("Plan：Confluence 父頁面 id 未設定")
    return problems


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------


def load_secrets() -> dict[str, str]:
    out = {k: "" for k in SECRET_KEYS}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^\s*([A-Za-z0-9_]+)\s*=\s*(.*)$", line)
            if m and not line.lstrip().startswith("#"):
                out[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return out


def save_secrets(secrets: dict[str, str]) -> None:
    lines = ["# ProjectSync 本機憑證（勿提交、勿分享）"]
    for k in SECRET_KEYS:
        lines.append(f"{k}={secrets.get(k, '')}")
    for k, v in secrets.items():
        if k not in SECRET_KEYS and v:
            lines.append(f"{k}={v}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def apply_secrets_to_env(secrets: dict[str, str]) -> None:
    for k in SECRET_KEYS:
        v = secrets.get(k, "")
        if v:
            os.environ[k] = v
        elif k in os.environ and k != "ATLASSIAN_API_TOKEN":
            os.environ.pop(k, None)


def import_jira_config(path: Path = JIRA_CONFIG_TXT, prefix: str = "JIRA") -> tuple[str, str]:
    """讀 jira_config.txt 的 <prefix>_EMAIL / <prefix>_TOKEN（不印出）。"""
    if not path.exists():
        raise FileNotFoundError(str(path))
    kv = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"^\s*([A-Za-z0-9_]+)\s*=\s*(.*)$", line)
        if m:
            kv[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return kv.get(f"{prefix}_EMAIL", ""), kv.get(f"{prefix}_TOKEN", "")


def mask(value: str) -> str:
    if not value:
        return "（未設定）"
    return f"已設定（{len(value)} 字元）"


def log_dir(slug: str, job: str = "") -> Path:
    d = LOGS_DIR / slug / job if job else LOGS_DIR / slug
    d.mkdir(parents=True, exist_ok=True)
    return d
