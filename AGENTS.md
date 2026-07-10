# AGENTS.md

## Cursor Cloud specific instructions

### What this repo is
This repository is a collection of ~15 **independent Python automation scripts**, each wired to
its own GitHub Actions workflow under `.github/workflows/`. There is **no long-running server or
web app** — every script is a one-shot batch job (stock analysis + LINE/email push, Confluence
report sync, CosIng/Guardian Selenium scrapers, an STM32 GPIO planner, a BOM manager, etc.).
Each workflow maps 1:1 to a script (e.g. `main.yml` → `DailyStockBot.py` + `DailyStockPush.py`,
`RunSTM_GPIO.yml` → `stm32_dashboard.py`, `worklog_to_confluence.yml` → `daily_worklog_to_confluence.py`).
Read the matching workflow file to see exactly how a script is invoked and which env vars it needs.

### Python environment
- Dependencies are installed into a virtualenv at `.venv/` (the startup update script builds it).
  Activate it before running anything: `source .venv/bin/activate`.
- The workflows pin Python 3.9–3.11, but the scripts run fine on the VM's Python 3.12.
- `requirements.txt` covers the stock bots + most scripts. Two packages are **not** in
  `requirements.txt` but are still required and are installed by the update script:
  - `google-genai` — used by `DailyStockPush.py` (`from google import genai`).
  - `line-bot-sdk` — used by `daily_worklog_to_confluence.py` (`from linebot import ...`).
  Note `requirements.txt` also ships `google-generativeai`, which is a *different* package from
  `google-genai`; both coexist.
- `python3.12-venv` (apt) is a one-time system prerequisite for `python -m venv`; it is already
  installed in the VM snapshot, so the update script assumes it is present.

### Secrets / why scripts exit early
Most scripts require external credentials passed as env vars and will **`sys.exit` at import time**
if they are missing (this is expected, not a dependency problem). Common env vars:
- Confluence scripts: `CONF_URL`, `CONF_USER`, `CONF_PASS`.
- Stock bots: `LINE_ACCESS_TOKEN`, `LINE_USER_ID`, `GOOGLE_SHEETS_JSON`, and optionally
  `GEMINI_API_KEY` / `CURSOR_API_KEY` (AI diagnosis, gated by `ENABLE_AI` / `AI_PROVIDER`).
- Selenium scrapers (`guardian_bot.py`, `mix_guardian_bot.py`, `cosing_automation.py`):
  `GOOGLE_SHEETS_JSON`, `MAIL_USERNAME`, `MAIL_PASSWORD`; these also need Chrome + a webdriver.
- `GOOGLE_SHEETS_JSON` is the full service-account JSON string (read directly from the env var,
  not a file on disk).

### Lint / test / build / run
- There is **no configured linter and no test suite** in this repo. Use
  `python -m py_compile *.py` from the repo root as the syntax/lint check (all 15 scripts compile).
- "Build" is a no-op (pure Python scripts).
- To run a script, activate the venv and run `python <script>.py`, supplying the env vars that its
  workflow lists. Without those secrets the script will exit early by design.

### Verifying core functionality without secrets
The stock bots' core engine (yfinance data fetch + technical indicators) works with only public
network access — no secrets needed. Quick smoke test:
```bash
source .venv/bin/activate
python -c "import DailyStockBot as bot; s,f=bot.get_tw_stock('2330'); df=s.history(period='3mo'); r,k,d=bot.calculate_indicators(df); print(f, len(df), round(df.iloc[-1]['Close'],2))"
```
This resolves the Taiwan ticker (`2330.TW`), pulls history, and computes RSI/KD using the repo's
own functions.
