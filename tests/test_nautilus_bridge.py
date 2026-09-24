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


def test_bridge_rejects_worker_connected_to_wrong_account(tmp_path):
    path = tmp_path / "bridge.sqlite"
    store = NautilusBridgeStore(path)
    _ready(store)
    adapter = NautilusBridgeExecutionAdapter(
        bridge_db=path,
        route_id="ibkr",
        live_orders_enabled=False,
        expected_account_id="DU999",
    )
    with pytest.raises(NautilusBridgeError, match="expected 'DU999'"):
        adapter.positions()


def test_batch_warm_serializes_dynamic_instrument_resolution(tmp_path):
    path = tmp_path / "bridge.sqlite"
    store = NautilusBridgeStore(path)
    _ready(store)
    adapter = NautilusBridgeExecutionAdapter(
        bridge_db=path,
        route_id="ibkr",
        live_orders_enabled=False,
        request_timeout_seconds=2,
    )

    observed: list[str] = []

    def worker() -> None:
        deadline = time.monotonic() + 2
        prices = {"AAPL": "225.50", "TLT": "90.25"}
        while time.monotonic() < deadline and len(observed) < 2:
            requests = store.claim_pending("ibkr")
            if not requests:
                time.sleep(0.01)
                continue
            assert len(requests) == 1
            req = requests[0]
            assert req["kind"] == "resolve_instrument"
            symbol = req["payload"]["instrument"]
            observed.append(symbol)
            store.upsert_instrument(
                "ibkr",
                symbol,
                nautilus_instrument_id=f"{symbol}=STK.SMART",
                price=Decimal(prices[symbol]),
                asset_class="equity",
                venue="SMART",
            )
            store.complete(
                req["request_id"],
                {
                    "instrument": symbol,
                    "nautilus_instrument_id": f"{symbol}=STK.SMART",
                    "price": prices[symbol],
                },
            )
        if len(observed) != 2:
            raise AssertionError(f"expected two serialized resolves, got {observed!r}")

    thread = threading.Thread(target=worker)
    thread.start()
    adapter.warm_instruments(["TLT", "AAPL", "AAPL"])
    thread.join(timeout=2)

    assert observed == ["AAPL", "TLT"]
    with store._connect() as conn:
        rows = conn.execute("SELECT kind FROM requests ORDER BY created_at, request_id").fetchall()
    assert [row["kind"] for row in rows] == ["resolve_instrument", "resolve_instrument"]


def test_batch_warm_reports_the_exact_symbol_that_times_out(tmp_path):
    path = tmp_path / "bridge.sqlite"
    store = NautilusBridgeStore(path)
    _ready(store)
    adapter = NautilusBridgeExecutionAdapter(
        bridge_db=path,
        route_id="ibkr",
        live_orders_enabled=False,
        request_timeout_seconds=0.05,
    )

    with pytest.raises(
        NautilusBridgeError,
        match=r"instrument warm-up failed for EQ\.US\.BUSE: timed out waiting for Nautilus request",
    ):
        adapter.warm_instruments(["EQ.US.BUSE", "EQ.US.XEL"])

    with store._connect() as conn:
        rows = conn.execute(
            "SELECT kind, payload_json FROM requests ORDER BY created_at, request_id"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["kind"] == "resolve_instrument"
    assert '"EQ.US.BUSE"' in rows[0]["payload_json"]



def test_batch_warm_skips_request_when_startup_snapshot_already_seeded_marks(tmp_path):
    path = tmp_path / "bridge.sqlite"
    store = NautilusBridgeStore(path)
    _ready(store)
    for symbol, price in (("AAPL", "225.50"), ("TLT", "90.25")):
        store.upsert_instrument(
            "ibkr",
            symbol,
            nautilus_instrument_id=f"{symbol}=STK.SMART",
            price=Decimal(price),
            asset_class="equity",
            venue="SMART",
        )
    adapter = NautilusBridgeExecutionAdapter(
        bridge_db=path,
        route_id="ibkr",
        live_orders_enabled=False,
        request_timeout_seconds=2,
    )

    adapter.warm_instruments(["TLT", "AAPL"])

    with store._connect() as conn:
        rows = conn.execute("SELECT kind FROM requests").fetchall()
    assert rows == []

def test_default_ibkr_request_timeout_allows_cold_cache_over_sixty_seconds(tmp_path):
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

[strategies.TEST]
route_id = "ibkr"
result_mode = "target_weights"
cwd = "."
command = ["python", "strategy.py"]

[portfolio]
allocator = "static"

[portfolio.static.weights]
TEST = 1.0
""".strip(),
        encoding="utf-8",
    )
    config = load_runtime_config(config_path)
    assert config.routes["ibkr"].request_timeout_seconds == 180


def test_broker_position_preflight_puts_all_held_instruments_in_startup_load_ids(tmp_path):
    from conductor.adapters.nautilus_ibkr_worker import (
        _preload_ids,
        _seed_startup_positions,
        canonical_us_equity_id,
    )
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

[risk]
max_gross_leverage = 2
max_instrument_nav = 0.5
""".strip(),
        encoding="utf-8",
    )
    config = load_runtime_config(config_path)
    store = NautilusBridgeStore(config.routes["ibkr"].bridge_db)
    positions = {f"SYM{i:02d}": Decimal(i + 1) for i in range(34)}

    prices = {symbol: Decimal("100") + i for i, symbol in enumerate(positions)}
    canonical_positions = _seed_startup_positions(
        store, "ibkr", positions, prices=prices
    )
    load_ids = _preload_ids(config, "ibkr", store)

    assert len(load_ids) == 34
    assert set(load_ids) == {f"SYM{i:02d}=STK.SMART" for i in range(34)}
    expected = {f"EQ.US.SYM{i:02d}" for i in range(34)}
    assert set(canonical_positions) == expected
    assert {position.instrument for position in store.positions("ibkr")} == expected
    assert store.instrument("ibkr", "EQ.US.SYM00")["price"] == "100"
    assert store.instrument("ibkr", "EQ.US.SYM33")["price"] == "133"
    assert canonical_us_equity_id("AEP") == "EQ.US.AEP"
    assert canonical_us_equity_id("EQ.US.AEP") == "EQ.US.AEP"


def test_startup_seed_preserves_previously_qualified_listing_venue(tmp_path):
    from conductor.adapters.nautilus_bridge import NautilusBridgeStore
    from conductor.adapters.nautilus_ibkr_worker import _seed_startup_positions

    store = NautilusBridgeStore(tmp_path / "bridge.sqlite")
    store.upsert_instrument(
        "ibkr_main",
        "EQ.US.AUB",
        nautilus_instrument_id="AUB=STK.NYSE",
        price="50",
        asset_class="equity",
        venue="NYSE",
        broker_id="123456",
    )

    _seed_startup_positions(
        store,
        "ibkr_main",
        {"AUB": Decimal("10")},
        prices={"AUB": Decimal("51")},
    )

    row = store.instrument("ibkr_main", "EQ.US.AUB")
    assert row["nautilus_instrument_id"] == "AUB=STK.NYSE"
    assert row["venue"] == "NYSE"
    assert row["price"] == "51"


def test_cold_stock_resolution_uses_ib_contract_qualification(monkeypatch):
    from types import SimpleNamespace

    import conductor.adapters.nautilus_ibkr_worker as worker_module
    from conductor.adapters.nautilus_ibkr_worker import (
        _BridgeStrategyMixin,
        ib_stock_contract_query,
    )

    assert ib_stock_contract_query("EQ.US.AUB") == {
        "symbol": "AUB",
        "secType": "STK",
        "exchange": "SMART",
        "currency": "USD",
    }

    class FakeClientId:
        @staticmethod
        def from_str(value):
            return f"CLIENT:{value}"

    monkeypatch.setattr(worker_module, "_import_nautilus", lambda: {"ClientId": FakeClientId})

    calls = []
    fake = SimpleNamespace(
        _instrument_requests_inflight=set(),
        request_instruments=lambda **kwargs: calls.append(kwargs),
    )

    _BridgeStrategyMixin._ensure_instrument_request(fake, "EQ.US.AUB")
    _BridgeStrategyMixin._ensure_instrument_request(fake, "EQ.US.AUB")

    assert calls == [
        {
            "client_id": "CLIENT:IB",
            "params": {
                "ib_contracts": [
                    {
                        "symbol": "AUB",
                        "secType": "STK",
                        "exchange": "SMART",
                        "currency": "USD",
                    },
                ]
            },
        }
    ]
    assert fake._instrument_requests_inflight == {"EQ.US.AUB"}


def test_bridge_uses_nautilus_on_instrument_callback_for_batch_contract_results():
    from conductor.adapters.nautilus_ibkr_worker import _BridgeStrategyMixin

    assert callable(_BridgeStrategyMixin.on_instrument)
    assert not hasattr(_BridgeStrategyMixin, "on_instruments")


def test_qualified_stock_response_binds_returned_listing_venue(monkeypatch):
    from types import SimpleNamespace

    import conductor.adapters.nautilus_ibkr_worker as worker_module
    from conductor.adapters.nautilus_ibkr_worker import _BridgeStrategyMixin

    class FakeId:
        def __init__(self, value: str, venue: str):
            self.value = value
            self.venue = venue
            self.symbol = value.split(".", 1)[0]

        def __str__(self):
            return self.value

    instrument_id = FakeId("AUB=STK.NYSE", "NYSE")
    instrument = SimpleNamespace(
        id=instrument_id,
        raw_symbol="AUB",
        multiplier=1,
        size_increment=1,
        info={"contract": {"conId": 123456}},
    )

    upserts = []
    store = SimpleNamespace(
        upsert_instrument=lambda *args, **kwargs: upserts.append((args, kwargs)),
        fail=lambda *_args, **_kwargs: None,
    )
    fake = SimpleNamespace(
        _bridge_store=store,
        _bridge_route_id="ibkr_main",
        _pending_resolves={"REQ1": ("EQ.US.AUB", None, 999.0)},
        _pending_warms={},
        _instrument_requests_inflight={"EQ.US.AUB"},
        _ensure_quote_subscription=lambda value: setattr(fake, "subscribed", value),
        _refresh_pending_resolves=lambda: None,
        _refresh_pending_warms=lambda: None,
        log=SimpleNamespace(info=lambda *_args, **_kwargs: None),
        subscribed=None,
    )

    _BridgeStrategyMixin.on_instrument(fake, instrument)

    assert fake._pending_resolves["REQ1"][1] is instrument_id
    assert fake._instrument_requests_inflight == set()
    assert fake.subscribed is instrument_id
    assert upserts[0][0][:2] == ("ibkr_main", "EQ.US.AUB")
    assert upserts[0][1]["nautilus_instrument_id"] == "AUB=STK.NYSE"
    assert upserts[0][1]["venue"] == "NYSE"
    assert upserts[0][1]["broker_id"] == "123456"



def test_qualified_stock_is_persisted_before_quote_subscription(monkeypatch):
    from types import SimpleNamespace

    from conductor.adapters.nautilus_ibkr_worker import _BridgeStrategyMixin

    class FakeId:
        def __init__(self, value: str, venue: str):
            self.value = value
            self.venue = venue
            self.symbol = value.split(".", 1)[0]

        def __str__(self):
            return self.value

    instrument_id = FakeId("AUB=STK.NYSE", "NYSE")
    instrument = SimpleNamespace(
        id=instrument_id,
        raw_symbol="AUB",
        multiplier=1,
        size_increment=1,
        info={"contract": {"conId": 366504295}},
    )

    upserts = []
    store = SimpleNamespace(
        upsert_instrument=lambda *args, **kwargs: upserts.append((args, kwargs)),
        fail=lambda *_args, **_kwargs: None,
    )
    fake = SimpleNamespace(
        _bridge_store=store,
        _bridge_route_id="ibkr_main",
        _pending_resolves={"REQ1": ("EQ.US.AUB", None, 999.0)},
        _pending_warms={},
        _instrument_requests_inflight={"EQ.US.AUB"},
        _ensure_quote_subscription=lambda _value: (_ for _ in ()).throw(
            RuntimeError("quote route failed")
        ),
        _refresh_pending_resolves=lambda: None,
        _refresh_pending_warms=lambda: None,
        log=SimpleNamespace(info=lambda *_args, **_kwargs: None),
    )

    try:
        _BridgeStrategyMixin.on_instrument(fake, instrument)
    except RuntimeError as exc:
        assert str(exc) == "quote route failed"
    else:
        raise AssertionError("expected quote subscription failure")

    assert upserts[0][0][:2] == ("ibkr_main", "EQ.US.AUB")
    assert upserts[0][1]["nautilus_instrument_id"] == "AUB=STK.NYSE"
    assert upserts[0][1]["broker_id"] == "366504295"
    assert fake._pending_resolves["REQ1"][1] is instrument_id
    assert fake._instrument_requests_inflight == set()

def test_expired_cold_stock_resolution_fails_and_clears_inflight(monkeypatch):
    from types import SimpleNamespace

    import conductor.adapters.nautilus_ibkr_worker as worker_module
    from conductor.adapters.nautilus_ibkr_worker import _BridgeStrategyMixin

    class FakePriceType:
        MID = "MID"
        LAST = "LAST"

    monkeypatch.setattr(worker_module, "_import_nautilus", lambda: {"PriceType": FakePriceType})
    monkeypatch.setattr(worker_module.time, "monotonic", lambda: 100.0)

    failures = []
    store = SimpleNamespace(fail=lambda request_id, error: failures.append((request_id, error)))
    fake = SimpleNamespace(
        cache=SimpleNamespace(instrument=lambda _instrument_id: None),
        _bridge_store=store,
        _pending_resolves={"REQ1": ("EQ.US.AUB", None, 99.0)},
        _instrument_requests_inflight={"EQ.US.AUB"},
    )
    fake._instrument_key = _BridgeStrategyMixin._instrument_key.__get__(fake)

    _BridgeStrategyMixin._refresh_pending_resolves(fake)

    assert failures == [("REQ1", "IB contract qualification timed out for EQ.US.AUB")]
    assert fake._pending_resolves == {}
    assert fake._instrument_requests_inflight == set()
