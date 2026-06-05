from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from trading_bot.monitoring.positions import parse_occ_option_symbol


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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS order_status_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                observed_at TEXT NOT NULL,
                mode TEXT NOT NULL,
                broker_order_id TEXT NOT NULL,
                client_order_id TEXT,
                symbol TEXT,
                status TEXT,
                filled_qty TEXT,
                qty TEXT,
                order_class TEXT,
                side TEXT,
                submitted_at TEXT,
                filled_at TEXT,
                canceled_at TEXT,
                expired_at TEXT,
                raw_order_json TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_order_status_events_order_id
            ON order_status_events (broker_order_id, id)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_order_status_events_mode_order_id
            ON order_status_events (mode, broker_order_id, id)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS spread_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mode TEXT NOT NULL,
                spread_id TEXT NOT NULL,
                underlying_symbol TEXT NOT NULL,
                expiration_date TEXT NOT NULL,
                short_put_symbol TEXT NOT NULL,
                long_put_symbol TEXT NOT NULL,
                short_strike TEXT NOT NULL,
                long_strike TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                entry_credit TEXT,
                max_loss TEXT,
                entry_order_id TEXT NOT NULL UNIQUE,
                entry_client_order_id TEXT,
                opened_at TEXT NOT NULL,
                close_debit TEXT,
                close_order_id TEXT,
                close_client_order_id TEXT,
                closed_at TEXT,
                status TEXT NOT NULL,
                raw_entry_order_json TEXT NOT NULL,
                raw_close_order_json TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_spread_trades_mode_status
            ON spread_trades (mode, status, opened_at)
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


def record_account_snapshot(
    db_path: Path,
    *,
    captured_at: str,
    broker: str,
    mode: str,
    payload: dict[str, Any],
) -> int:
    with closing(sqlite3.connect(db_path)) as conn:
        cursor = conn.execute(
            """
            INSERT INTO account_snapshots (captured_at, broker, mode, payload_json)
            VALUES (?, ?, ?, ?)
            """,
            (captured_at, broker, mode, json.dumps(payload, sort_keys=True)),
        )
        conn.commit()
        return int(cursor.lastrowid)


def first_account_snapshot(
    db_path: Path,
    *,
    mode: str,
    start_at: str,
    end_at: str,
) -> dict[str, Any] | None:
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            """
            SELECT captured_at, broker, mode, payload_json
            FROM account_snapshots
            WHERE mode = ? AND captured_at >= ? AND captured_at < ?
            ORDER BY captured_at ASC, id ASC
            LIMIT 1
            """,
            (mode, start_at, end_at),
        ).fetchone()
    if row is None:
        return None
    return {
        "captured_at": row[0],
        "broker": row[1],
        "mode": row[2],
        "payload": json.loads(row[3]),
    }


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


def record_order_status_changes(
    db_path: Path,
    *,
    observed_at: str,
    mode: str,
    orders: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    with closing(sqlite3.connect(db_path)) as conn:
        for order in orders:
            broker_order_id = str(order.get("id") or "")
            if not broker_order_id:
                continue

            status = _string_or_none(order.get("status"))
            filled_qty = _string_or_none(order.get("filled_qty"))
            previous = conn.execute(
                """
                SELECT status, filled_qty
                FROM order_status_events
                WHERE mode = ? AND broker_order_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (mode, broker_order_id),
            ).fetchone()
            previous_status = previous[0] if previous else None
            previous_filled_qty = previous[1] if previous else None
            if previous and previous_status == status and previous_filled_qty == filled_qty:
                if status == "filled":
                    _record_spread_trade_fill(conn, mode=mode, order=order, observed_at=observed_at)
                continue

            cursor = conn.execute(
                """
                INSERT INTO order_status_events (
                    observed_at,
                    mode,
                    broker_order_id,
                    client_order_id,
                    symbol,
                    status,
                    filled_qty,
                    qty,
                    order_class,
                    side,
                    submitted_at,
                    filled_at,
                    canceled_at,
                    expired_at,
                    raw_order_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observed_at,
                    mode,
                    broker_order_id,
                    _string_or_none(order.get("client_order_id")),
                    _string_or_none(order.get("symbol")),
                    status,
                    filled_qty,
                    _string_or_none(order.get("qty")),
                    _string_or_none(order.get("order_class")),
                    _string_or_none(order.get("side")),
                    _string_or_none(order.get("submitted_at")),
                    _string_or_none(order.get("filled_at")),
                    _string_or_none(order.get("canceled_at")),
                    _string_or_none(order.get("expired_at")),
                    json.dumps(order, sort_keys=True),
                ),
            )
            change = {
                "event_id": int(cursor.lastrowid),
                "observed_at": observed_at,
                "mode": mode,
                "broker_order_id": broker_order_id,
                "client_order_id": _string_or_none(order.get("client_order_id")),
                "symbol": _string_or_none(order.get("symbol")),
                "status": status,
                "previous_status": previous_status,
                "filled_qty": filled_qty,
                "previous_filled_qty": previous_filled_qty,
                "qty": _string_or_none(order.get("qty")),
                "order_class": _string_or_none(order.get("order_class")),
                "side": _string_or_none(order.get("side")),
                "submitted_at": _string_or_none(order.get("submitted_at")),
                "filled_at": _string_or_none(order.get("filled_at")),
                "canceled_at": _string_or_none(order.get("canceled_at")),
                "expired_at": _string_or_none(order.get("expired_at")),
                "raw_order": order,
            }
            changes.append(change)
            if status == "filled":
                _record_spread_trade_fill(conn, mode=mode, order=order, observed_at=observed_at)
        conn.commit()
    return changes


def summarize_order_status_events(
    db_path: Path,
    *,
    mode: str,
    start_at: str,
    end_at: str,
    recent_limit: int = 20,
) -> dict[str, Any]:
    with closing(sqlite3.connect(db_path)) as conn:
        total = conn.execute(
            """
            SELECT COUNT(*)
            FROM order_status_events
            WHERE mode = ? AND observed_at >= ? AND observed_at < ?
            """,
            (mode, start_at, end_at),
        ).fetchone()[0]
        by_status = {
            str(status or "unknown"): int(count)
            for status, count in conn.execute(
                """
                SELECT status, COUNT(*)
                FROM order_status_events
                WHERE mode = ? AND observed_at >= ? AND observed_at < ?
                GROUP BY status
                ORDER BY status
                """,
                (mode, start_at, end_at),
            ).fetchall()
        }
        recent = [
            {
                "observed_at": row[0],
                "broker_order_id": row[1],
                "client_order_id": row[2],
                "symbol": row[3],
                "status": row[4],
                "filled_qty": row[5],
                "qty": row[6],
                "order_class": row[7],
            }
            for row in conn.execute(
                """
                SELECT
                    observed_at,
                    broker_order_id,
                    client_order_id,
                    symbol,
                    status,
                    filled_qty,
                    qty,
                    order_class
                FROM order_status_events
                WHERE mode = ? AND observed_at >= ? AND observed_at < ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (mode, start_at, end_at, recent_limit),
            ).fetchall()
        ]
    return {
        "total": int(total),
        "by_status": by_status,
        "recent": recent,
    }


def summarize_execution_attempts(
    db_path: Path,
    *,
    mode: str,
    start_at: str,
    end_at: str,
    recent_limit: int = 20,
) -> dict[str, Any]:
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(requested), 0), COALESCE(SUM(submitted), 0)
            FROM execution_attempts
            WHERE mode = ? AND created_at >= ? AND created_at < ?
            """,
            (mode, start_at, end_at),
        ).fetchone()
        by_status = {
            str(status or "unknown"): int(count)
            for status, count in conn.execute(
                """
                SELECT status, COUNT(*)
                FROM execution_attempts
                WHERE mode = ? AND created_at >= ? AND created_at < ?
                GROUP BY status
                ORDER BY status
                """,
                (mode, start_at, end_at),
            ).fetchall()
        }
        recent = [
            {
                "created_at": item[0],
                "requested": bool(item[1]),
                "submitted": bool(item[2]),
                "status": item[3],
                "broker_error": item[4],
                "block_reasons": json.loads(item[5]),
            }
            for item in conn.execute(
                """
                SELECT
                    created_at,
                    requested,
                    submitted,
                    status,
                    broker_error,
                    block_reasons_json
                FROM execution_attempts
                WHERE mode = ? AND created_at >= ? AND created_at < ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (mode, start_at, end_at, recent_limit),
            ).fetchall()
        ]
    return {
        "total": int(row[0]),
        "requested": int(row[1]),
        "submitted": int(row[2]),
        "by_status": by_status,
        "recent": recent,
    }


def count_submitted_open_attempts(
    db_path: Path,
    *,
    mode: str,
    start_at: str,
    end_at: str,
) -> int:
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute(
            """
            SELECT order_preview_json, order_payload_json
            FROM execution_attempts
            WHERE mode = ? AND created_at >= ? AND created_at < ? AND submitted = 1
            """,
            (mode, start_at, end_at),
        ).fetchall()
    return sum(1 for preview_json, payload_json in rows if _attempt_json_is_open(preview_json, payload_json))


def list_spread_trades(
    db_path: Path,
    *,
    mode: str,
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    where_status = "AND status = ?" if status else ""
    params: tuple[Any, ...] = (mode, status, limit) if status else (mode, limit)
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute(
            f"""
            SELECT
                id,
                mode,
                spread_id,
                underlying_symbol,
                expiration_date,
                short_put_symbol,
                long_put_symbol,
                short_strike,
                long_strike,
                quantity,
                entry_credit,
                max_loss,
                entry_order_id,
                entry_client_order_id,
                opened_at,
                close_debit,
                close_order_id,
                close_client_order_id,
                closed_at,
                status
            FROM spread_trades
            WHERE mode = ? {where_status}
            ORDER BY opened_at DESC, id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [
        {
            "id": row[0],
            "mode": row[1],
            "spread_id": row[2],
            "underlying_symbol": row[3],
            "expiration_date": row[4],
            "short_put_symbol": row[5],
            "long_put_symbol": row[6],
            "short_strike": row[7],
            "long_strike": row[8],
            "quantity": row[9],
            "entry_credit": row[10],
            "max_loss": row[11],
            "entry_order_id": row[12],
            "entry_client_order_id": row[13],
            "opened_at": row[14],
            "close_debit": row[15],
            "close_order_id": row[16],
            "close_client_order_id": row[17],
            "closed_at": row[18],
            "status": row[19],
        }
        for row in rows
    ]


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


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


def _record_spread_trade_fill(
    conn: sqlite3.Connection,
    *,
    mode: str,
    order: dict[str, Any],
    observed_at: str,
) -> None:
    payload = _spread_order_payload(order)
    if payload is None:
        return
    if payload["kind"] == "open":
        _insert_open_spread_trade(conn, mode=mode, order=order, observed_at=observed_at, payload=payload)
    elif payload["kind"] == "close":
        _mark_spread_trade_closed(conn, mode=mode, order=order, observed_at=observed_at, payload=payload)


def _insert_open_spread_trade(
    conn: sqlite3.Connection,
    *,
    mode: str,
    order: dict[str, Any],
    observed_at: str,
    payload: dict[str, Any],
) -> None:
    entry_credit = _order_credit_or_debit(order, payload)
    width = payload["short_strike"] - payload["long_strike"]
    max_loss = None
    if entry_credit is not None:
        max_loss = max(Decimal("0"), width - entry_credit) * Decimal("100") * Decimal(payload["quantity"])

    conn.execute(
        """
        INSERT OR IGNORE INTO spread_trades (
            mode,
            spread_id,
            underlying_symbol,
            expiration_date,
            short_put_symbol,
            long_put_symbol,
            short_strike,
            long_strike,
            quantity,
            entry_credit,
            max_loss,
            entry_order_id,
            entry_client_order_id,
            opened_at,
            status,
            raw_entry_order_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            mode,
            payload["spread_id"],
            payload["underlying_symbol"],
            payload["expiration_date"],
            payload["short_put_symbol"],
            payload["long_put_symbol"],
            _fmt_decimal(payload["short_strike"]),
            _fmt_decimal(payload["long_strike"]),
            payload["quantity"],
            _fmt_optional_decimal(entry_credit),
            _fmt_optional_decimal(max_loss),
            str(order.get("id")),
            _string_or_none(order.get("client_order_id")),
            _string_or_none(order.get("filled_at")) or observed_at,
            "open",
            json.dumps(order, sort_keys=True),
        ),
    )


def _mark_spread_trade_closed(
    conn: sqlite3.Connection,
    *,
    mode: str,
    order: dict[str, Any],
    observed_at: str,
    payload: dict[str, Any],
) -> None:
    close_debit = _order_credit_or_debit(order, payload)
    conn.execute(
        """
        UPDATE spread_trades
        SET
            close_debit = ?,
            close_order_id = ?,
            close_client_order_id = ?,
            closed_at = ?,
            status = ?,
            raw_close_order_json = ?
        WHERE id = (
            SELECT id
            FROM spread_trades
            WHERE
                mode = ?
                AND status = 'open'
                AND short_put_symbol = ?
                AND long_put_symbol = ?
            ORDER BY opened_at ASC, id ASC
            LIMIT 1
        )
        """,
        (
            _fmt_optional_decimal(close_debit),
            str(order.get("id")),
            _string_or_none(order.get("client_order_id")),
            _string_or_none(order.get("filled_at")) or observed_at,
            "closed",
            json.dumps(order, sort_keys=True),
            mode,
            payload["short_put_symbol"],
            payload["long_put_symbol"],
        ),
    )


def _spread_order_payload(order: dict[str, Any]) -> dict[str, Any] | None:
    legs = order.get("legs")
    if not isinstance(legs, list):
        return None
    parsed_legs = []
    for leg in legs:
        if not isinstance(leg, dict):
            continue
        parsed = parse_occ_option_symbol(str(leg.get("symbol") or ""))
        if parsed is None or parsed.option_type != "put":
            continue
        parsed_legs.append((leg, parsed))

    open_short = _find_leg(parsed_legs, "sell_to_open")
    open_long = _find_leg(parsed_legs, "buy_to_open")
    close_short = _find_leg(parsed_legs, "buy_to_close")
    close_long = _find_leg(parsed_legs, "sell_to_close")
    if open_short and open_long:
        kind = "open"
        short_leg, short_parsed = open_short
        long_leg, long_parsed = open_long
    elif close_short and close_long:
        kind = "close"
        short_leg, short_parsed = close_short
        long_leg, long_parsed = close_long
    else:
        return None

    if short_parsed.underlying_symbol != long_parsed.underlying_symbol:
        return None
    if short_parsed.expiration_date != long_parsed.expiration_date:
        return None
    if short_parsed.strike <= long_parsed.strike:
        return None
    quantity = _decimal_or_none(order.get("filled_qty")) or _decimal_or_none(order.get("qty")) or Decimal("0")
    quantity_int = int(abs(quantity))
    if quantity_int <= 0:
        return None

    spread_id = (
        f"{short_parsed.underlying_symbol}-{short_parsed.expiration_date.isoformat()}-"
        f"{_fmt_decimal(short_parsed.strike)}P-{_fmt_decimal(long_parsed.strike)}P"
    )
    return {
        "kind": kind,
        "spread_id": spread_id,
        "underlying_symbol": short_parsed.underlying_symbol,
        "expiration_date": short_parsed.expiration_date.isoformat(),
        "short_put_symbol": short_parsed.symbol,
        "long_put_symbol": long_parsed.symbol,
        "short_strike": short_parsed.strike,
        "long_strike": long_parsed.strike,
        "quantity": quantity_int,
        "short_leg": short_leg,
        "long_leg": long_leg,
    }


def _find_leg(
    parsed_legs: list[tuple[dict[str, Any], Any]],
    position_intent: str,
) -> tuple[dict[str, Any], Any] | None:
    for leg, parsed in parsed_legs:
        if str(leg.get("position_intent") or "") == position_intent:
            return leg, parsed
    return None


def _order_credit_or_debit(order: dict[str, Any], payload: dict[str, Any]) -> Decimal | None:
    parent_price = _decimal_or_none(order.get("filled_avg_price"))
    if parent_price is not None:
        return abs(parent_price)
    short_price = _decimal_or_none(payload["short_leg"].get("filled_avg_price"))
    long_price = _decimal_or_none(payload["long_leg"].get("filled_avg_price"))
    if short_price is None or long_price is None:
        return None
    return abs(short_price - long_price)


def _attempt_json_is_open(preview_json: str, payload_json: str) -> bool:
    preview = _loads_json(preview_json)
    if isinstance(preview, dict) and preview.get("kind") == "alpaca_mleg_order_preview":
        return True
    payload = _loads_json(payload_json)
    if not isinstance(payload, dict):
        return False
    legs = payload.get("legs")
    if not isinstance(legs, list):
        return False
    intents = {str(leg.get("position_intent") or "") for leg in legs if isinstance(leg, dict)}
    return bool({"sell_to_open", "buy_to_open"} & intents)


def _loads_json(value: str) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _fmt_optional_decimal(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return _fmt_decimal(value)


def _fmt_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")
