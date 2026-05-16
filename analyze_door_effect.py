#!/usr/bin/env python3
"""
Analyze whether opening the battery compartment door during shore charging
has a measurable effect on charging performance.

Groups Shore sessions by door state (from the Note field) and compares
CC charge rate, average amps, thermal derating, and knee SOC.

Usage (from src/):
    python3 analyze_door_effect.py
    python3 analyze_door_effect.py --db path/to/victron_data.db
    python3 analyze_door_effect.py --sessions 8   # look at last N shore sessions
"""

import argparse
import configparser
import sqlite3
import sys
from statistics import mean, stdev

sys.path.insert(0, '.')
from boondockers.engine import (
    charging_session_stats,
    detect_charging_sessions,
    filter_sessions,
    detect_sessions,
    session_id,
)
from boondockers.db import load_readings, load_notes, load_charge_types


def load_config(cfg_path='config.ini'):
    cfg = configparser.ConfigParser()
    cfg.read(cfg_path)
    return cfg


def classify_door(note):
    n = note.lower()
    if any(w in n for w in ('open', 'ajar', 'cracked')):
        return 'open'
    if any(w in n for w in ('closed', 'shut', 'close')):
        return 'closed'
    return 'unknown'


def fmt_val(v, fmt='.1f', unit=''):
    if v is None:
        return '—'
    return f'{v:{fmt}}{unit}'


def group_stats(sessions):
    cc_rates   = [s['cc_rate_pct_per_hour'] for s in sessions if s['cc_rate_pct_per_hour'] is not None]
    avg_amps_  = [s['avg_amps'] for s in sessions]
    knee_socs  = [s['knee_soc'] for s in sessions if s['knee_soc'] is not None]
    derating   = [s for s in sessions if s['is_derating']]
    return {
        'n': len(sessions),
        'cc_rate_mean': mean(cc_rates) if cc_rates else None,
        'cc_rate_sd': stdev(cc_rates) if len(cc_rates) > 1 else None,
        'avg_amps_mean': mean(avg_amps_) if avg_amps_ else None,
        'knee_soc_mean': mean(knee_socs) if knee_socs else None,
        'derating_count': len(derating),
        'derating_pct': 100 * len(derating) / len(sessions) if sessions else 0,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', default='victron_data.db')
    parser.add_argument('--sessions', type=int, default=6,
                        help='Number of most-recent Shore sessions to analyze')
    args = parser.parse_args()

    cfg = load_config()
    charging_threshold = cfg.getfloat('charging', 'threshold_amps', fallback=2.0)
    max_gap_hours      = cfg.getfloat('charging', 'max_gap_hours', fallback=4.0)
    min_session_pct    = cfg.getfloat('report', 'min_session_pct', fallback=1.0)

    print(f"Loading all readings from {args.db} ...")
    readings = load_readings(args.db)
    if not readings:
        print("No readings found.")
        return

    discharge_sessions = detect_sessions(readings, charging_threshold, max_gap_hours)
    charging_sessions  = detect_charging_sessions(readings, charging_threshold, max_gap_hours)
    discharge_sessions, charging_sessions = filter_sessions(
        discharge_sessions, charging_sessions, min_pct=min_session_pct
    )

    # Build stats + assign session_ids
    charging_stats = []
    for s in charging_sessions:
        if len(s) < 2:
            continue
        stat = charging_session_stats(s)
        stat['sid'] = session_id('charge', stat['ts_start'].isoformat())
        stat['readings'] = s
        charging_stats.append(stat)

    # Match with notes and filter to Shore sessions
    notes_map      = load_notes(args.db)          # {sid: {note, charge_type, ...}}
    charge_type_map = load_charge_types(args.db)  # {sid: charge_type_str}

    shore_stats = []
    for stat in charging_stats:
        sid = stat['sid']
        ct = charge_type_map.get(sid, '')
        if 'Shore' not in ct:
            continue
        note_entry = notes_map.get(sid, {})
        stat['note'] = note_entry.get('note', '')
        stat['charge_type'] = ct
        shore_stats.append(stat)

    if not shore_stats:
        print("No Shore sessions found. Make sure sessions are tagged as 'Shore' in the dashboard.")
        return

    # Take the last N
    shore_stats = shore_stats[-args.sessions:]

    # ── Per-session table ────────────────────────────────────────────────────
    print(f"\n{'='*90}")
    print(f"  Last {args.sessions} Shore charging sessions")
    print(f"{'='*90}")
    print(f"  {'Date':<12} {'SOC%':>10} {'Hrs':>5} {'CC%/hr':>7} {'AvgA':>6} {'KneeSOC':>8} {'Derate':>6}  Door  Note")
    print(f"  {'-'*85}")
    for s in shore_stats:
        door = classify_door(s['note'])
        door_tag = {'open': 'OPEN', 'closed': 'SHUT', 'unknown': '?'}[door]
        soc_range = f"{fmt_val(s['soc_start'], '.0f')}→{fmt_val(s['soc_end'], '.0f')}%"
        print(
            f"  {s['date']:<12} "
            f"{soc_range:>10} "
            f"{fmt_val(s['hours'], '.2f'):>5} "
            f"{fmt_val(s['cc_rate_pct_per_hour'], '.2f'):>7} "
            f"{fmt_val(s['avg_amps'], '.1f'):>6} "
            f"{fmt_val(s['knee_soc'], '.1f'):>8} "
            f"{'YES' if s['is_derating'] else 'no':>6}  "
            f"[{door_tag}]  {s['note']}"
        )

    # ── Classify and group ───────────────────────────────────────────────────
    open_sessions    = [s for s in shore_stats if classify_door(s['note']) == 'open']
    closed_sessions  = [s for s in shore_stats if classify_door(s['note']) == 'closed']
    unknown_sessions = [s for s in shore_stats if classify_door(s['note']) == 'unknown']

    if unknown_sessions:
        print(f"\n  Note: {len(unknown_sessions)} session(s) not classified (no 'open'/'closed' keyword in note):")
        for s in unknown_sessions:
            print(f"    {s['date']}  note='{s['note']}'")

    if not open_sessions or not closed_sessions:
        print("\n  Need sessions in both groups to compare.")
        print("  Tip: Add notes containing 'door open' or 'door closed' in the dashboard Notes column.")
        return

    gs_open   = group_stats(open_sessions)
    gs_closed = group_stats(closed_sessions)

    # ── Group comparison table ───────────────────────────────────────────────
    print(f"\n{'='*90}")
    print(f"  Comparison: Door OPEN ({gs_open['n']} sessions)  vs  Door CLOSED ({gs_closed['n']} sessions)")
    print(f"{'='*90}")
    print(f"  {'Metric':<34} {'Door OPEN':>12}   {'Door SHUT':>12}    Delta")
    print(f"  {'-'*80}")

    def cmp_row(label, o_val, c_val, unit='', fmt='.2f'):
        o_s = f'{o_val:{fmt}}{unit}' if o_val is not None else '—'
        c_s = f'{c_val:{fmt}}{unit}' if c_val is not None else '—'
        delta = ''
        if o_val is not None and c_val is not None and c_val != 0:
            diff = o_val - c_val
            pct  = 100 * diff / abs(c_val)
            sign = '+' if diff >= 0 else ''
            delta = f'{sign}{pct:.1f}%'
        print(f"  {label:<34} {o_s:>12}   {c_s:>12}   {delta}")

    cmp_row('CC charge rate (%/hr)',
            gs_open['cc_rate_mean'], gs_closed['cc_rate_mean'], unit='%/hr')
    if gs_open['cc_rate_sd'] is not None or gs_closed['cc_rate_sd'] is not None:
        cmp_row('  ± std dev',
                gs_open['cc_rate_sd'], gs_closed['cc_rate_sd'], unit='%/hr')
    cmp_row('Avg charge current (A)',
            gs_open['avg_amps_mean'], gs_closed['avg_amps_mean'], unit='A', fmt='.1f')
    cmp_row('Knee SOC (CC→CV, %)',
            gs_open['knee_soc_mean'], gs_closed['knee_soc_mean'], unit='%', fmt='.1f')
    print(
        f"  {'Thermal derating events':<34} "
        f"{gs_open['derating_count']}/{gs_open['n']} ({gs_open['derating_pct']:.0f}%)   "
        f"{gs_closed['derating_count']}/{gs_closed['n']} ({gs_closed['derating_pct']:.0f}%)"
    )

    # ── Interpretation ───────────────────────────────────────────────────────
    print(f"\n{'='*90}")
    print("  Interpretation")
    print(f"{'='*90}")

    cc_o, cc_c = gs_open['cc_rate_mean'], gs_closed['cc_rate_mean']
    if cc_o and cc_c:
        diff_pct = 100 * (cc_o - cc_c) / cc_c
        if abs(diff_pct) < 3:
            print("  CC charge rate: no meaningful difference (<3%). Door state is not a")
            print("  detectable factor at this sample size.")
        elif diff_pct > 0:
            print(f"  CC charge rate: door-open sessions are {diff_pct:.1f}% faster — consistent with")
            print("  better charger ventilation reducing thermal derating.")
        else:
            print(f"  CC charge rate: door-closed sessions are {-diff_pct:.1f}% faster — door state may not")
            print("  be the dominant variable (check SOC start levels, ambient temp).")

    d_o, d_c = gs_open['derating_pct'], gs_closed['derating_pct']
    if d_o == 0 and d_c > 0:
        print("  Thermal derating: only detected in door-closed sessions — supports the")
        print("  ventilation theory.")
    elif d_o > 0 and d_c == 0:
        print("  Thermal derating: only detected in door-open sessions — unexpected.")
        print("  Check for other confounders (hot ambient day, long session duration).")
    elif d_o == 0 and d_c == 0:
        print("  Thermal derating: not detected in either group.")
    else:
        print(f"  Thermal derating: {d_o:.0f}% of open sessions, {d_c:.0f}% of closed sessions.")

    print()
    print("  Confounders to watch for (can mask or amplify the door effect):")
    soc_starts_open   = [s['soc_start'] for s in open_sessions]
    soc_starts_closed = [s['soc_start'] for s in closed_sessions]
    avg_soc_o = mean(soc_starts_open)
    avg_soc_c = mean(soc_starts_closed)
    print(f"  • SOC at session start: open avg={avg_soc_o:.1f}%  closed avg={avg_soc_c:.1f}%", end='')
    if abs(avg_soc_o - avg_soc_c) > 5:
        print("  ← SIGNIFICANT DIFFERENCE — lower SOC = more CC headroom = faster apparent rate")
    else:
        print("  (comparable — not a major confounder)")
    print("  • Ambient temperature (hot days cause more derating regardless of door)")
    print("  • Battery acceptance near full (batteries taper sooner above ~90% SOC)")
    print()


if __name__ == '__main__':
    main()
