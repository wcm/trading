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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS option_scan_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scanned_at TEXT NOT NULL,
                mode TEXT NOT NULL,
                symbols_json TEXT NOT NULL,
                feed TEXT NOT NULL,
                stock_feed TEXT NOT NULL,
                contracts_seen INTEGER NOT NULL,
                snapshots_seen INTEGER NOT NULL,
                candidate_count INTEGER NOT NULL,
                warnings_json TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS option_scan_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_run_id INTEGER NOT NULL,
                candidate_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                FOREIGN KEY (scan_run_id) REFERENCES option_scan_runs(id)
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


def record_option_scan(db_path: Path, *, mode: str, scan_result: Any) -> int:
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO option_scan_runs (
                scanned_at,
                mode,
                symbols_json,
                feed,
                stock_feed,
                contracts_seen,
                snapshots_seen,
                candidate_count,
                warnings_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scan_result.scanned_at,
                mode,
                json.dumps(scan_result.symbols),
                scan_result.feed,
                scan_result.stock_feed,
                scan_result.contracts_seen,
                scan_result.snapshots_seen,
                len(scan_result.candidates),
                json.dumps(scan_result.warnings),
            ),
        )
        scan_run_id = int(cursor.lastrowid)
        conn.executemany(
            """
            INSERT INTO option_scan_candidates (scan_run_id, candidate_id, payload_json)
            VALUES (?, ?, ?)
            """,
            [
                (
                    scan_run_id,
                    candidate.candidate_id,
                    json.dumps(candidate.to_dict(), sort_keys=True),
                )
                for candidate in scan_result.candidates
            ],
        )
        conn.commit()
        return scan_run_id
