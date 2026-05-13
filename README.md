# CC Budget Dashboard

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=flat-square)](LICENSE)
[![claude-code](https://img.shields.io/badge/claude--code-black?style=flat-square)](https://claude.ai/code)

**Track your Claude Code API budget runway on a workday-pacing basis, locally and offline.**

A self-contained, single-localhost-port dashboard for developers who pay-as-they-go on the Anthropic API. Shows how much of your monthly budget you can spend today, how many workdays remain, and whether you're on pace, behind, or about to run dry.

This is a fork of [phuryn/claude-usage](https://github.com/phuryn/claude-usage) — credit there for the original JSONL scanner. This fork keeps the scanner, drops the original "explore your usage" dashboard, and replaces it with a budget-focused one.

---

## What it does

Claude Code writes a JSONL transcript for every session under `~/.claude/projects/`. Each assistant turn includes token usage, model, and — starting in Claude Code v2.1.97 — a precise `costUSD` field computed from the actual API response. This tool:

1. **Scans** those JSONL files into a local SQLite database (`~/.claude/usage.db`), incrementally and per-message-id deduplicated.
2. **Serves** a single-page dashboard at `http://127.0.0.1:8099` that pages against the DB to answer one question: *what's my budget runway today?*

Nothing leaves your machine. Chart.js is vendored — zero external requests at runtime.

---

## Prerequisites

- Python 3.8+ (only stdlib — no `pip install`, no virtual environment).
- Claude Code v2.1.97+ for `costUSD` precision. Older sessions still work; the dashboard falls back to per-token pricing for any row that doesn't have `costUSD`.

---

## Setup

```bash
git clone <your-fork-url> cc-budget
cd cc-budget

# Copy and edit the config (the server will auto-copy on first run if you skip this)
cp budget_config.example.json budget_config.json
# Open budget_config.json and set monthly_budget to your actual API budget.

# Populate the database from your JSONL transcripts
python cli.py scan

# Start the dashboard
python budget_dashboard.py
# Then open http://127.0.0.1:8099
```

The server runs scan on every API request, so new Claude Code sessions appear within a few seconds without restarting.

### Config (`budget_config.json`)

| Field | Default | Notes |
|---|---|---|
| `monthly_budget` | `500` | Your monthly API budget in `currency_symbol`. |
| `port` | `8099` | Localhost port to bind. |
| `db_path` | `~/.claude/usage.db` | SQLite path the scanner writes. |
| `refresh_interval_seconds` | `30` | Auto-refresh cadence in the browser. |
| `currency_symbol` | `$` | Display only — does not convert. |

`budget_config.json` is gitignored.

---

## How the workday math works

The dashboard treats Mon–Fri as workdays and weekends as zero-spend buffer:

- `total_workdays` — count of Mon–Fri in the current calendar month
- `workdays_elapsed` — Mon–Fri from the 1st through today (inclusive if today is a workday)
- `workdays_remaining` — `max(1, total_workdays - workdays_elapsed)`
- `daily_budget` — `(monthly_budget - month_to_date_spend) / workdays_remaining`
- `todays_runway` — `daily_budget - today_spend`

Weekend spend draws from the same monthly pool but adds nothing to the denominator. The intent is to give yourself room to ship hard on weekdays without blowing past month-end.

**Projection / surplus:**
- `avg_workday_spend` — `month_to_date_spend / max(1, workdays_elapsed)`
- `projected_month_end` — current MTD spend plus `avg_workday_spend × workdays_remaining`
- `projected_surplus` — `monthly_budget - projected_month_end`
- Status colours: **green** (surplus > $50) · **yellow** ($0–50) · **red** (< 0). When red, the dashboard computes a *dry date* — the workday at which cumulative spend at current pace exceeds the budget.

The budget-pace bar shows MTD spend as a filled bar with a vertical marker at the expected position (`workdays_elapsed / total_workdays`). If the bar runs ahead of the marker, you're spending faster than pace.

---

## Calibration (optional)

If you want the dashboard to know how much your local estimate drifts from Anthropic's actual invoiced amount, drop a `calibration.json` in the repo root:

```json
{
  "calibrations": [
    { "date": "2026-05-13", "actual_spend": 47.23, "local_estimate": 44.80 }
  ]
}
```

The dashboard averages drift across all entries and shows a small note under the budget-pace bar (e.g. *"Calibration: local estimates track 5.1% below invoice."*). It does **not** auto-apply a correction to displayed numbers — it's informational, so you can decide whether to adjust `monthly_budget` to account for the gap.

`calibration.json` is gitignored.

---

## Layout

| Section | What it shows |
|---|---|
| Header | "CC Budget Monitor" · month/year · pulsing refresh indicator that flashes on update |
| Today's Runway gauge | Donut sized to fraction of daily budget remaining; green > 66% · yellow 33–66% · red < 33% |
| Month Remaining gauge | Donut of % consumed; remaining dollars + workdays-left sublabel |
| Stat cards | Daily Budget · Avg Daily Spend · Sessions this month · Projected Surplus (coloured, with dry-date sublabel when red) |
| Daily Spend bar chart | One bar per day of the month — amber weekdays, dimmed weekends with red overlay on weekend spend, dashed line at the daily-budget threshold, today highlighted |
| Model Breakdown | Horizontal bars per family (opus / sonnet / haiku) with cost and percentage |
| Budget Pace bar | Filled bar = % consumed · vertical marker at expected position · calibration note when present |

Auto-refresh fetches `/api/summary`, `/api/daily`, `/api/models` every `refresh_interval_seconds`. Each request triggers an incremental scan, so the dashboard stays current while you work.

---

## CLI

The original CLI commands still work for terminal usage:

```bash
python cli.py scan      # Update the database from JSONL files
python cli.py today     # Print today's usage by model
python cli.py week      # Last 7 days
python cli.py stats     # All-time totals
```

---

## Privacy / security

- Server binds to `127.0.0.1` only — not reachable on the LAN.
- Chart.js is vendored under `public/vendor/chart.min.js` — no CDN.
- No telemetry. No outbound HTTP. Your usage data stays in `~/.claude/usage.db`.

---

## File layout

```
cc-budget/
  cli.py                       # scan / today / week / stats subcommands
  scanner.py                   # JSONL → SQLite (with cost_usd column)
  budget_dashboard.py          # HTTP server (stdlib only)
  budget_config.example.json   # template — copy to budget_config.json
  budget_config.json           # your settings (gitignored)
  calibration.json             # optional invoice anchors (gitignored)
  public/
    index.html                 # single-page dashboard (inline CSS/JS)
    vendor/
      chart.min.js             # vendored Chart.js v4.5.1
  tests/                       # scanner + cli unit tests
  LICENSE                      # MIT
```

---

## Credits / license

- Upstream scanner & CLI: [phuryn/claude-usage](https://github.com/phuryn/claude-usage) (MIT)
- Chart.js: [chartjs/Chart.js](https://github.com/chartjs/Chart.js) (MIT)
- This fork: MIT, inherited from upstream.
