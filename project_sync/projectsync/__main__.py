"""CLI：python -m projectsync --project c27 --job register|plan [--direction both] [--dry-run]

不帶 --job 時開啟 GUI。
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="projectsync", description="ProjectSync 本機同步工具")
    parser.add_argument("--project", help="專案代碼（projects/<slug>.yaml）")
    parser.add_argument("--job", choices=["register", "plan", "test-mail", "init-plan", "preview", "test"],
                        help="register=Register 同步；plan=排程同步；init-plan=產生計畫表；preview=只排程；test=連線測試")
    parser.add_argument("--direction", choices=["both", "push", "pull"], default="both")
    parser.add_argument("--dry-run", action="store_true", help="預覽，不寫入任何系統")
    parser.add_argument("--allow-mass-removal", action="store_true")
    parser.add_argument("--no-mail", action="store_true")
    parser.add_argument("--c27-template", action="store_true", help="init-plan 時使用 C27 範本")
    parser.add_argument("--overwrite", action="store_true", help="init-plan 時覆寫既有 xlsx")
    args = parser.parse_args(argv)

    if not args.job:
        from .gui import run_gui

        run_gui(args.project)
        return 0
    if not args.project:
        parser.error("--project 必填")

    from . import profiles

    profile = profiles.load_profile(args.project)
    secrets = profiles.load_secrets()
    slug = profile["project"]["slug"]
    try:
        if args.job == "register":
            from .register_adapter import run_register

            rep = run_register(profile, secrets, dry_run=args.dry_run)
            return 1 if rep.errors else 0
        if args.job == "plan":
            from .plan_sync import run_plan_sync

            rep = run_plan_sync(
                profile, secrets, profiles.APP_DIR, direction=args.direction, dry_run=args.dry_run,
                allow_mass_removal=args.allow_mass_removal, send_mail=False if args.no_mail else None,
                log_dir=profiles.log_dir(slug, "plan"), on_profile_update=profiles.save_profile)
            return 1 if rep.errors else 0
        if args.job == "test-mail":
            from .mailer import send_test_mail

            print(send_test_mail(profile, secrets))
            return 0
        if args.job == "init-plan":
            from .wizard import step_plan_sheet

            print(step_plan_sheet(profile, secrets, use_c27=args.c27_template, overwrite=args.overwrite))
            return 0
        if args.job == "preview":
            from .wizard import preview_schedule

            tasks, notes = preview_schedule(profile, secrets)
            for n in notes:
                print(n)
            for t in tasks:
                print(f"{t.id:12} {t.group:4} {t.duration:>3}d {t.start} ~ {t.finish}  {'Y' if t.sync else 'N'}  {t.name}")
            return 0
        if args.job == "test":
            from . import wizard

            for name, fn in (("Atlassian", lambda: wizard.test_atlassian(profile, secrets)),
                             ("Register S 表", lambda: wizard.test_register_sheet(profile)),
                             ("SMTP", lambda: wizard.test_smtp(secrets)),
                             ("Google", lambda: wizard.test_google(secrets))):
                try:
                    print(f"✅ {name}: {fn()}")
                except Exception as exc:  # noqa: BLE001
                    print(f"❌ {name}: {exc}")
            return 0
    except Exception as exc:  # noqa: BLE001
        print(f"執行失敗：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
