#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul Cezanne
"""
report.py — Analyse logged battery readings and generate an HTML report.

Methodology (matches BoondockingBatteries.csv):
  - A "discharge session" is a contiguous period where the battery is not charging.
  - A "charging session" is a contiguous period where the battery is charging.
  - Charging = current above threshold_amps (catches generator, shore power, solar).
  - Sessions split on data gaps > max_gap_hours (laptop was off).
  - %/day  = (SOC drop %)  / (elapsed hours) * 24
  - %/hour = (SOC gain %)  / (elapsed hours)  [measured charge rate]
  - Daily maintenance hours = running avg %/day / measured charge rate
  - Hours to target SOC    = (target_soc - current_soc) / measured charge rate

Usage:
    python3 report.py              # all data, open in browser
    python3 report.py --days 7     # last 7 days
    python3 report.py --no-open    # generate file without opening browser
"""

import argparse
import configparser
import hashlib
import sqlite3
import statistics
import webbrowser
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

CONFIG_PATH = Path(__file__).parent.parent / 'config.ini'


# ---------------------------------------------------------------------------
# Config + data loading
# ---------------------------------------------------------------------------

def load_config():
    cfg = configparser.ConfigParser()
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f'Config file not found: {CONFIG_PATH}')
    cfg.read(CONFIG_PATH)
    return cfg


def load_readings(db_path, since=None):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    if since:
        rows = conn.execute(
            """SELECT * FROM readings
               WHERE soc IS NOT NULL AND current IS NOT NULL AND timestamp >= ?
               ORDER BY timestamp""",
            (since.isoformat(),),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT * FROM readings
               WHERE soc IS NOT NULL AND current IS NOT NULL
               ORDER BY timestamp""",
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------

def parse_ts(ts_str):
    ts_str = ts_str.split('+')[0].rstrip('Z')
    return datetime.fromisoformat(ts_str).replace(tzinfo=timezone.utc)


def to_local(ts):
    """Convert a UTC-aware datetime to the system local timezone."""
    return ts.astimezone()


def fmt_dt(ts, time_format='12h', include_date=True, date_format='%B %-d, %Y'):
    """Format a UTC datetime in local time using the configured format."""
    local = to_local(ts)
    if time_format == '12h':
        t = local.strftime('%I:%M %p').lstrip('0')
    else:
        t = local.strftime('%H:%M')
    if include_date:
        return local.strftime(date_format) + ' ' + t
    return t


def midnights_in_range(ts_list):
    """Return local-time midnight datetimes covering the range of ts_list."""
    if not ts_list:
        return []
    local_min = to_local(min(ts_list)).replace(hour=0, minute=0, second=0, microsecond=0)
    local_max = to_local(max(ts_list))
    result = []
    current = local_min + timedelta(days=1)   # first midnight after data start
    while current <= local_max:
        result.append(current)
        current += timedelta(days=1)
    return result


# ---------------------------------------------------------------------------
# Session detection
# ---------------------------------------------------------------------------

def detect_sessions(readings, charging_threshold, max_gap_hours=4.0):
    """
    Return discharge sessions — contiguous blocks of non-charging readings.
    Data gaps > max_gap_hours split a session (handles laptop-off gaps).

    A single anomalous charging reading (BLE glitch) is ignored — the session
    only ends when two or more consecutive readings are above the threshold.
    """
    sessions = []
    current_session = []

    i = 0
    while i < len(readings):
        r = readings[i]
        is_charging = (r['current'] or 0) > charging_threshold

        if is_charging:
            # Only commit the session end if the next reading is also charging.
            # A lone charging blip in the middle of discharge is silently skipped.
            next_also_charging = (
                i + 1 < len(readings) and
                (readings[i + 1]['current'] or 0) > charging_threshold
            )
            if next_also_charging and current_session:
                sessions.append(current_session)
                current_session = []
        else:
            if current_session:
                prev_ts = parse_ts(current_session[-1]['timestamp'])
                ts = parse_ts(r['timestamp'])
                gap_hours = (ts - prev_ts).total_seconds() / 3600
                if gap_hours > max_gap_hours:
                    sessions.append(current_session)
                    current_session = [r]
                else:
                    current_session.append(r)
            else:
                current_session.append(r)
        i += 1

    if current_session:
        sessions.append(current_session)

    return sessions


def detect_charging_sessions(readings, charging_threshold, max_gap_hours=4.0):
    """
    Return charging sessions — contiguous blocks of charging readings.

    A single anomalous non-charging reading (BLE glitch) is ignored — the
    session only ends when two or more consecutive readings are below the threshold.
    """
    sessions = []
    current_session = []

    i = 0
    while i < len(readings):
        r = readings[i]
        is_charging = (r['current'] or 0) > charging_threshold

        if not is_charging:
            # Only commit the session end if the next reading is also not charging.
            next_also_discharging = (
                i + 1 < len(readings) and
                not ((readings[i + 1]['current'] or 0) > charging_threshold)
            )
            if next_also_discharging and current_session:
                sessions.append(current_session)
                current_session = []
        else:
            if current_session:
                prev_ts = parse_ts(current_session[-1]['timestamp'])
                ts = parse_ts(r['timestamp'])
                gap_hours = (ts - prev_ts).total_seconds() / 3600
                if gap_hours > max_gap_hours:
                    sessions.append(current_session)
                    current_session = [r]
                else:
                    current_session.append(r)
            else:
                current_session.append(r)
        i += 1

    if current_session:
        sessions.append(current_session)

    return sessions


# ---------------------------------------------------------------------------
# CC/CV knee detection
# ---------------------------------------------------------------------------

def _find_cc_cv_knee(session_readings, window=5, plateau_fraction=0.85,
                     cv_threshold=0.75):
    """Return the index of the last CC-phase reading, or None.

    Algorithm:
    1. Smooth current with a rolling mean to reduce BLE noise.
    2. Estimate the CC plateau as the median of the middle 50% of smoothed
       readings (skips the initial thermal-derating ramp and the CV tail).
    3. If the session ends near the plateau (final current > cv_threshold ×
       plateau), the session was stopped in CC phase — return None.
    4. Otherwise scan backwards to find the last reading where smoothed current
       is still ≥ plateau_fraction × plateau; that is the knee.

    Returns None for sessions with fewer than 15 readings (too short to
    distinguish derating from taper) or plateau ≤ 2 A.
    """
    n = len(session_readings)
    if n < 15:
        return None

    currents = [(r.get('current') or 0) for r in session_readings]

    # Rolling mean
    smoothed = []
    for i in range(n):
        start = max(0, i - window + 1)
        smoothed.append(sum(currents[start:i + 1]) / (i - start + 1))

    # Plateau: median of middle 50%
    mid_start, mid_end = n // 4, 3 * n // 4
    mid_vals = sorted(smoothed[mid_start:mid_end])
    if not mid_vals:
        return None
    plateau = mid_vals[len(mid_vals) // 2]
    if plateau <= 2.0:
        return None

    # Session ended in CC phase?
    final_avg = sum(smoothed[max(0, n - 5):]) / min(5, n)
    if final_avg > plateau * cv_threshold:
        return None

    # Scan backwards for last reading above the knee threshold
    threshold = plateau * plateau_fraction
    for i in range(n - 1, -1, -1):
        if smoothed[i] >= threshold:
            return i

    return None


def _detect_thermal_derating(session_readings, drop_pct=15, window_minutes=30):
    """Detect charger thermal derating: a high current peak that drops and plateaus.

    Returns (is_derating, peak_amps, plateau_amps).  Returns (False, None, None)
    if the session is too short or the pattern is absent.

    Algorithm:
    1. Estimate the per-reading interval from first and last timestamps.
    2. Collect readings in the initial window_minutes.
    3. Peak = max current in that window.
    4. Plateau = median current in the second half of the session.
    5. Flag if peak > 30 A and (peak − plateau) / peak ≥ drop_pct / 100.
    6. Suppress false positive: if voltage at the first-drop reading is within
       0.1 V of the session peak voltage, the charger had already reached the
       absorption setpoint — this is a normal CC→CV transition, not derating.
    """
    n = len(session_readings)
    if n < 15:
        return False, None, None

    ts0 = parse_ts(session_readings[0]['timestamp'])
    ts_last = parse_ts(session_readings[-1]['timestamp'])
    total_seconds = (ts_last - ts0).total_seconds()
    if total_seconds <= 0:
        return False, None, None

    interval_seconds = total_seconds / (n - 1)
    window_idx = max(1, int(window_minutes * 60 / interval_seconds))
    if window_idx >= n:
        window_idx = n // 2
    if window_idx < 1:
        return False, None, None

    early = [r.get('current') or 0 for r in session_readings[:window_idx]]
    later = [r.get('current') or 0 for r in session_readings[n // 2:]]
    if not early or not later:
        return False, None, None

    peak = max(early)
    plateau = statistics.median(later)

    if peak <= 30:
        return False, None, None
    if peak <= 0:
        return False, None, None
    if (peak - plateau) / peak < drop_pct / 100:
        return False, None, None

    # Step 6: voltage check — suppress if the drop coincides with CV onset.
    valid_v = [r.get('voltage') for r in session_readings if r.get('voltage') is not None]
    if valid_v:
        v_peak = max(valid_v)
        peak_idx = early.index(peak)
        drop_threshold = peak * (1 - drop_pct / 100)
        drop_idx = next(
            (i for i in range(peak_idx, n)
             if (session_readings[i].get('current') or 0) < drop_threshold),
            None,
        )
        if drop_idx is not None:
            v_at_drop = session_readings[drop_idx].get('voltage')
            if v_at_drop is not None and v_at_drop >= v_peak - 0.1:
                return False, None, None  # voltage at absorption — CC→CV, not derating

    return True, round(peak, 1), round(plateau, 1)


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def session_stats(session, time_format='12h', date_format='%B %-d, %Y'):
    soc_start = session[0]['soc']
    soc_end = session[-1]['soc']
    pct_drop = soc_start - soc_end
    ts_start = parse_ts(session[0]['timestamp'])
    ts_end = parse_ts(session[-1]['timestamp'])
    hours = (ts_end - ts_start).total_seconds() / 3600
    pct_per_day = (pct_drop / hours * 24) if hours > 0 else 0
    voltage_readings = [r['voltage'] for r in session if r.get('voltage') is not None]
    avg_voltage = sum(voltage_readings) / len(voltage_readings) if voltage_readings else None
    ah_start = session[0].get('consumed_ah')
    ah_end   = session[-1].get('consumed_ah')
    consumed_ah = abs(ah_end - ah_start) if (ah_start is not None and ah_end is not None) else None
    # Option C: derive avg watts from the BMV Coulomb counter (integrates continuously,
    # so it captures brief high-draw events that 1-min polling might still miss)
    if consumed_ah is not None and hours > 0 and avg_voltage is not None:
        avg_watts = consumed_ah / hours * avg_voltage
    else:
        power_readings = [r['power_watts'] for r in session if r['power_watts'] is not None]
        avg_watts = sum(power_readings) / len(power_readings) if power_readings else 0
    return {
        'date': to_local(ts_start).strftime('%Y-%m-%d'),
        'start': fmt_dt(ts_start, time_format, date_format=date_format),
        'end': fmt_dt(ts_end, time_format, date_format=date_format),
        'soc_start': soc_start,
        'soc_end': soc_end,
        'pct_drop': pct_drop,
        'hours': hours,
        'pct_per_day': pct_per_day,
        'avg_watts': avg_watts,
        'avg_voltage': avg_voltage,
        'consumed_ah': consumed_ah,
        'ts_start': ts_start,
        'ts_end': ts_end,
        'n_readings': len(session),
    }


def charging_session_stats(session, time_format='12h', date_format='%B %-d, %Y'):
    soc_start = session[0]['soc']
    soc_end = session[-1]['soc']
    pct_gain = soc_end - soc_start
    ts_start = parse_ts(session[0]['timestamp'])
    ts_end = parse_ts(session[-1]['timestamp'])
    hours = (ts_end - ts_start).total_seconds() / 3600
    charge_rate = (pct_gain / hours) if hours > 0 else 0
    amps_list = [r['current'] for r in session if r.get('current') is not None]
    avg_amps = sum(amps_list) / len(amps_list) if amps_list else 0

    # CC/CV phase analysis
    knee_idx = _find_cc_cv_knee(session)
    if knee_idx is not None and knee_idx > 0:
        ts_knee = parse_ts(session[knee_idx]['timestamp'])
        cc_hours = (ts_knee - ts_start).total_seconds() / 3600
        cc_gain = session[knee_idx]['soc'] - soc_start
        cc_rate = (cc_gain / cc_hours) if cc_hours > 0 else charge_rate
        knee_soc = session[knee_idx]['soc']
        cv_detected = True
    else:
        cc_rate = charge_rate   # whole session is CC (or indeterminate)
        knee_soc = None
        cv_detected = False

    # Thermal derating detection (uses default thresholds; config thresholds applied
    # in compute_diagnostics so this function stays config-free)
    is_derating, derating_peak_amps, derating_plateau_amps = _detect_thermal_derating(session)

    return {
        'date': to_local(ts_start).strftime('%Y-%m-%d'),
        'start': fmt_dt(ts_start, time_format, date_format=date_format),
        'end': fmt_dt(ts_end, time_format, date_format=date_format),
        'soc_start': soc_start,
        'soc_end': soc_end,
        'pct_gain': pct_gain,
        'hours': hours,
        'charge_rate_pct_per_hour': charge_rate,
        'cc_rate_pct_per_hour': cc_rate,
        'knee_soc': knee_soc,
        'cv_detected': cv_detected,
        'avg_amps': avg_amps,
        'is_derating': is_derating,
        'derating_peak_amps': derating_peak_amps,
        'derating_plateau_amps': derating_plateau_amps,
        'ts_start': ts_start,
        'ts_end': ts_end,
        'n_readings': len(session),
    }


def compute_daily_rates(discharge_sessions):
    """
    Compute per-calendar-day discharge rates, splitting sessions at midnight.

    For each session, group readings by local calendar date. Each day-slice
    uses its first and last readings for SOC drop and elapsed hours — the
    same method as session_stats, just applied per day. Day-slices from
    different sessions on the same date are merged into one bar.
    """
    daily = defaultdict(lambda: {'drop': 0.0, 'hours': 0.0, 'ah': 0.0})
    for session in discharge_sessions:
        # Group readings by local calendar date
        day_groups = defaultdict(list)
        for r in session:
            date = to_local(parse_ts(r['timestamp'])).strftime('%Y-%m-%d')
            day_groups[date].append(r)

        for date, day_readings in day_groups.items():
            if len(day_readings) < 2:
                continue
            soc_drop = (day_readings[0]['soc'] or 0) - (day_readings[-1]['soc'] or 0)
            if soc_drop <= 0:
                continue
            ts_start = parse_ts(day_readings[0]['timestamp'])
            ts_end   = parse_ts(day_readings[-1]['timestamp'])
            hours = (ts_end - ts_start).total_seconds() / 3600
            if hours <= 0:
                continue
            ah_start = day_readings[0].get('consumed_ah') or 0
            ah_end   = day_readings[-1].get('consumed_ah') or 0
            daily[date]['drop'] += soc_drop
            daily[date]['hours'] += hours
            daily[date]['ah']    += abs(ah_end - ah_start)
    return sorted(
        [(d, v['drop'] / v['hours'] * 24, v['ah'], v['hours']) for d, v in daily.items() if v['hours'] > 0]
    )


def compute_summary(discharge_sessions, discharge_stats, charging_stats,
                    fallback_charge_rate=6.0, current_soc=None, target_soc=95.0,
                    capacity_ah=None, charge_type_map=None, shore_power_sids=None):
    result = {}

    _EXCLUDE_TYPES = {'Shore', 'Driving'}

    def _charge_types_for(stat):
        """Return set of charge type strings for a charging_session_stats dict."""
        if not charge_type_map:
            return set()
        sid = session_id('charge', stat['ts_start'].isoformat())
        raw = charge_type_map.get(sid, '')
        return {t.strip() for t in raw.split(',') if t.strip()}

    # Charging sessions: exclude Shore and Driving from card calculations
    if charge_type_map:
        summary_charging_stats = [
            s for s in charging_stats
            if not (_charge_types_for(s) & _EXCLUDE_TYPES)
        ]
    else:
        summary_charging_stats = charging_stats

    # Discharge sessions: exclude only those explicitly marked Shore Power by the user
    if shore_power_sids:
        summary_discharge_stats = [
            s for s in discharge_stats
            if session_id('discharge', s['ts_start'].isoformat()) not in shore_power_sids
        ]
        summary_discharge_sessions = [
            sess for sess, stat in zip(discharge_sessions, discharge_stats)
            if session_id('discharge', stat['ts_start'].isoformat()) not in shore_power_sids
        ]
    else:
        summary_discharge_stats    = discharge_stats
        summary_discharge_sessions = discharge_sessions

    # --- Discharge ---
    if summary_discharge_stats:
        daily_rates = compute_daily_rates(summary_discharge_sessions)
        today_str = datetime.now().astimezone().strftime('%Y-%m-%d')
        # Exclude today (partial day) from averages — only complete calendar days count
        complete_rates = [(d, r, ah, h) for d, r, ah, h in daily_rates if d != today_str]

        # Both running and 7-day averages are day-based (unweighted average of per-day
        # rates) so they are methodologically consistent — only the time window differs.
        all_day_rates = [r for _, r, _, _ in complete_rates]
        running_avg = sum(all_day_rates) / len(all_day_rates) if all_day_rates else 0

        last7 = [r for _, r, _, _ in complete_rates[-7:]]
        weekly_avg = sum(last7) / len(last7) if last7 else 0

        all_ahs = [ah for _, _, ah, _ in complete_rates]
        last7_ahs = [ah for _, _, ah, _ in complete_rates[-7:]]
        avg_ah_per_day = sum(all_ahs) / len(all_ahs) if all_ahs else 0
        weekly_avg_ah = sum(last7_ahs) / len(last7_ahs) if last7_ahs else 0

        # Implied watts from SOC-derived rate × capacity × avg voltage
        volt_list = [s['avg_voltage'] for s in summary_discharge_stats if s.get('avg_voltage')]
        avg_voltage_all = sum(volt_list) / len(volt_list) if volt_list else 12.0
        if capacity_ah and avg_voltage_all:
            avg_watts = (running_avg / 100) * capacity_ah * avg_voltage_all / 24
        else:
            watts_list = [s['avg_watts'] for s in summary_discharge_stats if s['avg_watts'] > 0]
            avg_watts = sum(watts_list) / len(watts_list) if watts_list else 0

        # Last-24h discharge rate — based on sessions that ended within the past 24 hours
        now_utc = datetime.now(timezone.utc)
        recent = [s for s in summary_discharge_stats if s['ts_end'] >= now_utc - timedelta(hours=24)]
        if recent:
            drop_24h = sum(s['pct_drop'] for s in recent)
            hrs_24h  = sum(s['hours'] for s in recent)
            rate_24h = (drop_24h / hrs_24h * 24) if hrs_24h > 0 else None
        else:
            rate_24h = None

        result.update({
            'running_avg_pct_per_day': running_avg,
            'weekly_avg_pct_per_day': weekly_avg,
            'last_24h_pct_per_day': rate_24h,
            'avg_discharge_watts': avg_watts,
            'avg_ah_per_day': avg_ah_per_day,
            'weekly_avg_ah_per_day': weekly_avg_ah,
            'total_discharge_sessions': len(summary_discharge_stats),
            'total_discharge_hours': sum(s['hours'] for s in summary_discharge_stats),
            'daily_rates': daily_rates,
            'today_str': today_str,
        })
    else:
        result.update({
            'running_avg_pct_per_day': 0,
            'weekly_avg_pct_per_day': 0,
            'last_24h_pct_per_day': None,
            'avg_discharge_watts': 0,
            'avg_ah_per_day': 0,
            'weekly_avg_ah_per_day': 0,
            'total_discharge_sessions': 0,
            'total_discharge_hours': 0,
            'daily_rates': [],
        })

    # --- Charging ---
    if summary_charging_stats:
        # Use the most recent session with a substantial SOC gain for the rate card.
        # Top-off sessions at 99%+ SOC have unreliable rates — the charger is already
        # in CV/float mode and current is tapered, so the computed rate is far below
        # the charger's actual CC capacity. Prefer sessions where the battery was
        # meaningfully discharged (gain >= 5%), falling back to any session if none qualify.
        _MIN_RATE_GAIN_PCT = 5.0
        substantial = [s for s in summary_charging_stats if s['pct_gain'] >= _MIN_RATE_GAIN_PCT]
        rate_sessions = substantial if substantial else summary_charging_stats
        last_rate = rate_sessions[-1]['cc_rate_pct_per_hour']
        result.update({
            'total_charging_sessions': len(summary_charging_stats),
            'measured_charge_rate': last_rate,
            'charge_rate_source': 'measured',
        })
    else:
        result.update({
            'total_charging_sessions': 0,
            'measured_charge_rate': None,
            'charge_rate_source': 'config fallback',
        })

    # --- Generator hours ---
    effective_rate = result['measured_charge_rate'] or fallback_charge_rate
    running_avg = result['running_avg_pct_per_day']
    result['effective_charge_rate'] = effective_rate

    # Daily maintenance: hours to replace average daily usage
    result['daily_maintenance_hours'] = (
        running_avg / effective_rate if effective_rate > 0 else 0
    )

    # --- Per-source rates for the targeted time cards ---
    # Generator rate: for AGS timing ("how long to run the generator?")
    # Shore rate: for hookup planning ("when can I leave?")
    generator_stats = [s for s in charging_stats if _charge_types_for(s) == {'Generator'}]
    shore_stats     = [s for s in charging_stats if _charge_types_for(s) == {'Shore'}]
    generator_rate  = generator_stats[-1]['cc_rate_pct_per_hour'] if generator_stats else None
    shore_rate      = shore_stats[-1]['cc_rate_pct_per_hour']     if shore_stats     else None
    result['generator_rate'] = generator_rate
    result['shore_rate']     = shore_rate

    # hours_to_target: use generator rate; fall back to effective_rate if no tagged generator sessions
    rate_for_target = generator_rate if generator_rate else effective_rate
    # hours_to_100: use shore rate only — None (N/A) if no shore sessions tagged yet
    if current_soc is not None and rate_for_target > 0:
        result['hours_to_target'] = max(0.0, target_soc - current_soc) / rate_for_target
    else:
        result['hours_to_target'] = None
    if current_soc is not None and shore_rate and shore_rate > 0:
        result['hours_to_100'] = max(0.0, 100.0 - current_soc) / shore_rate
    else:
        result['hours_to_100'] = None
    result['current_soc'] = current_soc
    result['target_soc'] = target_soc
    result['capacity_ah'] = capacity_ah
    result['remaining_ah'] = (current_soc / 100 * capacity_ah) if (current_soc is not None and capacity_ah) else None

    # --- Days remaining at each usage rate ---
    def _days_rem(rate):
        if current_soc and rate and rate > 0:
            return current_soc / rate
        return None

    def _days_full(rate):
        if rate and rate > 0:
            return 100.0 / rate
        return None

    result['days_remaining_24h'] = _days_rem(result.get('last_24h_pct_per_day'))
    result['days_remaining_7d']  = _days_rem(result.get('weekly_avg_pct_per_day'))
    result['full_battery_24h']   = _days_full(result.get('last_24h_pct_per_day'))
    result['full_battery_7d']    = _days_full(result.get('weekly_avg_pct_per_day'))

    return result


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

def compute_diagnostics(charging_stats, discharge_stats, all_readings,
                        charge_type_map, cfg_diag):
    """Analyse sessions and readings for anomalous patterns.

    cfg_diag is a plain dict with keys matching the [diagnostics] config section.
    Returns a dict with four lists:
        thermal_derating, knee_soc, charge_rate_decline, parasitic_drain.
    """
    knee_baseline  = int(cfg_diag.get('knee_soc_baseline_sessions', 5))
    knee_drop      = float(cfg_diag.get('knee_soc_drop_threshold_pct', 10))
    rate_drop_pct  = float(cfg_diag.get('charge_rate_decline_threshold_pct', 20))
    derate_drop    = float(cfg_diag.get('thermal_derating_drop_pct', 15))
    parasitic_pct  = float(cfg_diag.get('parasitic_drain_threshold_pct', 2))
    parasitic_hrs  = float(cfg_diag.get('parasitic_drain_min_hours', 4))

    # --- 1. Thermal derating (re-evaluate with config thresholds) ---
    thermal_results = []
    for s in charging_stats:
        peak    = s.get('derating_peak_amps')
        plateau = s.get('derating_plateau_amps')
        if peak is not None and plateau is not None and peak > 30:
            if (peak - plateau) / peak >= derate_drop / 100:
                sid = _session_id_from_stat(s, 'charge')
                thermal_results.append({
                    'sid': sid,
                    'date': s['date'],
                    'peak': peak,
                    'plateau': plateau,
                })

    # --- 2. Knee SOC drift ---
    knee_results = []
    cv_sessions = [s for s in charging_stats if s.get('cv_detected') and s.get('knee_soc') is not None]
    if len(cv_sessions) > knee_baseline:
        for i in range(knee_baseline, len(cv_sessions)):
            baseline_knees = [s['knee_soc'] for s in cv_sessions[i - knee_baseline:i]]
            median_knee = statistics.median(baseline_knees)
            current_knee = cv_sessions[i]['knee_soc']
            if current_knee < median_knee - knee_drop:
                sid = _session_id_from_stat(cv_sessions[i], 'charge')
                knee_results.append({
                    'sid': sid,
                    'date': cv_sessions[i]['date'],
                    'knee_soc': current_knee,
                    'baseline_median': round(median_knee, 1),
                    'delta': round(median_knee - current_knee, 1),
                })

    # --- 3. Charge rate decline (per single charge type) ---
    rate_results = {}
    charge_type_map = charge_type_map or {}
    _SINGLE_TYPES = {'Shore', 'Generator', 'Driving'}
    by_type = defaultdict(list)
    for s in charging_stats:
        sid = _session_id_from_stat(s, 'charge')
        raw_type = charge_type_map.get(sid, '')
        types = [t.strip() for t in raw_type.split(',') if t.strip()]
        if len(types) == 1 and types[0] in _SINGLE_TYPES:
            by_type[types[0]].append(s)

    for ctype, sessions in by_type.items():
        if len(sessions) < 4:
            continue
        all_rates = [s['cc_rate_pct_per_hour'] for s in sessions]
        all_time_avg = sum(all_rates) / len(all_rates)
        recent_3 = all_rates[-3:]
        recent_avg = sum(recent_3) / len(recent_3)
        if recent_avg < all_time_avg * (1 - rate_drop_pct / 100):
            flagged_sids = [_session_id_from_stat(s, 'charge') for s in sessions[-3:]]
            rate_results[ctype] = {
                'sids': flagged_sids,
                'recent_avg': round(recent_avg, 2),
                'all_time_avg': round(all_time_avg, 2),
                'decline_pct': round((1 - recent_avg / all_time_avg) * 100, 1),
            }

    # --- 4. Parasitic drain (SOC drop during idle periods) ---
    parasitic_results = []
    if all_readings:
        # Build sorted list of session intervals from both session types
        all_intervals = []
        for s in charging_stats:
            all_intervals.append((s['ts_start'], s['ts_end']))
        for s in discharge_stats:
            all_intervals.append((s['ts_start'], s['ts_end']))
        all_intervals.sort()

        # Find idle gaps: reading spans not covered by any session
        sorted_readings = sorted(all_readings, key=lambda r: r['timestamp'])
        n = len(sorted_readings)
        i = 0
        while i < n:
            ts_i = parse_ts(sorted_readings[i]['timestamp'])
            # Find gap start: first reading not inside any interval
            in_session = any(ts_start <= ts_i <= ts_end for ts_start, ts_end in all_intervals)
            if in_session:
                i += 1
                continue

            # Collect contiguous idle readings
            gap_start_idx = i
            while i < n:
                ts_cur = parse_ts(sorted_readings[i]['timestamp'])
                if any(ts_start <= ts_cur <= ts_end for ts_start, ts_end in all_intervals):
                    break
                i += 1
            gap_end_idx = i - 1

            gap_readings = sorted_readings[gap_start_idx:gap_end_idx + 1]
            if len(gap_readings) < 2:
                continue

            ts_gap_start = parse_ts(gap_readings[0]['timestamp'])
            ts_gap_end   = parse_ts(gap_readings[-1]['timestamp'])
            gap_hours = (ts_gap_end - ts_gap_start).total_seconds() / 3600
            if gap_hours < parasitic_hrs:
                continue

            soc_start_gap = gap_readings[0]['soc']
            soc_end_gap   = gap_readings[-1]['soc']
            soc_drop = soc_start_gap - soc_end_gap
            if soc_drop < parasitic_pct:
                continue

            period_start_iso = ts_gap_start.isoformat()
            diag_id = hashlib.sha256(
                f'parasitic_drain:{period_start_iso}'.encode()
            ).hexdigest()[:16]
            parasitic_results.append({
                'diag_id': diag_id,
                'period_start': period_start_iso,
                'period_end': ts_gap_end.isoformat(),
                'soc_drop': round(soc_drop, 1),
                'hours': round(gap_hours, 1),
            })

    return {
        'thermal_derating': thermal_results,
        'knee_soc': knee_results,
        'charge_rate_decline': rate_results,
        'parasitic_drain': parasitic_results,
    }


def _session_id_from_stat(stat, session_type):
    """Derive the stable session_id from a stats dict."""
    raw = f'{session_type}:{stat["ts_start"].isoformat()}'
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Chart helpers
# ---------------------------------------------------------------------------

def rolling_avg(values, window=7):
    """Simple rolling mean over up to `window` preceding values (inclusive)."""
    result = []
    for i, _ in enumerate(values):
        start = max(0, i - window + 1)
        result.append(sum(values[start:i + 1]) / (i - start + 1))
    return result


def lttb_downsample(points, n_buckets):
    """Largest-Triangle-Three-Buckets downsampling.

    points: list of (x, y) where x is numeric (epoch seconds).
    n_buckets: target number of output points.
    Returns: list of selected indices into points.
    """
    n = len(points)
    if n_buckets >= n or n_buckets < 3:
        return list(range(n))

    selected = [0]
    bucket_size = (n - 2) / (n_buckets - 2)
    a = 0

    for i in range(n_buckets - 2):
        b_start = int((i + 1) * bucket_size) + 1
        b_end   = min(int((i + 2) * bucket_size) + 1, n - 1)
        b_end   = max(b_end, b_start + 1)  # ensure at least one candidate
        c_start = min(int((i + 2) * bucket_size) + 1, n - 1)
        c_end   = min(int((i + 3) * bucket_size) + 1, n)
        c_end   = max(c_end, c_start + 1)

        c_x = sum(points[j][0] for j in range(c_start, c_end)) / (c_end - c_start)
        c_y = sum(points[j][1] for j in range(c_start, c_end)) / (c_end - c_start)

        ax, ay = points[a]
        max_area, best = -1, b_start
        for j in range(b_start, min(b_end, n - 1)):
            bx, by = points[j]
            area = abs((ax - c_x) * (by - ay) - (ax - bx) * (c_y - ay))
            if area > max_area:
                max_area, best = area, j

        selected.append(best)
        a = best

    selected.append(n - 1)
    return selected


def adaptive_downsample(readings, boundary_ts_set, downsample_cfg):
    """Downsample readings for the SOC chart using LTTB with three age tiers.

    Tier 1 (< tier1_hours old): kept at full resolution.
    Tier 2 (tier1–tier2_hours old): LTTB to one point per tier2_mins minutes.
    Tier 3 (> tier2_hours old): LTTB to one point per tier3_mins minutes.

    boundary_ts_set: set of raw timestamp strings that must be preserved
    (session start/end points) regardless of tier.
    Returns: (timestamps_local, soc_values) as parallel lists.
    """
    if not readings:
        return [], [], []

    tier1_hours = downsample_cfg.get('tier1_hours', 6.0)
    tier2_hours = downsample_cfg.get('tier2_hours', 24.0)
    tier2_mins  = downsample_cfg.get('tier2_mins',  5.0)
    tier3_mins  = downsample_cfg.get('tier3_mins',  15.0)

    now_ts    = parse_ts(readings[-1]['timestamp'])
    t1_cutoff = now_ts - timedelta(hours=tier1_hours)
    t2_cutoff = now_ts - timedelta(hours=tier2_hours)

    tier1, tier2, tier3 = [], [], []
    for r in readings:
        ts = parse_ts(r['timestamp'])
        if ts >= t1_cutoff:
            tier1.append(r)
        elif ts >= t2_cutoff:
            tier2.append(r)
        else:
            tier3.append(r)

    def apply_lttb(tier_readings, interval_mins):
        if len(tier_readings) < 3:
            return tier_readings
        duration_mins = (
            parse_ts(tier_readings[-1]['timestamp']).timestamp() -
            parse_ts(tier_readings[0]['timestamp']).timestamp()
        ) / 60
        n_buckets = max(2, round(duration_mins / interval_mins))
        points = [(parse_ts(r['timestamp']).timestamp(), r['soc']) for r in tier_readings]
        indices = set(lttb_downsample(points, n_buckets))
        # Pin session boundary readings so shading regions stay accurate
        for i, r in enumerate(tier_readings):
            if r['timestamp'] in boundary_ts_set:
                indices.add(i)
        return [tier_readings[i] for i in sorted(indices)]

    sampled2 = apply_lttb(tier2, tier2_mins)
    sampled3 = apply_lttb(tier3, tier3_mins)

    all_readings = sampled3 + sampled2 + tier1
    timestamps_local = [to_local(parse_ts(r['timestamp'])) for r in all_readings]
    soc_values       = [min(round(r['soc'], 1), 100.0) for r in all_readings]
    return timestamps_local, soc_values, all_readings


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------

def session_id(session_type, ts_start_iso):
    """Stable identifier: sha256 of type + start timestamp."""
    raw = f'{session_type}:{ts_start_iso}'
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def load_charge_types(db_path):
    """Return {session_id: charge_type_str} for all charge sessions with a stored type."""
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT session_id, charge_type FROM session_notes WHERE session_type='charge'"
        ).fetchall()
        conn.close()
        return {r[0]: r[1] or '' for r in rows}
    except Exception:
        return {}


def load_session_notes(db_path):
    """Return {session_id: note_text} for sessions that have a non-empty note."""
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT session_id, note FROM session_notes WHERE note != '' AND note IS NOT NULL"
        ).fetchall()
        conn.close()
        return {r[0]: r[1] for r in rows}
    except Exception:
        return {}


CHARGE_TYPE_ORDER = ['Shore', 'Generator', 'Driving', 'Unclassified']
CHARGE_TYPE_COLORS = {
    'Shore':                'rgba(46, 139, 87, 0.7)',
    'Generator':            'rgba(200, 120, 30, 0.7)',
    'Driving':              'rgba(52, 110, 180, 0.7)',
    'Unclassified':         'rgba(150, 150, 150, 0.7)',
    'All Sessions':         'rgba(46, 139, 87, 0.7)',
    'Generator + Driving':  'rgba(140, 80, 140, 0.7)',  # purple blend of orange+blue
}

_KNOWN_SINGLE_TYPES = set(CHARGE_TYPE_ORDER) - {'Unclassified'}


def _plan_charge_subplots(charging_stats, charge_type_map):
    """Return [(type_label, [stat_dicts])] for types with ≥1 session, in display order.

    Sessions tagged with a single type go into that type's bucket.
    Sessions tagged with multiple types go into a combined bucket (e.g. 'Generator + Driving')
    rather than being duplicated across individual buckets.
    """
    if charge_type_map is None:
        return [('All Sessions', charging_stats)]
    buckets = {t: [] for t in CHARGE_TYPE_ORDER}
    combined_buckets = {}  # combined_label -> [stat_dicts]
    for stat in charging_stats:
        sid = session_id('charge', stat['ts_start'].isoformat())
        raw = charge_type_map.get(sid, '')
        types = [t.strip() for t in raw.split(',') if t.strip()] if raw else []
        known = [t for t in types if t in _KNOWN_SINGLE_TYPES]
        if not types or not known:
            buckets['Unclassified'].append(stat)
        elif len(known) == 1:
            buckets[known[0]].append(stat)
        else:
            # Multi-type: build a stable combined label ordered by CHARGE_TYPE_ORDER
            ordered = sorted(known, key=lambda x: CHARGE_TYPE_ORDER.index(x)
                             if x in CHARGE_TYPE_ORDER else 99)
            label = ' + '.join(ordered)
            combined_buckets.setdefault(label, []).append(stat)
    result = [(label, buckets[label]) for label in CHARGE_TYPE_ORDER if buckets[label]]
    result += sorted(combined_buckets.items())
    return result


def build_figure(readings, discharge_sessions, charging_sessions,
                 discharge_stats, charging_stats, summary,
                 time_format='12h', downsample_cfg=None, charge_type_map=None,
                 note_map=None, window_days=3):
    """Build and return the Plotly fig object. No HTML output."""
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    if downsample_cfg is None:
        downsample_cfg = {'tier1_hours': 6.0, 'tier2_hours': 24.0,
                          'tier2_mins': 5.0, 'tier3_mins': 15.0}

    # Build boundary set for LTTB: discharge sessions pin start/end only;
    # charging sessions pin every reading so CC/CV curves are always full resolution.
    boundary_ts_set = set()
    for session in discharge_sessions:
        if session:
            boundary_ts_set.add(session[0]['timestamp'])
            boundary_ts_set.add(session[-1]['timestamp'])
    for session in charging_sessions:
        for r in session:
            boundary_ts_set.add(r['timestamp'])

    timestamps, soc_values, ds_readings = adaptive_downsample(readings, boundary_ts_set, downsample_cfg)
    voltage_values = [r.get('voltage') for r in ds_readings]
    current_values = [r.get('current') for r in ds_readings]

    tick_fmt = '%I:%M %p' if time_format == '12h' else '%H:%M'

    # Determine per-type charge subplots (dynamic row count)
    charge_plan = _plan_charge_subplots(charging_stats, charge_type_map)
    n_charge_rows = len(charge_plan) if charge_plan else 1
    n_rows = 2 + n_charge_rows   # SOC + daily + N×charge  (V and A overlay on SOC)

    # Y-axis names for V and A overlaid on the SOC chart.
    # Must not conflict with the per-row subplot axes (yaxis … yaxis{n_rows}).
    volt_yaxis_num  = n_rows + 1
    curr_yaxis_num  = n_rows + 2
    volt_yaxis_name = f'y{volt_yaxis_num}'
    curr_yaxis_name = f'y{curr_yaxis_num}'
    volt_layout_key = f'yaxis{volt_yaxis_num}'
    curr_layout_key = f'yaxis{curr_yaxis_num}'

    remaining_h = 1.0 - 0.45 - 0.20
    charge_h = remaining_h / n_charge_rows
    row_heights = [0.45, 0.20] + [charge_h] * n_charge_rows

    if charge_plan:
        charge_titles = tuple(
            f'Constant Current Phase Rate — {lbl} (%/hour)' if lbl != 'All Sessions'
            else 'Constant Current Phase Rate per Session (%/hour)'
            for lbl, _ in charge_plan
        )
    else:
        charge_titles = ('Charge Rate per Session (%/hour)',)
    subplot_titles = (
        'State of Charge Over Time',
        'Daily Battery Usage (%/day)',
        *charge_titles,
    )

    fig = make_subplots(
        rows=n_rows, cols=1,
        subplot_titles=subplot_titles,
        shared_xaxes=True,
        vertical_spacing=0.07,
        row_heights=row_heights,
    )

    # Read actual subplot y-domains so we can pin each legend to its subplot
    soc_legend_y    = fig.layout.yaxis.domain[1]   # top of row 1
    charge_legend_y = fig.layout.yaxis3.domain[1]  # top of row 3 (first charge row)

    fig.add_trace(go.Scatter(
        x=timestamps, y=soc_values,
        mode='lines', name='State of Charge',
        line=dict(color='steelblue', width=2),
        showlegend=True, legend='legend',
    ), row=1, col=1)

    # Midnight lines
    for midnight in midnights_in_range(timestamps):
        fig.add_vline(
            x=midnight.timestamp() * 1000,  # Plotly uses milliseconds
            line_color='black', line_width=1, line_dash='solid',
            row=1, col=1,
        )

    # Dummy traces so shading colours appear in the SOC legend
    fig.add_trace(go.Scatter(
        x=[None], y=[None], mode='markers', name='Discharging',
        marker=dict(symbol='square', size=12, color='rgba(255, 165, 0, 0.6)'),
        showlegend=True, legend='legend',
    ), row=1, col=1)
    fig.add_trace(go.Scatter(
        x=[None], y=[None], mode='markers', name='Charging',
        marker=dict(symbol='square', size=12, color='rgba(46, 139, 87, 0.6)'),
        showlegend=True, legend='legend',
    ), row=1, col=1)
    fig.add_trace(go.Scatter(
        x=[None], y=[None], mode='markers', name='Logged (idle / transitioning)',
        marker=dict(symbol='square', size=12, color='rgba(180, 180, 180, 0.8)'),
        showlegend=True, legend='legend',
    ), row=1, col=1)
    fig.add_trace(go.Scatter(
        x=[None], y=[None], mode='markers', name='No data (logger off)',
        marker=dict(symbol='square', size=12, color='white',
                    line=dict(color='#aaa', width=1)),
        showlegend=True, legend='legend',
    ), row=1, col=1)

    if summary.get('daily_rates'):
        days, rates, ahs, hours_list = zip(*summary['daily_rates'])
        today_str = summary.get('today_str', '')
        # Use local noon as the x-position for each bar so the axis is a proper
        # datetime axis — lets us set bar width in milliseconds.
        day_dts = [
            datetime.strptime(d + 'T12:00:00', '%Y-%m-%dT%H:%M:%S').astimezone()
            for d in days
        ]
        # Split into complete days and today (partial)
        complete_x, complete_y, complete_ah, complete_h = [], [], [], []
        partial_x, partial_y, partial_ah, partial_h = [], [], [], []
        for dt, d, r, ah, h in zip(day_dts, days, rates, ahs, hours_list):
            if d == today_str:
                partial_x.append(dt); partial_y.append(r); partial_ah.append(ah); partial_h.append(h)
            else:
                complete_x.append(dt); complete_y.append(r); complete_ah.append(ah); complete_h.append(h)
        if complete_x:
            fig.add_trace(go.Bar(
                x=complete_x, y=complete_y,
                name='Daily usage rate', marker_color='steelblue', showlegend=False,
                width=5 * 3600 * 1000,
                customdata=[[a, h] for a, h in zip(complete_ah, complete_h)],
                hovertemplate=(
                    '<b>%{x|%b %-d}</b><br>'
                    '%{y:.1f}%/day<br>'
                    '%{customdata[0]:.1f} Ah consumed<br>'
                    '%{customdata[1]:.1f}h of discharge data<extra></extra>'
                ),
            ), row=2, col=1)
        if partial_x:
            fig.add_trace(go.Bar(
                x=partial_x, y=partial_y,
                name='Today (partial)', marker_color='lightsteelblue',
                marker_line=dict(color='steelblue', width=1),
                showlegend=False,
                width=5 * 3600 * 1000,
                customdata=[[a, h] for a, h in zip(partial_ah, partial_h)],
                hovertemplate=(
                    '<b>%{x|%b %-d}</b> <i>(today)</i><br>'
                    '%{y:.1f}%/day<br>'
                    '%{customdata[0]:.1f} Ah so far<br>'
                    '%{customdata[1]:.1f}h of data so far<br>'
                    '<i>Partial day — rate will change</i><extra></extra>'
                ),
            ), row=2, col=1)
        if summary['running_avg_pct_per_day']:
            fig.add_hline(
                y=summary['running_avg_pct_per_day'],
                line_dash='dash', line_color='darkorange',
                annotation_text=f"All days avg {summary['running_avg_pct_per_day']:.1f}%/day",
                annotation_position='top right',
                row=2, col=1,
            )

    # Charge rate per session — one subplot per type
    for idx, (type_label, type_stats) in enumerate(charge_plan):
        charge_row = 3 + idx
        if not type_stats:
            continue
        bar_color = CHARGE_TYPE_COLORS.get(type_label, CHARGE_TYPE_COLORS['Unclassified'])
        # Snap to local noon so bars align with daily bars and the SOC chart
        session_times = [
            to_local(s['ts_start']).replace(hour=12, minute=0, second=0, microsecond=0)
            for s in type_stats
        ]
        cc_rates      = [s['cc_rate_pct_per_hour'] for s in type_stats]
        full_rates    = [s['charge_rate_pct_per_hour'] for s in type_stats]
        rolling       = rolling_avg(cc_rates)
        session_amps  = [s['avg_amps'] for s in type_stats]
        knee_socs     = [s.get('knee_soc') for s in type_stats]

        # customdata: [avg_amps, full_rate, knee_soc_or_nan]
        customdata = [
            [a, fr, ks if ks is not None else float('nan')]
            for a, fr, ks in zip(session_amps, full_rates, knee_socs)
        ]
        hovertemplate = (
            '<b>%{y:.1f}%/hr CC phase</b><br>'
            '%{customdata[0]:.1f} A avg<br>'
            'Full session: %{customdata[1]:.1f}%/hr<br>'
            'Knee SOC: %{customdata[2]:.1f}%<br>'
            '<i style="color:white;font-size:11px">CC = Constant Current phase: fast charging<br>'
            'before the charger tapers to CV (top-off). CC rate<br>'
            'is the meaningful speed; CV tail skews session avg.</i>'
            '<extra></extra>'
        )

        fig.add_trace(go.Bar(
            x=session_times, y=cc_rates,
            name=f'{type_label} charge rate', marker_color=bar_color,
            showlegend=False,
            width=5 * 3600 * 1000,
            customdata=customdata,
            hovertemplate=hovertemplate,
        ), row=charge_row, col=1)

        fig.add_trace(go.Scatter(
            x=session_times, y=rolling,
            mode='lines+markers', name='7-session avg',
            line=dict(color='darkgreen', width=2),
            marker=dict(size=5),
            showlegend=(idx == 0), legend='legend2',
        ), row=charge_row, col=1)

        if type_label not in ('Unclassified',):
            type_avg = sum(cc_rates) / len(cc_rates)
            fig.add_hline(
                y=type_avg,
                line_dash='dash', line_color='gray',
                annotation_text=f"All-time avg {type_avg:.1f}%/h",
                annotation_position='top left',
                row=charge_row, col=1,
            )


    # Voltage and Current overlaid on the SOC chart using secondary y-axes
    if voltage_values and any(v is not None for v in voltage_values):
        fig.add_trace(go.Scatter(
            x=timestamps, y=voltage_values,
            mode='lines', name='Voltage (V)',
            line=dict(color='#e67e22', width=1.5),
            showlegend=True, legend='legend',
            connectgaps=False,
            yaxis=volt_yaxis_name,
            hovertemplate=f'<b>%{{y:.2f}} V</b><br>%{{x|{tick_fmt}}}<extra></extra>',
        ))

    if current_values and any(v is not None for v in current_values):
        fig.add_trace(go.Scatter(
            x=timestamps, y=current_values,
            mode='lines', name='Current (A)',
            line=dict(color='#8e44ad', width=1.5),
            showlegend=True, legend='legend',
            connectgaps=False,
            yaxis=curr_yaxis_name,
            hovertemplate=f'<b>%{{y:.1f}} A</b><br>%{{x|{tick_fmt}}}<extra></extra>',
        ))

    # Shade logged-but-unclassified periods light grey so that true data gaps
    # (white) are visually distinct from periods where the logger was running
    # but the current was neutral or the session was too short to keep.
    # Algorithm: find contiguous reading spans (breaks at gaps > 10 min), then
    # draw one grey vrect per span. Orange and green vrects paint on top.
    _GAP_THRESHOLD = timedelta(minutes=10)
    if readings:
        span_starts = []
        span_ends = []
        span_start = to_local(parse_ts(readings[0]['timestamp']))
        prev_ts = span_start
        for r in readings[1:]:
            cur_ts = to_local(parse_ts(r['timestamp']))
            if cur_ts - prev_ts > _GAP_THRESHOLD:
                span_ends.append(prev_ts)
                span_starts.append(cur_ts)
            prev_ts = cur_ts
        span_starts.insert(0, span_start)
        span_ends.append(prev_ts)
        for x0, x1 in zip(span_starts, span_ends):
            fig.add_vrect(
                x0=x0, x1=x1,
                fillcolor='rgba(180, 180, 180, 0.40)', layer='below', line_width=0,
                row=1, col=1,
            )

    # Shade discharge sessions amber, charging sessions green — SOC chart only.
    # Split vrects at data gaps (>10 min) so gaps within a session show white,
    # consistent with the grey logged-span logic above.
    def _session_vrects(session, fillcolor):
        if len(session) < 2:
            return
        span_start = to_local(parse_ts(session[0]['timestamp']))
        prev_ts = span_start
        for r in session[1:]:
            cur_ts = to_local(parse_ts(r['timestamp']))
            if cur_ts - prev_ts > _GAP_THRESHOLD:
                fig.add_vrect(x0=span_start, x1=prev_ts,
                              fillcolor=fillcolor, layer='below', line_width=0,
                              row=1, col=1)
                span_start = cur_ts
            prev_ts = cur_ts
        fig.add_vrect(x0=span_start, x1=prev_ts,
                      fillcolor=fillcolor, layer='below', line_width=0,
                      row=1, col=1)

    for session in discharge_sessions:
        _session_vrects(session, 'rgba(255, 165, 0, 0.25)')
    for session in charging_sessions:
        _session_vrects(session, 'rgba(46, 139, 87, 0.25)')

    # Session note markers: one visible circle per noted session, pinned near the
    # top of the SOC axis (y=97%) at the session's temporal midpoint.  A single
    # point doesn't compete with the SOC/V/A traces — hover fires only when the
    # cursor is directly over the marker.
    if note_map:
        def _escape(text, wrap=50):
            import textwrap
            lines = []
            for paragraph in text.splitlines():
                lines.extend(textwrap.wrap(paragraph, wrap) or [''])
            return '<br>'.join(
                l.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                for l in lines
            )

        _NOTE_Y = 3  # fixed SOC % — near bottom of axis, clear of SOC/V/A traces

        for sessions_list, stype, color in [
            (discharge_sessions, 'discharge', 'rgba(230, 120, 0, 0.85)'),
            (charging_sessions,  'charge',    'rgba(34, 120, 70, 0.85)'),
        ]:
            for session in sessions_list:
                if len(session) < 2:
                    continue
                sid = session_id(stype, parse_ts(session[0]['timestamp']).isoformat())
                note = note_map.get(sid, '')
                if not note:
                    continue

                t_start = to_local(parse_ts(session[0]['timestamp']))
                t_end   = to_local(parse_ts(session[-1]['timestamp']))
                x_mid   = t_start + (t_end - t_start) / 2

                fig.add_trace(go.Scatter(
                    x=[x_mid], y=[_NOTE_Y],
                    mode='markers+text',
                    text=['ⓘ'],
                    textposition='middle center',
                    textfont=dict(size=16, color=color),
                    marker=dict(size=22, color='rgba(0,0,0,0)'),  # invisible hit area
                    hovertemplate=f'<b>Note</b><br>{_escape(note)}<extra></extra>',
                    showlegend=False,
                    name='',
                ), row=1, col=1)

    _legend_style = dict(
        xanchor='left', yanchor='top',
        bgcolor='rgba(255,255,255,0.85)',
        bordercolor='#ddd', borderwidth=1,
        font=dict(size=11),
    )
    fig.update_layout(
        height=900 + (n_charge_rows - 1) * 200,
        showlegend=True, template='plotly_white',
        margin=dict(t=50, b=40, l=60, r=200),
        legend =dict(x=1.01, y=soc_legend_y,    **_legend_style),
        legend2=dict(x=1.01, y=charge_legend_y, **_legend_style),
        **{
            volt_layout_key: dict(
                overlaying='y', side='right', anchor='free', position=0.96,
                ticksuffix='V', tickfont=dict(color='#e67e22'),
                showgrid=False, zeroline=False,
            ),
            curr_layout_key: dict(
                overlaying='y', side='right', anchor='free', position=0.91,
                ticksuffix='A', tickfont=dict(color='#8e44ad'),
                showgrid=False,
                zeroline=True, zerolinecolor='rgba(128,128,128,0.5)', zerolinewidth=1,
            ),
        },
    )
    fig.update_yaxes(title_text='SOC %', range=[0, 105], row=1, col=1)
    fig.update_yaxes(title_text='%/day', row=2, col=1)
    for i in range(n_charge_rows):
        fig.update_yaxes(title_text='%/hour', row=3 + i, col=1)
    # Restrict x-domain so V/A axes fit in the right margin before the edge
    fig.update_xaxes(domain=[0, 0.91])
    # Build noon-aligned tick positions explicitly — one per day across the full dataset.
    # tick0+dtick doesn't work without both set; tick0 alone is ignored by Plotly.
    # tickmode='array' guarantees labels land exactly at bar centres (local noon).
    # Always 1-day step so the default 3-day viewport always has visible tick marks;
    # Plotly auto-skips overlapping labels when the user zooms out to see all history.
    if readings:
        _noon = dict(hour=12, minute=0, second=0, microsecond=0)
        _t0 = to_local(parse_ts(readings[0]['timestamp'])).replace(**_noon)
        _t1 = to_local(parse_ts(readings[-1]['timestamp'])).replace(**_noon)
        _tick_dts, _t = [], _t0
        while _t <= _t1:
            _tick_dts.append(_t)
            _t += timedelta(days=1)
        fig.update_xaxes(
            tickmode='array',
            tickvals=_tick_dts,
            ticktext=[dt.strftime('%b %-d') for dt in _tick_dts],
            showticklabels=True,
        )
        # Set initial viewport to last window_days; add range-selector buttons for
        # quick switching between time windows (no minimap duplication).
        if window_days:
            _last_ts = to_local(parse_ts(readings[-1]['timestamp']))
            _win_start = _last_ts - timedelta(days=window_days)
            fig.update_xaxes(range=[_win_start, _last_ts + timedelta(hours=2)])
        # xaxis_rangeselector targets only xaxis (row 1 = SOC chart), not xaxis2/3/…
        # so buttons appear once, below the SOC chart title, not on every subplot row.
        # active=0 highlights the first button (3d) on load.
        fig.update_layout(
            xaxis_rangeselector=dict(
                buttons=[
                    dict(count=3,  label='3d',  step='day', stepmode='backward'),
                    dict(count=7,  label='7d',  step='day', stepmode='backward'),
                    dict(count=30, label='30d', step='day', stepmode='backward'),
                    dict(step='all', label='All'),
                ],
                bgcolor='#f4f6f9',
                activecolor='#1a5276',
                font=dict(size=11),
            )
        )
    else:
        fig.update_xaxes(showticklabels=True)

    return fig


def generate_html(readings, discharge_sessions, charging_sessions,
                  discharge_stats, charging_stats, summary, output_path,
                  time_format='12h', downsample_cfg=None, charge_type_map=None,
                  note_map=None):
    import plotly.io as pio

    fig = build_figure(readings, discharge_sessions, charging_sessions,
                       discharge_stats, charging_stats, summary,
                       time_format=time_format, downsample_cfg=downsample_cfg,
                       charge_type_map=charge_type_map, note_map=note_map)

    chart_html = pio.to_html(fig, full_html=False, include_plotlyjs='cdn')

    # Discharge session table
    discharge_rows = ''
    for s in discharge_stats:
        color = '#c0392b' if s['pct_per_day'] > 50 else '#1a5276'
        ah_cell = f'{s["consumed_ah"]:.1f}' if s.get('consumed_ah') is not None else '—'
        sid = session_id('discharge', s['ts_start'].isoformat())
        note = (note_map.get(sid, '') or '') if note_map else ''
        note_cell = f'<td style="color:#555;font-size:0.85em">{note}</td>' if note else '<td style="color:#bbb">—</td>'
        discharge_rows += (
            f'<tr>'
            f'<td>{s["start"]}</td><td>{s["end"]}</td>'
            f'<td>{s["soc_start"]:.1f}%</td><td>{s["soc_end"]:.1f}%</td>'
            f'<td>{s["pct_drop"]:.1f}%</td><td>{ah_cell}</td><td>{s["hours"]:.1f}h</td>'
            f'<td style="color:{color};font-weight:bold">{s["pct_per_day"]:.1f}</td>'
            f'<td>{s["avg_watts"]:.0f}W</td>'
            f'{note_cell}'
            f'</tr>\n'
        )

    # Charging session table
    charging_rows = ''
    for s in charging_stats:
        sid = session_id('charge', s['ts_start'].isoformat())
        raw_type = charge_type_map.get(sid, '') if charge_type_map else ''
        type_display = raw_type.replace(',', ' + ') if raw_type else '<span style="color:#aaa">—</span>'
        note = (note_map.get(sid, '') or '') if note_map else ''
        note_cell = f'<td style="color:#555;font-size:0.85em">{note}</td>' if note else '<td style="color:#bbb">—</td>'
        if s.get('cv_detected'):
            cc_rate_cell = f'<td style="color:#1a7a4a;font-weight:bold">{s["cc_rate_pct_per_hour"]:.1f}</td>'
            knee_cell    = f'<td>{s["knee_soc"]:.1f}%</td>'
        else:
            cc_rate_cell = '<td style="color:#bbb">—</td>'
            knee_cell    = '<td style="color:#bbb">—</td>'
        charging_rows += (
            f'<tr>'
            f'<td>{s["start"]}</td><td>{s["end"]}</td>'
            f'<td>{s["soc_start"]:.1f}%</td><td>{s["soc_end"]:.1f}%</td>'
            f'<td>+{s["pct_gain"]:.1f}%</td><td>{s["hours"]:.1f}h</td>'
            f'<td>{s["charge_rate_pct_per_hour"]:.1f}</td>'
            f'{cc_rate_cell}'
            f'{knee_cell}'
            f'<td>{s["avg_amps"]:.1f}</td>'
            f'<td>{type_display}</td>'
            f'{note_cell}'
            f'</tr>\n'
        )

    running       = summary.get('running_avg_pct_per_day', 0)
    weekly        = summary.get('weekly_avg_pct_per_day', 0)
    rate_24h      = summary.get('last_24h_pct_per_day')
    watts         = summary.get('avg_discharge_watts', 0)
    avg_ah        = summary.get('avg_ah_per_day', 0)
    weekly_ah     = summary.get('weekly_avg_ah_per_day', 0)
    remaining_ah  = summary.get('remaining_ah')
    capacity_ah   = summary.get('capacity_ah')

    def fmt_days(d):
        if d is None:
            return 'N/A'
        if d < 1:
            return f'{d * 24:.0f}h'
        return f'{d:.1f}d'

    days_rem_24h_str  = fmt_days(summary.get('days_remaining_24h'))
    days_rem_7d_str   = fmt_days(summary.get('days_remaining_7d'))
    full_bat_24h_str  = fmt_days(summary.get('full_battery_24h'))
    full_bat_7d_str   = fmt_days(summary.get('full_battery_7d'))
    rate_24h_str      = f'{rate_24h:.1f}' if rate_24h is not None else 'no'
    maint_hours   = summary.get('daily_maintenance_hours', 0)
    hours_to_tgt  = summary.get('hours_to_target')
    hours_to_100  = summary.get('hours_to_100')
    eff_rate      = summary.get('effective_charge_rate', 0)
    rate_source   = summary.get('charge_rate_source', 'config fallback')
    cur_soc       = summary.get('current_soc')
    tgt_soc       = summary.get('target_soc', 95.0)
    n_dis         = summary.get('total_discharge_sessions', 0)
    n_chg         = summary.get('total_charging_sessions', 0)
    generated     = datetime.now().strftime('%Y-%m-%d %H:%M')

    if remaining_ah is not None:
        remaining_ah_card = (
            f'<div class="card" data-tip="Amp-hours of usable charge remaining right now,'
            f' calculated as current SOC &times; battery capacity ({capacity_ah:.0f} Ah).'
            f' A concrete measure of how much energy is left in the bank.">'
            f'\n    <div class="value">{remaining_ah:.0f} Ah</div>'
            f'\n    <div class="label">Remaining in bank</div>'
            f'\n  </div>'
        )
    else:
        remaining_ah_card = ''

    if hours_to_tgt is not None and cur_soc is not None:
        if cur_soc >= tgt_soc:
            to_target_display = 'At target'
        else:
            to_target_display = f'{hours_to_tgt:.1f}h'
    else:
        to_target_display = 'N/A'
    to_target_label = f'To {tgt_soc:.0f}% \u2014 Generator'

    if hours_to_100 is not None and cur_soc is not None:
        if cur_soc >= 100.0:
            to_100_display = 'Full'
        else:
            to_100_display = f'{hours_to_100:.1f}h'
    else:
        to_100_display = 'N/A'
    to_100_label = 'To 100% \u2014 Shore'

    charge_rate_note = (
        f'Charge rate from most recent session: {eff_rate:.1f} %/hour'
        if rate_source == 'measured'
        else f'No charging sessions recorded yet — using config fallback: {eff_rate:.1f} %/hour'
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Battery Usage Report — {generated}</title>
<style>
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, sans-serif;
    max-width: 1100px; margin: 40px auto; padding: 0 20px; color: #333;
  }}
  h1 {{ color: #1a5276; margin-bottom: 4px; }}
  h2 {{ color: #1a5276; margin-top: 40px; }}
  .subtitle {{ color: #888; margin-bottom: 24px; font-size: 0.9em; }}
  .summary {{ display: flex; gap: 16px; flex-wrap: wrap; margin: 20px 0; }}
  .group-label {{
    font-size: 0.75em; font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.08em; color: #888; margin: 20px 0 6px 2px;
  }}
  .card {{
    background: #f4f6f9; border-radius: 10px; padding: 16px 24px;
    min-width: 140px; text-align: center; border: 1px solid #e0e4ea;
    position: relative; cursor: default;
  }}
  .card .value {{ font-size: 2em; font-weight: 700; color: #1a5276; }}
  .card .label {{ font-size: 0.82em; color: #666; margin-top: 4px; }}
  .card .sublabel {{ font-size: 0.78em; color: #999; margin-top: 2px; }}
  .card.green .value {{ color: #1a7a4a; }}
  /* Tooltip */
  .card[data-tip]::after {{
    content: attr(data-tip);
    position: absolute; bottom: 110%; left: 50%;
    transform: translateX(-50%);
    background: #222; color: #fff;
    padding: 8px 12px; border-radius: 6px;
    font-size: 0.78em; font-weight: 400; line-height: 1.45;
    width: 230px; text-align: left; white-space: normal;
    pointer-events: none; z-index: 100;
    opacity: 0; transition: opacity 0.15s;
  }}
  .card[data-tip]::before {{
    content: '';
    position: absolute; bottom: calc(110% - 6px); left: 50%;
    transform: translateX(-50%);
    border: 6px solid transparent; border-top-color: #222;
    pointer-events: none; z-index: 100;
    opacity: 0; transition: opacity 0.15s;
  }}
  .card[data-tip]:hover::after,
  .card[data-tip]:hover::before {{ opacity: 1; }}
  table {{
    border-collapse: collapse; width: 100%; margin-top: 16px; font-size: 0.88em;
  }}
  thead th {{
    background: #1a5276; color: white; padding: 10px 12px;
    text-align: left; white-space: nowrap;
    position: relative;
  }}
  thead.green th {{ background: #1a7a4a; }}
  thead th[data-tip] {{ cursor: help; }}
  thead th[data-tip]::after {{
    content: attr(data-tip);
    position: absolute; top: 110%; left: 50%;
    transform: translateX(-50%);
    background: #222; color: #fff;
    padding: 8px 12px; border-radius: 6px;
    font-size: 0.78em; font-weight: 400; line-height: 1.45;
    width: 220px; text-align: left; white-space: normal;
    pointer-events: none; z-index: 100;
    opacity: 0; transition: opacity 0.15s;
  }}
  thead th[data-tip]::before {{
    content: '';
    position: absolute; top: calc(110% - 6px); left: 50%;
    transform: translateX(-50%);
    border: 6px solid transparent; border-bottom-color: #222;
    pointer-events: none; z-index: 100;
    opacity: 0; transition: opacity 0.15s;
  }}
  thead th[data-tip]:hover::after,
  thead th[data-tip]:hover::before {{ opacity: 1; }}
  tbody td {{ padding: 8px 12px; border-bottom: 1px solid #e8ecf0; }}
  tbody tr:hover {{ background: #f4f6f9; }}
  .note {{ color: #999; font-size: 0.8em; margin-top: 32px; }}
</style>
</head>
<body>

<h1>Battery Usage Report</h1>
<p class="subtitle">
  Generated {generated} &middot; {len(readings):,} total readings &middot;
  {n_dis} discharge sessions &middot; {n_chg} charging sessions
</p>

<div class="group-label">Usage</div>
<div class="summary">
  <div class="card" data-tip="Average daily SOC consumption over the last 7 complete calendar days (today excluded — partial days skew the rate low). Each day counts equally. Use this to spot short-term trends.">
    <div class="value">{weekly:.1f}%</div>
    <div class="label">7-day avg / day</div>
    <div class="sublabel">{weekly_ah:.1f} Ah/day</div>
  </div>
  <div class="card" data-tip="Average daily SOC consumption across all complete calendar days on record (today excluded). Each day counts equally — same method as 7-day avg, just a longer window. Becomes more stable as more data accumulates.">
    <div class="value">{running:.1f}%</div>
    <div class="label">All days avg / day</div>
    <div class="sublabel">{avg_ah:.1f} Ah/day</div>
  </div>
  <div class="card" data-tip="Implied average power draw, calculated as (running avg %/day &divide; 100) &times; battery capacity &times; avg voltage &divide; 24. Derived from the SOC truth rather than sampled current readings, so it correctly accounts for brief high-draw events like hair dryers.">
    <div class="value">{watts:.0f}W</div>
    <div class="label">Avg discharge power</div>
  </div>
</div>

<div class="group-label">Power Remaining</div>
<div class="summary">
  <div class="card" data-tip="Days of battery left at your last 24 hours of discharge rate ({rate_24h_str}%/day). Most responsive to recent consumption — use this when today is heavier or lighter than usual.">
    <div class="value">{days_rem_24h_str}</div>
    <div class="label">At 24h rate</div>
  </div>
  <div class="card" data-tip="Days of battery left at your 7-day average discharge rate ({weekly:.1f}%/day). Smooths out single-day anomalies — good for typical planning.">
    <div class="value">{days_rem_7d_str}</div>
    <div class="label">At 7-day avg</div>
  </div>
  {remaining_ah_card}
</div>

<div class="group-label">Full Battery Would Last</div>
<div class="summary">
  <div class="card" data-tip="How many days a full charge would last at your last 24 hours of discharge rate ({rate_24h_str}%/day). Use this to plan generator runs on a heavy-use day.">
    <div class="value">{full_bat_24h_str}</div>
    <div class="label">At 24h rate</div>
  </div>
  <div class="card" data-tip="How many days a full charge would last at your 7-day average discharge rate ({weekly:.1f}%/day). Your baseline boondocking range from a full charge.">
    <div class="value">{full_bat_7d_str}</div>
    <div class="label">At 7-day avg</div>
  </div>
</div>

<div class="group-label">Charging</div>
<div class="summary">
  <div class="card green" data-tip="CC phase charge rate from your most recent generator or shore power session: SOC gain from session start to the CC&rarr;CV knee &divide; CC hours. Strips the CV tail so shore and generator sessions are comparable. Updates after every session. Falls back to config (charge_rate_pct_per_hour) until data is available.">
    <div class="value">{eff_rate:.1f}%/h</div>
    <div class="label">Charge rate</div>
  </div>
  <div class="card" data-tip="Generator run time needed to replace one average day of usage. Calculated as running avg %/day &divide; charge rate. This is your steady-state daily generator budget — it does not account for any existing SOC deficit.">
    <div class="value">{maint_hours:.1f}h</div>
    <div class="label">Daily maintenance</div>
  </div>
  <div class="card" data-tip="Generator run time needed right now to reach your target SOC. Calculated as (target &minus; current SOC) &divide; generator CC rate (last Generator-tagged session). Falls back to most-recent non-shore session rate if no generator sessions are tagged yet. Set target_soc_pct in config.ini to change the target.">
    <div class="value">{to_target_display}</div>
    <div class="label">{to_target_label}</div>
  </div>
  <div class="card" data-tip="Shore power time needed right now to reach 100%. Calculated as (100% &minus; current SOC) &divide; shore CC rate (last Shore-tagged session). Shows N/A until at least one session is tagged as Shore. Useful for planning how long to stay on hookup before boondocking.">
    <div class="value">{to_100_display}</div>
    <div class="label">{to_100_label}</div>
  </div>
</div>

{chart_html}

<h2>Discharge Sessions</h2>
<table>
  <thead>
    <tr>
      <th>Start</th><th>End</th>
      <th data-tip="State of charge at the start of the session">SOC Start</th>
      <th data-tip="State of charge at the end of the session">SOC End</th>
      <th data-tip="Total SOC percentage lost during this session">Drop</th>
      <th data-tip="Amp-hours consumed, derived from the BMV-712 consumed Ah counter">Ah</th>
      <th>Duration</th>
      <th data-tip="Discharge rate normalised to a full 24-hour day: SOC drop &divide; hours &times; 24">%/day</th>
      <th data-tip="Mean power draw in watts (voltage &times; |current|) across all readings in the session">Avg Power</th>
      <th>Note</th>
    </tr>
  </thead>
  <tbody>{discharge_rows}</tbody>
</table>

<h2>Charging Sessions</h2>
<table>
  <thead class="green">
    <tr>
      <th>Start</th><th>End</th>
      <th data-tip="State of charge at the start of the session">SOC Start</th>
      <th data-tip="State of charge at the end of the session">SOC End</th>
      <th data-tip="Total SOC percentage gained during this session">Gain</th>
      <th>Duration</th>
      <th data-tip="Full-session charge rate: total SOC gain &divide; total hours">%/hour</th>
      <th data-tip="Constant Current phase rate: how fast the battery charged from session start to the CC&rarr;CV knee. Your charger runs in two phases: CC (full power, fast charging) then CV (tapered current, slow top-off). The CC rate is the meaningful charger speed &mdash; the CV tail skews the full-session average downward. Shows &mdash; when no CC&rarr;CV transition was detected (session ended before the knee, or charger has no CV phase).">CC %/hr</th>
      <th data-tip="SOC at the CC&rarr;CV transition. Your charger switches from full-power CC to tapered CV at a specific SOC &mdash; typically 90&ndash;95% for LiFePO&#8324;. A knee consistently lower than your historical baseline can indicate a weak cell or BMS cutoff. Shows &mdash; when not detectable.">Knee SOC</th>
      <th data-tip="Mean charging current in amps across all readings in the session">Avg A</th>
      <th data-tip="Charging source: Shore, Generator, Driving (or combination)">Type</th>
      <th>Note</th>
    </tr>
  </thead>
  <tbody>
    {'<tr><td colspan="12" style="color:#999;font-style:italic">No charging sessions recorded yet.</td></tr>' if not charging_rows else charging_rows}
  </tbody>
</table>

<p class="note">
  Discharge sessions = current below threshold_amps in config.ini.
  Charging sessions = current above threshold_amps.
  Data gaps &gt; max_gap_hours split sessions in two.<br>
  %/day = (SOC drop) &divide; hours &times; 24 &nbsp;|&nbsp;
  {charge_rate_note}<br>
  Daily maintenance = running avg %/day &divide; charge rate &nbsp;|&nbsp;
  To reach target = (target &minus; current SOC) &divide; charge rate.
</p>

</body>
</html>"""

    output_path.write_text(html, encoding='utf-8')
    return output_path


# ---------------------------------------------------------------------------
# Session filtering
# ---------------------------------------------------------------------------

def filter_sessions(discharge_sessions, charging_sessions, min_pct=1.0):
    """Drop micro-sessions (float oscillation, BLE glitches) below min_pct SOC change."""
    def soc_change(session, is_discharge):
        if len(session) < 2:
            return 0.0
        if is_discharge:
            return (session[0]['soc'] or 0) - (session[-1]['soc'] or 0)
        else:
            return (session[-1]['soc'] or 0) - (session[0]['soc'] or 0)

    def _keep_charge(session):
        gain = soc_change(session, False)
        if gain >= min_pct:
            return True
        # Keep sessions that reached near-full (≥99% SOC) with any measurable gain.
        # Top-offs from ≥99% SOC have <1% gain by definition, but "reached essentially
        # full" is a meaningful event. Require gain > 0 to exclude flat float-mode noise.
        # BLE dropout can cut a session short before 100% is logged, so 99.0% not 99.5%.
        if gain <= 0:
            return False
        soc_end = (session[-1]['soc'] or 0) if session else 0
        return soc_end >= 99.0

    filtered_dis = [s for s in discharge_sessions if soc_change(s, True) >= min_pct]
    filtered_chg = [s for s in charging_sessions if _keep_charge(s)]
    return filtered_dis, filtered_chg


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='Generate Victron battery usage report')
    parser.add_argument('--days', type=int, default=None,
                        help='Limit report to last N days of data')
    parser.add_argument('--open', dest='open_browser', action='store_true', default=True)
    parser.add_argument('--no-open', dest='open_browser', action='store_false')
    args = parser.parse_args()

    cfg = load_config()
    db_path = cfg.get('logging', 'db_path', fallback='victron_data.db')
    charging_threshold = cfg.getfloat('charging', 'threshold_amps', fallback=2.0)
    max_gap_hours = cfg.getfloat('charging', 'max_gap_hours', fallback=4.0)
    fallback_rate = cfg.getfloat('report', 'charge_rate_pct_per_hour', fallback=6.0)
    target_soc = cfg.getfloat('report', 'target_soc_pct', fallback=95.0)
    time_format = cfg.get('report', 'time_format', fallback='12h').strip()
    date_format = cfg.get('report', 'date_format', fallback='%B %-d, %Y').strip()
    capacity_ah = cfg.getfloat('report', 'battery_capacity_ah', fallback=None)
    downsample_cfg = {
        'tier1_hours': cfg.getfloat('report', 'downsample_tier1_hours', fallback=6.0),
        'tier2_hours': cfg.getfloat('report', 'downsample_tier2_hours', fallback=24.0),
        'tier2_mins':  cfg.getfloat('report', 'downsample_tier2_mins',  fallback=5.0),
        'tier3_mins':  cfg.getfloat('report', 'downsample_tier3_mins',  fallback=15.0),
    }
    output_dir = Path(cfg.get('report', 'output_dir', fallback='reports'))
    output_dir.mkdir(exist_ok=True)

    since = None
    if args.days:
        since = datetime.now(timezone.utc) - timedelta(days=args.days)

    readings = load_readings(db_path, since=since)
    if not readings:
        print('No readings found. Run logger.py first to collect data.')
        return

    discharge_sessions = detect_sessions(readings, charging_threshold, max_gap_hours)
    charging_sessions  = detect_charging_sessions(readings, charging_threshold, max_gap_hours)
    min_session_pct    = cfg.getfloat('report', 'min_session_pct', fallback=0.5)
    discharge_sessions, charging_sessions = filter_sessions(
        discharge_sessions, charging_sessions, min_pct=min_session_pct
    )

    discharge_stats = [session_stats(s, time_format, date_format) for s in discharge_sessions if len(s) >= 2]
    charging_stats  = [charging_session_stats(s, time_format, date_format) for s in charging_sessions if len(s) >= 2]

    current_soc = readings[-1]['soc'] if readings else None
    charge_type_map = load_charge_types(db_path)
    note_map = load_session_notes(db_path)
    summary = compute_summary(discharge_sessions, discharge_stats, charging_stats,
                              fallback_rate, current_soc=current_soc, target_soc=target_soc,
                              capacity_ah=capacity_ah, charge_type_map=charge_type_map)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    output_path = output_dir / f'report_{ts}.html'
    generate_html(readings, discharge_sessions, charging_sessions,
                  discharge_stats, charging_stats, summary, output_path,
                  time_format=time_format, downsample_cfg=downsample_cfg,
                  charge_type_map=charge_type_map, note_map=note_map)

    def _fmt(d):
        if d is None: return 'N/A'
        return f'{d * 24:.0f}h' if d < 1 else f'{d:.1f}d'

    print(f'\nReport:       {output_path}')
    print(f'7-day avg:    {summary.get("weekly_avg_pct_per_day", 0):.1f} %/day'
          f'  →  {_fmt(summary.get("days_remaining_7d"))} remaining'
          f'  ({_fmt(summary.get("full_battery_7d"))} from full)')
    r24 = summary.get("last_24h_pct_per_day")
    print(f'24h rate:     {r24:.1f} %/day  →  {_fmt(summary.get("days_remaining_24h"))} remaining'
          f'  ({_fmt(summary.get("full_battery_24h"))} from full)'
          if r24 else f'24h rate:     no discharge in last 24h')
    print(f'Avg power:    {summary.get("avg_discharge_watts", 0):.0f} W')
    print(f'Charge rate:  {summary.get("effective_charge_rate", 0):.1f} %/hour'
          f' ({summary.get("charge_rate_source", "")})')
    print(f'Maintenance:  {summary.get("daily_maintenance_hours", 0):.1f} h/day')
    h2t = summary.get('hours_to_target')
    cur = summary.get('current_soc')
    tgt = summary.get('target_soc', 95)
    if h2t is not None and cur is not None:
        if cur >= tgt:
            print(f'To {tgt:.0f}%:       Already at target ({cur:.1f}%)')
        else:
            print(f'To {tgt:.0f}%:       {h2t:.1f}h (currently {cur:.1f}%)')

    if args.open_browser:
        webbrowser.open(output_path.resolve().as_uri())


if __name__ == '__main__':
    main()
