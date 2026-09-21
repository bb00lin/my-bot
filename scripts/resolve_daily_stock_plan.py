#!/usr/bin/env python3

"""解析 DailyStockBot workflow 的執行計畫（要跑哪些階段、用哪家 AI）。

由 .github/workflows/main.yml 的「解析執行計畫」步驟呼叫，只用標準庫，
邏輯才能在本機離線驗證（見 test_daily_stock_workflow_verify.py）。

輸入（環境變數）：
    EVENT_NAME       github.event_name
    RAW_STAGE        github.event.inputs.stage（非 workflow_dispatch 觸發時為空字串）
    RAW_AI_PROVIDER  github.event.inputs.ai_provider（同上）

輸出（附加寫入 $GITHUB_OUTPUT）：
    stage / run_scan / run_push / enable_ai / ai_provider
"""

from __future__ import annotations

import os
import sys


# stage -> (是否執行全市場掃描, 是否執行 AI 戰略推播)
STAGES = {
    "full": (True, True),
    "scan": (True, False),
    "push": (False, True),
}

AI_PROVIDERS = ("none", "cursor", "gemini")

# repository_dispatch (trigger-push) 沿用合併前 DailyStockPush 的行為：只跑推播
DEFAULT_STAGE_BY_EVENT = {"repository_dispatch": "push"}
DEFAULT_STAGE = "full"

# 排程與 repository_dispatch 沿用合併前的行為：自動觸發一律用 Cursor API
DEFAULT_AI_PROVIDER = "cursor"


def resolve_plan(event_name: str, raw_stage: str, raw_ai_provider: str) -> dict[str, str]:
    """把觸發事件與 inputs 轉成執行計畫。

    排程觸發時 github.event.inputs.* 是空字串，一律視為「使用預設值」而非 false。
    """
    stage = (raw_stage or "").strip().lower()
    if not stage:
        stage = DEFAULT_STAGE_BY_EVENT.get(event_name, DEFAULT_STAGE)
    if stage not in STAGES:
        raise ValueError(f"未知的 stage：{stage}（可用：{', '.join(STAGES)}）")

    provider = (raw_ai_provider or "").strip().lower()
    if not provider:
        provider = DEFAULT_AI_PROVIDER
    if provider not in AI_PROVIDERS:
        raise ValueError(f"未知的 ai_provider：{provider}（可用：{', '.join(AI_PROVIDERS)}）")

    run_scan, run_push = STAGES[stage]
    enable_ai = provider != "none"

    return {
        "stage": stage,
        "run_scan": "true" if run_scan else "false",
        "run_push": "true" if run_push else "false",
        "enable_ai": "true" if enable_ai else "false",
        "ai_provider": provider if enable_ai else "",
    }


def main() -> int:
    try:
        plan = resolve_plan(
            os.environ.get("EVENT_NAME", ""),
            os.environ.get("RAW_STAGE", ""),
            os.environ.get("RAW_AI_PROVIDER", ""),
        )
    except ValueError as exc:
        print(f"❌ {exc}")
        return 1

    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as fh:
            for key, value in plan.items():
                fh.write(f"{key}={value}\n")

    print(
        f"📋 執行計畫：stage={plan['stage']}"
        f" / 全市場掃描={plan['run_scan']}"
        f" / AI 戰略推播={plan['run_push']}"
        f" / ENABLE_AI={plan['enable_ai']}"
        f" / AI_PROVIDER={plan['ai_provider'] or '(未啟用)'}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
