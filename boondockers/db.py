#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul Cezanne
"""
db.py — Schema management and all SQLite database I/O.

Call ensure_schema(db_path) once at startup before using any other function.
"""

import sqlite3
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Schema DDL (canonical, fully-migrated)
# ---------------------------------------------------------------------------

_READINGS_DDL = """
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
"""

# Fully-migrated schema — includes all columns that exist after all migrations.
_SESSION_NOTES_DDL = """
CREATE TABLE IF NOT EXISTS session_notes (
    session_id   TEXT PRIMARY KEY,
    session_type TEXT NOT NULL,
    note         TEXT DEFAULT '',
    charge_type  TEXT DEFAULT '',
    shore_power  INTEGER DEFAULT 0,
    flags        TEXT DEFAULT '',
    updated_at   TEXT
);
"""

_SYSTEM_DIAGNOSTICS_DDL = """
CREATE TABLE IF NOT EXISTS system_diagnostics (
    diag_id      TEXT PRIMARY KEY,
    diag_type    TEXT NOT NULL,
    detected_at  TEXT,
    period_start TEXT,
    period_end   TEXT,
    soc_drop     REAL,
    hours        REAL,
    details      TEXT
);
"""


# ---------------------------------------------------------------------------
# Schema versioning
# ---------------------------------------------------------------------------

def ensure_schema(db_path):
    """Create or migrate the database schema to the current version.

    Uses PRAGMA user_version as a migration counter:
      v0 → v1: CREATE TABLE IF NOT EXISTS for all three tables
      v1 → v2: ADD COLUMN flags to session_notes (idempotent guard)

    Safe to call on both fresh and existing databases.
    """
    conn = sqlite3.connect(db_path)
    version = conn.execute('PRAGMA user_version').fetchone()[0]

    if version < 1:
        conn.execute(_READINGS_DDL)
        conn.execute(_SESSION_NOTES_DDL)
        conn.execute(_SYSTEM_DIAGNOSTICS_DDL)
        conn.execute('PRAGMA user_version = 1')
        conn.commit()
        version = 1

    if version < 2:
        # Add flags column to existing DBs that were created before v2
        cols = {row[1] for row in conn.execute('PRAGMA table_info(session_notes)')}
        if 'flags' not in cols:
            conn.execute("ALTER TABLE session_notes ADD COLUMN flags TEXT DEFAULT ''")
        conn.execute('PRAGMA user_version = 2')
        conn.commit()

    conn.close()


# ---------------------------------------------------------------------------
# Readings
# ---------------------------------------------------------------------------

def load_readings(db_path, since=None, until=None):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    where = 'WHERE soc IS NOT NULL AND current IS NOT NULL'
    params = []
    if since:
        where += ' AND timestamp >= ?'
        params.append(since.isoformat())
    if until:
        where += ' AND timestamp <= ?'
        params.append(until.isoformat())
    rows = conn.execute(
        f'SELECT * FROM readings {where} ORDER BY timestamp',
        params,
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Charge types and session notes (report-side reads)
# ---------------------------------------------------------------------------

def load_charge_types(db_path):
    """Return {session_id: charge_type_str} for all charge sessions with a stored type."""
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT session_id, charge_type FROM session_notes WHERE session_type='charge'"
        ).fetchall()
        conn.close()
        return {r[0]: r[1] or '' for r in rows}
    except Exception:
        return {}


def load_session_notes(db_path):
    """Return {session_id: note_text} for sessions that have a non-empty note."""
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT session_id, note FROM session_notes WHERE note != '' AND note IS NOT NULL"
        ).fetchall()
        conn.close()
        return {r[0]: r[1] for r in rows}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Notes, flags, and diagnostics (dashboard reads/writes)
# ---------------------------------------------------------------------------

def load_notes(db_path):
    """Return {session_id: {'note', 'charge_type', 'shore_power', 'flags'}} for all stored notes."""
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        'SELECT session_id, note, charge_type, shore_power, flags FROM session_notes'
    ).fetchall()
    conn.close()
    return {
        r[0]: {
            'note': r[1] or '',
            'charge_type': r[2] or '',
            'shore_power': bool(r[3]),
            'flags': r[4] or '',
        }
        for r in rows
    }


def load_shore_power_sessions(db_path):
    """Return set of session_ids where shore_power = 1."""
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        'SELECT session_id FROM session_notes WHERE shore_power = 1'
    ).fetchall()
    conn.close()
    return {r[0] for r in rows}


def load_diagnostics(db_path):
    """Load persisted diagnostic state from the DB (fast path before first Refresh).

    Returns {'session_flags': {sid: [flag_str, ...]}, 'system_diags': [row_dicts]}.
    """
    conn = sqlite3.connect(db_path)
    flag_rows = conn.execute(
        "SELECT session_id, flags FROM session_notes WHERE flags IS NOT NULL AND flags != ''"
    ).fetchall()
    diag_rows = conn.execute(
        'SELECT diag_id, diag_type, period_start, period_end, soc_drop, hours FROM system_diagnostics'
    ).fetchall()
    conn.close()

    session_flags = {}
    for sid, flags_str in flag_rows:
        session_flags[sid] = [f.strip() for f in flags_str.split(',') if f.strip()]

    system_diags = [
        {'diag_id': r[0], 'diag_type': r[1], 'period_start': r[2],
         'period_end': r[3], 'soc_drop': r[4], 'hours': r[5]}
        for r in diag_rows
    ]

    return {'session_flags': session_flags, 'system_diags': system_diags}


def _save_session_flags(conn, sid, flags_str):
    """Update only the flags column for a session — does not touch note/charge_type/shore_power."""
    conn.execute(
        "INSERT OR IGNORE INTO session_notes (session_id, session_type, flags) VALUES (?, 'charge', '')",
        (sid,),
    )
    conn.execute(
        "UPDATE session_notes SET flags=?, updated_at=? WHERE session_id=?",
        (flags_str, datetime.now(timezone.utc).isoformat(), sid),
    )


def save_note(db_path, sid, session_type, note, charge_type=None, shore_power=None):
    conn = sqlite3.connect(db_path)
    # Build update dynamically based on which optional fields are provided
    if charge_type is None and shore_power is None:
        conn.execute(
            """INSERT INTO session_notes (session_id, session_type, note, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(session_id) DO UPDATE SET
                 note=excluded.note, updated_at=excluded.updated_at""",
            (sid, session_type, note, datetime.now(timezone.utc).isoformat()),
        )
    elif charge_type is not None and shore_power is None:
        conn.execute(
            """INSERT INTO session_notes (session_id, session_type, note, charge_type, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(session_id) DO UPDATE SET
                 note=excluded.note, charge_type=excluded.charge_type,
                 updated_at=excluded.updated_at""",
            (sid, session_type, note, charge_type, datetime.now(timezone.utc).isoformat()),
        )
    elif charge_type is None and shore_power is not None:
        conn.execute(
            """INSERT INTO session_notes (session_id, session_type, note, shore_power, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(session_id) DO UPDATE SET
                 note=excluded.note, shore_power=excluded.shore_power,
                 updated_at=excluded.updated_at""",
            (sid, session_type, note, int(shore_power), datetime.now(timezone.utc).isoformat()),
        )
    else:
        conn.execute(
            """INSERT INTO session_notes (session_id, session_type, note, charge_type, shore_power, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(session_id) DO UPDATE SET
                 note=excluded.note, charge_type=excluded.charge_type,
                 shore_power=excluded.shore_power, updated_at=excluded.updated_at""",
            (sid, session_type, note, charge_type, int(shore_power),
             datetime.now(timezone.utc).isoformat()),
        )
    conn.commit()
    conn.close()
