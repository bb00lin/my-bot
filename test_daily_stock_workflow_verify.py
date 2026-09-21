#!/usr/bin/env python3

"""離線驗證合併後的 DailyStockBot workflow（不需憑證、不連外網）。

驗證三件事：
1. scripts/resolve_daily_stock_plan.py 的真值表，涵蓋合併前四種觸發情境的等價行為。
2. .github/workflows/main.yml 的結構：觸發條件、階段開關、secrets 覆蓋率、套件覆蓋率。
3. DailyStockBot.py / DailyStockPush.py 的契約：ENABLE_AI 語意與 WATCH_LIST 的先後依賴。

不 import DailyStockPush（它在 module 層就會呼叫 FinMind 抓台股清單），
腳本端一律用原始碼比對，必要時只取出被測運算式單獨評估。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import types
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent
WORKFLOW = ROOT / ".github" / "workflows" / "main.yml"
MERGED_AWAY = ROOT / ".github" / "workflows" / "daily_stock.yml"
PLAN_SCRIPT = ROOT / "scripts" / "resolve_daily_stock_plan.py"
SCAN_SCRIPT = ROOT / "DailyStockBot.py"
PUSH_SCRIPT = ROOT / "DailyStockPush.py"

SECRET_NAMES = (
    "GOOGLE_SHEETS_JSON",
    "LINE_ACCESS_TOKEN",
    "LINE_USER_ID",
    "GEMINI_API_KEY",
    "CURSOR_API_KEY",
    "MAIL_USERNAME",
    "MAIL_PASSWORD",
)

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    status = "PASS" if ok else "FAIL"
    if ok:
        PASS += 1
    else:
        FAIL += 1
    line = f"[{status}] {name}"
    if detail:
        line += f" — {detail}"
    print(line)


def hr(title: str) -> None:
    print("\n" + "=" * 70)
    print(f"== {title}")
    print("=" * 70)


# ==========================================
# 1. 執行計畫真值表
# ==========================================
def verify_plan_logic() -> None:
    hr("1. 執行計畫真值表 (scripts/resolve_daily_stock_plan.py)")

    sys.path.insert(0, str(ROOT / "scripts"))
    import resolve_daily_stock_plan as planner

    # (情境說明, event_name, raw_stage, raw_ai_provider, 期望輸出)
    cases = [
        (
            "合併前 DailyStockBot 手動觸發：掃描+推播、固定用 Cursor",
            "workflow_dispatch", "full", "cursor",
            {"stage": "full", "run_scan": "true", "run_push": "true",
             "enable_ai": "true", "ai_provider": "cursor"},
        ),
        (
            "合併前 DailyStockPush 手動觸發、兩個勾選框都不勾：只推播、不啟用 AI",
            "workflow_dispatch", "push", "none",
            {"stage": "push", "run_scan": "false", "run_push": "true",
             "enable_ai": "false", "ai_provider": ""},
        ),
        (
            "合併前 DailyStockPush 手動觸發、勾 Cursor",
            "workflow_dispatch", "push", "cursor",
            {"stage": "push", "run_scan": "false", "run_push": "true",
             "enable_ai": "true", "ai_provider": "cursor"},
        ),
        (
            "合併前 DailyStockPush 手動觸發、勾 Gemini",
            "workflow_dispatch", "push", "gemini",
            {"stage": "push", "run_scan": "false", "run_push": "true",
             "enable_ai": "true", "ai_provider": "gemini"},
        ),
        (
            "合併前 DailyStockPush 的 repository_dispatch (trigger-push)：只推播、固定用 Cursor",
            "repository_dispatch", "", "",
            {"stage": "push", "run_scan": "false", "run_push": "true",
             "enable_ai": "true", "ai_provider": "cursor"},
        ),
        (
            "排程觸發 (inputs 為空字串)：跑完整 pipeline、固定用 Cursor",
            "schedule", "", "",
            {"stage": "full", "run_scan": "true", "run_push": "true",
             "enable_ai": "true", "ai_provider": "cursor"},
        ),
        (
            "新增能力：只跑全市場掃描、不推播也不花 AI Token",
            "workflow_dispatch", "scan", "none",
            {"stage": "scan", "run_scan": "true", "run_push": "false",
             "enable_ai": "false", "ai_provider": ""},
        ),
        (
            "大小寫與前後空白容錯",
            "workflow_dispatch", " FULL ", " Gemini ",
            {"stage": "full", "run_scan": "true", "run_push": "true",
             "enable_ai": "true", "ai_provider": "gemini"},
        ),
    ]

    for label, event, raw_stage, raw_provider, expected in cases:
        got = planner.resolve_plan(event, raw_stage, raw_provider)
        check(label, got == expected, f"got={got}" if got != expected else "")

    for bad_stage in ("deploy", "true", "1. 完整模式"):
        try:
            planner.resolve_plan("workflow_dispatch", bad_stage, "none")
            ok = False
        except ValueError:
            ok = True
        check(f"未知 stage '{bad_stage}' 應直接報錯而非默默跑錯階段", ok)

    for bad_provider in ("openai", "true"):
        try:
            planner.resolve_plan("workflow_dispatch", "push", bad_provider)
            ok = False
        except ValueError:
            ok = True
        check(f"未知 ai_provider '{bad_provider}' 應直接報錯", ok)


def verify_plan_cli() -> None:
    hr("2. 執行計畫的 GITHUB_OUTPUT 介面")

    def run_plan(event: str, stage: str, provider: str) -> tuple[int, dict[str, str], str]:
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "github_output"
            out_path.touch()
            env = dict(os.environ)
            env.update({
                "EVENT_NAME": event,
                "RAW_STAGE": stage,
                "RAW_AI_PROVIDER": provider,
                "GITHUB_OUTPUT": str(out_path),
            })
            proc = subprocess.run(
                [sys.executable, str(PLAN_SCRIPT)],
                env=env, capture_output=True, text=True,
            )
            outputs = {}
            for line in out_path.read_text(encoding="utf-8").splitlines():
                if "=" in line:
                    key, _, value = line.partition("=")
                    outputs[key] = value
            return proc.returncode, outputs, proc.stdout

    code, outputs, stdout = run_plan("workflow_dispatch", "full", "cursor")
    check("完整流程：exit code 0", code == 0, f"exit={code}")
    check(
        "完整流程：五個 output 都有寫入 $GITHUB_OUTPUT",
        set(outputs) == {"stage", "run_scan", "run_push", "enable_ai", "ai_provider"},
        f"keys={sorted(outputs)}",
    )
    check(
        "完整流程：output 值正確",
        outputs.get("run_scan") == "true" and outputs.get("run_push") == "true"
        and outputs.get("enable_ai") == "true" and outputs.get("ai_provider") == "cursor",
        f"outputs={outputs}",
    )
    check("執行計畫會印在 log 上便於事後追查", "執行計畫" in stdout, stdout.strip())

    code, outputs, _ = run_plan("workflow_dispatch", "push", "none")
    check(
        "不啟用 AI 時 ai_provider 要輸出空字串（避免腳本自動挑到別家 AI）",
        code == 0 and outputs.get("enable_ai") == "false" and outputs.get("ai_provider") == "",
        f"exit={code} outputs={outputs}",
    )

    code, outputs, _ = run_plan("workflow_dispatch", "bogus", "none")
    check("非法 stage 應以非 0 exit code 讓 job 立刻失敗", code != 0, f"exit={code}")
    check("非法 stage 不得寫出任何 output", outputs == {}, f"outputs={outputs}")


# ==========================================
# 3. workflow 結構
# ==========================================
def _steps(job: dict) -> list[dict]:
    return job.get("steps") or []


def _find_step(steps: list[dict], needle: str) -> dict | None:
    for step in steps:
        if needle in str(step.get("run", "")):
            return step
    return None


def verify_workflow() -> None:
    hr("3. 合併後的 workflow 結構 (.github/workflows/main.yml)")

    check("原 DailyStockPush 的 daily_stock.yml 已併入而不再存在", not MERGED_AWAY.exists())

    raw = WORKFLOW.read_text(encoding="utf-8")
    cfg = yaml.safe_load(raw)
    # PyYAML 會把 YAML 1.1 的 `on:` 當成布林 True 當 key
    triggers = cfg.get("on", cfg.get(True)) or {}

    check("workflow 名稱維持 DailyStockBot", cfg.get("name") == "DailyStockBot", str(cfg.get("name")))
    check("permissions 維持唯讀", (cfg.get("permissions") or {}).get("contents") == "read")

    check(
        "repository_dispatch 的 trigger-push 入口保留",
        (triggers.get("repository_dispatch") or {}).get("types") == ["trigger-push"],
        str(triggers.get("repository_dispatch")),
    )
    check("schedule 維持註解停用狀態", "schedule" not in triggers)
    check(
        "註解裡仍保留原本的 cron 設定供日後啟用",
        re.search(r"#\s*-\s*cron:\s*'10 08 \* \* 1-5'", raw) is not None,
    )

    inputs = (triggers.get("workflow_dispatch") or {}).get("inputs") or {}
    check("workflow_dispatch 只保留 stage / ai_provider 兩個輸入",
          set(inputs) == {"stage", "ai_provider"}, f"inputs={sorted(inputs)}")

    stage_input = inputs.get("stage") or {}
    check("stage 是下拉選單且涵蓋 full/scan/push",
          stage_input.get("type") == "choice" and stage_input.get("options") == ["full", "scan", "push"],
          str(stage_input.get("options")))
    check("stage 預設為 full（等同合併前 DailyStockBot 的行為）",
          stage_input.get("default") == "full", str(stage_input.get("default")))

    provider_input = inputs.get("ai_provider") or {}
    check("ai_provider 是下拉選單且涵蓋 none/cursor/gemini",
          provider_input.get("type") == "choice"
          and provider_input.get("options") == ["none", "cursor", "gemini"],
          str(provider_input.get("options")))
    check("ai_provider 預設為 none，手動觸發不會意外消耗 Token",
          provider_input.get("default") == "none", str(provider_input.get("default")))
    check("兩家 AI 改用單一下拉選單，結構上不可能同時選中（原本的互斥檢查步驟可移除）",
          provider_input.get("type") == "choice")

    jobs = cfg.get("jobs") or {}
    check("合併成單一 job，掃描與推播共用同一次 checkout 與套件安裝", len(jobs) == 1, f"jobs={sorted(jobs)}")
    job = next(iter(jobs.values()))
    check("timeout 取兩階段總和 270 分鐘", job.get("timeout-minutes") == 270, str(job.get("timeout-minutes")))
    check("runner 維持 ubuntu-latest", job.get("runs-on") == "ubuntu-latest", str(job.get("runs-on")))

    steps = _steps(job)
    uses = [str(s.get("uses", "")) for s in steps]
    check("保留 actions/checkout@v4", any(u.startswith("actions/checkout@v4") for u in uses), str(uses))
    check("保留 actions/setup-python@v5 與 pip 快取",
          any(u.startswith("actions/setup-python@v5") for u in uses)
          and any((s.get("with") or {}).get("cache") == "pip" for s in steps))
    check("Python 版本維持 3.10",
          any(str((s.get("with") or {}).get("python-version")) == "3.10" for s in steps))

    plan_step = _find_step(steps, "resolve_daily_stock_plan.py")
    check("有呼叫執行計畫解析腳本的步驟", plan_step is not None)
    if plan_step:
        check("執行計畫步驟的 id 為 plan（後續步驟靠它取 outputs）",
              plan_step.get("id") == "plan", str(plan_step.get("id")))
        plan_env = plan_step.get("env") or {}
        check("執行計畫步驟有接 event_name 與兩個 inputs",
              set(plan_env) == {"EVENT_NAME", "RAW_STAGE", "RAW_AI_PROVIDER"}, f"env={sorted(plan_env)}")
        check("RAW_STAGE 讀 github.event.inputs.stage",
              "github.event.inputs.stage" in str(plan_env.get("RAW_STAGE")))
        check("RAW_AI_PROVIDER 讀 github.event.inputs.ai_provider",
              "github.event.inputs.ai_provider" in str(plan_env.get("RAW_AI_PROVIDER")))
        check("執行計畫步驟排在掃描與推播之前",
              steps.index(plan_step) < min(
                  steps.index(_find_step(steps, "DailyStockBot.py")),
                  steps.index(_find_step(steps, "DailyStockPush.py")),
              ))

    install_step = _find_step(steps, "pip install")
    check("有安裝套件的步驟", install_step is not None)
    if install_step:
        install_cmd = str(install_step.get("run"))
        # 兩支腳本的第三方 import（import 名 -> pip 套件名）
        for pip_name in ("yfinance", "pandas", "numpy", "requests", "FinMind",
                         "gspread", "oauth2client", "google-genai"):
            check(f"套件安裝涵蓋 {pip_name}", pip_name in install_cmd)

    scan_step = _find_step(steps, "python DailyStockBot.py")
    push_step = _find_step(steps, "python DailyStockPush.py")
    check("保留執行 DailyStockBot.py 的步驟", scan_step is not None)
    check("保留執行 DailyStockPush.py 的步驟", push_step is not None)

    if scan_step:
        check("掃描步驟由 plan 的 run_scan 決定是否執行",
              str(scan_step.get("if")) == "steps.plan.outputs.run_scan == 'true'", str(scan_step.get("if")))
        scan_env = scan_step.get("env") or {}
        for name in ("LINE_ACCESS_TOKEN", "LINE_USER_ID", "GOOGLE_SHEETS_JSON"):
            check(f"掃描步驟有傳入 {name}", name in scan_env, f"env={sorted(scan_env)}")

    if push_step:
        check("推播步驟由 plan 的 run_push 決定是否執行",
              str(push_step.get("if")) == "steps.plan.outputs.run_push == 'true'", str(push_step.get("if")))
        push_env = push_step.get("env") or {}
        for name in ("LINE_ACCESS_TOKEN", "GOOGLE_SHEETS_JSON", "GEMINI_API_KEY",
                     "CURSOR_API_KEY", "MAIL_USERNAME", "MAIL_PASSWORD"):
            check(f"推播步驟有傳入 {name}", name in push_env, f"env={sorted(push_env)}")
        check("ENABLE_AI 來自 plan 的 outputs",
              "steps.plan.outputs.enable_ai" in str(push_env.get("ENABLE_AI")), str(push_env.get("ENABLE_AI")))
        check("AI_PROVIDER 來自 plan 的 outputs",
              "steps.plan.outputs.ai_provider" in str(push_env.get("AI_PROVIDER")), str(push_env.get("AI_PROVIDER")))
        check("推播步驟同時具備 Gemini 與 Cursor 金鑰，兩種提供者都能用",
              "GEMINI_API_KEY" in push_env and "CURSOR_API_KEY" in push_env)

    run_blocks = [str(s.get("run", "")) for s in steps]
    check(
        "沒有任何步驟把 Google 金鑰寫成 .json 檔（維持環境變數傳遞）",
        not any("GOOGLE_SHEETS_JSON" in block for block in run_blocks),
    )
    leaked = [
        (name, block) for block in run_blocks for name in SECRET_NAMES
        if re.search(rf"echo[^\n]*{name}", block)
    ]
    check("沒有任何步驟在 log 印出 secret", not leaked, str(leaked))


# ==========================================
# 4. 腳本端契約
# ==========================================
def verify_script_contracts() -> None:
    hr("4. 腳本端契約 (DailyStockBot.py / DailyStockPush.py)")

    scan_src = SCAN_SCRIPT.read_text(encoding="utf-8")
    push_src = PUSH_SCRIPT.read_text(encoding="utf-8")

    # 取出 DailyStockPush.py 真正判斷 ENABLE_AI 的那行運算式單獨評估，
    # 避免 import 整個模組（module 層會呼叫 FinMind 抓台股清單）。
    match = re.search(r"enable_ai_env\s*=\s*(.+)", push_src)
    check("DailyStockPush.py 仍以 ENABLE_AI 環境變數控制 AI 開關", match is not None)
    if match:
        expr = match.group(1).strip()

        def enable_ai(value: str | None) -> bool:
            fake = {} if value is None else {"ENABLE_AI": value}
            namespace = {"os": types.SimpleNamespace(getenv=lambda k, d=None: fake.get(k, d))}
            return bool(eval(expr, namespace))  # noqa: S307 - 刻意評估被測運算式

        check("ENABLE_AI='true' 會啟用 AI", enable_ai("true") is True)
        check("ENABLE_AI='false' 會關閉 AI", enable_ai("false") is False)
        check(
            "ENABLE_AI='' 會被當成關閉，所以 workflow 必須永遠明確給 true/false",
            enable_ai("") is False,
        )
        check("ENABLE_AI 未設定時預設啟用 AI", enable_ai(None) is True)

    check(
        "DailyStockPush.py 接受 AI_PROVIDER 為 cursor 或 gemini",
        'os.getenv("AI_PROVIDER"' in push_src
        and re.search(r'provider\s+in\s+\("gemini",\s*"cursor"\)', push_src) is not None,
    )

    check(
        "DailyStockBot.py 負責寫入 WATCH_LIST（推播階段的資料來源）",
        "update_watch_list_sheet" in scan_src
        and 'client.open("WATCH_LIST")' in scan_src
        and "append_rows" in scan_src,
    )
    check(
        "DailyStockPush.py 負責讀取 WATCH_LIST，因此掃描必須先於推播",
        "get_watch_list_from_sheet" in push_src
        and 'client.open("WATCH_LIST")' in push_src
        and "get_all_records" in push_src,
    )
    check(
        "DailyStockBot.py 未使用 numpy 以外的額外相依（僅 import，可安全保留安裝）",
        "import numpy" in scan_src,
    )


def main() -> int:
    verify_plan_logic()
    verify_plan_cli()
    verify_workflow()
    verify_script_contracts()

    hr("驗證結果")
    print(f"PASS: {PASS}    FAIL: {FAIL}")
    if FAIL:
        print("❌ 有項目未通過，請先修正再 push。")
        return 1
    print("✅ 全部通過。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
