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

Polls every `poll_interval_minutes` (default 15). The setup wizard can install this as a
macOS launchd agent so it runs automatically in the background.

### Live dashboard

```bash
./start_dashboard.sh
```

Opens a browser tab at `http://localhost:8050`. The dashboard shows:

- **Summary cards** — current SOC, usage rate, hours remaining, generator time needed
- **State of Charge chart** — SOC over time with voltage and current overlaid
- **Daily Battery Usage** — % SOC consumed per calendar day, with 7-day average
- **Charge Rate** — charging speed per session, grouped by source (Shore / Generator / Driving)
- **Diagnostics** — alerts for declining charge rate, thermal derating, parasitic drain, and battery knee drift

Hit **Refresh** to reload. Hit **Download Report** to save a standalone HTML file.

### HTML report

```bash
python3 -m victron.report               # last 30 days, opens in browser
python3 -m victron.report --days 14     # last 14 days
python3 -m victron.report --no-open     # generate without opening
```

---

## Dashboard Tips

- **Session notes**: click the Notes cell in either session table to add a comment.
  Notes appear as hover tooltips on the SOC chart.
- **Charge types**: label each charging session as Shore, Generator, or Driving.
  Shore and Driving sessions are excluded from the summary cards so plugged-in days
  don't skew your off-grid averages.
- Notes and labels save automatically to the local database.

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
