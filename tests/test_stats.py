from factories import charging_session, discharge_session, make_reading

from victron.report import charging_session_stats, session_stats


def test_session_stats_pct_drop():
    # 10% SOC drop over 4 hours
    readings = discharge_session('2024-01-15T08:00:00', 4.0, 90, 80, n_readings=10)
    stats = session_stats(readings)
    assert abs(stats['pct_drop'] - 10.0) < 0.2


def test_session_stats_pct_per_day():
    # 10% drop / 4h * 24 = 60%/day
    readings = discharge_session('2024-01-15T08:00:00', 4.0, 90, 80, n_readings=10)
    stats = session_stats(readings)
    assert abs(stats['pct_per_day'] - 60.0) < 1.0


def test_session_stats_hours():
    readings = discharge_session('2024-01-15T08:00:00', 6.0, 90, 75, n_readings=10)
    stats = session_stats(readings)
    assert abs(stats['hours'] - 6.0) < 0.01


def test_session_stats_consumed_ah_delta():
    readings = discharge_session('2024-01-15T08:00:00', 4.0, 90, 80, n_readings=10,
                                  consumed_ah_start=50.0)
    stats = session_stats(readings)
    assert stats['consumed_ah'] is not None
    assert stats['consumed_ah'] > 0


def test_session_stats_none_consumed_ah():
    readings = discharge_session('2024-01-15T08:00:00', 4.0, 90, 80, n_readings=10)
    for r in readings:
        r['consumed_ah'] = None
    stats = session_stats(readings)
    # Must not crash; consumed_ah should be None
    assert stats['consumed_ah'] is None


def test_session_stats_avg_watts_computed():
    readings = discharge_session('2024-01-15T08:00:00', 4.0, 90, 80, n_readings=10,
                                  consumed_ah_start=0.0)
    stats = session_stats(readings)
    # avg_watts should be positive (battery is discharging)
    assert stats['avg_watts'] > 0


def test_session_stats_single_reading():
    # Single reading → 0 hours → no crash
    reading = [make_reading('2024-01-15T08:00:00', soc=80.0, current=-5.0)]
    stats = session_stats(reading)
    assert stats['hours'] == 0.0
    assert stats['pct_per_day'] == 0.0


def test_charging_session_stats_pct_gain():
    # 20% gain over 4 hours
    readings = charging_session('2024-01-15T08:00:00', 4.0, 50, 70, n_readings=10)
    stats = charging_session_stats(readings)
    assert abs(stats['pct_gain'] - 20.0) < 0.2


def test_charging_session_stats_pct_per_hour():
    # 20% / 4h = 5%/h
    readings = charging_session('2024-01-15T08:00:00', 4.0, 50, 70, n_readings=10)
    stats = charging_session_stats(readings)
    assert abs(stats['charge_rate_pct_per_hour'] - 5.0) < 0.1


def test_charging_session_stats_avg_amps():
    readings = charging_session('2024-01-15T08:00:00', 4.0, 50, 70, n_readings=10, amps=30.0)
    stats = charging_session_stats(readings)
    assert abs(stats['avg_amps'] - 30.0) < 0.1


def test_charging_session_stats_single_reading():
    # Single reading → 0 hours → no division by zero
    reading = [make_reading('2024-01-15T08:00:00', soc=50.0, current=30.0)]
    stats = charging_session_stats(reading)
    assert stats['charge_rate_pct_per_hour'] == 0
    assert stats['hours'] == 0.0
