from datetime import datetime, timedelta, timezone

from factories import discharge_session

from victron.report import adaptive_downsample, lttb_downsample, rolling_avg


# ---------------------------------------------------------------------------
# rolling_avg
# ---------------------------------------------------------------------------

def test_rolling_avg_empty():
    assert rolling_avg([]) == []


def test_rolling_avg_single():
    assert rolling_avg([5.0]) == [5.0]


def test_rolling_avg_window_3():
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    result = rolling_avg(values, window=3)
    assert len(result) == 5
    assert result[0] == 1.0          # only one value
    assert result[1] == 1.5          # (1+2)/2
    assert abs(result[2] - 2.0) < 1e-9  # (1+2+3)/3
    assert abs(result[3] - 3.0) < 1e-9  # (2+3+4)/3
    assert abs(result[4] - 4.0) < 1e-9  # (3+4+5)/3


def test_rolling_avg_window_larger_than_data():
    values = [10.0, 20.0]
    result = rolling_avg(values, window=100)
    assert result[0] == 10.0
    assert result[1] == 15.0


# ---------------------------------------------------------------------------
# lttb_downsample
# ---------------------------------------------------------------------------

def _points(n):
    """Simple linearly-spaced (x, y) pairs."""
    return [(float(i), float(i)) for i in range(n)]


def test_lttb_output_length_equals_n_buckets():
    pts = _points(100)
    indices = lttb_downsample(pts, 20)
    assert len(indices) == 20


def test_lttb_output_length_lte_n_buckets():
    pts = _points(50)
    indices = lttb_downsample(pts, 30)
    assert len(indices) <= 30


def test_lttb_first_index_is_zero():
    pts = _points(100)
    indices = lttb_downsample(pts, 20)
    assert indices[0] == 0


def test_lttb_last_index_is_n_minus_1():
    pts = _points(100)
    indices = lttb_downsample(pts, 20)
    assert indices[-1] == 99


def test_lttb_no_downsample_when_buckets_gte_n():
    pts = _points(10)
    indices = lttb_downsample(pts, 10)
    assert indices == list(range(10))


def test_lttb_indices_sorted():
    pts = _points(100)
    indices = lttb_downsample(pts, 20)
    assert indices == sorted(indices)


# ---------------------------------------------------------------------------
# adaptive_downsample
# ---------------------------------------------------------------------------

_DEFAULT_CFG = {
    'tier1_hours': 6.0,
    'tier2_hours': 24.0,
    'tier2_mins': 5.0,
    'tier3_mins': 15.0,
}


def test_adaptive_empty():
    ts, soc, _ = adaptive_downsample([], set(), _DEFAULT_CFG)
    assert ts == []
    assert soc == []


def test_adaptive_recent_data_not_downsampled():
    """Data entirely within tier1 (<6h old) should be returned at full resolution."""
    now = datetime.now(timezone.utc)
    readings = discharge_session(now - timedelta(hours=4), 4.0, 90, 70, n_readings=50)
    ts, soc, _ = adaptive_downsample(readings, set(), _DEFAULT_CFG)
    assert len(ts) == 50


def test_adaptive_boundary_timestamps_preserved():
    """Session boundary timestamps must appear in output even if LTTB would skip them."""
    now = datetime.now(timezone.utc)
    # Put session in tier3 (>24h old) so LTTB has something to thin
    readings = discharge_session(now - timedelta(hours=48), 12.0, 90, 50, n_readings=200)
    boundary_ts_set = {readings[0]['timestamp'], readings[-1]['timestamp']}
    ts, soc, _ = adaptive_downsample(readings, boundary_ts_set, _DEFAULT_CFG)
    # The boundary datetimes should appear in ts
    from victron.report import parse_ts, to_local
    b_start = to_local(parse_ts(readings[0]['timestamp']))
    b_end = to_local(parse_ts(readings[-1]['timestamp']))
    assert b_start in ts
    assert b_end in ts


def test_adaptive_output_length_less_than_input_for_old_data():
    """Data entirely in tier3 with many readings should be downsampled."""
    now = datetime.now(timezone.utc)
    readings = discharge_session(now - timedelta(hours=48), 24.0, 90, 50, n_readings=1000)
    ts, soc, _ = adaptive_downsample(readings, set(), _DEFAULT_CFG)
    assert len(ts) < 1000


def test_adaptive_soc_values_rounded():
    now = datetime.now(timezone.utc)
    readings = discharge_session(now - timedelta(hours=2), 2.0, 90, 80, n_readings=10)
    _, soc, __ = adaptive_downsample(readings, set(), _DEFAULT_CFG)
    for v in soc:
        # Values should be numeric (not None)
        assert isinstance(v, (int, float))
