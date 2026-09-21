from __future__ import annotations

import threading
import time
from decimal import Decimal

import pytest

from conductor.adapters.nautilus_bridge import (
    NautilusBridgeError,
    NautilusBridgeExecutionAdapter,
    NautilusBridgeStore,
)
from conductor.adapters.nautilus_ibkr_worker import canonical_to_ib_raw
from conductor.domain.models import BrokerPosition, TradeDelta


def _ready(store: NautilusBridgeStore, route: str = "ibkr") -> None:
    store.heartbeat(
        route,
        ready=True,
        net_liquidation=Decimal("250000"),
        account_id="DU123",
    )


def test_bridge_rejects_missing_worker(tmp_path):
    adapter = NautilusBridgeExecutionAdapter(
        bridge_db=tmp_path / "bridge.sqlite",
        route_id="ibkr",
        live_orders_enabled=False,
    )
    with pytest.raises(NautilusBridgeError, match="never published"):
        adapter.positions()


def test_bridge_reads_worker_positions_and_nav(tmp_path):
    path = tmp_path / "bridge.sqlite"
    store = NautilusBridgeStore(path)
    _ready(store)
    store.replace_positions(
        "ibkr",
        [BrokerPosition("AAPL", Decimal("150"), "ibkr")],
    )
    adapter = NautilusBridgeExecutionAdapter(
        bridge_db=path,
        route_id="ibkr",
        live_orders_enabled=False,
    )
    assert list(adapter.positions()) == [BrokerPosition("AAPL", Decimal("150"), "ibkr")]
    assert adapter.net_liquidation() == Decimal("250000")


def test_shadow_execution_never_enqueues_live_request(tmp_path):
    path = tmp_path / "bridge.sqlite"
    store = NautilusBridgeStore(path)
    _ready(store)
    adapter = NautilusBridgeExecutionAdapter(
        bridge_db=path,
        route_id="ibkr",
        live_orders_enabled=False,
    )
    reports = adapter.submit_deltas(
        [TradeDelta("AAPL", Decimal("100"), Decimal("120"), Decimal("20"), route_id="ibkr")]
    )
    assert len(reports) == 1
    assert reports[0].status == "shadow"
    assert reports[0].filled_quantity == Decimal("0")
    with store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0


def test_dynamic_instrument_resolution_uses_worker_response(tmp_path):
    path = tmp_path / "bridge.sqlite"
    store = NautilusBridgeStore(path)
    _ready(store)
    adapter = NautilusBridgeExecutionAdapter(
        bridge_db=path,
        route_id="ibkr",
        live_orders_enabled=False,
        request_timeout_seconds=2,
    )

    def worker() -> None:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            requests = store.claim_pending("ibkr")
            if requests:
                req = requests[0]
                assert req["kind"] == "resolve_instrument"
                store.upsert_instrument(
                    "ibkr",
                    "AAPL",
                    nautilus_instrument_id="AAPL=STK.SMART",
                    price=Decimal("225.50"),
                    asset_class="equity",
                    venue="SMART",
                )
                store.complete(req["request_id"], {"ok": True})
                return
            time.sleep(0.01)
        raise AssertionError("no resolve request")

    thread = threading.Thread(target=worker)
    thread.start()
    spec = adapter.instrument_spec("AAPL")
    thread.join(timeout=2)
    assert spec.price == Decimal("225.50")
    assert spec.venue == "SMART"


def test_live_execution_returns_worker_fill_reports(tmp_path):
    path = tmp_path / "bridge.sqlite"
    store = NautilusBridgeStore(path)
    _ready(store)
    adapter = NautilusBridgeExecutionAdapter(
        bridge_db=path,
        route_id="ibkr",
        live_orders_enabled=True,
        request_timeout_seconds=2,
    )

    def worker() -> None:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            requests = store.claim_pending("ibkr")
            if requests:
                req = requests[0]
                assert req["kind"] == "execute"
                store.complete(
                    req["request_id"],
                    {
                        "reports": [
                            {
                                "route_id": "ibkr",
                                "instrument": "AAPL",
                                "requested_quantity": "20",
                                "filled_quantity": "20",
                                "avg_price": "226.10",
                                "commission": "1.00",
                                "status": "filled",
                                "order_id": "O-1",
                            }
                        ]
                    },
                )
                # Worker state is authoritative after fill/reconciliation.
                store.replace_positions(
                    "ibkr", [BrokerPosition("AAPL", Decimal("120"), "ibkr")]
                )
                return
            time.sleep(0.01)
        raise AssertionError("no execute request")

    thread = threading.Thread(target=worker)
    thread.start()
    reports = adapter.submit_deltas(
        [TradeDelta("AAPL", Decimal("100"), Decimal("120"), Decimal("20"), route_id="ibkr")]
    )
    thread.join(timeout=2)
    assert reports[0].filled_quantity == Decimal("20")
    assert reports[0].avg_price == Decimal("226.10")
    assert reports[0].commission == Decimal("1.00")


def test_claimed_requests_requeue_after_worker_restart(tmp_path):
    store = NautilusBridgeStore(tmp_path / "bridge.sqlite")
    _ready(store)
    request_id = store.enqueue("ibkr", "resolve_instrument", {"instrument": "AAPL"})
    claimed = store.claim_pending("ibkr")
    assert claimed[0]["request_id"] == request_id
    assert store.requeue_claimed("ibkr") == 1
    claimed_again = store.claim_pending("ibkr")
    assert claimed_again[0]["request_id"] == request_id


def test_initial_ib_equity_canonical_mapping():
    assert canonical_to_ib_raw("AAPL") == "AAPL=STK.SMART"
    assert canonical_to_ib_raw("EQ.US.AAPL") == "AAPL=STK.SMART"
    with pytest.raises(ValueError, match="equities"):
        canonical_to_ib_raw("FUT.CME.ES.202612")


def test_route_bootstrap_preloads_are_added_to_nautilus_scope(tmp_path):
    from conductor.adapters.nautilus_ibkr_worker import _preload_ids
    from conductor.adapters.nautilus_bridge import NautilusBridgeStore
    from conductor.config import load_runtime_config

    config_path = tmp_path / "conductor.toml"
    config_path.write_text(
        """
[node]
id = "windows"
state_db = "state.sqlite"
run_root = "runs"
portfolio_nav = 100000

[routes.ibkr]
adapter = "nautilus_ibkr"
account = "DU123"
bridge_db = "bridge.sqlite"
preload_instruments = ["AAPL", "EQ.US.MSFT"]

[risk]
max_gross_leverage = 2
max_instrument_nav = 0.5
""".strip(),
        encoding="utf-8",
    )
    config = load_runtime_config(config_path)
    store = NautilusBridgeStore(config.routes["ibkr"].bridge_db)
    load_ids = _preload_ids(config, "ibkr", store)
    assert load_ids == ["AAPL=STK.SMART", "MSFT=STK.SMART"]
    assert {row["instrument"] for row in store.known_instruments("ibkr")} == {
        "AAPL",
        "EQ.US.MSFT",
    }


def test_bridge_rejects_stale_worker_heartbeat(tmp_path):
    from datetime import datetime, timedelta, timezone

    path = tmp_path / "bridge.sqlite"
    store = NautilusBridgeStore(path)
    _ready(store)
    stale = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    with store._connect() as conn:
        conn.execute(
            "UPDATE worker_state SET heartbeat_at=? WHERE route_id='ibkr'",
            (stale,),
        )
    adapter = NautilusBridgeExecutionAdapter(
        bridge_db=path,
        route_id="ibkr",
        live_orders_enabled=False,
        worker_stale_after_seconds=15,
    )
    with pytest.raises(NautilusBridgeError, match="stale"):
        adapter.positions()
