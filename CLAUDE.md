# Boondockers' Helper

## Project Overview

Log battery data from a Victron BMV-712 Smart via Bluetooth LE on an M2 MacBook Air.
Replaces a manual screenshot → spreadsheet workflow with automated polling, persistent
storage, and daily power-consumption summaries.

## Device

- **Model**: BMV-712 Smart (BLE name: `SmartBMV HQ2144NDYDN`)
- **BLE UUID** (this Mac only — macOS UUIDs are pairing-specific):
  `91FD9E07-C70E-EEBC-10B4-EC867F31294A`
- **Encryption key**: Sensitive — never commit. Store in a gitignored `config.ini` or `.env`.
  Retrieve from VictronConnect iOS app → connect to device → `...` menu → Product Info →
  Instant Readout Details → Show.

## Tool

```bash
pip install victron-ble
```

Discover device UUID:
```bash
victron-ble discover
```

Read live data:
```bash
victron-ble read "<UUID>@<encryption_key>"
```

Data fields available: `voltage` (V), `current` (A), `soc` (%), `consumed_ah`, `remaining_mins`,
`alarm`, `model_name`.

Power in watts = `voltage × abs(current)`. Negative current = discharging.

## Credentials

Store in a gitignored file, e.g. `config.ini`:

```ini
[device]
uuid = 91FD9E07-C70E-EEBC-10B4-EC867F31294A
key  = <your_encryption_key>

[logging]
poll_interval_minutes = 1
db_path = victron_data.db

[charging]
threshold_amps = 2.0    ; current above this = charging (excluded from usage stats)
max_gap_hours = 4.0     ; sessions split on data gaps larger than this

[report]
output_dir = reports
time_format = 12h       ; 12h or 24h
date_format = %B %-d, %Y  ; Python strftime; %-d = day without leading zero
charge_rate_pct_per_hour = 6.0
target_soc_pct = 95
battery_capacity_ah = 920  ; total usable bank capacity
downsample_tier1_hours = 6   ; keep full 1-min resolution for data younger than this
downsample_tier2_hours = 24  ; boundary between mid and old tiers
downsample_tier2_mins = 5    ; LTTB target interval for 6–24h data
downsample_tier3_mins = 15   ; LTTB target interval for data older than 24h
min_session_pct = 1.0        ; ignore sessions with less than this % SOC change (filters DC-DC blips, slide motor runs)

[diagnostics]
knee_soc_baseline_sessions = 5          ; sessions needed before knee SOC baseline is valid
knee_soc_drop_threshold_pct = 10        ; flag if knee SOC drops this many pts below rolling median
charge_rate_decline_threshold_pct = 20  ; flag if recent 3-session CC avg drops this % below all-time avg
thermal_derating_drop_pct = 15          ; flag if current drops this % from peak within window
thermal_derating_window_minutes = 30    ; detection window for initial current peak
parasitic_drain_threshold_pct = 2       ; flag if SOC drops this % during idle period
parasitic_drain_min_hours = 4           ; idle period must be at least this long
```

## Directory Layout

```
src/
├── boondockers/              ← Python package (source files)
│   ├── __init__.py
│   ├── engine.py             ← pure computation: session detection, stats, diagnostics, chart helpers
│   ├── db.py                 ← schema management (ensure_schema) + all SQLite I/O
│   ├── report.py             ← build_figure() + generate_html() + CLI entry point
│   ├── app.py                ← Plotly Dash live dashboard
│   ├── assets/
│   │   └── tooltip.css       ← CSS tooltips for Dash dashboard headers and ⚠ cells
│   └── providers/
│       ├── __init__.py
│       └── victron_ble.py    ← Victron BMV-712 BLE polling daemon
├── tests/
│   ├── conftest.py           ← synthetic data factories
│   ├── test_timestamps.py
│   ├── test_sessions.py
│   ├── test_stats.py
│   ├── test_summary.py
│   ├── test_downsample.py
│   ├── test_charge_types.py
│   ├── test_cc_cv_knee.py
│   └── test_diagnostics.py
├── run_tests.sh              ← runs pytest; passes through extra args
├── start_dashboard.sh        ← convenience launcher for the Dash app
├── requirements.txt          ← runtime + dev dependencies
├── pytest.ini
├── config.ini                ← gitignored credentials + settings
├── config.ini.example
├── victron_data.db           ← gitignored SQLite database
├── reports/                  ← generated HTML reports
└── setup/                    ← launchd / systemd + setup wizard
    ├── setup.sh              ← alpha-tester onboarding entry point (bash bootstrap)
    ├── wizard.py             ← interactive setup wizard (stdlib-only Python)
    └── com.victron.logger.plist  ← macOS launchd template (wizard customizes + installs)
```

Alpha-tester onboarding (from `src/`):
```bash
bash setup/setup.sh               # full wizard: BLE discovery → key → config → test → launchd
bash setup/setup.sh --skip-launchd  # config + connection test only
bash setup/setup.sh --skip-config   # reinstall launchd agent only (existing config.ini kept)
```
The wizard streams `victron-ble discover` output live, validates UUID + key, writes `config.ini`
from the example template (preserving all comments), runs `victron_ble.py --once` to verify the
BLE connection, then customizes and installs the launchd plist. Re-run-safe: if `config.ini`
already exists, offers skip / reconfigure / quit.

Running the HTML report (both forms work from `src/`):
- `python3 boondockers/report.py`
- `python3 -m boondockers.report`

Running the live dashboard:
- `./start_dashboard.sh`                              — opens browser tab at localhost:8050
- `./start_dashboard.sh --week`                       — rolling last 7 days on every Refresh
- `./start_dashboard.sh --2weeks`                     — rolling last 14 days
- `./start_dashboard.sh --days 30`                    — rolling last N days
- `./start_dashboard.sh --start 2026-05-01`           — from date to now (grows on Refresh)
- `./start_dashboard.sh --start … --end …`            — fixed range (same window on every Refresh)
- `./start_dashboard.sh --native`                     — native macOS window via pywebview
- `./start_dashboard.sh --no-open`                    — start server only

## Running Tests

```bash
./run_tests.sh                  # all tests, verbose
./run_tests.sh --cov=boondockers    # with coverage
./run_tests.sh tests/test_sessions.py  # single file
```

Tests use synthetic in-memory data; no BLE device or database required.

## Dash Dashboard

`boondockers/app.py` is a Plotly Dash live dashboard. It reuses all the same logic and
`build_figure()` from `report.py`. Key features:
- Refresh button reloads all data from the database
- Download Report button generates and serves a static HTML file (same as `report.py`)
- Per-session Notes column (discharge + charging tables) — persisted in `session_notes` SQLite table (`note` column)
- Per-charging-session Type multi-select — persisted in `session_notes.charge_type` (comma-separated: `"Generator,Driving"`); existing DBs auto-migrated via `ALTER TABLE` on first run
- Per-discharge-session Shore Power checkbox — persisted in `session_notes.shore_power` (INTEGER 0/1); when checked, the session is excluded from all summary cards and daily usage bars; existing DBs auto-migrated via `ALTER TABLE` on first run
- Summary cards exclude Shore/Driving charging sessions via `charge_type_map` and exclude explicitly-marked Shore Power discharge sessions via `shore_power_sids`; discharge exclusion is user-controlled only (no inference from preceding charge type)
- `--native` flag wraps the Dash server in a pywebview native macOS window
- Future: PyInstaller packaging → `.app` (macOS) and `.exe` (Windows)

## What's Built

- `boondockers/providers/victron_ble.py` — BLE polling daemon → SQLite (`victron_data.db`; includes `session_notes` table)
- `boondockers/engine.py` — pure computation: session detection, stats, diagnostics, chart helpers (no I/O)
- `boondockers/db.py` — schema management (`ensure_schema` with `PRAGMA user_version` migrations) + all SQLite I/O
- `boondockers/app.py` — Plotly Dash live dashboard (browser or native window; Refresh updates all cards + chart + diagnostics; Download Report opens HTML in system browser; per-session notes; per-charging-session Type multi-select dropdown: Shore / Generator / Driving — persisted in `session_notes.charge_type`; **Diagnostics panel** between chart and session tables shows active anomaly alerts; inline ⚠ icons in both session tables; diagnostic flags persisted in `session_notes.flags` and `system_diagnostics` table so panel loads immediately on page open)
- `boondockers/report.py` — `build_figure()` (shared with Dash app) + `generate_html()` + CLI; interactive Plotly HTML report with 5 subplots:
  - Each subplot has its own independent legend positioned next to it (Plotly named legends); SOC chart legend has shading key; charge rate chart legend has rolling avg line; V and A subplots have no legend (titles sufficient)
  - SOC over time with adaptive LTTB downsampling (3 age tiers: full res <6h, 5-min <24h, 15-min older) to keep HTML compact; session boundaries always pinned at full resolution; sessions with notes show the note as a hover tooltip over the shaded region (invisible filled rect trace; SOC line hover takes priority when on the line); default viewport = last 3 days (`window_days=3` param in `build_figure()`); rangeselector buttons (3d / 7d / 30d / All) above the SOC chart for quick navigation — note: `xaxis_rangeselector` targets only row 1; `update_xaxes(rangeselector=...)` would apply to all rows and break layout; all subplots scroll together (shared x-axis); ticks always 1-day spacing (Plotly auto-skips overlap when zoomed out)
  - SOC chart background shading: orange=discharge session, green=charging session, light grey=logger running but no session (brief transition, filtered short session, or neutral current), white=no data at all (logger off/asleep); grey spans computed from contiguous reading runs (gaps >10 min split spans)
  - Daily discharge bar chart (%/day, split at midnight; hover shows date, %/day, Ah consumed, and hours of discharge data for that day); today shown in lighter blue with "Partial day — rate will change" hover note; today excluded from 7-day avg and all-days avg (complete calendar days only); `compute_daily_rates()` returns 4-tuples `(date, rate_pct_per_day, ah, hours)`
  - Charge rate history: one subplot per charging type with data (Shore=green, Generator=orange, Driving=blue, Unclassified=gray); if no types assigned, shows one combined "All Sessions" subplot; subplot count is dynamic (4 + N charge rows); figure height scales +200px per extra charge row; rolling 7-session avg line per subplot; all-time avg dashed line (except Unclassified); multi-type sessions (e.g. Generator+Driving) go into a combined bucket, not duplicated across single-type subplots; chart shows CC phase rates (see CC/CV below)
  - **CC/CV phase analysis**: `_find_cc_cv_knee(session_readings)` detects the CC→CV transition by finding where smoothed current drops below 85% of the plateau; returns the index of the knee reading, or `None` if no CV phase is detected (session too short, session ended in CC phase, or current never tapers); `charging_session_stats()` returns three new fields: `cc_rate_pct_per_hour` (rate for CC phase only; equals full-session rate when no CV detected), `knee_soc` (SOC at the knee reading; `None` when no CV detected), `cv_detected` (bool); summary card charge rate and charge rate chart bars both use `cc_rate_pct_per_hour` so CV taper does not drag down reported rates; charging sessions table adds "CC %/hr" and "Knee SOC" columns
  - **Diagnostics**: `_detect_thermal_derating(session_readings)` flags sessions where charger current drops >15% from peak within the first 30 min AND the drop still descends past the window (step 6 suppression: if `_find_cc_cv_knee` finds a knee AND `drop_idx ≤ window_idx`, the session is a normal inrush→CC settling, not thermal derating — only suppress when the drop is AT OR BEFORE the window boundary); `compute_diagnostics(charging_stats, discharge_stats, all_readings, charge_type_map, cfg_diag)` runs four checks: (1) thermal derating per session, (2) knee SOC drift vs rolling median baseline, (3) charge rate decline vs all-time average per type, (4) parasitic drain during idle periods (SOC drop with no session active); results persisted in `session_notes.flags` (per-session: `thermal_derating`, `knee_soc_low`, `charge_rate_declining`) and `system_diagnostics` table (parasitic drain); all thresholds configurable under `[diagnostics]` in config.ini; **`make_layout` in `app.py` calls `compute_and_save_diagnostics()` BEFORE `load_notes()` on initial page load** — this ordering is critical: notes carry diagnostic flags, so notes must be loaded after diagnostics are freshly written to avoid stale ⚠ icons in the charging table; panel shows at most 5 recent events per anomaly type with a collapsible "▶ N earlier events" `<details>` below; parasitic drain events consolidated into a single card regardless of count
  - Voltage (V) and Current (A) overlaid on the SOC chart as secondary right-side y-axes (orange=V, purple=A); both appear in the SOC legend; A axis has a zero reference line; axes use `autoshift=True` to avoid label overlap
  - Summary cards in four rows with hover tooltips:
    - **Usage**: 7-day avg %/day (+ Ah/day sublabel), all-days avg %/day (+ Ah/day), implied avg power (Option B: %/day × capacity × voltage ÷ 24); both averages are unweighted averages of per-calendar-day rates (same method, different windows); all-days avg was formerly "running avg" which was session-hours-weighted — now day-based for consistency
    - **Power Remaining**: at 24h rate, at 7-day avg (from current SOC), Ah remaining in bank
    - **Full Battery Would Last**: at 24h rate, at 7-day avg (from 100% SOC)
    - **Charging**: charge rate (last non-shore/driving CC rate), daily maintenance hours, "To 95% — Generator" (hours to target SOC using last Generator-tagged session CC rate; falls back to effective_rate if none tagged), "To 100% — Shore" (hours to 100% using last Shore-tagged session CC rate; shows N/A if no shore sessions tagged); the two time cards answer different questions — 95% is AGS timing, 100% is hookup planning
  - Discharge Sessions table: Start/End (date+time), SOC start/end, Drop, Ah (from BMV Coulomb counter), Duration, %/day, Avg Power (Option C: consumed_ah ÷ hours × avg_voltage)
  - Charging Sessions table: Start/End, SOC start/end, Gain, Duration, %/hour, CC %/hr, Knee SOC, Avg A, Type (multi-select: Shore / Generator / Driving)
  - **Session filtering** (`filter_sessions()`): discharge sessions below `min_session_pct` (default 1.0%) SOC drop are dropped. Charging sessions below `min_session_pct` gain are also dropped, with one exception: `_keep_charge()` retains sessions that have `gain > 0` AND `soc_end >= 99.0%` — top-offs from near-full SOC have <1% headroom by definition, but "reached essentially full" is a meaningful event; BLE dropout can cut a session short before 100% is logged, so threshold is 99.0% not 99.5%. Rate card (`compute_summary`) uses a separate 5% gate to exclude top-off sessions from the measured charge rate.
  - **Tooltips**: instant CSS tooltips via `data-tip` attribute in `boondockers/assets/tooltip.css` (Dash auto-serves from `assets/`); no browser delay vs `title=`; two rules: `thead th[data-tip]` (opens downward, 300px wide) for column headers, `span[data-tip]` (opens upward, 260px wide, anchored left:0 to avoid off-screen clipping) for ⚠ warning triangles; both use `width` not `max-width` (absolute pseudo-elements require explicit width, not max-width); column headers (CC %/hr, Knee SOC, %/hour) explain the metric on hover; ⚠ cells show full diagnostic description on hover; charge rate bars include CC phase explanation in the Plotly `hovertemplate` (SVG subplot titles cannot receive CSS hover)
  - CLI flags: `--week` (7d), `--2weeks` (14d), `--days N` (last N days), `--start YYYY-MM-DD [--end YYYY-MM-DD]` (explicit range), `--no-open`; chart viewport auto-scales to the selected window; reports saved in `reports/`
- `setup/com.victron.logger.plist` — macOS launchd agent (script path: `boondockers/providers/victron_ble.py`)
- `setup/victron-logger.service` — Raspberry Pi systemd placeholder
- `config.ini.example` — safe template (copy to `config.ini` and fill in credentials)

## Known Limitation: macOS Bluetooth Permissions

macOS only grants Bluetooth permission to `.app` bundles via System Settings. Plain
binaries (including the `claude` CLI at `/opt/homebrew/Caskroom/claude-code/*/claude`)
cannot be added through the UI — clicking Open dismisses the dialog without adding the
entry. Workaround via `tccutil` requires SIP to be disabled, which is not recommended.

**Practical impact:** `boondockers/providers/victron_ble.py` works correctly when run from Terminal.app (which
has Bluetooth permission). The launchd agent also works. Claude Code's own Bash tool cannot
directly invoke `victron-ble` — test commands must be run manually in Terminal.

## Conventions

- **Version tags**: integer sequence — `v1`, `v2`, `v3`, … "Bump the version" means create the next tag.
- **CLAUDE.md**: update before every commit that changes code or config. This is the authoritative source for all project-specific knowledge — structure, conventions, behaviors, gotchas. Do not duplicate project detail into the global memory files (`~/.claude/projects/.../memory/`); those are for user preferences and cross-project context only.

## Next Steps

- Raspberry Pi deployment: decide BLE vs VE.Direct USB cable, finish systemd unit
- **SMS alerts** (all alert items below should notify via SMS when triggered)
- **BMS / cell imbalance alert**: now that `knee_soc` is tracked per session, watch for the
  CC→CV knee appearing at a lower SOC than a rolling baseline (e.g., knee historically at
  97% but now appearing at 80% would suggest a weak cell or BMS cutoff). Alert when knee
  SOC drops more than N% below the rolling median. Note: knee detection currently uses a
  current-taper heuristic; a voltage-inflection cross-check could improve accuracy on
  gradual curves.
- **Generator running but not charging alert**: integrate Magnum RS-485 (pymagnum + USB
  dongle) to read charger state directly. Fire alert when generator AC is present but
  BMV current stays below threshold_amps — indicates a generator-is-running-but-not-charging
  fault (tripped breaker, transfer switch issue, charger fault, etc.).
- **Driving + Generator overlap detection**: when driving with the generator on, the
  DC-DC charger (~30A) and generator charger (~70A) run in parallel, producing a visible
  step change in the current trace. Detection approach: within a Driving session, scan
  for a sustained current increase >20A held for >5 minutes — that's the generator
  coming on. Auto-split the session at the knee (first segment = Driving, second =
  Generator+Driving) or flag it in the dashboard as "possible generator overlap — split?"
  with a confirm button. Complications: DC-DC output varies with RPM and battery
  acceptance, so use a sustained threshold not a momentary spike; if the battery is
  near full when the generator starts, both sources taper and the knee may be subtle.
  Build after collecting a few real driving sessions to see what the knee actually looks
  like in the data.
- **Solar support**: model solar as a separate charging source. Requires distinguishing
  solar input from generator/shore in the current trace — likely via a second BMV or
  Victron MPPT controller on the VE.Direct bus. Summary cards, charge rate charts, and
  daily usage calculations all need solar-aware logic (solar offsets consumption without
  a discrete charging session).
- **Lead-acid / AGM support**: AGM has a slow CV tail and Peukert effect that would
  require different session math and charge rate assumptions. Low priority — the target
  user base has largely moved to lithium and this chemistry is declining in the RV/van
  space.
