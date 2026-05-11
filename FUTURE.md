# Future Work

## Auto-Classification of Charging Sessions

Non-technical users won't reliably label individual charging sessions. Without labels,
the unclassified bucket fills with noise and the charge rate card becomes unreliable.

The current rate card fix (ignore sessions with <5% SOC gain) papers over the symptom;
the real fix is auto-classification so labels are never required:

- **Generator**: sustained current >50A held for >20 minutes with no speed-correlated
  variation. Generators produce steady AC → steady DC output.
- **Shore power**: same sustained profile as generator but typically overnight or
  multi-hour; may be distinguishable by time-of-day heuristic if needed.
- **Driving (DC-DC)**: current is variable, correlated with engine RPM changes; tends
  to be 20–50A with step changes. Could use rate-of-change of current as a signal.
- **Solar**: low, slowly varying current, peaks at midday. Requires a second BMV or
  MPPT on VE.Direct to separate from other sources.

Confidence-based: label automatically when signal is clear, leave unclassified when
ambiguous, surface ambiguous sessions for optional user review.

---

## Generator Running But Not Charging — Detection via Magnum RS-485

### Problem

Experienced a failure where the AGS started the generator but the battery did not charge.
The Victron BMV-712 data alone cannot distinguish this from normal discharge — both look
identical in current/SOC readings. Need a way to know the generator state independently.

### Finding

The **Magnum ME-ARC50-L has no Bluetooth** — it is a wired RS-485 device (4-conductor RJ11
cable). However, it can be read programmatically:

- **Hardware**: RS-485-to-USB dongle (~$15–30, passive read-only tap on the RJ11 cable)
- **Library**: `pymagnum` — https://github.com/CharlesGodwin/pymagnum
  - Reads the Magnum ME-Bus (packets every 100 ms)
  - Exposes generator running state, inverter AC output, charger state, AGS status
  - Build instructions for the adapter: https://pymagnum.readthedocs.io/en/latest/building_an_adaptor.html

### Implementation sketch

1. Add `[magnum]` section to `config.ini` with serial port path (e.g. `/dev/tty.usbserial-*`)
2. In `logger.py`: poll Magnum bus alongside BLE; add `generator_running` column to SQLite
3. Alert condition: `generator_running=True` AND `current < threshold_amps` for 2+ consecutive
   readings → macOS notification via `osascript` or email via `smtplib`
4. In `report.py`: shade generator-running spans on the SOC chart; add a warning card when
   a generator run produced no charging session

### Files to modify

- `logger.py`
- `report.py`
- `config.ini` / `config.ini.example`
- `CLAUDE.md`

---

## Xantrex Freedom XC 3000 — Direct Charger State Integration

### Problem

The BMV-712 current reading cannot reliably distinguish charging sources. Two known
failure modes:
- **DC-DC charger from engine**: running the engine briefly (e.g. to operate slides
  during a wind storm) produces +25A from the alternator via the DC-DC charger —
  indistinguishable from a real charging session by current alone.
- **Short sessions filtered out**: `min_session_pct` was 1.0% — a 5-minute generator
  start produces no detectable SOC change and the session is silently dropped.
  Lowered to 0.5% but this is still inference, not ground truth.

The real fix is reading charger state directly from the inverter/charger.

### Finding

The **Xantrex Freedom XC 3000** (planned replacement for the Magnum as primary
inverter/charger) communicates via **RV-C**, a standard CAN-bus protocol used across
the RV industry.

- **Hardware required**: Freedom X Combox adapter (Xantrex accessory) + CAN-to-USB
  dongle (~$30 standard hardware)
- **Python library**: `ScottS45/Xantrex-Rvc-VenusOS` on GitHub — decodes Xantrex
  RV-C CAN frames, publishes to Victron Venus OS D-Bus; tested on Raspberry Pi with
  Python 3.8
- **What it exposes**: charger state (charging / idle / fault), AC input presence
  (shore connected), inverter state, fault codes

### What this unlocks

1. **Reliable session detection** — trigger a charging session from charger state,
   not SOC change. Eliminates both the DC-DC false-positive and the short-session
   drop problem entirely.
2. **Generator-running-but-not-charging alert** — AC input present + charger idle =
   fault condition (tripped breaker, transfer switch, charger fault). Currently
   impossible to detect with BMV alone.
3. **Shore power detection** — AC input present = shore connected; no manual
   checkbox needed. Could auto-tag discharge sessions as Shore Power.
4. **Driving detection** — engine running at same time as charging = DC-DC session;
   could auto-classify Driving sessions without manual tagging.

### Implementation sketch

1. Add `[xantrex]` section to `config.ini` with CAN interface path
2. Integrate `ScottS45/Xantrex-Rvc-VenusOS` (or adapt its RV-C decoding) into
   `logger.py`; add `charger_state`, `ac_input` columns to SQLite readings
3. Replace current-threshold session detection with charger-state-based detection
   as primary signal; keep current threshold as fallback
4. Wire charger state into the generator-running alert (see Magnum RS-485 section —
   this supersedes that approach once the Xantrex is installed)
5. Auto-populate `charge_type` from charger state + engine/driving detection

### Relationship to Magnum RS-485 work

The Magnum integration (see section above) was the previous plan for charger-state
awareness. Once the Xantrex Freedom XC 3000 is installed, the Xantrex Combox is the
preferred path — it uses a standard protocol with existing Python tooling. The Magnum
RS-485 work may still be worth doing while both units are installed in parallel.

### Files to modify

- `logger.py` — add RV-C polling, new DB columns
- `report.py` / `app.py` — use charger state for session detection; auto charge_type
- `config.ini` / `config.ini.example` — `[xantrex]` section

---

## Battery Chemistry / Charging Profile Awareness

Lithium (LiFePO4) charges at essentially constant current until near full, so the charge rate
is stable across a session. Lead-acid and AGM use CC/CV (bulk → absorb → float), meaning
current tapers significantly in the absorb phase — the measured %/hr rate early in a session
is not representative of the full session.

Practical impacts:
- `measured_charge_rate` (last session %/hr) may be inflated if the session ended before float
- The float current drop can fall below `threshold_amps`, creating spurious discharge sessions
- Daily Maintenance hours will be underestimated if charge rate is from a short bulk-phase session

### Ideas
- Add `battery_type = lithium | lead-acid | agm` to `config.ini`
- For non-lithium: compute charge rate only from the bulk phase (current above some threshold),
  or use full-session SOC gain regardless of current taper
- Float detection: if SOC ≥ 95% and current < threshold, mark as "float" not "discharge"

---

## Charging Session Color Perception

Older charging sessions look more blue-ish than the current one. Likely a perceptual effect —
all use identical `rgba(46, 139, 87, 0.25)` but narrow older vrects are dominated visually by
the steelblue SOC line. Revisit once there's more charging history and see if they equalize
when similarly sized. If they don't, investigate Plotly vrect rendering order.

---

## Session Mode Tagging — Boondocking vs Shore Power

### Problem

Running averages (daily %/day, Daily Maintenance hours) are polluted by shore power sessions
where heavy loads (dryer, etc.) are running that would never be used boondocking. The result
is a misleadingly high maintenance estimate.

### Idea

Add a structured **Mode** tag per discharge session (Boondocking / Shore Power / Driving / Other)
stored in `session_notes` (or a new `session_mode` column). Then show a second set of summary
cards — or a toggle — filtered to Boondocking-only sessions. That filtered running average is
the number that actually answers "how long will my batteries last?"

### Implementation sketch

1. Add a Mode dropdown per session row in the Dash table (stored in SQLite alongside the note)
2. `compute_summary()` gains a `mode_filter` parameter; filtered version excludes non-Boondocking
3. Dashboard shows Boondocking avg prominently; unfiltered avg available for reference

---

## Report Improvements

### Days Remaining summary card

The core boondocking question is "how many days do I have left?" This is already implicit
in the data but not displayed:

```
days remaining = current_soc / running_avg_pct_per_day
```

Should be a prominent summary card. Works regardless of battery bank size — no capacity
config needed since it's SOC-based.

### Days remaining chart (historical rolling rate)

A simple days-remaining chart is just the SOC chart rescaled — same shape, no new information.
The interesting version computes days remaining at each historical reading using the rolling
usage rate *at that point in time* (not today's constant). This shows how the estimate evolved
as habits changed. Requires computing historical rolling averages at each reading — more work,
and needs a month+ of data before it's meaningful. Skip until then.

### avg_watts reliability

The per-session average watts figure is calculated from 15-minute samples and systematically
misses brief high-draw events (microwave, hair dryer, toaster). It underestimates true load
and may be more confusing than useful. Options:
- Drop it from the report
- Replace with implied watts: `(running_avg_pct_per_day / 100) × battery_capacity_ah × voltage / 24`
  which would at least be consistent with the SOC truth

### consumed_ah for accurate energy accounting

The BMV-712 logs `consumed_ah` — its internal Coulomb counter that integrates current
continuously between polls. This is more accurate than sampled wattage for calculating
true energy consumption, especially for intermittent high-draw appliances. Worth exploring
as the basis for avg_watts calculation instead of sampled current readings.
