from datetime import datetime, timedelta, timezone

from factories import discharge_session

from boondockers.engine import (
    charging_session_stats,
    compute_daily_rates,
    compute_summary,
    session_stats,
)


def _stats(sessions):
    return [session_stats(s) for s in sessions if len(s) >= 2]


# ---------------------------------------------------------------------------
# compute_daily_rates
# ---------------------------------------------------------------------------

def test_daily_rates_two_sessions_same_day_merged():
    r1 = discharge_session('2024-01-15T08:00:00', 4.0, 90, 80, n_readings=10)
    r2 = discharge_session('2024-01-15T14:00:00', 4.0, 78, 68, n_readings=10)
    # Pass as two separate sessions (simulating a gap that didn't split them at
    # detect_sessions level, or two sessions grouped on same calendar day)
    rates = compute_daily_rates([r1, r2])
    dates = [d for d, _, _, _ in rates]
    assert dates.count('2024-01-15') == 1


def test_daily_rates_session_spanning_midnight():
    # Session starts 2024-01-15 at 20:00 UTC and runs 8 hours → crosses midnight
    r = discharge_session('2024-01-15T20:00:00', 8.0, 90, 60, n_readings=20)
    rates = compute_daily_rates([r])
    dates = [d for d, _, _, _ in rates]
    # At least one of the two local calendar days should appear
    assert len(dates) >= 1


def test_daily_rates_sorted_ascending():
    r1 = discharge_session('2024-01-15T08:00:00', 4.0, 90, 80, n_readings=10)
    r2 = discharge_session('2024-01-16T08:00:00', 4.0, 90, 80, n_readings=10)
    rates = compute_daily_rates([r1, r2])
    dates = [d for d, _, _, _ in rates]
    assert dates == sorted(dates)


def test_daily_rates_empty():
    assert compute_daily_rates([]) == []


# ---------------------------------------------------------------------------
# compute_summary
# ---------------------------------------------------------------------------

def test_compute_summary_empty_input():
    summary = compute_summary([], [], [], current_soc=80)
    assert summary['running_avg_pct_per_day'] == 0
    assert summary['weekly_avg_pct_per_day'] == 0
    assert summary['last_24h_pct_per_day'] is None
    assert summary['total_discharge_sessions'] == 0


def test_compute_summary_running_avg():
    # Two sessions: 10% drop / 4h and 20% drop / 8h
    # Running avg = 30 / 12 * 24 = 60%/day
    s1 = discharge_session('2024-01-10T08:00:00', 4.0, 90, 80, n_readings=10)
    s2 = discharge_session('2024-01-11T08:00:00', 8.0, 90, 70, n_readings=10)
    stats = _stats([s1, s2])
    summary = compute_summary([s1, s2], stats, [], current_soc=80)
    assert abs(summary['running_avg_pct_per_day'] - 60.0) < 1.0


def test_compute_summary_today_excluded_from_7day_avg():
    # One old session (always a complete day) + one session starting right now (today)
    old = discharge_session('2024-01-10T08:00:00', 4.0, 90, 80, n_readings=10)
    now_utc = datetime.now(timezone.utc)
    today_session = discharge_session(now_utc, 1.0, 90, 85, n_readings=5)

    all_sessions = [old, today_session]
    stats = _stats(all_sessions)
    summary = compute_summary(all_sessions, stats, [], current_soc=80)

    # today_str should be set
    assert 'today_str' in summary
    today_str = summary['today_str']

    # today should not appear in the 7-day avg calculation
    # (we can verify indirectly: weekly_avg should only reflect old session's rate)
    # old session: 10%/4h*24 = 60%/day; weekly avg from 1 complete day = 60%/day
    assert abs(summary['weekly_avg_pct_per_day'] - 60.0) < 5.0


def test_compute_summary_last_24h_only_recent():
    old_start = datetime.now(timezone.utc) - timedelta(days=5)
    recent_start = datetime.now(timezone.utc) - timedelta(hours=2)

    r_old = discharge_session(old_start, 4.0, 90, 80, n_readings=10)
    # 5% drop in 1h → 120%/day
    r_recent = discharge_session(recent_start, 1.0, 85, 80, n_readings=10)

    all_sessions = [r_old, r_recent]
    stats = _stats(all_sessions)
    summary = compute_summary(all_sessions, stats, [], current_soc=80)

    # last_24h_pct_per_day should reflect recent session only (~120%/day)
    assert summary['last_24h_pct_per_day'] is not None
    assert abs(summary['last_24h_pct_per_day'] - 120.0) < 10.0


def test_compute_summary_no_discharge_in_last_24h():
    old_start = datetime.now(timezone.utc) - timedelta(days=3)
    r_old = discharge_session(old_start, 4.0, 90, 80, n_readings=10)
    stats = _stats([r_old])
    summary = compute_summary([r_old], stats, [], current_soc=80)
    assert summary['last_24h_pct_per_day'] is None


def test_compute_summary_remaining_ah():
    s1 = discharge_session('2024-01-10T08:00:00', 4.0, 90, 80, n_readings=10)
    stats = _stats([s1])
    summary = compute_summary([s1], stats, [], current_soc=80, capacity_ah=920)
    assert summary['remaining_ah'] is not None
    assert abs(summary['remaining_ah'] - 0.80 * 920) < 1.0


def test_compute_summary_hours_to_target():
    s1 = discharge_session('2024-01-10T08:00:00', 4.0, 90, 80, n_readings=10)
    stats = _stats([s1])
    # Fake a charging session with 5%/h rate
    from factories import charging_session
    c1 = charging_session('2024-01-10T12:00:00', 4.0, 80, 100, n_readings=10, amps=30.0)
    c_stats = [charging_session_stats(c1)]
    summary = compute_summary([s1], stats, c_stats, current_soc=70, target_soc=95)
    assert summary['hours_to_target'] is not None
    # (95-70) / charge_rate; charge_rate = 20/4 = 5%/h → 5h
    assert abs(summary['hours_to_target'] - 5.0) < 0.5
