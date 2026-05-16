#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul Cezanne
"""
report.py — Build the Plotly figure and generate the HTML report.

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
    python3 report.py                                    # all data, open in browser
    python3 report.py --week                             # last 7 days
    python3 report.py --2weeks                           # last 14 days
    python3 report.py --days 30                          # last N days
    python3 report.py --start 2026-05-01                 # from date to now
    python3 report.py --start 2026-05-01 --end 2026-05-14  # explicit range
    python3 report.py --no-open                          # generate without opening browser
"""

import argparse
import configparser
import textwrap
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

from boondockers.engine import (
    CHARGE_TYPE_COLORS,
    CHARGE_TYPE_ORDER,
    _plan_charge_subplots,
    adaptive_downsample,
    charging_session_stats,
    compute_diagnostics,
    compute_summary,
    detect_charging_sessions,
    detect_sessions,
    filter_sessions,
    midnights_in_range,
    parse_ts,
    rolling_avg,
    session_id,
    session_stats,
    to_local,
)
from boondockers.db import (
    load_charge_types,
    load_readings,
    load_session_notes,
)

CONFIG_PATH = Path(__file__).parent.parent / 'config.ini'


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config():
    cfg = configparser.ConfigParser()
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f'Config file not found: {CONFIG_PATH}')
    cfg.read(CONFIG_PATH)
    return cfg


# ---------------------------------------------------------------------------
# Chart
# ---------------------------------------------------------------------------

def build_figure(readings, discharge_sessions, charging_sessions,
                 discharge_stats, charging_stats, summary,
                 time_format='12h', downsample_cfg=None, charge_type_map=None,
                 note_map=None, window_days=3, capacity_ah=None):
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

    if capacity_ah:
        _soc_y = [v * capacity_ah / 100.0 for v in soc_values]
        _soc_hover = f'<b>%{{customdata:.1f}}% / %{{y:.0f}} Ah</b><br>%{{x|{tick_fmt}}}<extra></extra>'
        fig.add_trace(go.Scatter(
            x=timestamps, y=_soc_y,
            mode='lines', name='State of Charge',
            line=dict(color='steelblue', width=2),
            showlegend=True, legend='legend',
            customdata=soc_values,
            hovertemplate=_soc_hover,
        ), row=1, col=1)
    else:
        fig.add_trace(go.Scatter(
            x=timestamps, y=soc_values,
            mode='lines', name='State of Charge',
            line=dict(color='steelblue', width=2),
            showlegend=True, legend='legend',
            hovertemplate=f'<b>%{{y:.1f}}% SOC</b><br>%{{x|{tick_fmt}}}<extra></extra>',
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
            import textwrap as _tw
            lines = []
            for paragraph in text.splitlines():
                lines.extend(_tw.wrap(paragraph, wrap) or [''])
            return '<br>'.join(
                l.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                for l in lines
            )

        _NOTE_Y = (3.0 * capacity_ah / 100.0) if capacity_ah else 3  # near bottom of axis, clear of SOC/V/A traces

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
    if capacity_ah:
        fig.update_yaxes(title_text='Ah', range=[0, capacity_ah * 1.05], row=1, col=1)
    else:
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
                    dict(count=14, label='14d', step='day', stepmode='backward'),
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


# ---------------------------------------------------------------------------
# HTML report helpers
# ---------------------------------------------------------------------------

def _diagnostics_panel_html(diagnostics):
    """Render the diagnostics panel as a plain HTML string for static reports."""
    if not diagnostics:
        return ''

    thermal   = diagnostics.get('thermal_derating', [])
    knee_list = diagnostics.get('knee_soc', [])
    rate_map  = diagnostics.get('charge_rate_decline', {})
    drain     = diagnostics.get('parasitic_drain', [])

    def _split(all_lines, keep=5):
        if len(all_lines) <= keep:
            return all_lines, []
        return all_lines[:keep], all_lines[keep:]

    def _card(color, title, lines, detail_lines=None):
        body = f'<strong style="color:{color}">{title}</strong>'
        for line in lines:
            body += f'<br>{line}'
        if detail_lines:
            n = len(detail_lines)
            inner = ''.join(f'<br>{l}' for l in detail_lines)
            label = f'\u25b6 {n} earlier event{"s" if n > 1 else ""}'
            body += (
                f'<details><summary style="cursor:pointer;color:#888;'
                f'font-size:0.9em;margin-top:6px">{label}</summary>{inner}</details>'
            )
        return f'<div class="diag-card" style="border-left:4px solid {color}">{body}</div>\n'

    cards = ''

    if thermal:
        n = len(thermal)
        session_lines = [
            f"&nbsp;&nbsp;{t['date']}: {t['peak']}A peak &rarr; {t['plateau']}A plateau"
            for t in reversed(thermal)
        ]
        visible, older = _split(session_lines)
        cards += _card(
            '#c0392b',
            f'Thermal Derating &mdash; {n} session{"s" if n > 1 else ""}',
            ['Charger output dropped significantly within the first 30 minutes.',
             'Check charger bay ventilation and airflow path.'] + visible,
            detail_lines=older or None,
        )

    if knee_list:
        item = knee_list[-1]
        cards += _card(
            '#f39c12',
            'CC&rarr;CV Knee SOC Drift',
            [f"Latest session: knee at {item['knee_soc']:.1f}% "
             f"vs baseline median {item['baseline_median']}% "
             f"(&Delta; {item['delta']} pts).",
             'Possible BMS protection or thermal limiting at lower SOC than normal.'],
        )

    for ctype, info in rate_map.items():
        cards += _card(
            '#e67e22',
            f'Charge Rate Declining &mdash; {ctype}',
            [f"Recent 3-session CC avg: {info['recent_avg']:.2f}%/hr &nbsp;"
             f"vs all-time avg: {info['all_time_avg']:.2f}%/hr "
             f"({info['decline_pct']:.0f}% decline).",
             'Check battery connections, charger output, and source health.'],
        )

    if drain:
        n = len(drain)
        all_drain_lines = []
        for d in reversed(drain):
            try:
                ps = d['period_start'][:16].replace('T', ' ')
                pe = d['period_end'][:16].replace('T', ' ')
            except Exception:
                ps = pe = '&mdash;'
            all_drain_lines.append(
                f"&nbsp;&nbsp;{ps} &ndash; {pe}: {d['soc_drop']:.1f}% SOC lost"
                f" over {d['hours']:.1f}h"
            )
        visible, older = _split(all_drain_lines)
        cards += _card(
            '#2980b9',
            f'Possible Parasitic Drain &mdash; {n} event{"s" if n > 1 else ""}',
            ['SOC dropped during idle periods with no active session.',
             'Check for loads running while the battery was otherwise idle.'] + visible,
            detail_lines=older or None,
        )

    header = '<h3 style="color:#555;margin:0 0 10px 0;font-size:1em;font-weight:600;text-transform:uppercase;letter-spacing:0.05em">Diagnostics</h3>'
    if not cards:
        no_flags = '<p style="color:#999;font-size:0.88em;margin:0">No anomalies detected.</p>'
        return f'<div class="diag-panel">{header}{no_flags}</div>\n'

    return f'<div class="diag-panel">{header}{cards}</div>\n'


def _soc_bar_html(soc_pct, remaining_ah=None):
    """Render the full-width tri-color SOC status bar as an HTML string."""
    if soc_pct is None:
        return ''
    color = '#27ae60' if soc_pct > 60 else ('#f39c12' if soc_pct > 30 else '#e74c3c')
    fill_pct = f'{min(soc_pct, 100):.1f}%'
    ah_part = f'<span class="soc-bar-ah">{remaining_ah:.0f} Ah</span>' if remaining_ah is not None else ''
    border_left = f'border-left: 5px solid {color};'
    return (
        f'<div class="soc-bar-wrap" style="{border_left}">'
        f'<div class="soc-bar-label">Current Charge</div>'
        f'<span class="soc-bar-number" style="color:{color}">{soc_pct:.1f}%</span>'
        f'{ah_part}'
        f'<div class="soc-bar-track">'
        f'<div class="soc-bar-fill" style="width:{fill_pct};background:{color}"></div>'
        f'</div>'
        f'</div>\n'
    )


def generate_html(readings, discharge_sessions, charging_sessions,
                  discharge_stats, charging_stats, summary, output_path,
                  time_format='12h', downsample_cfg=None, charge_type_map=None,
                  note_map=None, diagnostics=None, window_days=3):
    import plotly.io as pio

    fig = build_figure(readings, discharge_sessions, charging_sessions,
                       discharge_stats, charging_stats, summary,
                       time_format=time_format, downsample_cfg=downsample_cfg,
                       charge_type_map=charge_type_map, note_map=note_map,
                       window_days=window_days, capacity_ah=summary.get('capacity_ah'))

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
  .diag-panel {{
    margin-top: 28px; margin-bottom: 4px;
    padding: 14px 18px;
    border: 1px solid #e0e0e0;
    border-radius: 8px;
    background: #fff;
  }}
  .diag-card {{
    padding: 12px 16px; margin-bottom: 10px;
    border-radius: 6px; background: #fafafa;
    font-size: 0.88em; line-height: 1.5;
  }}
  .diag-card details summary {{ cursor: pointer; }}
  .soc-bar-wrap {{
    border-top: 1px solid #e0e4ea; border-right: 1px solid #e0e4ea;
    border-bottom: 1px solid #e0e4ea;
    border-radius: 10px; padding: 16px 24px;
    margin-bottom: 16px; background: #fafafa;
  }}
  .soc-bar-number {{ font-size: 2.4em; font-weight: 700; }}
  .soc-bar-ah {{ font-size: 1.1em; color: #555; margin-left: 12px; }}
  .soc-bar-track {{
    position: relative; width: 100%; height: 16px; border-radius: 8px;
    overflow: hidden; margin-top: 10px;
    background: linear-gradient(to right,
      rgba(231,76,60,0.20) 0% 30%,
      rgba(243,156,18,0.20) 30% 60%,
      rgba(39,174,96,0.20) 60% 100%);
  }}
  .soc-bar-fill {{
    position: absolute; left: 0; top: 0; bottom: 0;
    border-radius: 8px; opacity: 0.75;
  }}
  .soc-bar-label {{
    font-size: 0.75em; font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.08em; color: #888; margin-bottom: 6px;
  }}
</style>
</head>
<body>

<h1>Battery Usage Report</h1>
<p class="subtitle">
  Generated {generated} &middot; {len(readings):,} total readings &middot;
  {n_dis} discharge sessions &middot; {n_chg} charging sessions
</p>

{_soc_bar_html(cur_soc, remaining_ah)}

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
    {f'<div class="sublabel">{eff_rate / 100 * capacity_ah:.0f} Ah/h</div>' if capacity_ah else ''}
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

{_diagnostics_panel_html(diagnostics or {{}})}

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
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Generate Boondockers Helper battery usage report',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Time window examples:
              --week              last 7 days
              --2weeks            last 14 days
              --days 30           last 30 days
              --start 2026-05-01              May 1 to now
              --start 2026-05-01 --end 2026-05-14   specific range
        """),
    )
    window = parser.add_mutually_exclusive_group()
    window.add_argument('--days', type=int, metavar='N',
                        help='Last N days of data')
    window.add_argument('--week', action='store_true',
                        help='Last 7 days (shorthand for --days 7)')
    window.add_argument('--2weeks', dest='two_weeks', action='store_true',
                        help='Last 14 days (shorthand for --days 14)')
    window.add_argument('--start', metavar='YYYY-MM-DD',
                        help='Start date (inclusive); combine with --end for a range')
    parser.add_argument('--end', metavar='YYYY-MM-DD',
                        help='End date (inclusive, used with --start)')
    parser.add_argument('--open', dest='open_browser', action='store_true', default=True)
    parser.add_argument('--no-open', dest='open_browser', action='store_false')
    args = parser.parse_args()

    if args.end and not args.start:
        parser.error('--end requires --start')

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

    # Resolve time window
    since = until = None
    window_days = 3  # default chart viewport

    if args.week:
        since = datetime.now(timezone.utc) - timedelta(days=7)
        window_days = 7
    elif args.two_weeks:
        since = datetime.now(timezone.utc) - timedelta(days=14)
        window_days = 14
    elif args.days:
        since = datetime.now(timezone.utc) - timedelta(days=args.days)
        window_days = args.days
    elif args.start:
        since = datetime.strptime(args.start, '%Y-%m-%d').replace(tzinfo=timezone.utc)
        if args.end:
            # end of the specified day (23:59:59)
            until = (datetime.strptime(args.end, '%Y-%m-%d')
                     .replace(hour=23, minute=59, second=59, tzinfo=timezone.utc))
        # show the full requested range in the chart viewport
        span = (until or datetime.now(timezone.utc)) - since
        window_days = max(1, int(span.total_seconds() / 86400) + 1)

    # Always load all readings for accurate summary stats.
    # The time window controls the chart viewport only (via window_days / build_figure).
    readings = load_readings(db_path)
    if not readings:
        print('No readings found. Run providers/victron_ble.py first to collect data.')
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

    cfg_diag = {
        'knee_soc_baseline_sessions':        cfg.getint('diagnostics', 'knee_soc_baseline_sessions', fallback=5),
        'knee_soc_drop_threshold_pct':       cfg.getfloat('diagnostics', 'knee_soc_drop_threshold_pct', fallback=10),
        'charge_rate_decline_threshold_pct': cfg.getfloat('diagnostics', 'charge_rate_decline_threshold_pct', fallback=20),
        'thermal_derating_drop_pct':         cfg.getfloat('diagnostics', 'thermal_derating_drop_pct', fallback=15),
        'thermal_derating_window_minutes':   cfg.getfloat('diagnostics', 'thermal_derating_window_minutes', fallback=30),
        'parasitic_drain_threshold_pct':     cfg.getfloat('diagnostics', 'parasitic_drain_threshold_pct', fallback=2),
        'parasitic_drain_min_hours':         cfg.getfloat('diagnostics', 'parasitic_drain_min_hours', fallback=4),
    }
    diagnostics = compute_diagnostics(charging_stats, discharge_stats, readings,
                                      charge_type_map, cfg_diag)

    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    output_path = output_dir / f'report_{ts}.html'
    generate_html(readings, discharge_sessions, charging_sessions,
                  discharge_stats, charging_stats, summary, output_path,
                  time_format=time_format, downsample_cfg=downsample_cfg,
                  charge_type_map=charge_type_map, note_map=note_map,
                  diagnostics=diagnostics, window_days=window_days)

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
