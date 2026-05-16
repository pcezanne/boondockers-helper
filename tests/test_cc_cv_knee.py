"""
Tests for CC/CV knee detection and the cc_rate_pct_per_hour field in
charging_session_stats.
"""

from datetime import datetime, timedelta, timezone

from tests.factories import make_reading
from boondockers.engine import _find_cc_cv_knee, _session_started_in_cv, charging_session_stats


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_T0 = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _cc_cv_session(n_cc=80, n_cv=30, cc_amps=65.0, cv_end_amps=8.0,
                   soc_cc_start=62.0, soc_cc_end=95.0,
                   soc_cv_end=100.0, interval_minutes=1):
    """Build a synthetic CC+CV charging session.

    n_cc readings at constant cc_amps (CC phase), then n_cv readings with
    current linearly declining from cc_amps to cv_end_amps (CV phase).
    SOC rises linearly through each phase.
    """
    readings = []
    t = _T0

    # CC phase
    for i in range(n_cc):
        frac = i / max(n_cc - 1, 1)
        soc = soc_cc_start + (soc_cc_end - soc_cc_start) * frac
        readings.append(make_reading(t, soc=round(soc, 2), current=cc_amps))
        t += timedelta(minutes=interval_minutes)

    # CV phase
    for i in range(n_cv):
        frac = i / max(n_cv - 1, 1)
        soc = soc_cc_end + (soc_cv_end - soc_cc_end) * frac
        current = cc_amps + (cv_end_amps - cc_amps) * frac
        readings.append(make_reading(t, soc=round(soc, 2), current=round(current, 1)))
        t += timedelta(minutes=interval_minutes)

    return readings


def _cc_only_session(n=60, amps=65.0, soc_start=62.0, soc_end=87.0,
                     interval_minutes=1):
    """Build a session that ends in the CC phase (stopped before CV)."""
    readings = []
    t = _T0
    for i in range(n):
        frac = i / max(n - 1, 1)
        soc = soc_start + (soc_end - soc_start) * frac
        readings.append(make_reading(t, soc=round(soc, 2), current=amps))
        t += timedelta(minutes=interval_minutes)
    return readings


# ---------------------------------------------------------------------------
# _find_cc_cv_knee
# ---------------------------------------------------------------------------

def test_knee_detected_in_cc_cv_session():
    sess = _cc_cv_session(n_cc=80, n_cv=30)
    knee_idx = _find_cc_cv_knee(sess)
    assert knee_idx is not None


def test_knee_index_in_cc_phase():
    """Knee index should fall within the CC phase, not deep in the CV tail."""
    sess = _cc_cv_session(n_cc=80, n_cv=30)
    knee_idx = _find_cc_cv_knee(sess)
    # Knee should be near the end of CC phase (index 60–90 range)
    assert 60 <= knee_idx <= 90


def test_knee_not_detected_cc_only():
    """Session stopped before CV phase — no knee."""
    sess = _cc_only_session(n=60)
    assert _find_cc_cv_knee(sess) is None


def test_knee_not_detected_short_session():
    """Sessions with fewer than 15 readings return None."""
    sess = _cc_cv_session(n_cc=7, n_cv=5)
    assert _find_cc_cv_knee(sess) is None


def test_knee_not_detected_single_reading():
    sess = [make_reading(_T0, soc=80.0, current=50.0)]
    assert _find_cc_cv_knee(sess) is None


# ---------------------------------------------------------------------------
# charging_session_stats — new fields
# ---------------------------------------------------------------------------

def test_stats_cv_detected_flag():
    sess = _cc_cv_session(n_cc=80, n_cv=30)
    stats = charging_session_stats(sess)
    assert stats['cv_detected'] is True


def test_stats_no_cv_flag_for_cc_only():
    sess = _cc_only_session(n=60)
    stats = charging_session_stats(sess)
    assert stats['cv_detected'] is False


def test_stats_knee_soc_present_when_cv_detected():
    sess = _cc_cv_session(n_cc=80, n_cv=30, soc_cc_end=95.0)
    stats = charging_session_stats(sess)
    assert stats['knee_soc'] is not None
    # Knee SOC should be near the CC/CV boundary (between ~88% and 97%)
    assert 85.0 <= stats['knee_soc'] <= 97.0


def test_stats_knee_soc_none_when_no_cv():
    sess = _cc_only_session(n=60)
    stats = charging_session_stats(sess)
    assert stats['knee_soc'] is None


def test_cc_rate_higher_than_full_rate_when_cv_detected():
    """CC rate strips the slow CV tail so it should exceed the full-session rate."""
    sess = _cc_cv_session(n_cc=80, n_cv=30,
                          soc_cc_start=62.0, soc_cc_end=95.0, soc_cv_end=100.0)
    stats = charging_session_stats(sess)
    assert stats['cv_detected'] is True
    assert stats['cc_rate_pct_per_hour'] > stats['charge_rate_pct_per_hour']


def test_cc_rate_equals_full_rate_when_no_cv():
    """Without a detectable CV phase the CC rate falls back to full-session rate."""
    sess = _cc_only_session(n=60)
    stats = charging_session_stats(sess)
    assert stats['cc_rate_pct_per_hour'] == stats['charge_rate_pct_per_hour']


# ---------------------------------------------------------------------------
# _session_started_in_cv
# ---------------------------------------------------------------------------

def _cv_start_session(n=90, start_amps=90.0, end_amps=5.0,
                      soc_start=88.0, soc_end=97.0, interval_minutes=1):
    """Build a pure CV-start session: current tapers monotonically from start_amps
    to end_amps with no CC plateau.  Simulates a charger that was already in CV
    (absorption voltage already reached) when the session began."""
    readings = []
    t = _T0
    for i in range(n):
        frac = i / max(n - 1, 1)
        soc = soc_start + (soc_end - soc_start) * frac
        current = start_amps + (end_amps - start_amps) * frac
        readings.append(make_reading(t, soc=round(soc, 2), current=round(current, 1)))
        t += timedelta(minutes=interval_minutes)
    return readings


def _thermal_derating_cv_session(n_pre=20, n_cc=60, n_cv=30,
                                  initial_amps=100.0, cc_amps=70.0, cv_end_amps=8.0,
                                  soc_start=60.0, soc_cc_end=92.0, soc_cv_end=98.0,
                                  interval_minutes=1):
    """Build a session with thermal derating: high initial current → drops to a
    stable CC plateau → CV taper.  Should NOT be flagged as started_in_cv."""
    readings = []
    t = _T0
    # Pre-derating phase: current drops linearly from initial_amps to cc_amps
    for i in range(n_pre):
        frac = i / max(n_pre - 1, 1)
        soc = soc_start + (soc_cc_end - soc_start) * (i / (n_pre + n_cc + n_cv - 1))
        current = initial_amps + (cc_amps - initial_amps) * frac
        readings.append(make_reading(t, soc=round(soc, 2), current=round(current, 1)))
        t += timedelta(minutes=interval_minutes)
    # Stable CC plateau
    for i in range(n_cc):
        idx = n_pre + i
        soc = soc_start + (soc_cc_end - soc_start) * (idx / (n_pre + n_cc + n_cv - 1))
        readings.append(make_reading(t, soc=round(soc, 2), current=cc_amps))
        t += timedelta(minutes=interval_minutes)
    # CV taper
    for i in range(n_cv):
        idx = n_pre + n_cc + i
        frac = i / max(n_cv - 1, 1)
        soc = soc_cc_end + (soc_cv_end - soc_cc_end) * frac
        current = cc_amps + (cv_end_amps - cc_amps) * frac
        readings.append(make_reading(t, soc=round(soc, 2), current=round(current, 1)))
        t += timedelta(minutes=interval_minutes)
    return readings


def test_started_in_cv_pure_taper():
    """Pure CV-start session (monotone decreasing, no plateau) → True."""
    sess = _cv_start_session(n=90, start_amps=90.0, end_amps=5.0)
    assert _session_started_in_cv(sess) is True


def test_not_started_in_cv_normal_cc_cv():
    """Classic CC+CV session — starts at plateau, not above it → False."""
    sess = _cc_cv_session(n_cc=80, n_cv=30)
    assert _session_started_in_cv(sess) is False


def test_not_started_in_cv_thermal_derating():
    """Thermal derating: high initial current → stable CC plateau → CV taper.
    Middle section is stable (the post-derating CC), so condition 2 fails → False."""
    sess = _thermal_derating_cv_session()
    assert _session_started_in_cv(sess) is False


def test_charging_stats_cv_start():
    """charging_session_stats for a CV-start session returns started_in_cv=True,
    cc_rate_pct_per_hour=None, knee_soc=None, cv_detected=False."""
    sess = _cv_start_session(n=90, start_amps=90.0, end_amps=5.0)
    stats = charging_session_stats(sess)
    assert stats['started_in_cv'] is True
    assert stats['cc_rate_pct_per_hour'] is None
    assert stats['knee_soc'] is None
    assert stats['cv_detected'] is False
