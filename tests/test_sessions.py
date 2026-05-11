from factories import charging_session, discharge_session, make_reading

from victron.report import detect_charging_sessions, detect_sessions

THRESHOLD = 2.0
MAX_GAP = 4.0


def test_single_discharge_session_detected():
    readings = discharge_session('2024-01-15T08:00:00', 8.0, 90, 70, n_readings=20)
    sessions = detect_sessions(readings, THRESHOLD, MAX_GAP)
    assert len(sessions) == 1
    assert len(sessions[0]) == 20


def test_gap_larger_than_max_splits_session():
    r1 = discharge_session('2024-01-15T08:00:00', 2.0, 90, 85, n_readings=10)
    # 6-hour gap (> MAX_GAP=4)
    r2 = discharge_session('2024-01-15T16:00:00', 2.0, 80, 75, n_readings=10)
    sessions = detect_sessions(r1 + r2, THRESHOLD, MAX_GAP)
    assert len(sessions) == 2


def test_gap_smaller_than_max_does_not_split():
    r1 = discharge_session('2024-01-15T08:00:00', 2.0, 90, 85, n_readings=10)
    # 2-hour gap (< MAX_GAP=4)
    r2 = discharge_session('2024-01-15T12:00:00', 2.0, 83, 78, n_readings=10)
    sessions = detect_sessions(r1 + r2, THRESHOLD, MAX_GAP)
    assert len(sessions) == 1


def test_single_charging_blip_does_not_end_discharge():
    readings = discharge_session('2024-01-15T08:00:00', 8.0, 90, 70, n_readings=20)
    # Insert one charging reading in the middle
    blip = make_reading('2024-01-15T12:00:00', soc=80.0, current=5.0)
    mid = len(readings) // 2
    readings.insert(mid, blip)
    sessions = detect_sessions(readings, THRESHOLD, MAX_GAP)
    assert len(sessions) == 1


def test_two_consecutive_charging_readings_end_discharge():
    r1 = discharge_session('2024-01-15T08:00:00', 4.0, 90, 80, n_readings=10)
    r2 = charging_session('2024-01-15T12:00:00', 2.0, 80, 90, n_readings=5, amps=30.0)
    r3 = discharge_session('2024-01-15T14:00:00', 4.0, 88, 78, n_readings=10)
    sessions = detect_sessions(r1 + r2 + r3, THRESHOLD, MAX_GAP)
    assert len(sessions) == 2


def test_empty_readings_discharge():
    assert detect_sessions([], THRESHOLD, MAX_GAP) == []


def test_empty_readings_charging():
    assert detect_charging_sessions([], THRESHOLD, MAX_GAP) == []


def test_all_charging_gives_no_discharge_sessions():
    readings = charging_session('2024-01-15T08:00:00', 4.0, 50, 90, n_readings=10)
    discharge = detect_sessions(readings, THRESHOLD, MAX_GAP)
    assert len(discharge) == 0


def test_all_charging_gives_one_charging_session():
    readings = charging_session('2024-01-15T08:00:00', 4.0, 50, 90, n_readings=10)
    charging = detect_charging_sessions(readings, THRESHOLD, MAX_GAP)
    assert len(charging) == 1


def test_single_discharge_blip_does_not_end_charging():
    readings = charging_session('2024-01-15T08:00:00', 8.0, 50, 90, n_readings=20)
    blip = make_reading('2024-01-15T12:00:00', soc=70.0, current=-1.0)
    mid = len(readings) // 2
    readings.insert(mid, blip)
    sessions = detect_charging_sessions(readings, THRESHOLD, MAX_GAP)
    assert len(sessions) == 1


def test_sessions_preserve_all_readings():
    readings = discharge_session('2024-01-15T08:00:00', 6.0, 90, 75, n_readings=15)
    sessions = detect_sessions(readings, THRESHOLD, MAX_GAP)
    total = sum(len(s) for s in sessions)
    assert total == 15
