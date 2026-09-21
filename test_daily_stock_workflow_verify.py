#!/usr/bin/env python3

"""離線驗證合併後的 DailyStockBot workflow（不需憑證、不連外網）。

驗證三件事：
1. scripts/resolve_daily_stock_plan.py 的真值表，涵蓋合併前四種觸發情境的等價行為。
2. .github/workflows/main.yml 的結構：觸發條件、階段傳遞、secrets 覆蓋率、套件覆蓋率。
3. DailyStockBot.py 的契約：stage 詞彙一致、ENABLE_AI 語意、WATCH_LIST 的先後依賴。

腳本端一律用原始碼比對，必要時只取出被測運算式單獨評估
（DailyStockBot.py 要 import 得先備好 yfinance / FinMind / gspread 等第三方套件，
 完整的行為驗證見 test_stock_pipeline_metrics_verify.py）。
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
WORKFLOW_MERGED_AWAY = ROOT / ".github" / "workflows" / "daily_stock.yml"
PLAN_SCRIPT = ROOT / "scripts" / "resolve_daily_stock_plan.py"
PIPELINE_SCRIPT = ROOT / "DailyStockBot.py"
SCRIPT_MERGED_AWAY = ROOT / "DailyStockPush.py"

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
            {"stage": "full", "enable_ai": "true", "ai_provider": "cursor"},
        ),
        (
            "合併前 DailyStockPush 手動觸發、兩個勾選框都不勾：只推播、不啟用 AI",
            "workflow_dispatch", "push", "none",
            {"stage": "push", "enable_ai": "false", "ai_provider": ""},
        ),
        (
            "合併前 DailyStockPush 手動觸發、勾 Cursor",
            "workflow_dispatch", "push", "cursor",
            {"stage": "push", "enable_ai": "true", "ai_provider": "cursor"},
        ),
        (
            "合併前 DailyStockPush 手動觸發、勾 Gemini",
            "workflow_dispatch", "push", "gemini",
            {"stage": "push", "enable_ai": "true", "ai_provider": "gemini"},
        ),
        (
            "合併前 DailyStockPush 的 repository_dispatch (trigger-push)：只推播、固定用 Cursor",
            "repository_dispatch", "", "",
            {"stage": "push", "enable_ai": "true", "ai_provider": "cursor"},
        ),
        (
            "排程觸發 (inputs 為空字串)：跑完整 pipeline、固定用 Cursor",
            "schedule", "", "",
            {"stage": "full", "enable_ai": "true", "ai_provider": "cursor"},
        ),
        (
            "新增能力：只跑全市場掃描、不推播也不花 AI Token",
            "workflow_dispatch", "scan", "none",
            {"stage": "scan", "enable_ai": "false", "ai_provider": ""},
        ),
        (
            "大小寫與前後空白容錯",
            "workflow_dispatch", " FULL ", " Gemini ",
            {"stage": "full", "enable_ai": "true", "ai_provider": "gemini"},
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
        "完整流程：三個 output 都有寫入 $GITHUB_OUTPUT",
        set(outputs) == {"stage", "enable_ai", "ai_provider"},
        f"keys={sorted(outputs)}",
    )
    check(
        "完整流程：output 值正確",
        outputs.get("stage") == "full"
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

    check("原 DailyStockPush 的 daily_stock.yml 已併入而不再存在", not WORKFLOW_MERGED_AWAY.exists())
    check("原 DailyStockPush.py 已併入 DailyStockBot.py 而不再存在", not SCRIPT_MERGED_AWAY.exists())

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
        check("執行計畫步驟排在 pipeline 步驟之前",
              steps.index(plan_step) < steps.index(_find_step(steps, "DailyStockBot.py")))

    install_step = _find_step(steps, "pip install")
    check("有安裝套件的步驟", install_step is not None)
    if install_step:
        install_cmd = str(install_step.get("run"))
        # 合併後 DailyStockBot.py 的第三方 import（import 名 -> pip 套件名）
        for pip_name in ("yfinance", "pandas", "requests", "FinMind",
                         "gspread", "oauth2client", "google-genai"):
            check(f"套件安裝涵蓋 {pip_name}", pip_name in install_cmd)
        for dropped in ("numpy", "tqdm"):
            check(f"合併後未使用的 {dropped} 已從安裝清單移除", dropped not in install_cmd)

    pipeline_step = _find_step(steps, "DailyStockBot.py")
    check("合併成單一執行步驟", pipeline_step is not None)
    check("不再有獨立執行 DailyStockPush.py 的步驟",
          _find_step(steps, "DailyStockPush.py") is None)

    if pipeline_step:
        check("pipeline 步驟以 --stage 傳入階段",
              "--stage" in str(pipeline_step.get("run")), str(pipeline_step.get("run")).strip())
        check("pipeline 步驟沒有 if 條件（階段改由腳本自行判斷）",
              pipeline_step.get("if") is None, str(pipeline_step.get("if")))
        step_env = pipeline_step.get("env") or {}
        for name in ("LINE_ACCESS_TOKEN", "LINE_USER_ID", "GOOGLE_SHEETS_JSON",
                     "GEMINI_API_KEY", "CURSOR_API_KEY", "MAIL_USERNAME", "MAIL_PASSWORD"):
            check(f"pipeline 步驟有傳入 {name}", name in step_env, f"env={sorted(step_env)}")
        check("STAGE 來自 plan 的 outputs",
              "steps.plan.outputs.stage" in str(step_env.get("STAGE")), str(step_env.get("STAGE")))
        check("ENABLE_AI 來自 plan 的 outputs",
              "steps.plan.outputs.enable_ai" in str(step_env.get("ENABLE_AI")), str(step_env.get("ENABLE_AI")))
        check("AI_PROVIDER 來自 plan 的 outputs",
              "steps.plan.outputs.ai_provider" in str(step_env.get("AI_PROVIDER")), str(step_env.get("AI_PROVIDER")))
        check("同時具備 Gemini 與 Cursor 金鑰，兩種提供者都能用",
              "GEMINI_API_KEY" in step_env and "CURSOR_API_KEY" in step_env)

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
    hr("4. 腳本端契約 (DailyStockBot.py)")

    src = PIPELINE_SCRIPT.read_text(encoding="utf-8")
    plan_src = PLAN_SCRIPT.read_text(encoding="utf-8")

    # stage 詞彙必須兩邊一致，否則 workflow 傳進來的值腳本會不認
    plan_stages = re.search(r"STAGES\s*=\s*\(([^)]*)\)", plan_src)
    script_stages = re.findall(r'^\s*"(full|scan|push)":', src, re.M)
    check("執行計畫腳本定義了 stage 允許值", plan_stages is not None)
    if plan_stages:
        check(
            "workflow 端與腳本端的 stage 詞彙一致 (full/scan/push)",
            set(re.findall(r'"(\w+)"', plan_stages.group(1))) == set(script_stages) == {"full", "scan", "push"},
            f"plan={plan_stages.group(1).strip()} script={sorted(set(script_stages))}",
        )

    check("腳本以 --stage 參數切換階段", '"--stage"' in src)
    check("未指定 --stage 時改讀 STAGE 環境變數", 'os.getenv("STAGE"' in src)
    check("STAGE 為空字串時視為使用預設值 full", 'env_stage or "full"' in src)
    check("STAGE 值非法時直接報錯而非默默跑錯階段", "parser.error(" in src)

    # 取出真正判斷 ENABLE_AI 的那行運算式單獨評估，避免 import 整個模組
    # （DailyStockBot.py 需要 yfinance / FinMind / gspread 等第三方套件）。
    match = re.search(r"enable_ai_env\s*=\s*(.+)", src)
    check("仍以 ENABLE_AI 環境變數控制 AI 開關", match is not None)
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
        "接受 AI_PROVIDER 為 cursor 或 gemini",
        'os.getenv("AI_PROVIDER"' in src
        and re.search(r'provider\s+in\s+\("gemini",\s*"cursor"\)', src) is not None,
    )
    check(
        "AI 連線測試改由 run_push() 呼叫，掃描階段不會白跑",
        re.search(r"def run_push\(\):(?:.|\n){0,200}check_ai_health\(\)", src) is not None
        and re.search(r"^check_ai_health\(\)", src, re.M) is None,
    )

    check(
        "掃描階段負責寫入 WATCH_LIST（推播階段的資料來源）",
        "def update_watch_list_sheet" in src and "def run_scan" in src,
    )
    check(
        "推播階段負責讀取 WATCH_LIST，因此掃描必須先於推播",
        "def get_watch_list_from_sheet" in src and "get_all_records" in src
        and "def run_push" in src,
    )
    check(
        "main() 依 stage 決定先掃描後推播的順序",
        re.search(r"if do_scan:\s*\n\s*run_scan\(\)\s*\n\s*if do_push:\s*\n\s*run_push\(\)", src) is not None,
    )
    check(
        "兩張不同的報表各有獨立函式，不再共用同名的 sync_to_sheets",
        "def sync_institutional_sheet" in src and "def sync_diagnostic_sheet" in src
        and "def sync_to_sheets" not in src,
    )
    check(
        "台股清單改為延遲載入並快取，兩階段共用只抓一次",
        "def get_taiwan_stock_info_df" in src
        and re.search(r"^STOCK_INFO_MAP\s*=\s*get_global_stock_info\(\)", src, re.M) is None,
    )
    check(
        "共用工具在合併後只剩一份",
        src.count("def get_gspread_client") == 1
        and src.count("def get_tw_stock") == 1
        and src.count("def get_inst_stats") == 1
        and src.count("def get_line_quota_report") == 1,
    )
    check(
        "合併後未使用的 numpy / tqdm import 已移除",
        "import numpy" not in src and "tqdm" not in src,
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
