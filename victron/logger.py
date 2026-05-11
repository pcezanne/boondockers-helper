#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul Cezanne
"""
logger.py — Poll Victron BMV-712 Smart via BLE and log readings to SQLite.

Usage:
    python3 logger.py           # runs continuously (Ctrl-C to stop)
    python3 logger.py --once    # single poll then exit (for testing)
"""

import argparse
import configparser
import json
import logging
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Derive victron-ble path from the running Python interpreter so launchd
# (which has a minimal PATH) can find it without any PATH configuration.
VICTRON_BLE = str(Path(sys.executable).parent / 'victron-ble')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent.parent / 'config.ini'

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp      TEXT    NOT NULL,
    voltage        REAL,
    current        REAL,
    soc            REAL,
    consumed_ah    REAL,
    remaining_mins INTEGER,
    alarm          TEXT,
    power_watts    REAL
);

CREATE TABLE IF NOT EXISTS session_notes (
    session_id   TEXT PRIMARY KEY,
    session_type TEXT NOT NULL,
    note         TEXT DEFAULT '',
    charge_type  TEXT DEFAULT '',
    shore_power  INTEGER DEFAULT 0,
    updated_at   TEXT
);
"""


def load_config():
    cfg = configparser.ConfigParser()
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f'Config file not found: {CONFIG_PATH}')
    cfg.read(CONFIG_PATH)
    return cfg


def init_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    return conn


def read_device(uuid, key, timeout=30):
    """
    Call `victron-ble read` and return the first valid JSON payload dict, or None.
    The CLI streams one JSON line per BLE advertisement; we take the first and exit.
    """
    target = f"{uuid}@{key}"
    proc = None
    try:
        proc = subprocess.Popen(
            [VICTRON_BLE, 'read', target],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + timeout
        for line in proc.stdout:
            line = line.strip()
            if line.startswith('{'):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
            if time.monotonic() > deadline:
                log.warning('BLE read timed out after %ds', timeout)
                break
    except FileNotFoundError:
        log.error('victron-ble not found at %s — run: pip install victron-ble', VICTRON_BLE)
        raise
    finally:
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                pass
    return None


def insert_reading(conn, data):
    payload = data.get('payload', {})
    voltage = payload.get('voltage')
    current = payload.get('current')
    power_watts = None
    if voltage is not None and current is not None:
        power_watts = round(voltage * abs(current), 2)
    conn.execute(
        """INSERT INTO readings
               (timestamp, voltage, current, soc, consumed_ah, remaining_mins, alarm, power_watts)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            datetime.now(timezone.utc).isoformat(),
            voltage,
            current,
            payload.get('soc'),
            payload.get('consumed_ah'),
            payload.get('remaining_mins'),
            str(payload.get('alarm', '')),
            power_watts,
        ),
    )
    conn.commit()


def main():
    parser = argparse.ArgumentParser(description='Victron BMV-712 BLE data logger')
    parser.add_argument('--once', action='store_true', help='Poll once and exit (for testing)')
    args = parser.parse_args()

    cfg = load_config()
    uuid = cfg['device']['uuid']
    key = cfg['device']['key']
    poll_minutes = cfg.getfloat('logging', 'poll_interval_minutes', fallback=15)
    db_path = cfg.get('logging', 'db_path', fallback='victron_data.db')

    conn = init_db(db_path)
    log.info('Logging to %s  poll every %g min', db_path, poll_minutes)

    try:
        while True:
            log.info('Reading device...')
            data = read_device(uuid, key)
            if data:
                insert_reading(conn, data)
                p = data.get('payload', {})
                log.info(
                    'Logged: %.2fV  %.3fA  SOC=%.1f%%  power=%.0fW',
                    p.get('voltage', 0),
                    p.get('current', 0),
                    p.get('soc', 0),
                    abs((p.get('voltage') or 0) * (p.get('current') or 0)),
                )
            else:
                log.warning('No data received — device out of range?')

            if args.once:
                break
            time.sleep(poll_minutes * 60)
    except KeyboardInterrupt:
        log.info('Stopped.')
    finally:
        conn.close()


if __name__ == '__main__':
    main()
