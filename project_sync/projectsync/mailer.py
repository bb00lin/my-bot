"""寄信：沿用 register_sync 的 SMTP / Graph 實作（Gmail SMTP 需應用程式密碼）。"""

from __future__ import annotations

from typing import Any

from . import profiles
from . import register_sync as sr


def notify_cfg(profile: dict[str, Any], secrets: dict[str, str]) -> dict[str, Any]:
    m = profile.get("mail") or {}
    cc = m.get("cc") or []
    if isinstance(cc, str):
        cc = [x.strip() for x in cc.split(",") if x.strip()]
    notify = {
        "email_to": m.get("to", ""),
        "email_cc": cc,
        "mail_backend": m.get("backend", "smtp"),
        "email_on_github": True,
        "email_always": bool(m.get("send_on_real_run", True)),
    }
    if secrets.get("SMTP_HOST"):
        notify["smtp_host"] = secrets["SMTP_HOST"]
    if secrets.get("SMTP_PORT"):
        notify["smtp_port"] = int(secrets["SMTP_PORT"])
    return notify


def send_report(profile: dict[str, Any], secrets: dict[str, str], *, subject: str, body: str,
                verdict: str = "") -> None:
    profiles.apply_secrets_to_env(secrets)
    cfg = {"notify": notify_cfg(profile, secrets)}
    html_body = sr.build_diff_email_html_body(body, verdict=verdict or "無差異")
    sr.send_diff_email(cfg, subject=subject, body=body, html_body=html_body)


def send_test_mail(profile: dict[str, Any], secrets: dict[str, str]) -> str:
    name = (profile.get("project") or {}).get("name", "")
    send_report(profile, secrets, subject=f"[ProjectSync 測試信] {name}",
                body="無差異\n這是 ProjectSync 的測試信，收到代表寄信設定正確。", verdict="無差異")
    to, cc = sr.resolve_notify_recipients({"notify": notify_cfg(profile, secrets)})
    return f"已寄出測試信 To={', '.join(to)} Cc={', '.join(cc) or '(none)'}"
