# PMWC Register Sync (SharePoint → Confluence → Jira)

Scheduled by GitHub Actions workflow `.github/workflows/sync-register.yml`.

## Cron (Taiwan UTC+8)

- 09:00 → `0 1 * * *` (01:00 UTC)
- 17:00 → `0 9 * * *` (09:00 UTC)

Also supports `workflow_dispatch` and `repository_dispatch` (`sync-register`).

## Required GitHub Secrets

1. **ATLASSIAN_API_TOKEN** — Atlassian API token (same as local `.env`)
2. **CONFIG_YAML** — full contents of `config.yaml` (do not commit real `config.yaml`)

Set at: https://github.com/bb00lin/my-bot/settings/secrets/actions

## Local reference

- Example config: `config.example.yaml`
- Dependencies: `requirements-sync.txt` (does not replace repo `requirements.txt`)
- Manual/Windows backup: `scripts/install_windows_schedule.ps1`
