from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Iterator

from conductor.domain.models import ExposureType, VirtualTarget


class ConductorLedger:
    """Durable economic-ownership ledger and append-only event log."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS virtual_targets (
                    strategy_id TEXT NOT NULL,
                    sleeve_id TEXT NOT NULL,
                    instrument TEXT NOT NULL,
                    target TEXT NOT NULL,
                    notional TEXT NOT NULL DEFAULT '0',
                    exposure_type TEXT NOT NULL,
                    source_exposure_type TEXT NOT NULL DEFAULT 'quantity',
                    lot_size TEXT NOT NULL DEFAULT '1',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (strategy_id, instrument)
                );

                CREATE TABLE IF NOT EXISTS virtual_positions (
                    strategy_id TEXT NOT NULL,
                    sleeve_id TEXT NOT NULL,
                    instrument TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    notional TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (strategy_id, instrument)
                );

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
                """
            )
            self._ensure_column(conn, "virtual_targets", "notional", "TEXT NOT NULL DEFAULT '0'")
            self._ensure_column(
                conn,
                "virtual_targets",
                "source_exposure_type",
                "TEXT NOT NULL DEFAULT 'quantity'",
            )
            self._ensure_column(conn, "virtual_targets", "lot_size", "TEXT NOT NULL DEFAULT '1'")

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def replace_virtual_targets(self, targets: Iterable[VirtualTarget]) -> None:
        rows = list(targets)
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute("DELETE FROM virtual_targets")
            conn.executemany(
                """
                INSERT INTO virtual_targets
                    (strategy_id, sleeve_id, instrument, target, notional,
                     exposure_type, source_exposure_type, lot_size, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        t.strategy_id,
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
                SELECT strategy_id, sleeve_id, instrument, target, notional,
                       exposure_type, source_exposure_type, lot_size
                FROM virtual_targets
                ORDER BY strategy_id, instrument
                """
            ).fetchall()
        return [
            VirtualTarget(
                strategy_id=row[0],
                sleeve_id=row[1],
                instrument=row[2],
                target=Decimal(row[3]),
                notional=Decimal(row[4]),
                exposure_type=ExposureType(row[5]),
                source_exposure_type=ExposureType(row[6]),
                lot_size=Decimal(row[7]),
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
                    (strategy_id, sleeve_id, instrument, quantity, notional, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        t.strategy_id,
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
                conn,
                "virtual_positions_committed",
                {"count": len(rows)},
                now,
            )

    def virtual_positions(self) -> list[dict[str, str]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT strategy_id, sleeve_id, instrument, quantity, notional
                FROM virtual_positions
                ORDER BY strategy_id, instrument
                """
            ).fetchall()
        return [
            {
                "strategy_id": r[0],
                "sleeve_id": r[1],
                "instrument": r[2],
                "quantity": r[3],
                "notional": r[4],
            }
            for r in rows
        ]

    def record_run(
        self,
        run_id: str,
        *,
        state: str,
        reconciled: bool,
        details: dict,
    ) -> None:
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
                conn,
                event_type,
                payload,
                datetime.now(timezone.utc).isoformat(),
            )

    def events(self) -> list[dict[str, str]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT ts, event_type, payload_json FROM events ORDER BY id"
            ).fetchall()
        return [{"ts": r[0], "event_type": r[1], "payload_json": r[2]} for r in rows]

    @staticmethod
    def _append_event_on_conn(
        conn: sqlite3.Connection,
        event_type: str,
        payload: dict,
        ts: str,
    ) -> None:
        conn.execute(
            "INSERT INTO events (ts, event_type, payload_json) VALUES (?, ?, ?)",
            (ts, event_type, json.dumps(payload, sort_keys=True)),
        )
