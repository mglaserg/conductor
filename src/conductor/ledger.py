from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Iterator

from conductor.domain.models import ExposureType, StrategyIntent, VirtualTarget


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
        conn = sqlite3.connect(self.path, timeout=30)
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

                CREATE TABLE IF NOT EXISTS strategy_registry (
                    strategy_id TEXT NOT NULL,
                    book_id TEXT NOT NULL DEFAULT 'main',
                    lifecycle TEXT NOT NULL DEFAULT 'active',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (strategy_id, book_id)
                );

                CREATE TABLE IF NOT EXISTS strategy_runs (
                    run_id TEXT PRIMARY KEY,
                    strategy_id TEXT NOT NULL,
                    book_id TEXT NOT NULL,
                    trigger TEXT NOT NULL,
                    scheduled_for TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    command_json TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    exit_code INTEGER,
                    stdout_path TEXT,
                    stderr_path TEXT,
                    input_state_path TEXT,
                    output_path TEXT,
                    error TEXT,
                    canonical_revision INTEGER
                );

                CREATE INDEX IF NOT EXISTS ix_strategy_runs_book_status
                    ON strategy_runs(strategy_id, book_id, status);

                CREATE UNIQUE INDEX IF NOT EXISTS ux_strategy_runs_one_active
                    ON strategy_runs(strategy_id, book_id)
                    WHERE status IN ('starting', 'running');

                CREATE TABLE IF NOT EXISTS runtime_books (
                    strategy_id TEXT NOT NULL,
                    book_id TEXT NOT NULL,
                    sleeve_id TEXT NOT NULL,
                    route_id TEXT NOT NULL DEFAULT 'default',
                    revision INTEGER NOT NULL,
                    exposure_type TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    targets_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (strategy_id, book_id)
                );

                CREATE TABLE IF NOT EXISTS strategy_accounts (
                    strategy_id TEXT NOT NULL,
                    book_id TEXT NOT NULL DEFAULT 'main',
                    route_id TEXT NOT NULL DEFAULT 'default',
                    allocated_capital TEXT NOT NULL DEFAULT '0',
                    cash TEXT NOT NULL DEFAULT '0',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (strategy_id, book_id)
                );

                CREATE TABLE IF NOT EXISTS instrument_cache (
                    canonical_id TEXT NOT NULL,
                    route_id TEXT NOT NULL,
                    broker_id TEXT,
                    symbol TEXT NOT NULL,
                    asset_class TEXT NOT NULL,
                    venue TEXT,
                    currency TEXT,
                    metadata_json TEXT NOT NULL,
                    resolved_at TEXT NOT NULL,
                    validated_at TEXT NOT NULL,
                    PRIMARY KEY (canonical_id, route_id)
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
                    route_id TEXT NOT NULL DEFAULT 'default',
                    instrument TEXT NOT NULL,
                    target TEXT NOT NULL,
                    notional TEXT NOT NULL DEFAULT '0',
                    exposure_type TEXT NOT NULL,
                    source_exposure_type TEXT NOT NULL DEFAULT 'quantity',
                    lot_size TEXT NOT NULL DEFAULT '1',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (strategy_id, book_id, route_id, instrument)
                )
            """
        else:
            create_sql = """
                CREATE TABLE {table} (
                    strategy_id TEXT NOT NULL,
                    book_id TEXT NOT NULL DEFAULT 'main',
                    sleeve_id TEXT NOT NULL,
                    route_id TEXT NOT NULL DEFAULT 'default',
                    instrument TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    notional TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (strategy_id, book_id, route_id, instrument)
                )
            """

        if not self._table_exists(conn, table):
            conn.execute(create_sql.format(table=table))
            return

        info = conn.execute(f"PRAGMA table_info({table})").fetchall()
        columns = {row[1] for row in info}
        pk_columns = tuple(row[1] for row in sorted(info, key=lambda row: row[5]) if row[5] > 0)
        if {"book_id", "route_id"}.issubset(columns) and pk_columns == ("strategy_id", "book_id", "route_id", "instrument"):
            return

        legacy = f"{table}_legacy"
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
            select_book = "book_id" if "book_id" in legacy_columns else "'main'"
            select_route = "route_id" if "route_id" in legacy_columns else "'default'"
            conn.execute(
                f"""
                INSERT INTO {table}
                    (strategy_id, book_id, sleeve_id, route_id, instrument, target, notional,
                     exposure_type, source_exposure_type, lot_size, updated_at)
                SELECT strategy_id, {select_book}, sleeve_id, {select_route}, instrument, target,
                       {select_notional}, exposure_type, {select_source}, {select_lot}, updated_at
                FROM {legacy}
                """
            )
        else:
            select_book = "book_id" if "book_id" in legacy_columns else "'main'"
            select_route = "route_id" if "route_id" in legacy_columns else "'default'"
            conn.execute(
                f"""
                INSERT INTO {table}
                    (strategy_id, book_id, sleeve_id, route_id, instrument, quantity, notional, updated_at)
                SELECT strategy_id, {select_book}, sleeve_id, {select_route}, instrument, quantity, notional, updated_at
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
                    (strategy_id, book_id, sleeve_id, route_id, instrument, target, notional,
                     exposure_type, source_exposure_type, lot_size, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        t.strategy_id,
                        t.book_id,
                        t.sleeve_id,
                        t.route_id,
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
                SELECT strategy_id, book_id, sleeve_id, route_id, instrument, target, notional,
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
                route_id=row["route_id"],
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
                    (strategy_id, book_id, sleeve_id, route_id, instrument, quantity, notional, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        t.strategy_id,
                        t.book_id,
                        t.sleeve_id,
                        t.route_id,
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
                SELECT strategy_id, book_id, sleeve_id, route_id, instrument, quantity, notional
                FROM virtual_positions
                ORDER BY strategy_id, book_id, instrument
                """
            ).fetchall()
        return [
            {
                "strategy_id": row["strategy_id"],
                "book_id": row["book_id"],
                "sleeve_id": row["sleeve_id"],
                "route_id": row["route_id"],
                "instrument": row["instrument"],
                "quantity": row["quantity"],
                "notional": row["notional"],
            }
            for row in rows
        ]


    def ensure_strategy(
        self, strategy_id: str, *, book_id: str = "main", lifecycle: str = "active"
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO strategy_registry(strategy_id, book_id, lifecycle, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(strategy_id, book_id) DO NOTHING
                """,
                (strategy_id, book_id, lifecycle, now),
            )

    def set_strategy_lifecycle(
        self, strategy_id: str, lifecycle: str, *, book_id: str = "main"
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO strategy_registry(strategy_id, book_id, lifecycle, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(strategy_id, book_id) DO UPDATE SET
                    lifecycle=excluded.lifecycle, updated_at=excluded.updated_at
                """,
                (strategy_id, book_id, lifecycle, now),
            )
            self._append_event_on_conn(
                conn,
                "strategy.lifecycle_changed",
                {"strategy_id": strategy_id, "book_id": book_id, "lifecycle": lifecycle},
                now,
            )

    def strategy_lifecycle(self, strategy_id: str, *, book_id: str = "main") -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT lifecycle FROM strategy_registry WHERE strategy_id=? AND book_id=?",
                (strategy_id, book_id),
            ).fetchone()
        return row["lifecycle"] if row is not None else "active"

    def begin_strategy_run(
        self,
        *,
        run_id: str,
        strategy_id: str,
        book_id: str,
        trigger: str,
        scheduled_for: str | None,
        command: tuple[str, ...],
        cwd: str,
        stdout_path: str,
        stderr_path: str,
        input_state_path: str,
        output_path: str,
    ) -> bool:
        """Atomically acquire the one-run-per-strategy/book slot."""
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            # A deferred SQLite transaction permits two schedulers to both observe
            # "no active run" before either inserts. Take the write reservation
            # before reading so the check and insert are one cross-process lock.
            # The partial unique index is a second line of defense.
            conn.execute("BEGIN IMMEDIATE")
            lifecycle = conn.execute(
                "SELECT lifecycle FROM strategy_registry WHERE strategy_id=? AND book_id=?",
                (strategy_id, book_id),
            ).fetchone()
            if lifecycle is not None and lifecycle["lifecycle"] != "active":
                self._append_event_on_conn(
                    conn,
                    "strategy.run_rejected",
                    {
                        "run_id": run_id,
                        "strategy_id": strategy_id,
                        "book_id": book_id,
                        "reason": f"lifecycle_{lifecycle['lifecycle']}",
                    },
                    now,
                )
                return False

            active = conn.execute(
                """
                SELECT run_id FROM strategy_runs
                WHERE strategy_id=? AND book_id=? AND status IN ('starting', 'running')
                LIMIT 1
                """,
                (strategy_id, book_id),
            ).fetchone()
            if active is not None:
                self._append_event_on_conn(
                    conn,
                    "strategy.run_rejected",
                    {
                        "run_id": run_id,
                        "strategy_id": strategy_id,
                        "book_id": book_id,
                        "reason": "concurrent_run",
                        "active_run_id": active["run_id"],
                    },
                    now,
                )
                return False

            conn.execute(
                """
                INSERT INTO strategy_runs(
                    run_id, strategy_id, book_id, trigger, scheduled_for, started_at, status,
                    command_json, cwd, stdout_path, stderr_path, input_state_path, output_path
                ) VALUES (?, ?, ?, ?, ?, ?, 'starting', ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    strategy_id,
                    book_id,
                    trigger,
                    scheduled_for,
                    now,
                    json.dumps(command),
                    cwd,
                    stdout_path,
                    stderr_path,
                    input_state_path,
                    output_path,
                ),
            )
            self._append_event_on_conn(
                conn,
                "strategy.run_started",
                {
                    "run_id": run_id,
                    "strategy_id": strategy_id,
                    "book_id": book_id,
                    "trigger": trigger,
                    "scheduled_for": scheduled_for,
                    "command": list(command),
                    "cwd": cwd,
                },
                now,
            )
        return True

    def mark_strategy_run_running(self, run_id: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE strategy_runs SET status='running' WHERE run_id=?", (run_id,))

    def finish_strategy_run(
        self,
        run_id: str,
        *,
        status: str,
        exit_code: int | None = None,
        error: str | None = None,
        canonical_revision: int | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE strategy_runs
                SET finished_at=?, status=?, exit_code=?, error=?, canonical_revision=?
                WHERE run_id=?
                """,
                (now, status, exit_code, error, canonical_revision, run_id),
            )
            self._append_event_on_conn(
                conn,
                "strategy.run_finished",
                {
                    "run_id": run_id,
                    "status": status,
                    "exit_code": exit_code,
                    "error": error,
                    "canonical_revision": canonical_revision,
                },
                now,
            )

    def strategy_runs(self, *, limit: int = 100) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM strategy_runs
                ORDER BY started_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def next_runtime_revision(self, strategy_id: str, *, book_id: str = "main") -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT revision FROM runtime_books WHERE strategy_id=? AND book_id=?",
                (strategy_id, book_id),
            ).fetchone()
        return 1 if row is None else int(row["revision"]) + 1

    def replace_runtime_intent(self, intent: StrategyIntent, *, run_id: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            current = conn.execute(
                "SELECT revision FROM runtime_books WHERE strategy_id=? AND book_id=?",
                (intent.strategy_id, intent.book_id),
            ).fetchone()
            if current is not None and intent.revision <= int(current["revision"]):
                raise ValueError(
                    f"runtime book revision must increase for {intent.strategy_id}/{intent.book_id}"
                )
            conn.execute(
                """
                INSERT INTO runtime_books(
                    strategy_id, book_id, sleeve_id, route_id, revision, exposure_type,
                    as_of, run_id, targets_json, metadata_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(strategy_id, book_id) DO UPDATE SET
                    sleeve_id=excluded.sleeve_id, route_id=excluded.route_id,
                    revision=excluded.revision, exposure_type=excluded.exposure_type,
                    as_of=excluded.as_of, run_id=excluded.run_id,
                    targets_json=excluded.targets_json, metadata_json=excluded.metadata_json,
                    updated_at=excluded.updated_at
                """,
                (
                    intent.strategy_id,
                    intent.book_id,
                    intent.sleeve_id,
                    intent.route_id,
                    intent.revision,
                    intent.exposure_type.value,
                    intent.as_of.isoformat(),
                    run_id,
                    json.dumps({k: str(v) for k, v in intent.targets.items()}, sort_keys=True),
                    json.dumps(dict(intent.metadata), sort_keys=True),
                    now,
                ),
            )
            self._append_event_on_conn(
                conn,
                "runtime.target_resolved",
                {
                    "run_id": run_id,
                    "strategy_id": intent.strategy_id,
                    "book_id": intent.book_id,
                    "revision": intent.revision,
                    "exposure_type": intent.exposure_type.value,
                    "route_id": intent.route_id,
                    "targets": {k: str(v) for k, v in intent.targets.items()},
                },
                now,
            )

    def runtime_intents(self) -> list[StrategyIntent]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runtime_books ORDER BY strategy_id, book_id"
            ).fetchall()
        intents: list[StrategyIntent] = []
        for row in rows:
            targets = {
                instrument: Decimal(value)
                for instrument, value in json.loads(row["targets_json"]).items()
            }
            intents.append(
                StrategyIntent(
                    strategy_id=row["strategy_id"],
                    book_id=row["book_id"],
                    sleeve_id=row["sleeve_id"],
                    route_id=row["route_id"],
                    revision=int(row["revision"]),
                    exposure_type=ExposureType(row["exposure_type"]),
                    as_of=datetime.fromisoformat(row["as_of"]),
                    intent_id=row["run_id"],
                    metadata=json.loads(row["metadata_json"]),
                    targets=targets,
                )
            )
        return intents


    def seed_virtual_book(
        self,
        *,
        strategy_id: str,
        book_id: str,
        sleeve_id: str,
        route_id: str,
        positions: dict[str, Decimal],
        notionals: dict[str, Decimal] | None = None,
    ) -> None:
        """Seed one strategy's economic ownership without disturbing other books."""
        now = datetime.now(timezone.utc).isoformat()
        notionals = notionals or {}
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM virtual_positions WHERE strategy_id=? AND book_id=?",
                (strategy_id, book_id),
            )
            conn.executemany(
                """
                INSERT INTO virtual_positions(
                    strategy_id, book_id, sleeve_id, route_id, instrument, quantity, notional,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        strategy_id,
                        book_id,
                        sleeve_id,
                        route_id,
                        instrument,
                        str(quantity),
                        str(notionals.get(instrument, Decimal("0"))),
                        now,
                    )
                    for instrument, quantity in sorted(positions.items())
                    if quantity != 0
                ],
            )
            self._append_event_on_conn(
                conn,
                "strategy.positions_seeded",
                {
                    "strategy_id": strategy_id,
                    "book_id": book_id,
                    "route_id": route_id,
                    "positions": {k: str(v) for k, v in positions.items()},
                },
                now,
            )

    def seed_strategy_account(
        self,
        strategy_id: str,
        *,
        book_id: str = "main",
        route_id: str = "default",
        allocated_capital: Decimal,
        cash: Decimal,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO strategy_accounts(
                    strategy_id, book_id, route_id, allocated_capital, cash, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(strategy_id, book_id) DO UPDATE SET
                    route_id=excluded.route_id, allocated_capital=excluded.allocated_capital,
                    cash=excluded.cash, updated_at=excluded.updated_at
                """,
                (strategy_id, book_id, route_id, str(allocated_capital), str(cash), now),
            )
            self._append_event_on_conn(
                conn,
                "strategy.account_seeded",
                {
                    "strategy_id": strategy_id,
                    "book_id": book_id,
                    "route_id": route_id,
                    "allocated_capital": str(allocated_capital),
                    "cash": str(cash),
                },
                now,
            )

    def strategy_accounts(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM strategy_accounts ORDER BY strategy_id, book_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def strategy_account(self, strategy_id: str, *, book_id: str = "main") -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM strategy_accounts WHERE strategy_id=? AND book_id=?",
                (strategy_id, book_id),
            ).fetchone()
        return dict(row) if row is not None else None

    def update_strategy_cash(
        self, strategy_id: str, cash: Decimal, *, book_id: str = "main"
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            result = conn.execute(
                """
                UPDATE strategy_accounts SET cash=?, updated_at=?
                WHERE strategy_id=? AND book_id=?
                """,
                (str(cash), now, strategy_id, book_id),
            )
            if result.rowcount != 1:
                raise KeyError(f"strategy account not seeded: {strategy_id}/{book_id}")

    def strategy_positions(
        self, strategy_id: str, *, book_id: str = "main"
    ) -> dict[str, Decimal]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT instrument, quantity FROM virtual_positions
                WHERE strategy_id=? AND book_id=?
                ORDER BY instrument
                """,
                (strategy_id, book_id),
            ).fetchall()
        return {row["instrument"]: Decimal(row["quantity"]) for row in rows}

    def upsert_instrument_cache(
        self,
        *,
        canonical_id: str,
        route_id: str,
        symbol: str,
        asset_class: str,
        broker_id: str | None = None,
        venue: str | None = None,
        currency: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO instrument_cache(
                    canonical_id, route_id, broker_id, symbol, asset_class, venue, currency,
                    metadata_json, resolved_at, validated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(canonical_id, route_id) DO UPDATE SET
                    broker_id=excluded.broker_id, symbol=excluded.symbol,
                    asset_class=excluded.asset_class, venue=excluded.venue,
                    currency=excluded.currency, metadata_json=excluded.metadata_json,
                    validated_at=excluded.validated_at
                """,
                (
                    canonical_id,
                    route_id,
                    broker_id,
                    symbol,
                    asset_class,
                    venue,
                    currency,
                    json.dumps(metadata or {}, sort_keys=True),
                    now,
                    now,
                ),
            )

    def instrument_cache_entry(self, canonical_id: str, route_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM instrument_cache WHERE canonical_id=? AND route_id=?
                """,
                (canonical_id, route_id),
            ).fetchone()
        return dict(row) if row is not None else None

    def instrument_cache_entries(self, route_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM instrument_cache WHERE route_id=? ORDER BY canonical_id",
                (route_id,),
            ).fetchall()
        return [dict(row) for row in rows]

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
