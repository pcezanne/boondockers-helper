#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul Cezanne
"""
app.py — Interactive Plotly Dash dashboard for Victron BMV-712 data.

Usage:
    python3 victron/app.py                              # all data, browser tab
    python3 victron/app.py --week                       # rolling last 7 days
    python3 victron/app.py --2weeks                     # rolling last 14 days
    python3 victron/app.py --days 30                    # rolling last N days
    python3 victron/app.py --start 2026-05-01           # from date to now
    python3 victron/app.py --start 2026-05-01 --end 2026-05-14  # fixed range
    python3 victron/app.py --native                     # native macOS window
    python3 victron/app.py --no-open                    # server only
    python3 -m victron.app                              # same as above
"""

import argparse
import hashlib
import sqlite3
import threading
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

import dash
from dash import Input, Output, State, dcc, html

from victron.report import (
    compute_diagnostics,
    compute_summary,
    detect_charging_sessions,
    detect_sessions,
    filter_sessions,
    charging_session_stats,
    generate_html,
    load_charge_types,
    load_config,
    load_readings,
    load_session_notes,
    session_stats,
)

CONFIG_PATH = Path(__file__).parent.parent / 'config.ini'
APP_PORT = 8050


# ---------------------------------------------------------------------------
# Session ID — deterministic, survives re-detection
# ---------------------------------------------------------------------------

def session_id(session_type, ts_start_iso):
    """Stable identifier: sha256 of type + start timestamp."""
    raw = f'{session_type}:{ts_start_iso}'
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Notes persistence
# ---------------------------------------------------------------------------

SESSION_NOTES_DDL = """
CREATE TABLE IF NOT EXISTS session_notes (
    session_id   TEXT PRIMARY KEY,
    session_type TEXT NOT NULL,
    note         TEXT DEFAULT '',
    charge_type  TEXT DEFAULT '',
    shore_power  INTEGER DEFAULT 0,
    updated_at   TEXT
);
"""


def _ensure_notes_table(conn):
    conn.execute(SESSION_NOTES_DDL)
    conn.commit()
    _migrate_notes_table(conn)
    _migrate_system_diagnostics_table(conn)


def _migrate_notes_table(conn):
    """Add columns introduced after the initial schema."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(session_notes)")}
    if 'charge_type' not in cols:
        conn.execute("ALTER TABLE session_notes ADD COLUMN charge_type TEXT DEFAULT ''")
    if 'shore_power' not in cols:
        conn.execute("ALTER TABLE session_notes ADD COLUMN shore_power INTEGER DEFAULT 0")
    if 'flags' not in cols:
        conn.execute("ALTER TABLE session_notes ADD COLUMN flags TEXT DEFAULT ''")
    conn.commit()


def _migrate_system_diagnostics_table(conn):
    """Create the system_diagnostics table if it does not exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS system_diagnostics (
            diag_id      TEXT PRIMARY KEY,
            diag_type    TEXT NOT NULL,
            detected_at  TEXT,
            period_start TEXT,
            period_end   TEXT,
            soc_drop     REAL,
            hours        REAL,
            details      TEXT
        )
    """)
    conn.commit()


def load_notes(db_path):
    """Return {session_id: {'note', 'charge_type', 'shore_power', 'flags'}} for all stored notes."""
    conn = sqlite3.connect(db_path)
    _ensure_notes_table(conn)
    rows = conn.execute(
        'SELECT session_id, note, charge_type, shore_power, flags FROM session_notes'
    ).fetchall()
    conn.close()
    return {
        r[0]: {
            'note': r[1] or '',
            'charge_type': r[2] or '',
            'shore_power': bool(r[3]),
            'flags': r[4] or '',
        }
        for r in rows
    }


def load_shore_power_sessions(db_path):
    """Return set of session_ids where shore_power = 1."""
    conn = sqlite3.connect(db_path)
    _ensure_notes_table(conn)
    rows = conn.execute(
        'SELECT session_id FROM session_notes WHERE shore_power = 1'
    ).fetchall()
    conn.close()
    return {r[0] for r in rows}


def load_diagnostics(db_path):
    """Load persisted diagnostic state from the DB (fast path before first Refresh).

    Returns {'session_flags': {sid: [flag_str, ...]}, 'system_diags': [row_dicts]}.
    """
    conn = sqlite3.connect(db_path)
    _ensure_notes_table(conn)
    flag_rows = conn.execute(
        "SELECT session_id, flags FROM session_notes WHERE flags IS NOT NULL AND flags != ''"
    ).fetchall()
    diag_rows = conn.execute(
        'SELECT diag_id, diag_type, period_start, period_end, soc_drop, hours FROM system_diagnostics'
    ).fetchall()
    conn.close()

    session_flags = {}
    for sid, flags_str in flag_rows:
        session_flags[sid] = [f.strip() for f in flags_str.split(',') if f.strip()]

    system_diags = [
        {'diag_id': r[0], 'diag_type': r[1], 'period_start': r[2],
         'period_end': r[3], 'soc_drop': r[4], 'hours': r[5]}
        for r in diag_rows
    ]

    return {'session_flags': session_flags, 'system_diags': system_diags}


def _save_session_flags(conn, sid, flags_str):
    """Update only the flags column for a session — does not touch note/charge_type/shore_power."""
    conn.execute(
        "INSERT OR IGNORE INTO session_notes (session_id, session_type, flags) VALUES (?, 'charge', '')",
        (sid,),
    )
    conn.execute(
        "UPDATE session_notes SET flags=?, updated_at=? WHERE session_id=?",
        (flags_str, datetime.now(timezone.utc).isoformat(), sid),
    )


def compute_and_save_diagnostics(db_path, all_data, cfg):
    """Run all diagnostic checks and persist results to the DB.

    Returns the compute_diagnostics result dict for immediate UI use.
    """
    cfg_diag = {
        'knee_soc_baseline_sessions':      cfg.getint('diagnostics', 'knee_soc_baseline_sessions', fallback=5),
        'knee_soc_drop_threshold_pct':     cfg.getfloat('diagnostics', 'knee_soc_drop_threshold_pct', fallback=10),
        'charge_rate_decline_threshold_pct': cfg.getfloat('diagnostics', 'charge_rate_decline_threshold_pct', fallback=20),
        'thermal_derating_drop_pct':       cfg.getfloat('diagnostics', 'thermal_derating_drop_pct', fallback=15),
        'thermal_derating_window_minutes': cfg.getfloat('diagnostics', 'thermal_derating_window_minutes', fallback=30),
        'parasitic_drain_threshold_pct':   cfg.getfloat('diagnostics', 'parasitic_drain_threshold_pct', fallback=2),
        'parasitic_drain_min_hours':       cfg.getfloat('diagnostics', 'parasitic_drain_min_hours', fallback=4),
    }

    results = compute_diagnostics(
        all_data['charging_stats'],
        all_data['discharge_stats'],
        all_data['readings'],
        all_data.get('charge_type_map'),
        cfg_diag,
    )

    # Build per-session flag sets
    sid_flags = {}
    for item in results['thermal_derating']:
        sid_flags.setdefault(item['sid'], set()).add('thermal_derating')
    for item in results['knee_soc']:
        sid_flags.setdefault(item['sid'], set()).add('knee_soc_low')
    for ctype_info in results['charge_rate_decline'].values():
        for sid in ctype_info['sids']:
            sid_flags.setdefault(sid, set()).add('charge_rate_declining')

    # Persist session flags
    conn = sqlite3.connect(db_path)
    _ensure_notes_table(conn)
    for sid, flags in sid_flags.items():
        _save_session_flags(conn, sid, ','.join(sorted(flags)))
    # Clear flags for sessions no longer flagged
    all_charge_sids = {
        session_id('charge', s['ts_start'].isoformat())
        for s in all_data['charging_stats']
    }
    for sid in all_charge_sids - set(sid_flags.keys()):
        _save_session_flags(conn, sid, '')

    # Persist parasitic drain alerts
    now_iso = datetime.now(timezone.utc).isoformat()
    for drain in results['parasitic_drain']:
        conn.execute(
            """INSERT OR REPLACE INTO system_diagnostics
               (diag_id, diag_type, detected_at, period_start, period_end, soc_drop, hours)
               VALUES (?, 'parasitic_drain', ?, ?, ?, ?, ?)""",
            (drain['diag_id'], now_iso,
             drain['period_start'], drain['period_end'],
             drain['soc_drop'], drain['hours']),
        )

    conn.commit()
    conn.close()
    return results


def save_note(db_path, sid, session_type, note, charge_type=None, shore_power=None):
    conn = sqlite3.connect(db_path)
    _ensure_notes_table(conn)
    # Build update dynamically based on which optional fields are provided
    if charge_type is None and shore_power is None:
        conn.execute(
            """INSERT INTO session_notes (session_id, session_type, note, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(session_id) DO UPDATE SET
                 note=excluded.note, updated_at=excluded.updated_at""",
            (sid, session_type, note, datetime.now(timezone.utc).isoformat()),
        )
    elif charge_type is not None and shore_power is None:
        conn.execute(
            """INSERT INTO session_notes (session_id, session_type, note, charge_type, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(session_id) DO UPDATE SET
                 note=excluded.note, charge_type=excluded.charge_type,
                 updated_at=excluded.updated_at""",
            (sid, session_type, note, charge_type, datetime.now(timezone.utc).isoformat()),
        )
    elif charge_type is None and shore_power is not None:
        conn.execute(
            """INSERT INTO session_notes (session_id, session_type, note, shore_power, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(session_id) DO UPDATE SET
                 note=excluded.note, shore_power=excluded.shore_power,
                 updated_at=excluded.updated_at""",
            (sid, session_type, note, int(shore_power), datetime.now(timezone.utc).isoformat()),
        )
    else:
        conn.execute(
            """INSERT INTO session_notes (session_id, session_type, note, charge_type, shore_power, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(session_id) DO UPDATE SET
                 note=excluded.note, charge_type=excluded.charge_type,
                 shore_power=excluded.shore_power, updated_at=excluded.updated_at""",
            (sid, session_type, note, charge_type, int(shore_power),
             datetime.now(timezone.utc).isoformat()),
        )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_all_data(cfg, time_window=None):
    """Load and process all readings.

    time_window (optional dict):
      since_delta  — timedelta for a rolling window (recomputed from now each call)
      since_fixed  — fixed start datetime (--start)
      until_fixed  — fixed end datetime (--end)
      window_days  — chart viewport width in days
    """
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

    since = until = None
    window_days = 3
    if time_window:
        window_days = time_window.get('window_days', 3)
        if time_window.get('since_delta'):
            since = datetime.now(timezone.utc) - time_window['since_delta']
        elif time_window.get('since_fixed'):
            since = time_window['since_fixed']
        until = time_window.get('until_fixed')

    readings = load_readings(db_path, since=since, until=until)
    if not readings:
        return None

    discharge_sessions = detect_sessions(readings, charging_threshold, max_gap_hours)
    charging_sessions = detect_charging_sessions(readings, charging_threshold, max_gap_hours)
    min_session_pct = cfg.getfloat('report', 'min_session_pct', fallback=0.5)
    discharge_sessions, charging_sessions = filter_sessions(
        discharge_sessions, charging_sessions, min_pct=min_session_pct
    )

    discharge_stats = [session_stats(s, time_format, date_format) for s in discharge_sessions if len(s) >= 2]
    charging_stats = [charging_session_stats(s, time_format, date_format) for s in charging_sessions if len(s) >= 2]

    current_soc = readings[-1]['soc'] if readings else None
    charge_type_map = load_charge_types(db_path)
    shore_power_sids = load_shore_power_sessions(db_path)
    summary = compute_summary(
        discharge_sessions, discharge_stats, charging_stats,
        fallback_rate, current_soc=current_soc,
        target_soc=target_soc, capacity_ah=capacity_ah,
        charge_type_map=charge_type_map,
        shore_power_sids=shore_power_sids or None,
    )

    return dict(
        readings=readings,
        discharge_sessions=discharge_sessions,
        charging_sessions=charging_sessions,
        discharge_stats=discharge_stats,
        charging_stats=charging_stats,
        summary=summary,
        db_path=db_path,
        time_format=time_format,
        date_format=date_format,
        downsample_cfg=downsample_cfg,
        capacity_ah=capacity_ah,
        target_soc=target_soc,
        charge_type_map=charge_type_map,
        shore_power_sids=shore_power_sids,
        window_days=window_days,
    )


# ---------------------------------------------------------------------------
# Layout helpers
# ---------------------------------------------------------------------------

_DIAG_COLORS = {
    'thermal':    '#c0392b',
    'knee_soc':   '#f39c12',
    'charge_rate': '#e67e22',
    'parasitic':  '#2980b9',
}

_CARD_STYLE_BASE = {
    'padding': '12px 16px',
    'marginBottom': '10px',
    'borderRadius': '6px',
    'background': '#fafafa',
    'fontSize': '0.88em',
    'lineHeight': '1.5',
}


def _diag_card(color, title, lines, detail_lines=None):
    style = dict(_CARD_STYLE_BASE, borderLeft=f'4px solid {color}')
    children = [html.Strong(title, style={'color': color})]
    for line in lines:
        children += [html.Br(), html.Span(line)]
    if detail_lines:
        n = len(detail_lines)
        older_children = []
        for line in detail_lines:
            older_children += [html.Br(), html.Span(line)]
        children.append(html.Details([
            html.Summary(
                f'\u25b6 {n} earlier event{"s" if n > 1 else ""}',
                style={'cursor': 'pointer', 'color': '#888',
                       'fontSize': '0.9em', 'marginTop': '6px'},
            ),
            html.Div(older_children),
        ]))
    return html.Div(children, style=style)


def _diagnostics_panel(diagnostics):
    """Render the diagnostics panel from a compute_diagnostics result dict or
    a load_diagnostics dict.  Returns an empty div when nothing is flagged."""
    if not diagnostics:
        return html.Div()

    # Accept either compute_diagnostics output or load_diagnostics output
    thermal   = diagnostics.get('thermal_derating', [])
    knee_list = diagnostics.get('knee_soc', [])
    rate_map  = diagnostics.get('charge_rate_decline', {})
    drain     = diagnostics.get('parasitic_drain', [])
    # Also handle system_diags format from load_diagnostics
    if not drain:
        drain = [d for d in diagnostics.get('system_diags', [])
                 if d.get('diag_type') == 'parasitic_drain']

    def _split(all_lines, keep=5):
        """Return (visible_lines, older_lines) keeping the first `keep` items visible."""
        if len(all_lines) <= keep:
            return all_lines, []
        return all_lines[:keep], all_lines[keep:]

    cards = []

    if thermal:
        n = len(thermal)
        session_lines = [f"  {t['date']}: {t['peak']}A peak \u2192 {t['plateau']}A plateau"
                         for t in reversed(thermal)]
        visible, older = _split(session_lines)
        cards.append(_diag_card(
            _DIAG_COLORS['thermal'],
            f'Thermal Derating \u2014 {n} session{"s" if n > 1 else ""}',
            ['Charger output dropped significantly within the first 30 minutes.',
             'Check charger bay ventilation and airflow path.'] + visible,
            detail_lines=older or None,
        ))

    if knee_list:
        item = knee_list[-1]  # most recent flagged session
        cards.append(_diag_card(
            _DIAG_COLORS['knee_soc'],
            'CC\u2192CV Knee SOC Drift',
            [f"Latest session: knee at {item['knee_soc']:.1f}% "
             f"vs baseline median {item['baseline_median']}% "
             f"(\u0394 {item['delta']} pts).",
             'Possible BMS protection or thermal limiting at lower SOC than normal.'],
        ))

    for ctype, info in rate_map.items():
        cards.append(_diag_card(
            _DIAG_COLORS['charge_rate'],
            f'Charge Rate Declining \u2014 {ctype}',
            [f"Recent 3-session CC avg: {info['recent_avg']:.2f}%/hr  "
             f"vs all-time avg: {info['all_time_avg']:.2f}%/hr "
             f"({info['decline_pct']:.0f}% decline).",
             'Check battery connections, charger output, and source health.'],
        ))

    if drain:
        n = len(drain)
        all_drain_lines = []
        for d in reversed(drain):
            try:
                ps = d['period_start'][:16].replace('T', ' ')
                pe = d['period_end'][:16].replace('T', ' ')
            except Exception:
                ps = pe = '\u2014'
            all_drain_lines.append(
                f"  {ps} \u2013 {pe}: {d['soc_drop']:.1f}% SOC lost over {d['hours']:.1f}h"
            )
        visible, older = _split(all_drain_lines)
        cards.append(_diag_card(
            _DIAG_COLORS['parasitic'],
            f'Possible Parasitic Drain \u2014 {n} event{"s" if n > 1 else ""}',
            ['SOC dropped during idle periods with no active session.',
             'Check for loads running while the battery was otherwise idle.'] + visible,
            detail_lines=older or None,
        ))

    _panel_style = {'marginTop': '28px', 'marginBottom': '4px',
                    'padding': '14px 18px',
                    'border': '1px solid #e0e0e0',
                    'borderRadius': '8px',
                    'background': '#fff'}
    _header = html.H3('Diagnostics', style={'color': '#555', 'marginBottom': '10px',
                                            'fontSize': '1em', 'fontWeight': '600',
                                            'textTransform': 'uppercase',
                                            'letterSpacing': '0.05em'})

    if not cards:
        return html.Div([
            _header,
            html.P('No anomalies detected. Press Refresh to run a fresh check.',
                   style={'color': '#999', 'fontSize': '0.88em', 'margin': '0'}),
        ], style=_panel_style)

    return html.Div([_header, html.Div(cards)], style=_panel_style)


def _discharge_table(discharge_stats, notes, system_diags=None, show_all=False):
    if not discharge_stats:
        return html.P('No discharge sessions recorded yet.', style={'color': '#999'})

    system_diags = system_diags or []
    if not show_all:
        discharge_stats = discharge_stats[-10:]
    discharge_stats = list(reversed(discharge_stats))
    # Build set of drain period_end timestamps (ISO prefix) for proximity check
    drain_ends = []
    for d in system_diags:
        if d.get('diag_type') == 'parasitic_drain' and d.get('period_end'):
            try:
                from victron.report import parse_ts as _parse_ts
                drain_ends.append(_parse_ts(d['period_end']))
            except Exception:
                pass

    header = html.Thead(html.Tr([
        html.Th('', style={'width': '24px'}),
        html.Th('Start'), html.Th('End'),
        html.Th('SOC Start'), html.Th('SOC End'),
        html.Th('Drop'), html.Th('Ah'), html.Th('Duration'),
        html.Th('%/day'), html.Th('Avg Power'),
        html.Th('Shore Power', title='Check to exclude this session from boondocking stats'),
        html.Th('Note'),
    ], style={'background': '#1a5276', 'color': 'white'}))

    rows = []
    for s in discharge_stats:
        sid = session_id('discharge', s['ts_start'].isoformat())
        color = '#c0392b' if s['pct_per_day'] > 50 else '#1a5276'
        ah_cell = f'{s["consumed_ah"]:.1f}' if s.get('consumed_ah') is not None else '—'
        is_shore = notes.get(sid, {}).get('shore_power', False)

        # Check if a parasitic drain ended within 30 min before this session
        warn_drain = False
        for drain_end_ts in drain_ends:
            delta = (s['ts_start'] - drain_end_ts).total_seconds()
            if 0 <= delta <= 1800:
                warn_drain = True
                break

        warn_cell = html.Td(
            html.Span('\u26a0',
                      **{'data-tip': 'Possible parasitic drain detected immediately before this session'},
                      style={'color': _DIAG_COLORS['parasitic'], 'fontSize': '1em'})
            if warn_drain else '',
            style={'textAlign': 'center', 'padding': '4px 6px'},
        )

        rows.append(html.Tr([
            warn_cell,
            html.Td(s['start']), html.Td(s['end']),
            html.Td(f'{s["soc_start"]:.1f}%'), html.Td(f'{s["soc_end"]:.1f}%'),
            html.Td(f'{s["pct_drop"]:.1f}%'), html.Td(ah_cell),
            html.Td(f'{s["hours"]:.1f}h'),
            html.Td(f'{s["pct_per_day"]:.1f}', style={'color': color, 'fontWeight': 'bold'}),
            html.Td(f'{s["avg_watts"]:.0f}W'),
            html.Td(
                dcc.Checklist(
                    id={'type': 'shore-power', 'session': sid},
                    options=[{'label': '', 'value': 'shore'}],
                    value=['shore'] if is_shore else [],
                    style={'display': 'flex', 'justifyContent': 'center'},
                ),
                style={'textAlign': 'center', 'verticalAlign': 'middle'},
            ),
            html.Td(dcc.Textarea(
                id={'type': 'note', 'session': sid, 'stype': 'discharge'},
                value=notes.get(sid, {}).get('note', ''),
                style={'width': '100%', 'minWidth': '180px', 'fontSize': '0.82em',
                       'resize': 'vertical', 'border': '1px solid #ddd', 'borderRadius': '4px',
                       'padding': '4px'},
            )),
        ]))

    return html.Table([header, html.Tbody(rows)],
                      style={'borderCollapse': 'collapse', 'width': '100%', 'fontSize': '0.88em'})


def _charging_table(charging_stats, notes, show_all=False):
    if not charging_stats:
        return html.P('No charging sessions recorded yet.', style={'color': '#999'})
    if not show_all:
        charging_stats = charging_stats[-10:]
    charging_stats = list(reversed(charging_stats))

    _charge_type_options = [
        {'label': 'Shore',     'value': 'Shore'},
        {'label': 'Generator', 'value': 'Generator'},
        {'label': 'Driving',   'value': 'Driving'},
    ]

    header = html.Thead(html.Tr([
        html.Th('', style={'width': '24px'}),
        html.Th('Start'), html.Th('End'),
        html.Th('SOC Start'), html.Th('SOC End'),
        html.Th('Gain'), html.Th('Duration'),
        html.Th('%/hour',
                **{'data-tip': 'Full-session charge rate: total SOC gain \u00f7 total hours'}),
        html.Th('CC %/hr',
                **{'data-tip': (
                    'Constant Current phase rate: how fast the battery charged from session '
                    'start to the CC\u2192CV knee. Your charger runs in two phases: CC (full '
                    'power, fast charging) then CV (tapered current, slow top-off). The CC '
                    'rate is the meaningful charger speed \u2014 the CV tail skews the '
                    'full-session average downward. Shows \u2014 when no CC\u2192CV transition '
                    'was detected (session ended before the knee, or charger has no CV phase).'
                )}),
        html.Th('Knee SOC',
                **{'data-tip': (
                    'SOC at the CC\u2192CV transition. Your charger switches from full-power '
                    'CC to tapered CV at a specific SOC \u2014 typically 90\u201395% for '
                    'LiFePO\u2084. A knee consistently lower than your historical baseline '
                    'can indicate a weak cell or BMS cutoff. Shows \u2014 when not detectable.'
                )}),
        html.Th('Avg A'),
        html.Th('Type'),
        html.Th('Note'),
    ], style={'background': '#1a7a4a', 'color': 'white'}))

    _FLAG_LABELS = {
        'thermal_derating': (
            'Thermal derating: charger current dropped >15% from its peak within '
            'the first 30 min \u2014 possible heat throttling. Check charger bay ventilation.'
        ),
        'knee_soc_low': (
            'Knee SOC drift: CC\u2192CV transition occurred significantly lower than '
            'your historical baseline \u2014 possible weak cell or BMS cutoff.'
        ),
        'charge_rate_declining': (
            'Charge rate decline: recent sessions are charging significantly '
            'slower than your historical average \u2014 check connections and charger health.'
        ),
    }

    rows = []
    for s in charging_stats:
        sid = session_id('charge', s['ts_start'].isoformat())
        stored_type = notes.get(sid, {}).get('charge_type', '')
        type_value = [t.strip() for t in stored_type.split(',') if t.strip()] if stored_type else []

        flag_str = notes.get(sid, {}).get('flags', '')
        flag_list = [f.strip() for f in flag_str.split(',') if f.strip()]
        flag_tip = '; '.join(_FLAG_LABELS.get(f, f) for f in flag_list) if flag_list else ''
        warn_cell = html.Td(
            html.Span('\u26a0', **({'data-tip': flag_tip} if flag_list else {}),
                      style={'color': '#e67e22', 'fontSize': '1em'})
            if flag_list else '',
            style={'textAlign': 'center', 'padding': '4px 6px'},
        )

        rows.append(html.Tr([
            warn_cell,
            html.Td(s['start']), html.Td(s['end']),
            html.Td(f'{s["soc_start"]:.1f}%'), html.Td(f'{s["soc_end"]:.1f}%'),
            html.Td(f'+{s["pct_gain"]:.1f}%'), html.Td(f'{s["hours"]:.1f}h'),
            html.Td(f'{s["charge_rate_pct_per_hour"]:.1f}'),
            html.Td(f'{s["cc_rate_pct_per_hour"]:.1f}' if s.get('cv_detected') else '—',
                    style={'color': '#1a7a4a', 'fontWeight': 'bold'}
                    if s.get('cv_detected') else {}),
            html.Td(f'{s["knee_soc"]:.1f}%' if s.get('knee_soc') is not None else '—'),
            html.Td(f'{s["avg_amps"]:.1f}'),
            html.Td(dcc.Dropdown(
                id={'type': 'charge-type', 'session': sid},
                options=_charge_type_options,
                value=type_value,
                multi=True,
                placeholder='Select type…',
                clearable=True,
                style={'minWidth': '180px', 'fontSize': '0.82em'},
            ), style={'minWidth': '190px', 'verticalAlign': 'middle'}),
            html.Td(dcc.Textarea(
                id={'type': 'note', 'session': sid, 'stype': 'charge'},
                value=notes.get(sid, {}).get('note', ''),
                style={'width': '100%', 'minWidth': '180px', 'fontSize': '0.82em',
                       'resize': 'vertical', 'border': '1px solid #ddd', 'borderRadius': '4px',
                       'padding': '4px'},
            )),
        ]))

    return html.Table([header, html.Tbody(rows)],
                      style={'borderCollapse': 'collapse', 'width': '100%', 'fontSize': '0.88em'})


def _summary_cards(summary):
    def fmt_days(d):
        if d is None:
            return 'N/A'
        return f'{d * 24:.0f}h' if d < 1 else f'{d:.1f}d'

    running = summary.get('running_avg_pct_per_day', 0)
    weekly = summary.get('weekly_avg_pct_per_day', 0)
    watts = summary.get('avg_discharge_watts', 0)
    avg_ah = summary.get('avg_ah_per_day', 0)
    weekly_ah = summary.get('weekly_avg_ah_per_day', 0)
    eff_rate = summary.get('effective_charge_rate', 0)
    maint_hours = summary.get('daily_maintenance_hours', 0)
    remaining_ah = summary.get('remaining_ah')
    capacity_ah = summary.get('capacity_ah')
    cur_soc = summary.get('current_soc')
    tgt_soc = summary.get('target_soc', 95.0)
    hours_to_tgt = summary.get('hours_to_target')
    hours_to_100 = summary.get('hours_to_100')

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

    card_style = {
        'background': '#f4f6f9', 'borderRadius': '10px', 'padding': '16px 24px',
        'minWidth': '140px', 'textAlign': 'center', 'border': '1px solid #e0e4ea',
    }
    val_style = {'fontSize': '2em', 'fontWeight': '700', 'color': '#1a5276'}
    val_green = {'fontSize': '2em', 'fontWeight': '700', 'color': '#1a7a4a'}
    lbl_style = {'fontSize': '0.82em', 'color': '#666', 'marginTop': '4px'}
    sub_style = {'fontSize': '0.78em', 'color': '#999', 'marginTop': '2px'}
    grp_style = {'fontSize': '0.75em', 'fontWeight': '600', 'textTransform': 'uppercase',
                 'letterSpacing': '0.08em', 'color': '#888', 'margin': '20px 0 6px 2px'}
    row_style = {'display': 'flex', 'gap': '16px', 'flexWrap': 'wrap', 'margin': '0 0 8px 0'}

    def card(val, label, sub=None, green=False):
        children = [html.Div(val, style=val_green if green else val_style),
                    html.Div(label, style=lbl_style)]
        if sub:
            children.append(html.Div(sub, style=sub_style))
        return html.Div(children, style=card_style)

    remaining_ah_card = (
        card(f'{remaining_ah:.0f} Ah', 'Remaining in bank')
        if remaining_ah is not None else html.Div()
    )

    return html.Div([
        html.Div('Usage', style=grp_style),
        html.Div([
            card(f'{weekly:.1f}%', '7-day avg / day', f'{weekly_ah:.1f} Ah/day'),
            card(f'{running:.1f}%', 'All days avg / day', f'{avg_ah:.1f} Ah/day'),
            card(f'{watts:.0f}W', 'Avg discharge power'),
        ], style=row_style),

        html.Div('Power Remaining', style=grp_style),
        html.Div([
            card(fmt_days(summary.get('days_remaining_24h')), 'At 24h rate'),
            card(fmt_days(summary.get('days_remaining_7d')), 'At 7-day avg'),
            remaining_ah_card,
        ], style=row_style),

        html.Div('Full Battery Would Last', style=grp_style),
        html.Div([
            card(fmt_days(summary.get('full_battery_24h')), 'At 24h rate'),
            card(fmt_days(summary.get('full_battery_7d')), 'At 7-day avg'),
        ], style=row_style),

        html.Div('Charging', style=grp_style),
        html.Div([
            card(f'{eff_rate:.1f}%/h', 'Charge rate', green=True),
            card(f'{maint_hours:.1f}h', 'Daily maintenance'),
            card(to_target_display, to_target_label, green=True),
            card(to_100_display, to_100_label, green=True),
        ], style=row_style),
    ])


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

def build_app(cfg, time_window=None):
    from victron.report import build_figure

    app = dash.Dash(__name__, suppress_callback_exceptions=True)
    app.title = 'Victron Dashboard'

    data = load_all_data(cfg, time_window)
    db_path = cfg.get('logging', 'db_path', fallback='victron_data.db')

    def make_layout(data):
        if data is None:
            return html.Div([
                html.H1('Battery Dashboard', style={'color': '#1a5276'}),
                html.P('No readings found. Run victron/logger.py first to collect data.',
                       style={'color': '#999'}),
            ], style={'maxWidth': '1100px', 'margin': '40px auto', 'padding': '0 20px'})

        # Run full diagnostics first so that notes loaded below reflect fresh flags.
        # (load_notes reads session_notes.flags, which compute_and_save_diagnostics writes.)
        initial_diags = compute_and_save_diagnostics(data['db_path'], data, cfg)

        notes = load_notes(data['db_path'])
        note_map = {sid: d['note'] for sid, d in notes.items() if d.get('note')}
        fig = build_figure(
            data['readings'], data['discharge_sessions'], data['charging_sessions'],
            data['discharge_stats'], data['charging_stats'], data['summary'],
            time_format=data['time_format'], downsample_cfg=data['downsample_cfg'],
            charge_type_map=data.get('charge_type_map'),
            note_map=note_map or None,
            window_days=data.get('window_days', 3),
        )

        generated = datetime.now().strftime('%Y-%m-%d %H:%M')
        n_readings = len(data['readings'])
        n_dis = len(data['discharge_stats'])
        n_chg = len(data['charging_stats'])
        system_diags = initial_diags.get('parasitic_drain', [])

        return html.Div([
            html.H1('Battery Dashboard', style={'color': '#1a5276', 'marginBottom': '4px'}),
            html.P(
                f'Updated {generated} · {n_readings:,} readings · '
                f'{n_dis} discharge sessions · {n_chg} charging sessions',
                style={'color': '#888', 'fontSize': '0.9em', 'marginBottom': '16px'},
            ),
            html.Div([
                html.Button('Refresh', id='refresh-btn', n_clicks=0,
                            style={'marginRight': '12px', 'padding': '8px 20px',
                                   'background': '#1a5276', 'color': 'white',
                                   'border': 'none', 'borderRadius': '6px',
                                   'cursor': 'pointer', 'fontSize': '0.9em'}),
                html.Button('Download Report', id='download-btn', n_clicks=0,
                            style={'padding': '8px 20px',
                                   'background': '#1a7a4a', 'color': 'white',
                                   'border': 'none', 'borderRadius': '6px',
                                   'cursor': 'pointer', 'fontSize': '0.9em'}),
                dcc.Download(id='download-html'),
            ], style={'marginBottom': '24px'}),

            html.Div(id='summary-cards', children=_summary_cards(data['summary'])),

            dcc.Graph(figure=fig, id='main-chart',
                      style={'marginTop': '24px'},
                      config={'displayModeBar': True}),

            html.Div(id='diagnostics-panel',
                     children=_diagnostics_panel(initial_diags)),

            html.Div([
                html.H2('Discharge Sessions',
                        style={'color': '#1a5276', 'marginTop': '40px', 'display': 'inline-block', 'marginRight': '16px'}),
                dcc.Checklist(
                    id='show-all-discharge',
                    options=[{'label': ' Show all', 'value': 'show_all'}],
                    value=[],
                    style={'display': 'inline-block', 'verticalAlign': 'middle', 'fontSize': '0.9em'},
                ),
            ]),
            html.Div(id='discharge-table',
                     children=_discharge_table(data['discharge_stats'], notes, system_diags)),

            html.Div([
                html.H2('Charging Sessions',
                        style={'color': '#1a5276', 'marginTop': '40px', 'display': 'inline-block', 'marginRight': '16px'}),
                dcc.Checklist(
                    id='show-all-charging',
                    options=[{'label': ' Show all', 'value': 'show_all'}],
                    value=[],
                    style={'display': 'inline-block', 'verticalAlign': 'middle', 'fontSize': '0.9em'},
                ),
            ]),
            html.Div(id='charging-table',
                     children=_charging_table(data['charging_stats'], notes)),

            html.P(
                'Discharge sessions = current below threshold_amps. '
                'Charging sessions = current above threshold_amps. '
                'Data gaps > max_gap_hours split sessions in two.',
                style={'color': '#999', 'fontSize': '0.8em', 'marginTop': '32px'},
            ),

            # Hidden store carries current data state between callbacks
            dcc.Store(id='data-store', data={'loaded': True}),
        ], style={'maxWidth': '1100px', 'margin': '40px auto', 'padding': '0 20px',
                  'fontFamily': "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, sans-serif"})

    app.layout = make_layout(data)

    # --- Refresh callback ---
    @app.callback(
        Output('main-chart', 'figure'),
        Output('summary-cards', 'children'),
        Output('discharge-table', 'children'),
        Output('charging-table', 'children'),
        Output('diagnostics-panel', 'children'),
        Input('refresh-btn', 'n_clicks'),
        Input('show-all-discharge', 'value'),
        Input('show-all-charging', 'value'),
        prevent_initial_call=True,
    )
    def refresh_data(_n, show_all_discharge_val, show_all_charging_val):
        show_all_discharge = 'show_all' in (show_all_discharge_val or [])
        show_all_charging = 'show_all' in (show_all_charging_val or [])
        fresh = load_all_data(cfg, time_window)
        if fresh is None:
            empty = html.P('No data.', style={'color': '#999'})
            import plotly.graph_objects as go
            return go.Figure(), empty, empty, empty, html.Div()
        diagnostics = compute_and_save_diagnostics(fresh['db_path'], fresh, cfg)
        notes = load_notes(fresh['db_path'])
        note_map = {sid: d['note'] for sid, d in notes.items() if d.get('note')}
        fig = build_figure(
            fresh['readings'], fresh['discharge_sessions'], fresh['charging_sessions'],
            fresh['discharge_stats'], fresh['charging_stats'], fresh['summary'],
            time_format=fresh['time_format'], downsample_cfg=fresh['downsample_cfg'],
            charge_type_map=fresh.get('charge_type_map'),
            note_map=note_map or None,
            window_days=fresh.get('window_days', 3),
        )
        system_diags = diagnostics.get('parasitic_drain', [])
        return (
            fig,
            _summary_cards(fresh['summary']),
            _discharge_table(fresh['discharge_stats'], notes, system_diags, show_all=show_all_discharge),
            _charging_table(fresh['charging_stats'], notes, show_all=show_all_charging),
            _diagnostics_panel(diagnostics),
        )

    # --- Note save callback (fires when any textarea loses focus) ---
    # Saves the note then quickly rebuilds only the chart (skips diagnostics and
    # table rebuild so it's much faster than a full Refresh).
    @app.callback(
        Output('data-store', 'data'),
        Output('main-chart', 'figure', allow_duplicate=True),
        Input({'type': 'note', 'session': dash.ALL, 'stype': dash.ALL}, 'n_blur'),
        State({'type': 'note', 'session': dash.ALL, 'stype': dash.ALL}, 'value'),
        State({'type': 'note', 'session': dash.ALL, 'stype': dash.ALL}, 'id'),
        prevent_initial_call=True,
    )
    def save_notes(n_blurs, values, ids):
        ctx = dash.callback_context
        if not ctx.triggered:
            return dash.no_update, dash.no_update
        any_saved = False
        for n_blur, val, ident in zip(n_blurs, values, ids):
            if n_blur and val is not None:
                save_note(db_path, ident['session'], ident['stype'], val)
                any_saved = True
        if not any_saved:
            return dash.no_update, dash.no_update
        fresh = load_all_data(cfg, time_window)
        if fresh is None:
            return {'loaded': True}, dash.no_update
        notes = load_notes(fresh['db_path'])
        note_map = {sid: d['note'] for sid, d in notes.items() if d.get('note')}
        fig = build_figure(
            fresh['readings'], fresh['discharge_sessions'], fresh['charging_sessions'],
            fresh['discharge_stats'], fresh['charging_stats'], fresh['summary'],
            time_format=fresh['time_format'], downsample_cfg=fresh['downsample_cfg'],
            charge_type_map=fresh.get('charge_type_map'),
            note_map=note_map or None,
            window_days=fresh.get('window_days', 3),
        )
        return {'loaded': True}, fig

    # --- Charge type constraint: Driving and Shore are mutually exclusive ---
    @app.callback(
        Output({'type': 'charge-type', 'session': dash.MATCH}, 'value'),
        Input({'type': 'charge-type', 'session': dash.MATCH}, 'value'),
        prevent_initial_call=True,
    )
    def enforce_charge_type_constraints(value):
        if not value or len(value) <= 1:
            return dash.no_update
        if 'Shore' in value and 'Driving' in value:
            # Driving wins — you can't be plugged into shore while moving
            return [v for v in value if v != 'Shore']
        return dash.no_update

    # --- Charge type save callback (fires when any type dropdown changes) ---
    @app.callback(
        Output('data-store', 'data', allow_duplicate=True),
        Input({'type': 'charge-type', 'session': dash.ALL}, 'value'),
        State({'type': 'charge-type', 'session': dash.ALL}, 'id'),
        prevent_initial_call=True,
    )
    def save_charge_types(values, ids):
        ctx = dash.callback_context
        if not ctx.triggered:
            return dash.no_update
        existing = load_notes(db_path)
        for val, ident in zip(values, ids):
            if val is None:
                continue
            sid = ident['session']
            # Strip any Shore+Driving conflict before persisting
            clean = [v for v in val if v]
            if 'Shore' in clean and 'Driving' in clean:
                clean = [v for v in clean if v != 'Shore']
            charge_type_str = ','.join(clean)
            note = existing.get(sid, {}).get('note', '')
            save_note(db_path, sid, 'charge', note, charge_type=charge_type_str)
        return {'loaded': True}

    # --- Shore Power save callback (fires when any shore-power checkbox changes) ---
    @app.callback(
        Output('main-chart', 'figure', allow_duplicate=True),
        Output('summary-cards', 'children', allow_duplicate=True),
        Input({'type': 'shore-power', 'session': dash.ALL}, 'value'),
        State({'type': 'shore-power', 'session': dash.ALL}, 'id'),
        prevent_initial_call=True,
    )
    def save_shore_power(values, ids):
        ctx = dash.callback_context
        if not ctx.triggered:
            return dash.no_update, dash.no_update
        existing = load_notes(db_path)
        for val, ident in zip(values, ids):
            if val is None:
                continue
            sid = ident['session']
            is_shore = 'shore' in (val or [])
            note = existing.get(sid, {}).get('note', '')
            save_note(db_path, sid, 'discharge', note, shore_power=is_shore)
        fresh = load_all_data(cfg, time_window)
        if fresh is None:
            return dash.no_update, dash.no_update
        notes = load_notes(fresh['db_path'])
        note_map = {sid: d['note'] for sid, d in notes.items() if d.get('note')}
        fig = build_figure(
            fresh['readings'], fresh['discharge_sessions'], fresh['charging_sessions'],
            fresh['discharge_stats'], fresh['charging_stats'], fresh['summary'],
            time_format=fresh['time_format'], downsample_cfg=fresh['downsample_cfg'],
            charge_type_map=fresh.get('charge_type_map'),
            note_map=note_map or None,
            window_days=fresh.get('window_days', 3),
        )
        return fig, _summary_cards(fresh['summary'])

    # --- Download Report callback ---
    @app.callback(
        Output('download-html', 'data'),
        Input('download-btn', 'n_clicks'),
        prevent_initial_call=True,
    )
    def download_report(_n):
        fresh = load_all_data(cfg, time_window)
        if fresh is None:
            return dash.no_update
        output_dir = Path(cfg.get('report', 'output_dir', fallback='reports'))
        output_dir.mkdir(exist_ok=True)
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        output_path = output_dir / f'report_{ts}.html'
        report_note_map = load_session_notes(fresh['db_path'])
        diagnostics = compute_and_save_diagnostics(fresh['db_path'], fresh, cfg)
        generate_html(
            fresh['readings'], fresh['discharge_sessions'], fresh['charging_sessions'],
            fresh['discharge_stats'], fresh['charging_stats'], fresh['summary'],
            output_path,
            time_format=fresh['time_format'], downsample_cfg=fresh['downsample_cfg'],
            charge_type_map=fresh.get('charge_type_map'),
            note_map=report_note_map or None,
            diagnostics=diagnostics,
            window_days=fresh.get('window_days', 3),
        )
        # Open in system browser — works in both native window and browser tab modes.
        # dcc.send_file() is unreliable inside pywebview's WKWebView.
        webbrowser.open(output_path.resolve().as_uri())
        return dash.no_update

    return app


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    import textwrap
    parser = argparse.ArgumentParser(
        description='Victron interactive dashboard',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Time window — controls which readings are loaded on every Refresh:
              --week              rolling last 7 days
              --2weeks            rolling last 14 days
              --days N            rolling last N days
              --start YYYY-MM-DD  from that date to now (grows on Refresh)
              --start … --end …   fixed range (Refresh re-reads same window)
        """),
    )
    parser.add_argument('--native', action='store_true',
                        help='Open in a native macOS window via pywebview')
    parser.add_argument('--no-open', dest='open_browser', action='store_false', default=True,
                        help='Start the server without opening a browser or window')
    window = parser.add_mutually_exclusive_group()
    window.add_argument('--days', type=int, metavar='N',
                        help='Rolling last N days')
    window.add_argument('--week', action='store_true',
                        help='Rolling last 7 days')
    window.add_argument('--2weeks', dest='two_weeks', action='store_true',
                        help='Rolling last 14 days')
    window.add_argument('--start', metavar='YYYY-MM-DD',
                        help='Start date; combine with --end for a fixed range')
    parser.add_argument('--end', metavar='YYYY-MM-DD',
                        help='End date (inclusive, used with --start)')
    args = parser.parse_args()

    if args.end and not args.start:
        parser.error('--end requires --start')

    # Build time_window dict for load_all_data
    time_window = None
    if args.week:
        time_window = {'since_delta': timedelta(days=7), 'window_days': 7}
    elif args.two_weeks:
        time_window = {'since_delta': timedelta(days=14), 'window_days': 14}
    elif args.days:
        time_window = {'since_delta': timedelta(days=args.days), 'window_days': args.days}
    elif args.start:
        since_fixed = datetime.strptime(args.start, '%Y-%m-%d').replace(tzinfo=timezone.utc)
        until_fixed = None
        if args.end:
            until_fixed = (datetime.strptime(args.end, '%Y-%m-%d')
                           .replace(hour=23, minute=59, second=59, tzinfo=timezone.utc))
        span = (until_fixed or datetime.now(timezone.utc)) - since_fixed
        wdays = max(1, int(span.total_seconds() / 86400) + 1)
        time_window = {'since_fixed': since_fixed, 'until_fixed': until_fixed,
                       'window_days': wdays}

    cfg = load_config()
    app = build_app(cfg, time_window)

    if args.native:
        import webview
        def start_server():
            app.run(port=APP_PORT, debug=False)
        t = threading.Thread(target=start_server, daemon=True)
        t.start()
        import time; time.sleep(1)  # give Dash a moment to bind
        webview.create_window('Victron Dashboard', f'http://localhost:{APP_PORT}',
                              width=1200, height=900)
        webview.start()
    elif args.open_browser:
        def open_tab():
            import time; time.sleep(1)
            webbrowser.open(f'http://localhost:{APP_PORT}')
        threading.Thread(target=open_tab, daemon=True).start()
        app.run(port=APP_PORT, debug=False)
    else:
        app.run(port=APP_PORT, debug=False)


if __name__ == '__main__':
    main()
