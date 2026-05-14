"""
Tests for the diagnostics system:
  - _detect_thermal_derating
  - charging_session_stats derating fields
  - compute_diagnostics: knee SOC drift, charge rate decline, parasitic drain
"""

from datetime import datetime, timedelta, timezone

from factories import make_reading
from victron.report import (
    _detect_thermal_derating,
    charging_session_stats,
    compute_diagnostics,
)

_T0 = datetime(2024, 6, 1, 10, 0, 0, tzinfo=timezone.utc)

_CFG_DIAG = {
    'knee_soc_baseline_sessions': 5,
    'knee_soc_drop_threshold_pct': 10,
    'charge_rate_decline_threshold_pct': 20,
    'thermal_derating_drop_pct': 15,
    'thermal_derating_window_minutes': 30,
    'parasitic_drain_threshold_pct': 2,
    'parasitic_drain_min_hours': 4,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _derating_session(n_early=15, peak_amps=85.0, plateau_amps=65.0,
                      interval_minutes=1, soc_start=60.0, soc_end=90.0):
    """Build a session with a clear thermal derating signature.

    First n_early readings at peak_amps, remainder at plateau_amps.
    Total readings: n_early + 30.

    Voltage rises from 13.0 V to ~14.0 V during the high-current phase, then
    continues slowly to ~14.15 V — well below a typical absorption setpoint
    (14.4 V).  This lets the voltage check distinguish derating from CC→CV.
    """
    readings = []
    t = _T0
    n_total = n_early + 30
    for i in range(n_total):
        frac = i / max(n_total - 1, 1)
        soc = soc_start + (soc_end - soc_start) * frac
        current = peak_amps if i < n_early else plateau_amps
        if i < n_early:
            voltage = 13.0 + (14.0 - 13.0) * (i / n_early)
        else:
            voltage = 14.0 + (14.15 - 14.0) * ((i - n_early) / 30)
        readings.append(make_reading(t, soc=round(soc, 2), current=current,
                                     voltage=round(voltage, 3)))
        t += timedelta(minutes=interval_minutes)
    return readings


def _cv_transition_session(n_cc=15, n_cv=30, peak_amps=85.0, cv_end_amps=15.0,
                            v_absorption=14.4, interval_minutes=1,
                            soc_start=60.0, soc_end=95.0):
    """Build a session whose current drop looks like derating but voltage confirms CC→CV.

    Voltage rises to v_absorption during the CC phase, then holds flat.
    """
    readings = []
    t = _T0
    n_total = n_cc + n_cv
    for i in range(n_total):
        frac = i / max(n_total - 1, 1)
        soc = soc_start + (soc_end - soc_start) * frac
        if i < n_cc:
            current = peak_amps
            voltage = 13.0 + (v_absorption - 13.0) * (i / n_cc)
        else:
            cv_frac = (i - n_cc) / max(n_cv - 1, 1)
            current = peak_amps - (peak_amps - cv_end_amps) * cv_frac
            voltage = v_absorption
        readings.append(make_reading(t, soc=round(soc, 2), current=round(current, 1),
                                     voltage=round(voltage, 3)))
        t += timedelta(minutes=interval_minutes)
    return readings


def _steady_session(n=40, amps=50.0, soc_start=60.0, soc_end=90.0,
                    interval_minutes=1):
    """Build a session with constant current (no derating)."""
    readings = []
    t = _T0
    for i in range(n):
        frac = i / max(n - 1, 1)
        soc = soc_start + (soc_end - soc_start) * frac
        readings.append(make_reading(t, soc=round(soc, 2), current=amps))
        t += timedelta(minutes=interval_minutes)
    return readings


def _make_charge_stat(ts_start, cc_rate, knee_soc=None, cv_detected=False,
                      is_derating=False, derating_peak=None, derating_plateau=None):
    """Build a minimal charging_session_stats dict for cross-session tests."""
    ts_end = ts_start + timedelta(hours=2)
    return {
        'date': ts_start.strftime('%Y-%m-%d'),
        'start': ts_start.isoformat(),
        'end': ts_end.isoformat(),
        'soc_start': 60.0,
        'soc_end': 90.0,
        'pct_gain': 30.0,
        'hours': 2.0,
        'charge_rate_pct_per_hour': cc_rate,
        'cc_rate_pct_per_hour': cc_rate,
        'knee_soc': knee_soc,
        'cv_detected': cv_detected,
        'avg_amps': 50.0,
        'is_derating': is_derating,
        'derating_peak_amps': derating_peak,
        'derating_plateau_amps': derating_plateau,
        'ts_start': ts_start,
        'ts_end': ts_end,
        'n_readings': 40,
    }


def _make_discharge_stat(ts_start):
    ts_end = ts_start + timedelta(hours=8)
    return {
        'ts_start': ts_start,
        'ts_end': ts_end,
        'soc_start': 90.0,
        'soc_end': 60.0,
        'pct_drop': 30.0,
        'hours': 8.0,
        'pct_per_day': 90.0,
        'avg_watts': 200.0,
        'avg_voltage': 12.8,
        'consumed_ah': 30.0,
        'date': ts_start.strftime('%Y-%m-%d'),
        'start': ts_start.isoformat(),
        'end': ts_end.isoformat(),
    }


# ---------------------------------------------------------------------------
# Group 1: _detect_thermal_derating
# ---------------------------------------------------------------------------

def test_derating_detected_clear_signature():
    """High peak followed by stable lower plateau → derating flagged."""
    sess = _derating_session(n_early=15, peak_amps=85.0, plateau_amps=65.0)
    is_derating, peak, plateau = _detect_thermal_derating(sess)
    assert is_derating is True
    assert peak is not None and peak >= 80
    assert plateau is not None and plateau <= 70


def test_derating_not_detected_steady_current():
    """Constant current throughout → no derating."""
    sess = _steady_session(n=40, amps=50.0)
    is_derating, peak, plateau = _detect_thermal_derating(sess)
    assert is_derating is False
    assert peak is None
    assert plateau is None


def test_derating_not_detected_short_session():
    """Sessions with fewer than 15 readings → (False, None, None)."""
    t = _T0
    readings = [make_reading(t + timedelta(minutes=i), soc=70.0, current=80.0) for i in range(10)]
    is_derating, peak, plateau = _detect_thermal_derating(readings)
    assert is_derating is False


def test_derating_not_detected_low_peak():
    """Peak below 30 A threshold → not flagged even with a drop."""
    # 25A → 15A  (big % drop but low amps — not a charger)
    t = _T0
    readings = []
    for i in range(40):
        current = 25.0 if i < 10 else 15.0
        readings.append(make_reading(t + timedelta(minutes=i), soc=70.0 + i * 0.3, current=current))
    is_derating, peak, plateau = _detect_thermal_derating(readings)
    assert is_derating is False


def test_derating_not_flagged_cc_cv_transition():
    """Current drops within the window but voltage is at absorption setpoint — CC→CV, not derating."""
    sess = _cv_transition_session(n_cc=15, n_cv=30, peak_amps=85.0, cv_end_amps=15.0,
                                   v_absorption=14.4)
    is_derating, peak, plateau = _detect_thermal_derating(sess)
    assert is_derating is False
    assert peak is None
    assert plateau is None


def test_derating_marginal_drop_not_flagged():
    """A 10% drop when threshold is 15% → should NOT be flagged."""
    # peak 80 → plateau 72 = 10% drop < 15% threshold
    sess = _derating_session(n_early=15, peak_amps=80.0, plateau_amps=72.0)
    is_derating, _, _ = _detect_thermal_derating(sess, drop_pct=15)
    assert is_derating is False


# ---------------------------------------------------------------------------
# Group 2: charging_session_stats includes derating fields
# ---------------------------------------------------------------------------

def test_stats_includes_derating_fields():
    """charging_session_stats must return the three new derating keys."""
    sess = _steady_session(n=20)
    stats = charging_session_stats(sess)
    assert 'is_derating' in stats
    assert 'derating_peak_amps' in stats
    assert 'derating_plateau_amps' in stats


def test_stats_derating_false_for_steady_session():
    """A steady current session should have is_derating=False."""
    sess = _steady_session(n=40, amps=50.0)
    stats = charging_session_stats(sess)
    assert stats['is_derating'] is False
    assert stats['derating_peak_amps'] is None
    assert stats['derating_plateau_amps'] is None


def test_stats_derating_true_for_derating_session():
    """A clear derating session should have is_derating=True."""
    sess = _derating_session(n_early=15, peak_amps=85.0, plateau_amps=65.0)
    stats = charging_session_stats(sess)
    assert stats['is_derating'] is True
    assert stats['derating_peak_amps'] is not None
    assert stats['derating_plateau_amps'] is not None


# ---------------------------------------------------------------------------
# Group 3: compute_diagnostics — Knee SOC drift
# ---------------------------------------------------------------------------

def test_knee_soc_drift_flagged():
    """Knee SOC drops well below baseline → flagged."""
    base_ts = _T0
    # 6 sessions: first 5 at knee_soc=97, sixth at 83
    stats = [
        _make_charge_stat(base_ts + timedelta(days=i), cc_rate=7.0,
                          knee_soc=97.0, cv_detected=True)
        for i in range(5)
    ]
    stats.append(
        _make_charge_stat(base_ts + timedelta(days=5), cc_rate=7.0,
                          knee_soc=83.0, cv_detected=True)
    )
    result = compute_diagnostics(stats, [], [], {}, _CFG_DIAG)
    assert len(result['knee_soc']) == 1
    assert result['knee_soc'][0]['knee_soc'] == 83.0
    assert result['knee_soc'][0]['baseline_median'] == 97.0


def test_knee_soc_drift_not_flagged_insufficient_baseline():
    """Only 4 sessions with cv_detected — insufficient baseline (need 5)."""
    base_ts = _T0
    stats = [
        _make_charge_stat(base_ts + timedelta(days=i), cc_rate=7.0,
                          knee_soc=97.0, cv_detected=True)
        for i in range(4)
    ]
    stats.append(
        _make_charge_stat(base_ts + timedelta(days=4), cc_rate=7.0,
                          knee_soc=83.0, cv_detected=True)
    )
    result = compute_diagnostics(stats, [], [], {}, _CFG_DIAG)
    assert result['knee_soc'] == []


def test_knee_soc_drift_not_flagged_marginal_drop():
    """Knee drops 7 pts (below 10 pt threshold) — not flagged."""
    base_ts = _T0
    stats = [
        _make_charge_stat(base_ts + timedelta(days=i), cc_rate=7.0,
                          knee_soc=97.0, cv_detected=True)
        for i in range(5)
    ]
    stats.append(
        _make_charge_stat(base_ts + timedelta(days=5), cc_rate=7.0,
                          knee_soc=90.0, cv_detected=True)  # 97-90=7 < 10
    )
    result = compute_diagnostics(stats, [], [], {}, _CFG_DIAG)
    assert result['knee_soc'] == []


# ---------------------------------------------------------------------------
# Group 4: compute_diagnostics — charge rate decline
# ---------------------------------------------------------------------------

def _make_sid(ts_start):
    import hashlib
    raw = f'charge:{ts_start.isoformat()}'
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def test_charge_rate_decline_flagged():
    """Last-3-session avg well below all-time avg → flagged."""
    base_ts = _T0
    # 7 sessions: first 4 at 7%/hr, last 3 at 4%/hr
    stats = [
        _make_charge_stat(base_ts + timedelta(days=i), cc_rate=7.0)
        for i in range(4)
    ] + [
        _make_charge_stat(base_ts + timedelta(days=4 + i), cc_rate=4.0)
        for i in range(3)
    ]
    charge_type_map = {_make_sid(s['ts_start']): 'Generator' for s in stats}
    result = compute_diagnostics(stats, [], [], charge_type_map, _CFG_DIAG)
    assert 'Generator' in result['charge_rate_decline']
    info = result['charge_rate_decline']['Generator']
    assert info['recent_avg'] < info['all_time_avg']
    assert info['decline_pct'] > 20


def test_charge_rate_decline_not_flagged_marginal():
    """Recent avg only slightly below all-time — under 20% threshold."""
    base_ts = _T0
    # 7 sessions: rates [6.5, 6.5, 6.5, 6.5, 6.0, 6.0, 6.0]
    # all-time avg ~6.28, last-3 avg=6.0, decline ~4.5% < 20%
    stats = [
        _make_charge_stat(base_ts + timedelta(days=i), cc_rate=6.5)
        for i in range(4)
    ] + [
        _make_charge_stat(base_ts + timedelta(days=4 + i), cc_rate=6.0)
        for i in range(3)
    ]
    charge_type_map = {_make_sid(s['ts_start']): 'Generator' for s in stats}
    result = compute_diagnostics(stats, [], [], charge_type_map, _CFG_DIAG)
    assert 'Generator' not in result['charge_rate_decline']


def test_charge_rate_decline_requires_min_sessions():
    """Only 3 sessions — fewer than 4 required, not flagged."""
    base_ts = _T0
    stats = [
        _make_charge_stat(base_ts + timedelta(days=i), cc_rate=7.0 if i < 1 else 4.0)
        for i in range(3)
    ]
    charge_type_map = {_make_sid(s['ts_start']): 'Generator' for s in stats}
    result = compute_diagnostics(stats, [], [], charge_type_map, _CFG_DIAG)
    assert result['charge_rate_decline'] == {}


# ---------------------------------------------------------------------------
# Group 5: compute_diagnostics — parasitic drain
# ---------------------------------------------------------------------------

def _idle_readings(soc_start, soc_end, start_dt, duration_hours, n=30):
    """Readings with declining SOC and no session covering them."""
    readings = []
    for i in range(n):
        frac = i / max(n - 1, 1)
        ts = start_dt + timedelta(hours=duration_hours * frac)
        soc = soc_start + (soc_end - soc_start) * frac
        readings.append(make_reading(ts, soc=round(soc, 2), current=0.5))
    return readings


def test_parasitic_drain_flagged():
    """SOC drops >2% over >4h with no sessions → flagged."""
    readings = _idle_readings(80.0, 75.0, _T0, duration_hours=6.0)
    result = compute_diagnostics([], [], readings, {}, _CFG_DIAG)
    assert len(result['parasitic_drain']) == 1
    drain = result['parasitic_drain'][0]
    assert drain['soc_drop'] >= 4.9
    assert drain['hours'] >= 5.9


def test_parasitic_drain_ignored_short_idle():
    """Gap is only 2h — below the 4h minimum."""
    readings = _idle_readings(80.0, 75.0, _T0, duration_hours=2.0)
    result = compute_diagnostics([], [], readings, {}, _CFG_DIAG)
    assert result['parasitic_drain'] == []


def test_parasitic_drain_ignored_small_drop():
    """SOC drops only 1% — below the 2% threshold."""
    readings = _idle_readings(80.0, 79.0, _T0, duration_hours=6.0)
    result = compute_diagnostics([], [], readings, {}, _CFG_DIAG)
    assert result['parasitic_drain'] == []


def test_parasitic_drain_not_flagged_when_session_covers_gap():
    """If a charge session covers the gap period, it's not idle → not flagged."""
    readings = _idle_readings(80.0, 75.0, _T0, duration_hours=6.0)
    # Create a charging stat that exactly covers this period
    ts_end = _T0 + timedelta(hours=6)
    charge_stat = _make_charge_stat(_T0, cc_rate=7.0)
    charge_stat['ts_start'] = _T0
    charge_stat['ts_end'] = ts_end
    result = compute_diagnostics([charge_stat], [], readings, {}, _CFG_DIAG)
    assert result['parasitic_drain'] == []
