# AGENTS.md

## Cursor Cloud specific instructions

### What this repo is
`my-stock-bot` is a personal **automation monorepo**: ~15 standalone Python scripts, each a one-shot batch job triggered by a GitHub Actions workflow in `.github/workflows/` (mostly on a schedule or `workflow_dispatch`). There is **no long-running server / web app** (despite the name, `stm32_dashboard.py` is a CLI script, not a web dashboard). There is no shared package, no test suite, and no lint config — each `.py` file at the repo root is independent.

### Environment / running
- Dependencies are installed into a project virtualenv at `.venv/` (see the startup update script). Run scripts with `.venv/bin/python <script>.py` (or activate with `. .venv/bin/activate` first).
- The update script installs the system package `python3.12-venv` implicitly via the snapshot; that apt package was installed during setup and persists in the VM snapshot. If `python3 -m venv` ever fails with an `ensurepip` error, reinstall it: `sudo apt-get install -y python3.12-venv`.
- All deps come from `requirements.txt`. Note `pandas-ta-classic` is pulled from a GitHub zip URL and is built from source on install (slow-ish, needs network).
- Workflows pin Python 3.9/3.10/3.11 per script, but everything installs and runs fine on the VM's Python 3.12 for development.

### "Lint" / build / test
- There is no linter or test framework configured. The closest "build/lint" check is byte-compiling: `.venv/bin/python -m py_compile *.py` (all 15 files currently compile clean).
- To smoke-test the core stock logic **without any secrets**, run: `.venv/bin/python ManualStock.py 2330`. It fetches live data from Yahoo Finance + FinMind and prints a diagnosis; Google Sheets sync and LINE push are optional and are safely skipped (they only log a warning) when their secrets are absent. This is the recommended "hello world".

### Secrets (all optional for local smoke tests, required for real end-to-end runs)
Scripts read credentials from env vars (see `os.getenv` in each file). Set these as Cursor Secrets if you need full end-to-end behavior:
- `GOOGLE_SHEETS_JSON` — service-account JSON string; used by the stock bots, `bom_manager.py`, `guardian_bot.py`, `mix_guardian_bot.py`, `cosing_automation.py`, `stm32_dashboard.py`.
- `CONF_URL`, `CONF_USER`, `CONF_PASS` — Confluence/Jira scripts (`confluence_*.py`, `monthly_confluence_copy.py`, `daily_worklog_to_confluence.py`, `github_gantt_sync.py`) exit immediately if these are missing.
- `LINE_ACCESS_TOKEN` / `LINE_USER_ID` — LINE push (optional; skipped if absent).
- `GEMINI_API_KEY` or `CURSOR_API_KEY` — only used by `DailyStockPush.py` when `ENABLE_AI` is on.
- `FINMIND_TOKEN` — optional; raises FinMind rate limits.
- `MAIL_USERNAME` / `MAIL_PASSWORD` — Gmail SMTP for the scraper bots.

### Gotchas
- The Selenium scrapers (`guardian_bot.py`, `mix_guardian_bot.py`, `cosing_automation.py`) need a Chrome/Chromedriver install (the workflows install Chrome on the runner). Chrome is **not** part of the default VM or the update script — install it only if you specifically need to work on those scrapers.
- `ManualStock.py` runs `get_stock_name_map()` at import time (a FinMind network call), so importing it requires network access.
- `bom_manager.py` and `stm32_dashboard.py` fall back to interactive `input()` when not in CI; pipe input or set `EXECUTION_MODE` to avoid a hang in non-interactive shells.
