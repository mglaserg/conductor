from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterator, Sequence
from uuid import uuid4

from conductor.domain.models import BrokerPosition, ExecutionReport, InstrumentSpec, TradeDelta, ZERO


class NautilusBridgeError(RuntimeError):
    """Raised when the local Nautilus execution worker is unavailable or rejects work."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class NautilusBridgeStore:
    """Small durable IPC store shared by Conductor CLI processes and one Nautilus worker.

    SQLite is deliberate here: both processes are on the same Windows machine, request volume is
    tiny, WAL gives us safe concurrent readers/writers, and every request/response remains auditable
    after a process restart.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = str(Path(path).expanduser().resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
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
                CREATE TABLE IF NOT EXISTS worker_state (
                    route_id TEXT PRIMARY KEY,
                    heartbeat_at TEXT NOT NULL,
                    ready INTEGER NOT NULL DEFAULT 0,
                    net_liquidation TEXT,
                    account_id TEXT,
                    error TEXT,
                    details_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS positions (
                    route_id TEXT NOT NULL,
                    instrument TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (route_id, instrument)
                );

                CREATE TABLE IF NOT EXISTS instruments (
                    route_id TEXT NOT NULL,
                    instrument TEXT NOT NULL,
                    nautilus_instrument_id TEXT NOT NULL,
                    price TEXT,
                    contract_multiplier TEXT NOT NULL DEFAULT '1',
                    lot_size TEXT NOT NULL DEFAULT '1',
                    asset_class TEXT NOT NULL DEFAULT 'equity',
                    venue TEXT,
                    broker_id TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (route_id, instrument)
                );

                CREATE TABLE IF NOT EXISTS requests (
                    request_id TEXT PRIMARY KEY,
                    route_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    claimed_at TEXT,
                    completed_at TEXT,
                    response_json TEXT,
                    error TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_requests_pending
                    ON requests(route_id, status, created_at);
                """
            )

    def heartbeat(
        self,
        route_id: str,
        *,
        ready: bool,
        net_liquidation: Decimal | None = None,
        account_id: str | None = None,
        error: str | None = None,
        details: dict | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO worker_state(
                    route_id, heartbeat_at, ready, net_liquidation, account_id, error, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(route_id) DO UPDATE SET
                    heartbeat_at=excluded.heartbeat_at,
                    ready=excluded.ready,
                    net_liquidation=excluded.net_liquidation,
                    account_id=excluded.account_id,
                    error=excluded.error,
                    details_json=excluded.details_json
                """,
                (
                    route_id,
                    _utc_now(),
                    int(ready),
                    None if net_liquidation is None else str(net_liquidation),
                    account_id,
                    error,
                    json.dumps(details or {}, sort_keys=True),
                ),
            )

    def worker_state(self, route_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM worker_state WHERE route_id=?", (route_id,)
            ).fetchone()
        return None if row is None else dict(row)

    def replace_positions(self, route_id: str, positions: Sequence[BrokerPosition]) -> None:
        now = _utc_now()
        with self._connect() as conn:
            conn.execute("DELETE FROM positions WHERE route_id=?", (route_id,))
            conn.executemany(
                """
                INSERT INTO positions(route_id, instrument, quantity, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (route_id, position.instrument, str(position.quantity), now)
                    for position in positions
                    if position.quantity != ZERO
                ],
            )

    def positions(self, route_id: str) -> list[BrokerPosition]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT instrument, quantity FROM positions WHERE route_id=? ORDER BY instrument",
                (route_id,),
            ).fetchall()
        return [
            BrokerPosition(row["instrument"], Decimal(row["quantity"]), route_id)
            for row in rows
        ]

    def upsert_instrument(
        self,
        route_id: str,
        instrument: str,
        *,
        nautilus_instrument_id: str,
        price: Decimal | None,
        contract_multiplier: Decimal = Decimal("1"),
        lot_size: Decimal = Decimal("1"),
        asset_class: str = "equity",
        venue: str | None = None,
        broker_id: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO instruments(
                    route_id, instrument, nautilus_instrument_id, price, contract_multiplier,
                    lot_size, asset_class, venue, broker_id, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(route_id, instrument) DO UPDATE SET
                    nautilus_instrument_id=excluded.nautilus_instrument_id,
                    price=COALESCE(excluded.price, instruments.price),
                    contract_multiplier=excluded.contract_multiplier,
                    lot_size=excluded.lot_size,
                    asset_class=excluded.asset_class,
                    venue=excluded.venue,
                    broker_id=COALESCE(excluded.broker_id, instruments.broker_id),
                    updated_at=excluded.updated_at
                """,
                (
                    route_id,
                    instrument,
                    nautilus_instrument_id,
                    None if price is None else str(price),
                    str(contract_multiplier),
                    str(lot_size),
                    asset_class,
                    venue,
                    broker_id,
                    _utc_now(),
                ),
            )

    def instrument(self, route_id: str, instrument: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM instruments WHERE route_id=? AND instrument=?",
                (route_id, instrument),
            ).fetchone()
        return None if row is None else dict(row)

    def known_instruments(self, route_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM instruments WHERE route_id=? ORDER BY instrument", (route_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def enqueue(self, route_id: str, kind: str, payload: dict) -> str:
        request_id = uuid4().hex
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO requests(request_id, route_id, kind, payload_json, status, created_at)
                VALUES (?, ?, ?, ?, 'pending', ?)
                """,
                (request_id, route_id, kind, json.dumps(payload, sort_keys=True), _utc_now()),
            )
        return request_id

    def requeue_claimed(self, route_id: str) -> int:
        """Return abandoned in-flight requests to pending after a worker restart."""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE requests
                SET status='pending', claimed_at=NULL
                WHERE route_id=? AND status='claimed'
                """,
                (route_id,),
            )
            return int(cursor.rowcount)

    def claim_pending(self, route_id: str, *, limit: int = 20) -> list[dict]:
        """Atomically claim pending requests for the single worker on a route."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT * FROM requests
                WHERE route_id=? AND status='pending'
                ORDER BY created_at, request_id
                LIMIT ?
                """,
                (route_id, limit),
            ).fetchall()
            now = _utc_now()
            for row in rows:
                conn.execute(
                    "UPDATE requests SET status='claimed', claimed_at=? WHERE request_id=?",
                    (now, row["request_id"]),
                )
        result: list[dict] = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            item["status"] = "claimed"
            result.append(item)
        return result

    def complete(self, request_id: str, response: dict) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE requests
                SET status='completed', completed_at=?, response_json=?, error=NULL
                WHERE request_id=?
                """,
                (_utc_now(), json.dumps(response, sort_keys=True), request_id),
            )

    def fail(self, request_id: str, error: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE requests
                SET status='failed', completed_at=?, error=?
                WHERE request_id=?
                """,
                (_utc_now(), error, request_id),
            )

    def request(self, request_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM requests WHERE request_id=?", (request_id,)
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        if item.get("payload_json"):
            item["payload"] = json.loads(item["payload_json"])
        if item.get("response_json"):
            item["response"] = json.loads(item["response_json"])
        return item


class NautilusBridgeExecutionAdapter:
    """ExecutionAdapter used by one-shot Conductor processes.

    A separate persistent Nautilus worker owns the actual IB connection. This class never imports
    NautilusTrader and therefore keeps strategy orchestration/test processes lightweight.
    """

    def __init__(
        self,
        *,
        bridge_db: str | Path,
        route_id: str,
        live_orders_enabled: bool,
        worker_stale_after_seconds: int = 15,
        request_timeout_seconds: int = 60,
        expected_account_id: str | None = None,
    ) -> None:
        self.store = NautilusBridgeStore(bridge_db)
        self.route_id = route_id
        self.live_orders_enabled = live_orders_enabled
        self.worker_stale_after_seconds = worker_stale_after_seconds
        self.request_timeout_seconds = request_timeout_seconds
        self.expected_account_id = expected_account_id

    def _require_worker(self) -> dict:
        state = self.store.worker_state(self.route_id)
        if state is None:
            raise NautilusBridgeError(
                f"Nautilus worker has never published state for route {self.route_id}"
            )
        heartbeat = datetime.fromisoformat(state["heartbeat_at"])
        age = (datetime.now(timezone.utc) - heartbeat).total_seconds()
        if age > self.worker_stale_after_seconds:
            raise NautilusBridgeError(
                f"Nautilus worker for {self.route_id} is stale ({age:.1f}s old)"
            )
        if not bool(state["ready"]):
            raise NautilusBridgeError(
                f"Nautilus worker for {self.route_id} is not ready: {state.get('error') or 'unknown'}"
            )
        actual_account = state.get("account_id")
        if self.expected_account_id is not None and actual_account != self.expected_account_id:
            raise NautilusBridgeError(
                f"Nautilus worker for {self.route_id} is connected to account "
                f"{actual_account!r}, expected {self.expected_account_id!r}"
            )
        return state

    def positions(self) -> Sequence[BrokerPosition]:
        self._require_worker()
        return self.store.positions(self.route_id)

    def net_liquidation(self) -> Decimal:
        state = self._require_worker()
        value = state.get("net_liquidation")
        if value is None:
            raise NautilusBridgeError(f"worker has no NetLiquidation for {self.route_id}")
        return Decimal(value)

    def _wait(self, request_id: str) -> dict:
        deadline = time.monotonic() + self.request_timeout_seconds
        while time.monotonic() < deadline:
            self._require_worker()
            row = self.store.request(request_id)
            if row is None:
                raise NautilusBridgeError(f"bridge request disappeared: {request_id}")
            if row["status"] == "completed":
                return row.get("response") or {}
            if row["status"] == "failed":
                raise NautilusBridgeError(row.get("error") or f"request failed: {request_id}")
            time.sleep(0.05)
        raise NautilusBridgeError(f"timed out waiting for Nautilus request {request_id}")

    def instrument_spec(self, instrument: str) -> InstrumentSpec:
        self._require_worker()
        row = self.store.instrument(self.route_id, instrument)
        if row is None or row.get("price") is None:
            request_id = self.store.enqueue(
                self.route_id,
                "resolve_instrument",
                {"instrument": instrument},
            )
            self._wait(request_id)
            row = self.store.instrument(self.route_id, instrument)
        if row is None:
            raise NautilusBridgeError(f"worker did not resolve {instrument}")
        if row.get("price") is None:
            raise NautilusBridgeError(f"worker resolved {instrument} but has no current price")
        return InstrumentSpec(
            instrument=instrument,
            price=Decimal(row["price"]),
            contract_multiplier=Decimal(row["contract_multiplier"]),
            lot_size=Decimal(row["lot_size"]),
            asset_class=row["asset_class"],
            venue=row.get("venue"),
        )

    def submit_deltas(self, deltas: Sequence[TradeDelta]) -> Sequence[ExecutionReport]:
        self._require_worker()
        if not deltas:
            return []
        if not self.live_orders_enabled:
            return [
                ExecutionReport(
                    route_id=self.route_id,
                    instrument=delta.instrument,
                    requested_quantity=delta.delta,
                    filled_quantity=ZERO,
                    avg_price=None,
                    commission=ZERO,
                    status="shadow",
                    order_id=None,
                )
                for delta in deltas
            ]

        payload = {
            "deltas": [
                {
                    "route_id": delta.route_id,
                    "instrument": delta.instrument,
                    "current": str(delta.current),
                    "desired": str(delta.desired),
                    "delta": str(delta.delta),
                    "estimated_notional": str(delta.estimated_notional),
                }
                for delta in deltas
            ]
        }
        response = self._wait(self.store.enqueue(self.route_id, "execute", payload))
        reports = response.get("reports", [])
        return [
            ExecutionReport(
                route_id=str(row["route_id"]),
                instrument=str(row["instrument"]),
                requested_quantity=Decimal(str(row["requested_quantity"])),
                filled_quantity=Decimal(str(row["filled_quantity"])),
                avg_price=(
                    None if row.get("avg_price") is None else Decimal(str(row["avg_price"]))
                ),
                commission=Decimal(str(row.get("commission", "0"))),
                status=str(row.get("status", "unknown")),
                order_id=None if row.get("order_id") is None else str(row["order_id"]),
            )
            for row in reports
        ]

    def close(self) -> None:
        # The persistent Nautilus worker owns the broker socket lifecycle.
        return None


def execution_report_payload(report: ExecutionReport) -> dict:
    """Stable bridge serialization helper used by the worker and unit tests."""
    row = asdict(report)
    return {key: (str(value) if isinstance(value, Decimal) else value) for key, value in row.items()}
