from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Iterator

from conductor.domain.models import ExposureType, VirtualTarget


@dataclass(frozen=True, slots=True)
class SnapshotDecision:
    status: str
    reason: str
    strategy_id: str
    book_id: str
    revision: int
    event_id: str

    @property
    def accepted(self) -> bool:
        return self.status == "accepted"


class ConductorLedger:
    """Durable desired-state, economic-ownership, and append-only event ledger."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    state TEXT NOT NULL,
                    reconciled INTEGER NOT NULL DEFAULT 0,
                    details_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS intent_events (
                    source TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    strategy_id TEXT NOT NULL,
                    book_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (source, event_id)
                );

                CREATE TABLE IF NOT EXISTS desired_books (
                    strategy_id TEXT NOT NULL,
                    book_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    event_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    valid_until TEXT,
                    payload_json TEXT NOT NULL,
                    accepted_at TEXT NOT NULL,
                    PRIMARY KEY (strategy_id, book_id)
                );
                """
            )
            self._migrate_virtual_table(conn, "virtual_targets", targets=True)
            self._migrate_virtual_table(conn, "virtual_positions", targets=False)

    @staticmethod
    def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
        return (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            is not None
        )

    def _migrate_virtual_table(
        self,
        conn: sqlite3.Connection,
        table: str,
        *,
        targets: bool,
    ) -> None:
        if targets:
            create_sql = """
                CREATE TABLE {table} (
                    strategy_id TEXT NOT NULL,
                    book_id TEXT NOT NULL DEFAULT 'main',
                    sleeve_id TEXT NOT NULL,
                    instrument TEXT NOT NULL,
                    target TEXT NOT NULL,
                    notional TEXT NOT NULL DEFAULT '0',
                    exposure_type TEXT NOT NULL,
                    source_exposure_type TEXT NOT NULL DEFAULT 'quantity',
                    lot_size TEXT NOT NULL DEFAULT '1',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (strategy_id, book_id, instrument)
                )
            """
        else:
            create_sql = """
                CREATE TABLE {table} (
                    strategy_id TEXT NOT NULL,
                    book_id TEXT NOT NULL DEFAULT 'main',
                    sleeve_id TEXT NOT NULL,
                    instrument TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    notional TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (strategy_id, book_id, instrument)
                )
            """

        if not self._table_exists(conn, table):
            conn.execute(create_sql.format(table=table))
            return

        info = conn.execute(f"PRAGMA table_info({table})").fetchall()
        columns = {row[1] for row in info}
        pk_columns = tuple(row[1] for row in sorted(info, key=lambda row: row[5]) if row[5] > 0)
        if "book_id" in columns and pk_columns == ("strategy_id", "book_id", "instrument"):
            return

        legacy = f"{table}_v02_legacy"
        conn.execute(f"ALTER TABLE {table} RENAME TO {legacy}")
        conn.execute(create_sql.format(table=table))
        legacy_columns = {row[1] for row in conn.execute(f"PRAGMA table_info({legacy})")}

        if targets:
            select_notional = "notional" if "notional" in legacy_columns else "'0'"
            select_source = (
                "source_exposure_type"
                if "source_exposure_type" in legacy_columns
                else "'quantity'"
            )
            select_lot = "lot_size" if "lot_size" in legacy_columns else "'1'"
            conn.execute(
                f"""
                INSERT INTO {table}
                    (strategy_id, book_id, sleeve_id, instrument, target, notional,
                     exposure_type, source_exposure_type, lot_size, updated_at)
                SELECT strategy_id, 'main', sleeve_id, instrument, target,
                       {select_notional}, exposure_type, {select_source}, {select_lot}, updated_at
                FROM {legacy}
                """
            )
        else:
            conn.execute(
                f"""
                INSERT INTO {table}
                    (strategy_id, book_id, sleeve_id, instrument, quantity, notional, updated_at)
                SELECT strategy_id, 'main', sleeve_id, instrument, quantity, notional, updated_at
                FROM {legacy}
                """
            )
        conn.execute(f"DROP TABLE {legacy}")

    def record_snapshot(
        self,
        *,
        source: str,
        event_id: str,
        payload_hash: str,
        event_type: str,
        strategy_id: str,
        book_id: str,
        revision: int,
        as_of: str,
        valid_until: str | None,
        payload_json: str,
        prevalidated: bool,
        rejection_reason: str = "",
    ) -> SnapshotDecision:
        """Atomically dedupe, order, persist, and replace one desired book snapshot."""
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            existing_event = conn.execute(
                "SELECT payload_hash FROM intent_events WHERE source=? AND event_id=?",
                (source, event_id),
            ).fetchone()
            if existing_event is not None:
                if existing_event["payload_hash"] == payload_hash:
                    decision = SnapshotDecision(
                        "duplicate", "duplicate_event", strategy_id, book_id, revision, event_id
                    )
                else:
                    decision = SnapshotDecision(
                        "rejected",
                        "event_id_payload_collision",
                        strategy_id,
                        book_id,
                        revision,
                        event_id,
                    )
                self._append_event_on_conn(conn, "snapshot_decision", asdict(decision), now)
                return decision

            status = "accepted"
            reason = "accepted_new_revision"
            if not prevalidated:
                status = "rejected"
                reason = rejection_reason or "profile_validation_failed"
            else:
                current = conn.execute(
                    """
                    SELECT revision, event_type
                    FROM desired_books
                    WHERE strategy_id=? AND book_id=?
                    """,
                    (strategy_id, book_id),
                ).fetchone()
                if current is not None:
                    current_revision = int(current["revision"])
                    if event_type != current["event_type"]:
                        status, reason = "rejected", "book_type_conflict"
                    elif revision < current_revision:
                        status, reason = "rejected", "stale_revision"
                    elif revision == current_revision:
                        status, reason = "rejected", "revision_conflict"

            conn.execute(
                """
                INSERT INTO intent_events
                    (source, event_id, payload_hash, event_type, strategy_id, book_id,
                     revision, status, reason, received_at, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source,
                    event_id,
                    payload_hash,
                    event_type,
                    strategy_id,
                    book_id,
                    revision,
                    status,
                    reason,
                    now,
                    payload_json,
                ),
            )

            if status == "accepted":
                conn.execute(
                    """
                    INSERT INTO desired_books
                        (strategy_id, book_id, revision, event_id, source, event_type,
                         payload_hash, as_of, valid_until, payload_json, accepted_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(strategy_id, book_id) DO UPDATE SET
                        revision=excluded.revision,
                        event_id=excluded.event_id,
                        source=excluded.source,
                        event_type=excluded.event_type,
                        payload_hash=excluded.payload_hash,
                        as_of=excluded.as_of,
                        valid_until=excluded.valid_until,
                        payload_json=excluded.payload_json,
                        accepted_at=excluded.accepted_at
                    """,
                    (
                        strategy_id,
                        book_id,
                        revision,
                        event_id,
                        source,
                        event_type,
                        payload_hash,
                        as_of,
                        valid_until,
                        payload_json,
                        now,
                    ),
                )

            decision = SnapshotDecision(status, reason, strategy_id, book_id, revision, event_id)
            self._append_event_on_conn(conn, "snapshot_decision", asdict(decision), now)
            return decision

    def desired_book_payloads(self, *, event_type: str | None = None) -> list[str]:
        sql = "SELECT payload_json FROM desired_books"
        params: tuple[str, ...] = ()
        if event_type is not None:
            sql += " WHERE event_type=?"
            params = (event_type,)
        sql += " ORDER BY strategy_id, book_id"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [row["payload_json"] for row in rows]

    def desired_books(self) -> list[dict[str, str | int | None]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT strategy_id, book_id, revision, event_id, source, event_type,
                       as_of, valid_until, accepted_at
                FROM desired_books ORDER BY strategy_id, book_id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def intent_events(self) -> list[dict[str, str | int]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT source, event_id, event_type, strategy_id, book_id, revision,
                       status, reason, received_at
                FROM intent_events ORDER BY received_at, source, event_id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def replace_virtual_targets(self, targets: Iterable[VirtualTarget]) -> None:
        rows = list(targets)
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute("DELETE FROM virtual_targets")
            conn.executemany(
                """
                INSERT INTO virtual_targets
                    (strategy_id, book_id, sleeve_id, instrument, target, notional,
                     exposure_type, source_exposure_type, lot_size, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        t.strategy_id,
                        t.book_id,
                        t.sleeve_id,
                        t.instrument,
                        str(t.target),
                        str(t.notional),
                        t.exposure_type.value,
                        t.source_exposure_type.value,
                        str(t.lot_size),
                        now,
                    )
                    for t in rows
                ],
            )
            self._append_event_on_conn(conn, "virtual_targets_replaced", {"count": len(rows)}, now)

    def virtual_targets(self) -> list[VirtualTarget]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT strategy_id, book_id, sleeve_id, instrument, target, notional,
                       exposure_type, source_exposure_type, lot_size
                FROM virtual_targets
                ORDER BY strategy_id, book_id, instrument
                """
            ).fetchall()
        return [
            VirtualTarget(
                strategy_id=row["strategy_id"],
                book_id=row["book_id"],
                sleeve_id=row["sleeve_id"],
                instrument=row["instrument"],
                target=Decimal(row["target"]),
                notional=Decimal(row["notional"]),
                exposure_type=ExposureType(row["exposure_type"]),
                source_exposure_type=ExposureType(row["source_exposure_type"]),
                lot_size=Decimal(row["lot_size"]),
            )
            for row in rows
        ]

    def replace_virtual_positions(self, targets: Iterable[VirtualTarget]) -> None:
        """Commit economic ownership only after aggregate broker state reconciles."""
        rows = list(targets)
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute("DELETE FROM virtual_positions")
            conn.executemany(
                """
                INSERT INTO virtual_positions
                    (strategy_id, book_id, sleeve_id, instrument, quantity, notional, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        t.strategy_id,
                        t.book_id,
                        t.sleeve_id,
                        t.instrument,
                        str(t.target),
                        str(t.notional),
                        now,
                    )
                    for t in rows
                ],
            )
            self._append_event_on_conn(
                conn, "virtual_positions_committed", {"count": len(rows)}, now
            )

    def virtual_positions(self) -> list[dict[str, str]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT strategy_id, book_id, sleeve_id, instrument, quantity, notional
                FROM virtual_positions
                ORDER BY strategy_id, book_id, instrument
                """
            ).fetchall()
        return [
            {
                "strategy_id": row["strategy_id"],
                "book_id": row["book_id"],
                "sleeve_id": row["sleeve_id"],
                "instrument": row["instrument"],
                "quantity": row["quantity"],
                "notional": row["notional"],
            }
            for row in rows
        ]

    def record_run(self, run_id: str, *, state: str, reconciled: bool, details: dict) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO runs(run_id, started_at, state, reconciled, details_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, now, state, int(reconciled), json.dumps(details, sort_keys=True)),
            )

    def append_event(self, event_type: str, payload: dict) -> None:
        with self._connect() as conn:
            self._append_event_on_conn(
                conn, event_type, payload, datetime.now(timezone.utc).isoformat()
            )

    def events(self) -> list[dict[str, str]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT ts, event_type, payload_json FROM events ORDER BY id"
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _append_event_on_conn(
        conn: sqlite3.Connection, event_type: str, payload: dict, ts: str
    ) -> None:
        conn.execute(
            "INSERT INTO events (ts, event_type, payload_json) VALUES (?, ?, ?)",
            (ts, event_type, json.dumps(payload, sort_keys=True, default=str)),
        )
