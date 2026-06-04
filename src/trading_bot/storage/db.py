from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db_path)) as conn:
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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS llm_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                mode TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                packet_json TEXT NOT NULL,
                response_json TEXT NOT NULL,
                raw_response_json TEXT NOT NULL,
                validator_errors_json TEXT NOT NULL,
                accepted INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS execution_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                mode TEXT NOT NULL,
                decision_id INTEGER,
                requested INTEGER NOT NULL,
                submitted INTEGER NOT NULL,
                status TEXT NOT NULL,
                order_preview_json TEXT NOT NULL,
                order_payload_json TEXT NOT NULL,
                broker_response_json TEXT NOT NULL,
                broker_error TEXT,
                block_reasons_json TEXT NOT NULL,
                FOREIGN KEY (decision_id) REFERENCES llm_decisions(id)
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
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO bot_runs (started_at, mode, status, details_json)
            VALUES (?, ?, ?, ?)
            """,
            (started_at, mode, status, json.dumps(details, sort_keys=True)),
        )
        conn.commit()


def record_option_scan(db_path: Path, *, mode: str, scan_result: Any) -> int:
    with closing(sqlite3.connect(db_path)) as conn:
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


def record_llm_decision(
    db_path: Path,
    *,
    created_at: str,
    mode: str,
    provider: str,
    model: str,
    prompt_version: str,
    packet: dict[str, Any],
    response: dict[str, Any],
    raw_response: dict[str, Any],
    validator_errors: list[str],
) -> int:
    with closing(sqlite3.connect(db_path)) as conn:
        cursor = conn.execute(
            """
            INSERT INTO llm_decisions (
                created_at,
                mode,
                provider,
                model,
                prompt_version,
                packet_json,
                response_json,
                raw_response_json,
                validator_errors_json,
                accepted
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                created_at,
                mode,
                provider,
                model,
                prompt_version,
                json.dumps(packet, sort_keys=True),
                json.dumps(response, sort_keys=True),
                json.dumps(raw_response, sort_keys=True),
                json.dumps(validator_errors, sort_keys=True),
                0 if validator_errors else 1,
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)


def record_execution_attempt(
    db_path: Path,
    *,
    created_at: str,
    mode: str,
    decision_id: int | None,
    attempt: Any,
) -> int:
    attempt_dict = attempt.to_dict() if hasattr(attempt, "to_dict") else dict(attempt)
    with closing(sqlite3.connect(db_path)) as conn:
        cursor = conn.execute(
            """
            INSERT INTO execution_attempts (
                created_at,
                mode,
                decision_id,
                requested,
                submitted,
                status,
                order_preview_json,
                order_payload_json,
                broker_response_json,
                broker_error,
                block_reasons_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                created_at,
                mode,
                decision_id,
                1 if attempt_dict.get("requested") else 0,
                1 if attempt_dict.get("submitted") else 0,
                str(attempt_dict.get("status")),
                json.dumps(attempt_dict.get("order_preview"), sort_keys=True),
                json.dumps(attempt_dict.get("order_payload"), sort_keys=True),
                json.dumps(attempt_dict.get("broker_response"), sort_keys=True),
                attempt_dict.get("broker_error"),
                json.dumps(attempt_dict.get("block_reasons", []), sort_keys=True),
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)
