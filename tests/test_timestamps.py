from datetime import timezone

import pytest

from boondockers.engine import fmt_dt, midnights_in_range, parse_ts, to_local


def test_parse_ts_bare_iso():
    ts = parse_ts('2024-01-15T10:30:00')
    assert ts.year == 2024
    assert ts.month == 1
    assert ts.day == 15
    assert ts.hour == 10
    assert ts.tzinfo == timezone.utc


def test_parse_ts_z_suffix():
    ts = parse_ts('2024-01-15T10:30:00Z')
    assert ts.tzinfo == timezone.utc
    assert ts.hour == 10


def test_parse_ts_plus_suffix():
    ts = parse_ts('2024-01-15T10:30:00+00:00')
    assert ts.tzinfo == timezone.utc
    assert ts.hour == 10


def test_parse_ts_roundtrip():
    original = '2024-06-21T18:45:00'
    ts = parse_ts(original)
    assert ts.year == 2024
    assert ts.month == 6
    assert ts.day == 21
    assert ts.hour == 18
    assert ts.minute == 45


def test_to_local_returns_aware():
    ts = parse_ts('2024-01-15T18:00:00')
    local = to_local(ts)
    assert local.tzinfo is not None
    assert local.utcoffset() is not None


def test_to_local_same_instant():
    ts = parse_ts('2024-01-15T18:00:00')
    local = to_local(ts)
    # Converting back to UTC must yield the original instant
    assert local.astimezone(timezone.utc).replace(tzinfo=timezone.utc) == ts


def test_fmt_dt_12h_has_am_pm():
    ts = parse_ts('2024-01-15T17:00:00')  # some hour in UTC
    result = fmt_dt(ts, time_format='12h', include_date=False)
    assert 'AM' in result or 'PM' in result


def test_fmt_dt_24h_no_am_pm():
    ts = parse_ts('2024-01-15T17:00:00')
    result = fmt_dt(ts, time_format='24h', include_date=False)
    assert ':' in result
    assert 'AM' not in result
    assert 'PM' not in result


def test_fmt_dt_include_date():
    ts = parse_ts('2024-01-15T12:00:00')
    result = fmt_dt(ts, time_format='24h', include_date=True)
    # Should contain something that looks like a year
    assert '2024' in result


def test_midnights_in_range_empty():
    assert midnights_in_range([]) == []


def test_midnights_in_range_single_day():
    ts = parse_ts('2024-01-15T10:00:00')
    midnights = midnights_in_range([ts])
    # No midnight between start and a point on the same day (< 24h)
    for m in midnights:
        assert m.hour == 0
        assert m.minute == 0
        assert m.second == 0


def test_midnights_in_range_two_days():
    ts1 = parse_ts('2024-01-15T06:00:00')
    ts2 = parse_ts('2024-01-17T06:00:00')
    midnights = midnights_in_range([ts1, ts2])
    assert len(midnights) >= 2
    for m in midnights:
        assert m.hour == 0
        assert m.minute == 0
        assert m.second == 0


def test_midnights_in_range_ordering():
    ts1 = parse_ts('2024-01-15T06:00:00')
    ts2 = parse_ts('2024-01-20T06:00:00')
    midnights = midnights_in_range([ts1, ts2])
    for a, b in zip(midnights, midnights[1:]):
        assert a < b
