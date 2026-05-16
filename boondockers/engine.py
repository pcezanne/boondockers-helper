#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul Cezanne
"""
engine.py — Pure computation: session detection, statistics, diagnostics, chart helpers.

No I/O.  All functions are deterministic given their inputs.
"""

import hashlib
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone


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
# CC/CV knee detection
# ---------------------------------------------------------------------------

def _session_started_in_cv(session_readings, window=5,
                            cv_start_threshold=1.15, mid_slope_threshold=0.80):
    """Return True if the session had no CC plateau — charger was already in CV
    when logging began.

    Two conditions must both be true:
    1. The first few readings are significantly above the middle-50% median
       (start_avg > plateau * cv_start_threshold).  The plateau estimate is the
       same median-of-middle-50% used by _find_cc_cv_knee; if the session truly
       started in CV the plateau captures the midpoint of the taper rather than
       a stable CC level, making the initial current look elevated relative to it.
    2. The middle section of the session is still declining
       (second-half-of-middle avg < first-half-of-middle avg * mid_slope_threshold).
       A post-derating CC+CV session has a stable middle (post-derating CC phase),
       so condition 2 correctly excludes thermal-derating cases.
    """
    n = len(session_readings)
    if n < 15:
        return False

    currents = [(r.get('current') or 0) for r in session_readings]

    # Rolling mean (same as _find_cc_cv_knee)
    smoothed = []
    for i in range(n):
        start = max(0, i - window + 1)
        smoothed.append(sum(currents[start:i + 1]) / (i - start + 1))

    # Plateau: median of middle 50%
    mid_start, mid_end = n // 4, 3 * n // 4
    mid_vals_sorted = sorted(smoothed[mid_start:mid_end])
    if not mid_vals_sorted:
        return False
    plateau = mid_vals_sorted[len(mid_vals_sorted) // 2]
    if plateau <= 2.0:
        return False

    # Condition 1: first readings above the plateau?
    start_avg = sum(smoothed[:min(5, n)]) / min(5, n)
    if start_avg <= plateau * cv_start_threshold:
        return False

    # Condition 2: is the middle section still declining?
    mid_seq = smoothed[mid_start:mid_end]
    mid_n = len(mid_seq)
    if mid_n < 4:
        return False
    mid_first_avg = sum(mid_seq[:mid_n // 2]) / (mid_n // 2)
    mid_last_avg = sum(mid_seq[mid_n // 2:]) / max(1, mid_n - mid_n // 2)
    return mid_last_avg < mid_first_avg * mid_slope_threshold


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
    4. Plateau = median current in the settled CC window immediately after the
       inrush period (window_idx → 1.5×window_idx).  Using the second half of
       the session would contaminate the measurement with CV-phase taper, causing
       false positives on long complete charges.
    5. Flag if peak > 30 A and (peak − plateau) / peak ≥ drop_pct / 100.
    6. Suppress false positive: if _find_cc_cv_knee detects a CC→CV knee,
       the session completed normally.  Any early current drop was the charger
       settling to its sustained CC rate (inrush → nominal), not heat throttling.
       Sessions that abort before reaching the CV phase have no detectable knee
       and are still flagged.
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
    cc_window_end = min(n, window_idx + max(1, window_idx // 2))
    cc_phase = [r.get('current') or 0 for r in session_readings[window_idx:cc_window_end]]
    if not early or not cc_phase:
        return False, None, None

    peak = max(early)
    plateau = statistics.median(cc_phase)

    if peak <= 30:
        return False, None, None
    if peak <= 0:
        return False, None, None
    if (peak - plateau) / peak < drop_pct / 100:
        return False, None, None

    # Step 6: suppress false positive only when the current drop occurred WITHIN the
    # early window AND the session completed with a CC→CV knee.
    #
    # Physical logic:
    # - Drop within the window + completed session → charger settling from an initial
    #   boost to its rated CC current (inrush → nominal).  Not derating.
    # - Drop AFTER the window → the current had already been declining for more than
    #   30 min before crossing the threshold.  That sustained throttle is thermal
    #   derating, regardless of whether the session eventually completed normally.
    if _find_cc_cv_knee(session_readings) is not None:
        peak_idx_l = early.index(peak)
        drop_thr = peak * (1 - drop_pct / 100)
        drop_idx = next(
            (i for i in range(peak_idx_l, n)
             if (session_readings[i].get('current') or 0) < drop_thr),
            None,
        )
        if drop_idx is None or drop_idx <= window_idx:
            return False, None, None

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
    started_in_cv = _session_started_in_cv(session)
    if started_in_cv:
        # Charger was already in CV when logging began — no CC phase captured.
        # We cannot measure a meaningful CC rate; None signals "unknown" to
        # callers so they don't include this session in charge-rate trending.
        cc_rate = None
        knee_soc = None
        cv_detected = False
    else:
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
        'started_in_cv': started_in_cv,
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
                    capacity_ah=None, charge_type_map=None, shore_power_sids=None,
                    now=None):
    if now is None:
        now = datetime.now(timezone.utc)

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
        today_str = now.astimezone().strftime('%Y-%m-%d')
        # Exclude today (partial day) from averages — only complete calendar days count
        complete_rates = [(d, r, ah, h) for d, r, ah, h in daily_rates if d != today_str]

        # Hour-weighted averages: weight each slice by its discharge hours so a brief
        # 2-hour stop doesn't carry equal weight to a full boondocking day.
        def _hw_rate(slices):
            total_h = sum(h for _, _, _, h in slices)
            if not total_h:
                return 0
            return sum(r * h / 24 for _, r, _, h in slices) / total_h * 24

        def _hw_ah(slices):
            total_h = sum(h for _, _, _, h in slices)
            if not total_h:
                return 0
            return sum(ah for _, _, ah, _ in slices) / total_h * 24

        running_avg    = _hw_rate(complete_rates)
        avg_ah_per_day = _hw_ah(complete_rates)

        last7_slices  = complete_rates[-7:]
        weekly_avg    = _hw_rate(last7_slices)
        weekly_avg_ah = _hw_ah(last7_slices)

        # Implied watts from SOC-derived rate × capacity × avg voltage
        volt_list = [s['avg_voltage'] for s in summary_discharge_stats if s.get('avg_voltage')]
        avg_voltage_all = sum(volt_list) / len(volt_list) if volt_list else 12.0
        if capacity_ah and avg_voltage_all:
            avg_watts = (running_avg / 100) * capacity_ah * avg_voltage_all / 24
        else:
            watts_list = [s['avg_watts'] for s in summary_discharge_stats if s['avg_watts'] > 0]
            avg_watts = sum(watts_list) / len(watts_list) if watts_list else 0

        # Last-24h discharge rate — based on sessions that ended within the past 24 hours
        recent = [s for s in summary_discharge_stats if s['ts_end'] >= now - timedelta(hours=24)]
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
        substantial = [s for s in summary_charging_stats
                       if s['pct_gain'] >= _MIN_RATE_GAIN_PCT
                       and s.get('cc_rate_pct_per_hour') is not None]
        rate_sessions = substantial if substantial else [
            s for s in summary_charging_stats
            if s.get('cc_rate_pct_per_hour') is not None
        ]
        last_rate = rate_sessions[-1]['cc_rate_pct_per_hour'] if rate_sessions else None
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
    generator_stats  = [s for s in charging_stats if _charge_types_for(s) == {'Generator'}]
    shore_stats      = [s for s in charging_stats if _charge_types_for(s) == {'Shore'}]
    gen_with_rate    = [s for s in generator_stats if s.get('cc_rate_pct_per_hour') is not None]
    shore_with_rate  = [s for s in shore_stats     if s.get('cc_rate_pct_per_hour') is not None]
    generator_rate   = gen_with_rate[-1]['cc_rate_pct_per_hour']   if gen_with_rate   else None
    shore_rate       = shore_with_rate[-1]['cc_rate_pct_per_hour'] if shore_with_rate else None
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
        all_rates = [s['cc_rate_pct_per_hour'] for s in sessions
                     if s.get('cc_rate_pct_per_hour') is not None]
        if len(all_rates) < 4:
            continue
        all_time_avg = sum(all_rates) / len(all_rates)
        recent_3 = all_rates[-3:]
        recent_avg = sum(recent_3) / len(recent_3)
        if recent_avg < all_time_avg * (1 - rate_drop_pct / 100):
            flagged_sids = [_session_id_from_stat(s, 'charge') for s in sessions[-3:]
                            if s.get('cc_rate_pct_per_hour') is not None]
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


# ---------------------------------------------------------------------------
# ID helpers
# ---------------------------------------------------------------------------

def session_id(session_type, ts_start_iso):
    """Stable identifier: sha256 of type + start timestamp."""
    raw = f'{session_type}:{ts_start_iso}'
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _session_id_from_stat(stat, session_type):
    """Derive the stable session_id from a stats dict."""
    raw = f'{session_type}:{stat["ts_start"].isoformat()}'
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Algorithm helpers
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
    Returns: (timestamps_local, soc_values, downsampled_readings) as parallel lists.
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
# Chart helpers
# ---------------------------------------------------------------------------

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
