from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                mode TEXT NOT NULL,
                status TEXT NOT NULL,
                details_json TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS account_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_at TEXT NOT NULL,
                broker TEXT NOT NULL,
                mode TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            """
        )
        conn.commit()


def record_bot_run(
    db_path: Path,
    *,
    started_at: str,
    mode: str,
    status: str,
    details: dict[str, Any],
) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO bot_runs (started_at, mode, status, details_json)
            VALUES (?, ?, ?, ?)
            """,
            (started_at, mode, status, json.dumps(details, sort_keys=True)),
        )
        conn.commit()

