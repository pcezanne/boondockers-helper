#!/usr/bin/env python3
"""
tools/make_demo_db.py — Generate a synthetic demo database for screenshot purposes.

Creates victron_demo.db (or a path you specify) with:
  - 30 days of 1-minute readings
  - Varied discharge sessions showing realistic usage
  - Charging sessions triggering all four diagnostics:
      1. Thermal derating      — early sessions show high peak → lower plateau
      2. Knee SOC drift        — later sessions show CC→CV knee dropping to ~83%
      3. Charge rate declining — Generator CC rate falling over time
      4. Parasitic drain       — overnight idle with slow SOC bleed
  - Notes and charge types pre-populated so diagnostics fire on first load

Usage:
    python3 tools/make_demo_db.py
    python3 tools/make_demo_db.py --out /tmp/demo.db
"""

import argparse
import hashlib
import math
import random
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def ts(dt):
    """Return ISO 8601 string for a UTC datetime."""
    return dt.isoformat()


def session_id(session_type, ts_start_iso):
    raw = f'{session_type}:{ts_start_iso}'
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def insert_reading(conn, dt, soc, current, voltage=13.2, consumed_ah=0.0):
    power = round(voltage * abs(current), 2)
    conn.execute(
        """INSERT INTO readings
           (timestamp, voltage, current, soc, consumed_ah, remaining_mins, alarm, power_watts)
           VALUES (?, ?, ?, ?, ?, ?, '', ?)""",
        (ts(dt), round(voltage, 2), round(current, 2), round(soc, 2),
         round(consumed_ah, 2), None, power),
    )


def add_note(conn, sid, session_type, note='', charge_type='', flags=''):
    conn.execute(
        """INSERT OR REPLACE INTO session_notes
           (session_id, session_type, note, charge_type, shore_power, flags, updated_at)
           VALUES (?, ?, ?, ?, 0, ?, ?)""",
        (sid, session_type, note, charge_type, flags,
         datetime.now(timezone.utc).isoformat()),
    )


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

READINGS_DDL = """
CREATE TABLE IF NOT EXISTS readings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp     TEXT NOT NULL,
    voltage       REAL,
    current       REAL,
    soc           REAL,
    consumed_ah   REAL,
    remaining_mins INTEGER,
    alarm         TEXT,
    power_watts   REAL
);
"""

NOTES_DDL = """
CREATE TABLE IF NOT EXISTS session_notes (
    session_id   TEXT PRIMARY KEY,
    session_type TEXT NOT NULL,
    note         TEXT DEFAULT '',
    charge_type  TEXT DEFAULT '',
    shore_power  INTEGER DEFAULT 0,
    flags        TEXT DEFAULT '',
    updated_at   TEXT
);
"""

DIAG_DDL = """
CREATE TABLE IF NOT EXISTS system_diagnostics (
    diag_id      TEXT PRIMARY KEY,
    diag_type    TEXT NOT NULL,
    detected_at  TEXT,
    period_start TEXT,
    period_end   TEXT,
    soc_drop     REAL,
    hours        REAL,
    details      TEXT
);
"""


# ---------------------------------------------------------------------------
# Scenario builders
# ---------------------------------------------------------------------------

def add_discharge_block(conn, start_dt, duration_hours, soc_start, soc_end,
                        voltage=13.0, consumed_ah_offset=0.0):
    """Add a discharge block at 1-min resolution. Returns final consumed_ah."""
    n = int(duration_hours * 60)
    capacity_ah = 920.0
    ah_dropped = (soc_start - soc_end) / 100.0 * capacity_ah
    for i in range(n):
        frac = i / max(n - 1, 1)
        soc = soc_start + (soc_end - soc_start) * frac
        current = -(ah_dropped / duration_hours)   # constant average draw
        ah = consumed_ah_offset + ah_dropped * frac
        insert_reading(conn, start_dt + timedelta(minutes=i),
                       soc, current, voltage, ah)
    return consumed_ah_offset + ah_dropped


def add_charge_block_normal(conn, start_dt, soc_start, soc_end,
                            peak_amps, plateau_amps, voltage=13.8,
                            ramp_minutes=20, consumed_ah_offset=0.0):
    """Add a charging block with thermal derating: peak then plateau.

    Ramps down from peak_amps to plateau_amps in the first ramp_minutes, then
    holds at plateau_amps until soc_end. No CV tail (generator session).
    """
    capacity_ah = 920.0
    ah_to_add = (soc_end - soc_start) / 100.0 * capacity_ah

    # Estimate total minutes based on average current
    avg_amps = (peak_amps + plateau_amps) / 2.0
    total_minutes = int(ah_to_add / avg_amps * 60) + 1

    soc = soc_start
    consumed_ah = consumed_ah_offset
    t = start_dt
    for i in range(total_minutes):
        if i < ramp_minutes:
            # Linear ramp from peak down to plateau
            frac = i / max(ramp_minutes - 1, 1)
            current = peak_amps + (plateau_amps - peak_amps) * frac
        else:
            current = plateau_amps

        # Advance SOC
        delta_ah = current / 60.0
        delta_soc = delta_ah / capacity_ah * 100.0
        soc = min(soc + delta_soc, soc_end)
        insert_reading(conn, t, soc, current, voltage, consumed_ah)
        consumed_ah += delta_ah
        t += timedelta(minutes=1)
        if soc >= soc_end:
            break
    return consumed_ah, t


def add_charge_block_shore(conn, start_dt, soc_start, soc_end,
                           peak_amps, plateau_amps, knee_soc,
                           voltage=13.8, ramp_minutes=20,
                           consumed_ah_offset=0.0):
    """Shore charge: derating ramp then CC until knee_soc, then CV taper to soc_end."""
    capacity_ah = 920.0
    consumed_ah = consumed_ah_offset
    t = start_dt
    soc = soc_start

    # CC phase: derating ramp then plateau
    while soc < knee_soc:
        elapsed = int((t - start_dt).total_seconds() / 60)
        if elapsed < ramp_minutes:
            frac = elapsed / max(ramp_minutes - 1, 1)
            current = peak_amps + (plateau_amps - peak_amps) * frac
        else:
            current = plateau_amps
        delta_soc = (current / capacity_ah) * (100.0 / 60.0)
        soc = min(soc + delta_soc, knee_soc)
        insert_reading(conn, t, soc, current, voltage, consumed_ah)
        consumed_ah += current / 60.0
        t += timedelta(minutes=1)

    # CV phase: taper from plateau to ~5A as SOC rises to soc_end
    while soc < soc_end:
        cv_frac = (soc - knee_soc) / max(soc_end - knee_soc, 0.01)
        current = plateau_amps * (1.0 - cv_frac) + 5.0 * cv_frac
        current = max(current, 3.0)
        delta_soc = (current / capacity_ah) * (100.0 / 60.0)
        soc = min(soc + delta_soc, soc_end)
        insert_reading(conn, t, soc, current, voltage, consumed_ah)
        consumed_ah += current / 60.0
        t += timedelta(minutes=1)

    return consumed_ah, t


def add_idle_block(conn, start_dt, duration_hours, soc_start, drain_pct_per_hour=0.4,
                   voltage=13.1, consumed_ah_offset=0.0):
    """Add readings for an idle period (near-zero current, very slow SOC bleed)."""
    n = int(duration_hours * 60)
    for i in range(n):
        soc = soc_start - drain_pct_per_hour * (i / 60.0)
        current = -0.3   # ~4A equivalent: below session threshold
        insert_reading(conn, start_dt + timedelta(minutes=i),
                       soc, current, voltage, consumed_ah_offset)
    final_soc = soc_start - drain_pct_per_hour * duration_hours
    return consumed_ah_offset, final_soc


# ---------------------------------------------------------------------------
# Main scenario
# ---------------------------------------------------------------------------

def build_demo(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute(READINGS_DDL)
    conn.execute(NOTES_DDL)
    conn.execute(DIAG_DDL)
    conn.commit()

    random.seed(42)

    # Anchor: 30 days ago at midnight UTC
    now = datetime.now(timezone.utc).replace(hour=8, minute=0, second=0, microsecond=0)
    base = now - timedelta(days=30)

    soc = 85.0
    consumed_ah = 100.0
    t = base

    # We'll record charging session start times for note population
    charge_sessions = []   # list of (ts_start, ts_end, charge_type, flags)
    discharge_sessions = []

    # ------------------------------------------------------------------
    # Days 1–5: baseline healthy sessions (Generator, normal rates ~7%/hr)
    # No diagnostics should fire yet.
    # ------------------------------------------------------------------
    for day in range(5):
        # Overnight discharge (8h, ~30% drop)
        d_start = t
        soc_end_d = max(soc - 30.0 - random.uniform(-3, 3), 55.0)
        consumed_ah = add_discharge_block(conn, t, 8.0, soc, soc_end_d,
                                          voltage=12.8, consumed_ah_offset=consumed_ah)
        d_end = t + timedelta(hours=8)
        discharge_sessions.append((d_start, d_end))
        t += timedelta(hours=8)
        soc = soc_end_d

        # 30-min idle after discharge
        t += timedelta(minutes=30)

        # Generator charge: healthy, peak~85A, plateau~80A, ~7%/hr CC
        c_start = t
        peak = 85.0 + random.uniform(-3, 3)
        plateau = 80.0 + random.uniform(-2, 2)
        target_soc = min(soc + 35.0, 95.0)
        consumed_ah, c_end = add_charge_block_normal(
            conn, t, soc, target_soc, peak, plateau,
            voltage=13.8, ramp_minutes=15,
            consumed_ah_offset=consumed_ah,
        )
        charge_sessions.append((c_start, c_end, 'Generator', ''))
        soc = target_soc
        t = c_end + timedelta(hours=1)

    # ------------------------------------------------------------------
    # Days 6–10: thermal derating kicks in — peak still high but plateau drops
    # ------------------------------------------------------------------
    for day in range(5):
        d_start = t
        soc_end_d = max(soc - 28.0 - random.uniform(-3, 3), 55.0)
        consumed_ah = add_discharge_block(conn, t, 7.5, soc, soc_end_d,
                                          voltage=12.8, consumed_ah_offset=consumed_ah)
        d_end = t + timedelta(hours=7, minutes=30)
        discharge_sessions.append((d_start, d_end))
        t += timedelta(hours=7, minutes=30)
        soc = soc_end_d

        t += timedelta(minutes=30)

        c_start = t
        peak = 86.0 + random.uniform(-2, 2)    # still starts high
        plateau = 63.0 + random.uniform(-3, 3)  # but plateaus much lower (derating)
        target_soc = min(soc + 33.0, 94.0)
        consumed_ah, c_end = add_charge_block_normal(
            conn, t, soc, target_soc, peak, plateau,
            voltage=13.8, ramp_minutes=20,
            consumed_ah_offset=consumed_ah,
        )
        charge_sessions.append((c_start, c_end, 'Generator', 'thermal_derating'))
        soc = target_soc
        t = c_end + timedelta(hours=1)

    # ------------------------------------------------------------------
    # Days 11–15: shore power sessions — normal knee SOC ~97%
    # These establish the knee baseline.
    # ------------------------------------------------------------------
    for day in range(5):
        d_start = t
        soc_end_d = max(soc - 32.0 - random.uniform(-3, 3), 55.0)
        consumed_ah = add_discharge_block(conn, t, 8.0, soc, soc_end_d,
                                          voltage=12.8, consumed_ah_offset=consumed_ah)
        d_end = t + timedelta(hours=8)
        discharge_sessions.append((d_start, d_end))
        t += timedelta(hours=8)
        soc = soc_end_d

        t += timedelta(minutes=30)

        c_start = t
        peak = 85.0 + random.uniform(-2, 2)
        plateau = 75.0 + random.uniform(-2, 2)
        knee = 97.0 + random.uniform(-0.5, 0.5)
        target_soc = 100.0
        consumed_ah, c_end = add_charge_block_shore(
            conn, t, soc, target_soc, peak, plateau, knee,
            voltage=13.8, ramp_minutes=15,
            consumed_ah_offset=consumed_ah,
        )
        charge_sessions.append((c_start, c_end, 'Shore', ''))
        soc = target_soc
        t = c_end + timedelta(hours=1)

    # ------------------------------------------------------------------
    # Day 16: parasitic drain overnight (idle block with slow SOC bleed)
    # ------------------------------------------------------------------
    idle_start = t
    # 6-hour idle with ~0.5%/hr drain (slow load, below session threshold)
    consumed_ah, soc = add_idle_block(conn, t, 6.0, soc,
                                      drain_pct_per_hour=0.5,
                                      voltage=13.0,
                                      consumed_ah_offset=consumed_ah)
    idle_end = t + timedelta(hours=6)
    soc_drop = 3.0  # ~3% lost
    t = idle_end

    # Record the parasitic drain in system_diagnostics
    diag_id = hashlib.sha256(
        f'parasitic_drain:{idle_start.isoformat()}'.encode()
    ).hexdigest()[:16]
    conn.execute(
        """INSERT OR REPLACE INTO system_diagnostics
           (diag_id, diag_type, detected_at, period_start, period_end, soc_drop, hours)
           VALUES (?, 'parasitic_drain', ?, ?, ?, ?, ?)""",
        (diag_id, datetime.now(timezone.utc).isoformat(),
         idle_start.isoformat(), idle_end.isoformat(), soc_drop, 6.0),
    )

    # Discharge that follows the drain (⚠ icon will appear here)
    d_start = t
    soc_end_d = max(soc - 28.0, 55.0)
    consumed_ah = add_discharge_block(conn, t, 7.0, soc, soc_end_d,
                                      voltage=12.8, consumed_ah_offset=consumed_ah)
    d_end = t + timedelta(hours=7)
    discharge_sessions.append((d_start, d_end))
    t += timedelta(hours=7)
    soc = soc_end_d

    t += timedelta(minutes=30)

    c_start = t
    peak = 85.0
    plateau = 63.0
    target_soc = min(soc + 30.0, 93.0)
    consumed_ah, c_end = add_charge_block_normal(
        conn, t, soc, target_soc, peak, plateau,
        voltage=13.8, ramp_minutes=20, consumed_ah_offset=consumed_ah,
    )
    charge_sessions.append((c_start, c_end, 'Generator', 'thermal_derating'))
    soc = target_soc
    t = c_end + timedelta(hours=1)

    # ------------------------------------------------------------------
    # Days 17–22: shore sessions with declining knee SOC (BMS/thermal)
    # First two establish more of the baseline (knee ~97%), then it drops.
    # ------------------------------------------------------------------
    knee_schedule = [97.2, 96.8, 89.0, 84.5, 83.0]
    for idx, knee in enumerate(knee_schedule):
        d_start = t
        soc_end_d = max(soc - 30.0 - random.uniform(-2, 2), 55.0)
        consumed_ah = add_discharge_block(conn, t, 8.0, soc, soc_end_d,
                                          voltage=12.8, consumed_ah_offset=consumed_ah)
        d_end = t + timedelta(hours=8)
        discharge_sessions.append((d_start, d_end))
        t += timedelta(hours=8)
        soc = soc_end_d

        t += timedelta(minutes=30)

        c_start = t
        peak = 84.0 + random.uniform(-2, 2)
        plateau = 72.0 + random.uniform(-2, 2)
        flag = 'knee_soc_low' if knee < 90.0 else ''
        consumed_ah, c_end = add_charge_block_shore(
            conn, t, soc, 100.0, peak, plateau, knee,
            voltage=13.8, ramp_minutes=15, consumed_ah_offset=consumed_ah,
        )
        charge_sessions.append((c_start, c_end, 'Shore', flag))
        soc = 100.0
        t = c_end + timedelta(hours=1)

    # ------------------------------------------------------------------
    # Days 23–29: Generator rate declining (charge_rate_declining)
    # Rates drop from ~7 to ~4.5%/hr over 7 sessions.
    # ------------------------------------------------------------------
    declining_plateaus = [79, 75, 70, 66, 61, 57, 55]
    for day, plateau_val in enumerate(declining_plateaus):
        d_start = t
        soc_end_d = max(soc - 28.0 - random.uniform(-2, 2), 55.0)
        consumed_ah = add_discharge_block(conn, t, 7.5, soc, soc_end_d,
                                          voltage=12.8, consumed_ah_offset=consumed_ah)
        d_end = t + timedelta(hours=7, minutes=30)
        discharge_sessions.append((d_start, d_end))
        t += timedelta(hours=7, minutes=30)
        soc = soc_end_d

        t += timedelta(minutes=30)

        c_start = t
        peak = plateau_val + 10.0  # still starts above plateau
        plateau_a = float(plateau_val)
        flag = 'charge_rate_declining,thermal_derating' if day >= 3 else 'thermal_derating'
        target_soc = min(soc + 30.0, 93.0)
        consumed_ah, c_end = add_charge_block_normal(
            conn, t, soc, target_soc, peak, plateau_a,
            voltage=13.8, ramp_minutes=20, consumed_ah_offset=consumed_ah,
        )
        charge_sessions.append((c_start, c_end, 'Generator', flag))
        soc = target_soc
        t = c_end + timedelta(hours=1)

    conn.commit()

    # ------------------------------------------------------------------
    # Populate session_notes
    # ------------------------------------------------------------------
    for c_start, c_end, charge_type, flags in charge_sessions:
        sid = session_id('charge', c_start.isoformat())
        note = ''
        if 'thermal_derating' in flags and 'charge_rate_declining' not in flags:
            note = 'Charger bay was warm — thermal derating visible in first 20 min'
        elif 'charge_rate_declining' in flags:
            note = 'Generator may need service — rate declining'
        elif 'knee_soc_low' in flags:
            note = 'Shore session — CV phase started earlier than usual'
        add_note(conn, sid, 'charge', note, charge_type, flags)

    conn.commit()
    conn.close()

    print(f'Demo database written to: {db_path}')
    n_readings = sqlite3.connect(db_path).execute('SELECT count(*) FROM readings').fetchone()[0]
    print(f'  {n_readings:,} readings across ~30 days')
    print(f'  {len(charge_sessions)} charging sessions')
    print(f'  {len(discharge_sessions)} discharge sessions')
    print()
    print('To use it:')
    print('  1. Edit config.ini: set db_path = ' + str(Path(db_path).name))
    print('     (or copy it to victron_data.db)')
    print('  2. ./start_dashboard.sh')
    print('  3. Open the dashboard and click Refresh to compute diagnostics')


def main():
    parser = argparse.ArgumentParser(description='Generate a demo SQLite database')
    parser.add_argument('--out', default='victron_demo.db',
                        help='Output database path (default: victron_demo.db)')
    args = parser.parse_args()
    build_demo(args.out)


if __name__ == '__main__':
    main()
