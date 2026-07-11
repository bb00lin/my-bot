# AGENTS.md

## Cursor Cloud specific instructions

### What this repo is
`my-bot` is **not a single running application**. It is a collection of ~15 standalone
Python automation scripts at the repo root, each launched independently by a GitHub
Actions workflow in `.github/workflows/` (mostly on cron / `workflow_dispatch`). There is
**no local web server, database, or long-running service to boot** — every script runs to
completion and exits, persisting state through external SaaS APIs (Google Sheets,
Atlassian Confluence/Jira, LINE, Gemini, SMTP).

Rough grouping (see each workflow's `env:` block for the authoritative secret list):
- Stock analysis (the "my-stock-bot" core): `DailyStockPush.py`, `DailyStockBot.py`,
  `stock_bot_final.py`, `ManualStock.py`
- Confluence/Jira reporting: `confluence_api*.py`, `confluence_cleaner.py`,
  `monthly_confluence_copy.py`, `daily_worklog_to_confluence.py`, `github_gantt_sync.py`
- Selenium web-scraping bots: `guardian_bot.py`, `mix_guardian_bot.py`, `cosing_automation.py`
- Hardware pinout planner: `stm32_dashboard.py` (parses local `STM32MP133CAFx.xml`)
- BOM automation: `bom_manager.py`

### Environment
- Python 3.12 in a virtualenv at `.venv/` (workflows pin 3.9–3.11 but 3.12 works fine).
  Activate with `source .venv/bin/activate` before running anything.
- The update script creates `.venv` and installs `requirements.txt` **plus** extra packages
  that several scripts import but that are missing from `requirements.txt`
  (`google-genai`, `line-bot-sdk`, `atlassian-python-api`, `tqdm`, `numpy`,
  `python-dotenv`, `ta`, `lxml`). Note `requirements.txt` pins `google-generativeai`, but
  `DailyStockPush.py`/`DailyStockBot.py` actually `from google import genai`, which comes
  from the separate `google-genai` package.

### Running / testing
- There is **no test framework, no linter config, and no build step**. The de-facto
  syntax check is `python -m py_compile *.py`. "Testing" a bot = running it directly
  (`python <script>.py`) or triggering its workflow via `workflow_dispatch`.
- Almost every script requires runtime **secrets** and network access; without them they
  exit early. Common secrets: `GOOGLE_SHEETS_JSON` (Sheets service-account JSON, the
  datastore for stock/BOM/guardian/cosing/STM32 scripts), `CONF_URL`/`CONF_USER`/`CONF_PASS`
  (Confluence), `LINE_ACCESS_TOKEN`/`LINE_USER_ID`, and optional `GEMINI_API_KEY` /
  `CURSOR_API_KEY` / `FINMIND_TOKEN` / `MAIL_*`.
- Credential-free things that work end-to-end for a smoke test:
  - `stm32_dashboard.py`'s `STM32XMLParser` parses the bundled `STM32MP133CAFx.xml`
    (195 I/O pins, 25 peripherals) with no secrets.
  - Stock scripts' Yahoo Finance fetch (`get_tw_stock` via `yfinance`) needs only
    internet, e.g. `2330.TW` (TSMC) returns real OHLCV data.

### Gotchas
- The three Selenium bots (`guardian_bot.py`, `mix_guardian_bot.py`, `cosing_automation.py`)
  need a real Chrome/Chromedriver runtime (via `webdriver-manager`). Chrome is **not**
  installed by the update script; install it if you need to exercise those scrapers.
- `.github/workflows/` contains a couple of misnamed files (`bom_action.ym`,
  `Confluence Weekly Report2`, `Confluence Weekly2 Report.yml`) — expected, leave as-is.
- `requirements.txt` line 3 installs `pandas-ta-classic` from a GitHub zip URL (builds a
  wheel locally); this is normal and just slower on first install.
