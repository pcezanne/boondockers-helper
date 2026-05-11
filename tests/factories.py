"""
Synthetic data factories for unit tests.

All timestamps are UTC-aware datetimes or UTC ISO 8601 strings,
matching the format stored in the real SQLite DB.
"""

from datetime import datetime, timedelta, timezone


def make_reading(dt, soc, current, voltage=12.8, consumed_ah=None, power_watts=None):
    """Return a single reading dict in the format load_readings() produces.

    dt: UTC datetime or ISO string.
    """
    if isinstance(dt, datetime):
        ts = dt.isoformat()
    else:
        ts = dt
    if power_watts is None and voltage is not None and current is not None:
        power_watts = round(voltage * abs(current), 2)
    return {
        'id': None,
        'timestamp': ts,
        'voltage': voltage,
        'current': current,
        'soc': soc,
        'consumed_ah': consumed_ah,
        'remaining_mins': None,
        'alarm': '',
        'power_watts': power_watts,
    }


def _parse_start(start):
    """Accept UTC datetime or ISO string; always return UTC-aware datetime."""
    if isinstance(start, datetime):
        if start.tzinfo is None:
            return start.replace(tzinfo=timezone.utc)
        return start
    dt = datetime.fromisoformat(start.rstrip('Z'))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def discharge_session(start, duration_hours, soc_start, soc_end,
                      n_readings=10, voltage=12.8, consumed_ah_start=0.0):
    """Return a list of readings forming one clean discharge session.

    Current is negative (discharging); consumed_ah increases monotonically.
    """
    start_dt = _parse_start(start)
    readings = []
    for i in range(n_readings):
        frac = i / max(n_readings - 1, 1)
        ts = start_dt + timedelta(hours=duration_hours * frac)
        soc = soc_start + (soc_end - soc_start) * frac
        # consumed_ah is cumulative Ah discharged; increases as SOC falls
        ah = consumed_ah_start + (soc_start - soc) / 100.0 * 920.0
        readings.append(make_reading(
            ts,
            soc=round(soc, 2),
            current=-5.0,
            voltage=voltage,
            consumed_ah=round(ah, 2),
        ))
    return readings


def charging_session(start, duration_hours, soc_start, soc_end,
                     n_readings=10, voltage=12.8, amps=30.0):
    """Return a list of readings forming one clean charging session.

    Current is positive (charging).
    """
    start_dt = _parse_start(start)
    readings = []
    for i in range(n_readings):
        frac = i / max(n_readings - 1, 1)
        ts = start_dt + timedelta(hours=duration_hours * frac)
        soc = soc_start + (soc_end - soc_start) * frac
        readings.append(make_reading(
            ts,
            soc=round(soc, 2),
            current=amps,
            voltage=voltage,
        ))
    return readings
