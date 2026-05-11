# Boondockers Helper

Log and visualise battery data from a Victron BMV-712 Smart battery monitor via Bluetooth.
Replaces the manual screenshot-to-spreadsheet workflow with a live dashboard, interactive
HTML reports, and per-session charge-type tracking.

**Platforms:** macOS · Linux (Raspberry Pi)  
**Requirements:** Python 3.10+

---

## Known Limitations (read before you start)

- **Solar charging is not modelled.** The tool logs the effect of solar on battery SOC
  (rising SOC, positive current), but there is no solar-specific analysis. If your
  primary charging is solar, the charge rate numbers will be misleading. Solar support
  is planned but not yet built.
- **Alpha software.** Expect rough edges; feedback welcome.
- **macOS only for now:** Linux (Raspberry Pi) support exists but is less tested.
  Windows is untested.

---

## Hardware Required

- **Victron BMV-712 Smart** battery monitor (the Bluetooth model; the 700 without
  Smart/Bluetooth will not work)
- A computer with Bluetooth LE within range of the BMV-712

---

## Installation

```bash
git clone <repo-url>
cd boondockers-helper/src
bash setup/setup.sh
```

The setup wizard will guide you through BLE device discovery, encryption key entry,
`config.ini` creation, a live connection test, and optional background agent installation.

> `config.ini` is gitignored and will never be committed. Your encryption key stays on your machine.

---

## Running

### Continuous logging

```bash
python3 victron/logger.py
```

Polls every `poll_interval_minutes`. Leave this running or set it up as a launchd agent
(see `setup/com.victron.logger.plist`).

### Live dashboard

```bash
./start_dashboard.sh
```

Opens a browser tab at `http://localhost:8050`. The dashboard shows:

- **Summary cards** — current SOC, usage rate, time remaining at current rate,
  time remaining at 7-day average, charging estimates
- **State of Charge chart** — SOC over time with voltage (V) and current (A) overlaid
- **Daily Battery Usage** — bar chart of % SOC consumed per calendar day
- **Charge Rate** — bar chart of charging speed per session, grouped by type
  (Shore / Generator / Driving / Unclassified)

Hit **Refresh** to reload all data from the database.  
Hit **Download Report** to save a standalone HTML file.

### HTML report (standalone)

```bash
python3 -m victron.report               # last 30 days, opens in browser
python3 -m victron.report --days 14     # last 14 days
python3 -m victron.report --no-open     # generate file without opening
```

Reports are saved to `reports/report_YYYYMMDD_HHMMSS.html`.

---

## Dashboard Tips

- **Session notes**: click the Notes cell in the Discharge or Charging Sessions table to
  add a comment. Notes appear as hover tooltips on the SOC chart.
- **Charge types**: use the Type dropdown in the Charging Sessions table to label each
  session as Shore, Generator, or Driving. Sessions labelled Shore or Driving are
  **excluded from the summary cards** — they skew the averages when you are plugged in.
- Notes and charge types save automatically to the local database.

---

## macOS Background Agent (launchd)

The setup wizard installs this automatically. To manage it manually:

```bash
# Check it's running
launchctl list | grep victron
tail -f ~/Library/Logs/victron-logger.log

# Stop
launchctl unload ~/Library/LaunchAgents/com.victron.logger.plist

# Reinstall (e.g. after moving the project folder)
bash setup/setup.sh --skip-config
```

---

## Tips for Good Data

- The more data you collect, the more accurate the averages become. Give it a week of
  normal usage before trusting the 7-day summary cards.
- The logger handles gaps gracefully — sessions are split when the gap exceeds
  `max_gap_hours`. You can stop and restart the logger freely.
- Label your charging sessions (Shore / Generator / Driving) as they come in. The
  summary cards only use Generator sessions by default once you start labelling.

---

## File Layout

```
src/
├── victron/
│   ├── logger.py        — BLE polling daemon
│   ├── report.py        — HTML report generator
│   └── app.py           — Plotly Dash live dashboard
├── tests/               — unit tests (pytest)
├── setup/               — launchd (macOS) and systemd (Linux) service files
├── config.ini           — your credentials and settings (gitignored)
├── config.ini.example   — safe template — copy this to config.ini
├── victron_data.db      — SQLite database (gitignored, created on first run)
├── reports/             — generated HTML reports (gitignored)
└── requirements.txt     — Python dependencies
```

---

## Running Tests

```bash
./run_tests.sh
```

Tests use synthetic in-memory data — no device or database required.
