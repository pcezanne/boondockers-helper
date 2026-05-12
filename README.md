# Boondockers' Helper

You're two days into a boondock, 150 miles from the nearest hookup. Your battery is at
62%. Is that fine, or do you need to run the generator tonight?

The Victron app tells you what's happening right now. It doesn't tell you whether today's
drain is normal, how long the generator needs to run, or whether your battery is slowly
getting weaker. You end up guessing — or taking notes on your phone.

Boondockers' Helper fixes that. It logs your battery data continuously in the background,
then gives you a dashboard with the answers that actually matter:

- **How many hours of power do I have left** at today's rate? At my 7-day average?
- **How long do I need to run the generator** to get back to 95%?
- **Am I using more power than usual?** Has something changed?
- **Is my battery getting weaker?** Charge rate trending down? Thermal derating kicking in?
- **Did something drain the battery overnight** while everything was supposed to be off?

Give it a week of data and it knows your patterns. It knows what a normal day looks like,
what your generator does in two hours, and when something is off.

---

## Screenshots

**Summary cards** — the answers you actually need, at a glance:

![Summary cards](docs/images/summary_cards.png)

**State of Charge chart** — SOC, voltage, and current over time, with discharge and
charging sessions shaded. Notes you've added appear as hover tooltips:

![SOC chart with hover note](docs/images/dashboard_soc_hover.png)

**Discharge session table** — every discharge logged, with duration, Ah consumed, %/day
rate, average power, and your notes:

![Discharge table](docs/images/table_discharge.png)

---

## Features

### Summary Cards
- **Usage**: running average and 7-day average battery drain (%/day and Ah/day), implied average power draw
- **Power Remaining**: hours left at today's rate and 7-day average, Ah remaining in bank
- **Full Battery Would Last**: projected days from 100% at each rate
- **Charging**: last charge rate, daily maintenance hours, estimated time to 95% (generator) and 100% (shore)
- Shore and Driving sessions are excluded from all off-grid averages automatically

### State of Charge Chart
- SOC over time with voltage (V) and current (A) overlaid on secondary axes
- Background shading: orange = discharging, green = charging, grey = logger running/idle, white = logger off
- Adaptive downsampling keeps the chart fast on large datasets while preserving full resolution near session boundaries
- Session notes appear as hover tooltips over the shaded regions
- Default view: last 3 days; range buttons for 3d / 7d / 30d / All
- All subplots share the same x-axis and scroll together

### Daily Battery Usage
- % SOC consumed per calendar day, split at midnight
- Running average line; today shown as partial with a note
- 7-day and all-days averages (today excluded — incomplete day)

### Charge Rate History
- One subplot per charging type (Shore, Generator, Driving) with sessions that have data
- **CC/CV phase analysis**: rates reflect the Constant Current phase only — not dragged down by the CV top-off tail
- Rolling 7-session average line and all-time average dashed line, per type
- Knee SOC: where the charger transitioned from CC to CV, tracked per session

### Session Tables
- **Discharge**: Start/End, SOC start/end, Drop, Ah, Duration, %/day, Avg Power, Note
- **Charging**: Start/End, SOC start/end, Gain, Duration, %/hour, CC %/hour, Knee SOC, Avg A, Type
- Notes save automatically; appear as hover tooltips on the SOC chart
- Charge type labels (Shore / Generator / Driving) drive which sessions count toward off-grid averages
- Shore Power checkbox on discharge sessions excludes them from all usage stats
- Tables default to the 10 most recent sessions; a "Show all" checkbox expands to full history

### Diagnostics
Alerts for battery health issues, shown in a panel between the chart and the session tables:
- **Declining charge rate**: recent sessions charging significantly slower than your historical average
- **Thermal derating**: charger current drops >15% from peak within the first 30 minutes
- **Knee SOC drift**: CC→CV transition appearing at a lower SOC than your rolling baseline (early sign of a weak cell)
- **Parasitic drain**: SOC dropping during idle periods when no session was active

### HTML Report
- Same charts as the dashboard, exported as a standalone HTML file
- `python3 -m victron.report` or via the **Download Report** button in the dashboard

### Background Logger
- Polls the BMV-712 over Bluetooth LE on a configurable interval (default: 1 minute)
- Installs as a macOS launchd agent — logs automatically whenever your Mac is on, with no terminal open

---

## Known Limitations

- **Solar charging is not modelled.** The tool logs the effect of solar on battery SOC
  but there is no solar-specific analysis. If solar is your primary charging source, the
  charge rate numbers will be misleading. Solar support is planned.
- **Alpha software.** Expect rough edges; feedback welcome.
- **macOS only for now.** Linux (Raspberry Pi) support exists but is less tested.
  Windows is untested.

---

## What You Need

- **Victron BMV-712 Smart** battery monitor (the Bluetooth model — the 700 series without
  "Smart" in the name will not work)
- A Mac with Bluetooth, running macOS and Python 3.10+
- The **VictronConnect** app on your phone to retrieve the encryption key (one-time setup)

---

## Installation

```bash
git clone https://github.com/pcezanne/boondockers-helper.git
cd boondockers-helper/src
bash setup/setup.sh
```

The setup wizard walks you through finding your device UUID, entering your encryption key,
and optionally installing a background agent that logs automatically whenever your Mac is on.

> Your encryption key stays on your machine — it is never committed to git.

---

## Running

### Continuous logging

```bash
python3 victron/logger.py
```

Polls every `poll_interval_minutes` (default 1). The setup wizard can install this as a
macOS launchd agent so it runs automatically in the background.

### Live dashboard

```bash
./start_dashboard.sh
```

Opens a browser tab at `http://localhost:8050`. Hit **Refresh** to reload data.
Hit **Download Report** to save a standalone HTML file.

### HTML report

```bash
python3 -m victron.report               # last 30 days, opens in browser
python3 -m victron.report --days 14     # last 14 days
python3 -m victron.report --no-open     # generate without opening
```

---

## macOS Background Agent

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

- Give it at least a week before trusting the 7-day averages.
- The logger handles gaps gracefully — you can close your laptop freely.
- Label your charging sessions as they come in. The summary cards use only Generator
  sessions once you start labelling, keeping your off-grid averages clean.

---

## File Layout

```
src/
├── victron/
│   ├── logger.py        — BLE polling daemon
│   ├── report.py        — HTML report generator
│   └── app.py           — Plotly Dash live dashboard
├── tests/               — unit tests (pytest)
├── setup/               — setup wizard and background agent files
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
