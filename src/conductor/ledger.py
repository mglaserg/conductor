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
    """Durable virtual ownership ledger and append-only event log."""

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
                    exposure_type TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (strategy_id, instrument)
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                """
            )

    def replace_virtual_targets(self, targets: Iterable[VirtualTarget]) -> None:
        rows = list(targets)
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute("DELETE FROM virtual_targets")
            conn.executemany(
                """
                INSERT INTO virtual_targets
                    (strategy_id, sleeve_id, instrument, target, exposure_type, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        t.strategy_id,
                        t.sleeve_id,
                        t.instrument,
                        str(t.target),
                        t.exposure_type.value,
                        now,
                    )
                    for t in rows
                ],
            )
            self._append_event_on_conn(
                conn,
                "virtual_targets_replaced",
                {"count": len(rows)},
                now,
            )

    def virtual_targets(self) -> list[VirtualTarget]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT strategy_id, sleeve_id, instrument, target, exposure_type
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
                exposure_type=ExposureType(row[4]),
            )
            for row in rows
        ]

    def append_event(self, event_type: str, payload: dict) -> None:
        with self._connect() as conn:
            self._append_event_on_conn(
                conn,
                event_type,
                payload,
                datetime.now(timezone.utc).isoformat(),
            )

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
