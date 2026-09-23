from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from conductor.adapters.nautilus import check_nautilus_v2
from conductor.adapters.nautilus_bridge import NautilusBridgeStore, execution_report_payload
from conductor.config import RuntimeConfig, load_runtime_config
from conductor.domain.models import BrokerPosition, ExecutionReport, ZERO
from conductor.ledger import ConductorLedger


class NautilusWorkerConfigError(RuntimeError):
    pass


def canonical_to_ib_raw(canonical_id: str) -> str:
    """Map Conductor's initial US-equity IDs to Nautilus IB RAW symbology.

    V0.4's production migration is equities-only. Futures/options will get explicit parsers rather
    than guessing contract semantics from a string.
    """
    value = canonical_id.strip()
    if not value:
        raise ValueError("empty canonical instrument")
    if value.startswith("EQ.US."):
        symbol = value.removeprefix("EQ.US.")
    elif "." not in value:
        # Backwards-compatible migration path for the current ETSA/RPS/TLAQ ticker output.
        symbol = value
    else:
        raise ValueError(
            f"V0.4 IB worker only resolves US equities; unsupported canonical ID {canonical_id!r}"
        )
    if not symbol or any(ch.isspace() for ch in symbol):
        raise ValueError(f"invalid US equity symbol {symbol!r}")
    return f"{symbol}=STK.SMART"


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    for name in ("as_decimal", "to_decimal"):
        method = getattr(value, name, None)
        if callable(method):
            return Decimal(str(method()))
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _signed_position_qty(position: Any) -> Decimal:
    method = getattr(position, "signed_decimal_qty", None)
    if callable(method):
        return Decimal(str(method()))
    value = getattr(position, "signed_qty", None)
    if value is not None:
        return Decimal(str(value))
    quantity = _decimal(getattr(position, "quantity", None)) or ZERO
    side = str(getattr(position, "side", "")).upper()
    return -quantity if "SHORT" in side else quantity


def _select_equity_value(equity: Any) -> Decimal | None:
    """Select the headline account-equity value from Nautilus' equity result."""
    if isinstance(equity, dict):
        selected = None
        for currency, money in equity.items():
            if "USD" in str(currency).upper():
                selected = money
                break
        if selected is None and len(equity) == 1:
            selected = next(iter(equity.values()))
        return _decimal(selected)
    return _decimal(equity)


def _resolve_account_net_liquidation(
    portfolio: Any,
    configured_account: str,
    *,
    account_id_type: Any,
    venue_type: Any,
) -> Decimal | None:
    """Resolve NAV for one configured broker account.

    Nautilus 2.x supports account-scoped Portfolio queries. Prefer those so two IBKR
    accounts on the same venue can never be aggregated accidentally. Older venue-only
    calls remain as a compatibility fallback for earlier 2.0 release candidates.
    """
    account_id = account_id_type(configured_account)

    try:
        nav = _select_equity_value(portfolio.equity(account_id=account_id))
        if nav is not None:
            return nav
    except Exception:
        pass

    try:
        account = portfolio.account(account_id=account_id)
        if account is not None:
            nav = _decimal(account.balance_total())
            if nav is not None:
                return nav
    except Exception:
        pass

    for venue_name in ("INTERACTIVE_BROKERS", "SMART"):
        try:
            venue = venue_type.from_str(venue_name)
            nav = _select_equity_value(portfolio.equity(venue=venue))
            if nav is not None:
                return nav
        except Exception:
            continue
    return None


@dataclass(slots=True)
class _TrackedOrder:
    request_id: str
    instrument: str
    requested_quantity: Decimal
    client_order_id: str
    filled_quantity: Decimal = ZERO
    fill_notional: Decimal = ZERO
    commission: Decimal = ZERO
    terminal_status: str | None = None

    @property
    def avg_price(self) -> Decimal | None:
        qty = abs(self.filled_quantity)
        return None if qty == ZERO else self.fill_notional / qty


class _BridgeStrategyMixin:
    """Implementation mixed into a runtime-created Nautilus Strategy subclass."""

    def _bridge_init(
        self,
        *,
        store: NautilusBridgeStore,
        route_id: str,
        account: str,
        live_orders_enabled: bool,
        order_timeout_seconds: int,
    ) -> None:
        self._bridge_store = store
        self._bridge_route_id = route_id
        self._bridge_account = account
        self._bridge_live = live_orders_enabled
        self._order_timeout_seconds = order_timeout_seconds
        self._pending_resolves: dict[str, tuple[str, Any]] = {}
        self._orders: dict[str, _TrackedOrder] = {}
        self._request_orders: dict[str, set[str]] = {}
        self._request_deadlines: dict[str, float] = {}

    def on_start(self) -> None:  # Nautilus callback
        self._bridge_store.requeue_claimed(self._bridge_route_id)
        self._subscribe_known_instruments()
        self._publish_state(ready=True)
        self.clock.set_timer(
            "conductor.bridge.poll",
            timedelta(milliseconds=250),
            callback=self._on_bridge_timer,
            fire_immediately=True,
        )

    def on_stop(self) -> None:  # Nautilus callback
        try:
            self.clock.cancel_timer("conductor.bridge.poll")
        finally:
            self._publish_state(ready=False, error="worker stopping")

    def on_degrade(self) -> None:  # Nautilus callback
        self._publish_state(ready=False, error="Nautilus strategy degraded")

    def on_fault(self) -> None:  # Nautilus callback
        self._publish_state(ready=False, error="Nautilus strategy faulted")

    def _on_bridge_timer(self, _event: Any) -> None:
        try:
            self._refresh_pending_resolves()
            self._enforce_execution_deadlines()
            for request in self._bridge_store.claim_pending(self._bridge_route_id):
                try:
                    if request["kind"] == "resolve_instrument":
                        self._handle_resolve(request)
                    elif request["kind"] == "execute":
                        self._handle_execute(request)
                    else:
                        raise ValueError(f"unknown bridge request kind {request['kind']!r}")
                except Exception as exc:
                    self._bridge_store.fail(request["request_id"], repr(exc))
            self._publish_state(ready=True)
        except Exception as exc:
            self._publish_state(ready=False, error=repr(exc))
            self.log.error(f"Conductor bridge poll failed: {exc!r}")

    def _subscribe_known_instruments(self) -> None:
        InstrumentId = _import_nautilus()["InstrumentId"]
        for row in self._bridge_store.known_instruments(self._bridge_route_id):
            instrument_id = InstrumentId.from_str(row["nautilus_instrument_id"])
            if self.cache.instrument(instrument_id) is not None:
                self.subscribe_quotes(instrument_id)

    def _handle_resolve(self, request: dict) -> None:
        InstrumentId = _import_nautilus()["InstrumentId"]
        canonical = str(request["payload"]["instrument"])
        nautilus_id = canonical_to_ib_raw(canonical)
        instrument_id = InstrumentId.from_str(nautilus_id)
        if self.cache.instrument(instrument_id) is None:
            self.request_instrument(instrument_id)
        else:
            self.subscribe_quotes(instrument_id)
        self._pending_resolves[request["request_id"]] = (canonical, instrument_id)
        self._refresh_pending_resolves()

    def _refresh_pending_resolves(self) -> None:
        PriceType = _import_nautilus()["PriceType"]
        for request_id, (canonical, instrument_id) in list(self._pending_resolves.items()):
            instrument = self.cache.instrument(instrument_id)
            if instrument is None:
                continue
            # Once loaded, keep a live top-of-book mark available for sizing.
            self.subscribe_quotes(instrument_id)
            price = self.cache.price(instrument_id, PriceType.MID)
            if price is None:
                price = self.cache.price(instrument_id, PriceType.LAST)
            decimal_price = _decimal(price)
            multiplier = _decimal(getattr(instrument, "multiplier", None)) or Decimal("1")
            size_increment = _decimal(getattr(instrument, "size_increment", None)) or Decimal("1")
            broker_id = None
            info = getattr(instrument, "info", None)
            if isinstance(info, dict):
                broker_id = str(info.get("conId") or info.get("conid") or "") or None
            self._bridge_store.upsert_instrument(
                self._bridge_route_id,
                canonical,
                nautilus_instrument_id=str(instrument_id),
                price=decimal_price,
                contract_multiplier=multiplier,
                lot_size=size_increment,
                asset_class="equity",
                venue=str(getattr(instrument_id, "venue", "SMART")),
                broker_id=broker_id,
            )
            if decimal_price is not None:
                self._bridge_store.complete(
                    request_id,
                    {
                        "instrument": canonical,
                        "nautilus_instrument_id": str(instrument_id),
                        "price": str(decimal_price),
                    },
                )
                del self._pending_resolves[request_id]

    def _handle_execute(self, request: dict) -> None:
        if not self._bridge_live:
            raise RuntimeError("worker is configured shadow-only; live execute request refused")
        types = _import_nautilus()
        InstrumentId = types["InstrumentId"]
        OrderSide = types["OrderSide"]
        request_id = request["request_id"]
        order_ids: set[str] = set()
        for row in request["payload"].get("deltas", []):
            signed_delta = Decimal(str(row["delta"]))
            if signed_delta == ZERO:
                continue
            canonical = str(row["instrument"])
            resolved = self._bridge_store.instrument(self._bridge_route_id, canonical)
            if resolved is None:
                raise RuntimeError(f"instrument not resolved before execute: {canonical}")
            instrument_id = InstrumentId.from_str(resolved["nautilus_instrument_id"])
            instrument = self.cache.instrument(instrument_id)
            if instrument is None:
                raise RuntimeError(f"instrument not loaded in Nautilus cache: {instrument_id}")
            side = OrderSide.BUY if signed_delta > ZERO else OrderSide.SELL
            order = self.order_factory.market(
                instrument_id=instrument_id,
                order_side=side,
                quantity=instrument.make_qty(abs(signed_delta)),
                tags=[f"CONDUCTOR_REQUEST:{request_id}", f"CONDUCTOR_INSTRUMENT:{canonical}"],
            )
            client_order_id = str(order.client_order_id)
            self._orders[client_order_id] = _TrackedOrder(
                request_id=request_id,
                instrument=canonical,
                requested_quantity=signed_delta,
                client_order_id=client_order_id,
            )
            order_ids.add(client_order_id)
            self.submit_order(order)
        self._request_orders[request_id] = order_ids
        self._request_deadlines[request_id] = time.monotonic() + self._order_timeout_seconds
        if not order_ids:
            self._bridge_store.complete(request_id, {"reports": []})
            self._request_deadlines.pop(request_id, None)

    def _enforce_execution_deadlines(self) -> None:
        now = time.monotonic()
        for request_id, deadline in list(self._request_deadlines.items()):
            if now < deadline:
                continue
            for client_id in self._request_orders.get(request_id, set()):
                tracked = self._orders.get(client_id)
                if tracked is None or tracked.terminal_status is not None:
                    continue
                order = self.cache.order(client_id)
                if order is not None and not bool(getattr(order, "is_closed", False)):
                    self.cancel_order(order)
            # Leave the request in-flight until Nautilus emits terminal cancel/fill events.
            # This avoids inventing an order state just because the local deadline elapsed.
            self._request_deadlines.pop(request_id, None)

    def on_order_filled(self, event: Any) -> None:
        tracked = self._orders.get(str(event.client_order_id))
        if tracked is None:
            return
        last_qty = _decimal(event.last_qty) or ZERO
        last_px = _decimal(event.last_px) or ZERO
        sign = Decimal("1") if tracked.requested_quantity > ZERO else Decimal("-1")
        tracked.filled_quantity += sign * last_qty
        tracked.fill_notional += abs(last_qty) * last_px
        commission = getattr(event, "commission", None)
        if commission is not None:
            amount = _decimal(getattr(commission, "as_decimal", lambda: commission)())
            if amount is not None:
                tracked.commission += amount
        order = self.cache.order(event.client_order_id)
        if order is not None and bool(getattr(order, "is_closed", False)):
            tracked.terminal_status = str(getattr(order, "status", "filled")).lower()
        self._finish_request_if_terminal(tracked.request_id)

    def on_order_rejected(self, event: Any) -> None:
        self._mark_terminal(event, "rejected")

    def on_order_denied(self, event: Any) -> None:
        self._mark_terminal(event, "denied")

    def on_order_canceled(self, event: Any) -> None:
        self._mark_terminal(event, "canceled")

    def on_order_expired(self, event: Any) -> None:
        self._mark_terminal(event, "expired")

    def _mark_terminal(self, event: Any, status: str) -> None:
        tracked = self._orders.get(str(event.client_order_id))
        if tracked is None:
            return
        tracked.terminal_status = status
        self._finish_request_if_terminal(tracked.request_id)

    def _finish_request_if_terminal(self, request_id: str) -> None:
        ids = self._request_orders.get(request_id, set())
        if not ids:
            return
        rows = [self._orders[client_id] for client_id in ids]
        # The fill handler can be called before an order's closed flag becomes visible. Treat a
        # fully-filled quantity as terminal as well as explicit terminal events.
        for row in rows:
            if row.terminal_status is None and abs(row.filled_quantity) >= abs(row.requested_quantity):
                row.terminal_status = "filled"
        if any(row.terminal_status is None for row in rows):
            return
        reports = [
            ExecutionReport(
                route_id=self._bridge_route_id,
                instrument=row.instrument,
                requested_quantity=row.requested_quantity,
                filled_quantity=row.filled_quantity,
                avg_price=row.avg_price,
                commission=row.commission,
                status=row.terminal_status or "unknown",
                order_id=row.client_order_id,
            )
            for row in rows
        ]
        self._bridge_store.complete(
            request_id,
            {"reports": [execution_report_payload(report) for report in reports]},
        )
        for client_id in ids:
            self._orders.pop(client_id, None)
        self._request_orders.pop(request_id, None)
        self._request_deadlines.pop(request_id, None)

    def _publish_state(self, *, ready: bool, error: str | None = None) -> None:
        types = _import_nautilus()
        InstrumentId = types["InstrumentId"]
        PriceType = types["PriceType"]
        Venue = types["Venue"]
        reverse = {
            row["nautilus_instrument_id"]: row["instrument"]
            for row in self._bridge_store.known_instruments(self._bridge_route_id)
        }
        positions: list[BrokerPosition] = []
        for position in self.cache.positions_open():
            nautilus_id = str(position.instrument_id)
            canonical = reverse.get(nautilus_id)
            if canonical is None:
                # Unknown broker state is intentionally not invented/mapped. Mark worker unhealthy
                # so Conductor's doctor forces explicit resolution before execution.
                ready = False
                error = f"unmapped live position {nautilus_id}"
                continue
            positions.append(
                BrokerPosition(canonical, _signed_position_qty(position), self._bridge_route_id)
            )
        self._bridge_store.replace_positions(self._bridge_route_id, positions)

        # Refresh marks for every loaded/cached Conductor instrument.
        for row in self._bridge_store.known_instruments(self._bridge_route_id):
            instrument_id = InstrumentId.from_str(row["nautilus_instrument_id"])
            instrument = self.cache.instrument(instrument_id)
            if instrument is None:
                continue
            price = self.cache.price(instrument_id, PriceType.MID)
            if price is None:
                price = self.cache.price(instrument_id, PriceType.LAST)
            self._bridge_store.upsert_instrument(
                self._bridge_route_id,
                row["instrument"],
                nautilus_instrument_id=row["nautilus_instrument_id"],
                price=_decimal(price),
                contract_multiplier=_decimal(getattr(instrument, "multiplier", None)) or Decimal("1"),
                lot_size=_decimal(getattr(instrument, "size_increment", None)) or Decimal("1"),
                asset_class=row.get("asset_class") or "equity",
                venue=row.get("venue"),
                broker_id=row.get("broker_id"),
            )

        AccountId = types["AccountId"]
        nav = _resolve_account_net_liquidation(
            self.portfolio,
            self._bridge_account,
            account_id_type=AccountId,
            venue_type=Venue,
        )
        if nav is None and ready:
            ready = False
            error = error or (
                f"account state for {self._bridge_account} is loaded without NetLiquidation"
            )
        self._bridge_store.heartbeat(
            self._bridge_route_id,
            ready=ready,
            net_liquidation=nav,
            account_id=self._bridge_account,
            error=error,
            details={
                "live_orders_enabled": self._bridge_live,
                "nav_source": "nautilus_portfolio_account",
            },
        )


def _import_nautilus() -> dict[str, Any]:
    # Keeping imports local lets the rest of Conductor (including strategy processes/tests) run
    # without installing the optional heavy Nautilus runtime.
    from nautilus_trader.model import (
        AccountId,
        InstrumentId,
        OrderSide,
        OmsType,
        PriceType,
        StrategyId,
        TraderId,
        Venue,
    )

    return {
        "AccountId": AccountId,
        "InstrumentId": InstrumentId,
        "OrderSide": OrderSide,
        "OmsType": OmsType,
        "PriceType": PriceType,
        "StrategyId": StrategyId,
        "TraderId": TraderId,
        "Venue": Venue,
    }


def _preload_ids(config: RuntimeConfig, route_id: str, store: NautilusBridgeStore) -> list[str]:
    values: set[str] = set()
    ledger = ConductorLedger(config.state_db)
    for row in ledger.virtual_positions():
        if row["route_id"] != route_id:
            continue
        nautilus_id = canonical_to_ib_raw(row["instrument"])
        values.add(nautilus_id)
        # Seed the reverse mapping before Nautilus startup reconciliation. The worker will fill in
        # the quote/contract metadata once the provider has loaded the instrument.
        store.upsert_instrument(
            route_id,
            row["instrument"],
            nautilus_instrument_id=nautilus_id,
            price=None,
            asset_class="equity",
            venue="SMART",
        )
    # Bootstrap/reconciliation-only instruments make the initial account scope exhaustive.
    # They deliberately have no virtual owner: doctor will therefore flag a non-zero broker
    # position until the operator seeds/assigns it or removes it from the account.
    for canonical in config.routes[route_id].preload_instruments:
        nautilus_id = canonical_to_ib_raw(canonical)
        values.add(nautilus_id)
        store.upsert_instrument(
            route_id,
            canonical,
            nautilus_instrument_id=nautilus_id,
            price=None,
            asset_class="equity",
            venue="SMART",
        )
    for row in store.known_instruments(route_id):
        values.add(str(row["nautilus_instrument_id"]))
    return sorted(values)


def run_nautilus_ibkr_worker(config_path: str | Path, route_id: str) -> None:
    installed = check_nautilus_v2()
    config = load_runtime_config(config_path)
    try:
        route_cfg = config.routes[route_id]
    except KeyError as exc:
        raise NautilusWorkerConfigError(f"unknown route {route_id!r}") from exc
    if route_cfg.route.adapter.lower() != "nautilus_ibkr":
        raise NautilusWorkerConfigError(
            f"route {route_id} uses {route_cfg.route.adapter!r}, not 'nautilus_ibkr'"
        )
    if not route_cfg.route.account or route_cfg.route.account == "paper":
        raise NautilusWorkerConfigError("IBKR account must be explicitly configured")

    from nautilus_trader.adapters.interactive_brokers import InteractiveBrokersDataClientConfig
    from nautilus_trader.adapters.interactive_brokers import InteractiveBrokersDataClientFactory
    from nautilus_trader.adapters.interactive_brokers import InteractiveBrokersExecutionClientConfig
    from nautilus_trader.adapters.interactive_brokers import InteractiveBrokersExecutionClientFactory
    from nautilus_trader.adapters.interactive_brokers import InteractiveBrokersInstrumentProviderConfig
    from nautilus_trader.adapters.interactive_brokers import MarketDataType
    from nautilus_trader.adapters.interactive_brokers import SymbologyMethod
    from nautilus_trader.common import Environment
    from nautilus_trader.config import StrategyConfig
    from nautilus_trader.live import LiveNode
    from nautilus_trader.model import InstrumentId, OmsType, StrategyId, TraderId
    from nautilus_trader.trading import Strategy

    bridge_db = route_cfg.bridge_db
    if bridge_db is None:
        raise NautilusWorkerConfigError(f"route {route_id} has no bridge_db")
    store = NautilusBridgeStore(bridge_db)
    preload = _preload_ids(config, route_id, store)
    load_ids = {InstrumentId.from_str(value) for value in preload}
    provider_config = InteractiveBrokersInstrumentProviderConfig(
        symbology_method=SymbologyMethod.RAW,
        load_ids=load_ids,
        cache_path=str(route_cfg.instrument_cache_path) if route_cfg.instrument_cache_path else None,
    )
    market_data_type = getattr(MarketDataType, route_cfg.market_data_type.upper())
    data_config = InteractiveBrokersDataClientConfig(
        host=route_cfg.host,
        port=route_cfg.port,
        client_id=route_cfg.data_client_id,
        market_data_type=market_data_type,
        instrument_provider=provider_config,
    )
    exec_config = InteractiveBrokersExecutionClientConfig(
        host=route_cfg.host,
        port=route_cfg.port,
        client_id=route_cfg.exec_client_id,
        account_id=route_cfg.route.account,
        fetch_all_open_orders=True,
        instrument_provider=provider_config,
    )

    class ConductorIbkrStrategy(Strategy, _BridgeStrategyMixin):
        def __init__(self) -> None:
            strategy_kwargs = {
                "strategy_id": StrategyId("ConductorIbkr-001"),
                "oms_type": OmsType.NETTING,
            }
            claims = list(load_ids)
            if hasattr(StrategyConfig, "external_order_claims"):
                strategy_kwargs["external_order_claims"] = claims
            elif hasattr(StrategyConfig, "external_order_instrument_ids"):
                strategy_kwargs["external_order_instrument_ids"] = claims
            else:
                raise NautilusWorkerConfigError(
                    "installed Nautilus StrategyConfig exposes no external-order claim field"
                )
            Strategy.__init__(self, StrategyConfig(**strategy_kwargs))
            self._bridge_init(
                store=store,
                route_id=route_id,
                account=route_cfg.route.account,
                live_orders_enabled=route_cfg.live_orders_enabled,
                order_timeout_seconds=route_cfg.order_timeout_seconds,
            )

        # Explicitly bind mixin callbacks so the extension type dispatches to Python methods.
        on_start = _BridgeStrategyMixin.on_start
        on_stop = _BridgeStrategyMixin.on_stop
        on_degrade = _BridgeStrategyMixin.on_degrade
        on_fault = _BridgeStrategyMixin.on_fault
        on_order_filled = _BridgeStrategyMixin.on_order_filled
        on_order_rejected = _BridgeStrategyMixin.on_order_rejected
        on_order_denied = _BridgeStrategyMixin.on_order_denied
        on_order_canceled = _BridgeStrategyMixin.on_order_canceled
        on_order_expired = _BridgeStrategyMixin.on_order_expired

    builder = (
        LiveNode.builder(
            f"CONDUCTOR-{route_id}",
            TraderId("CONDUCTOR-001"),
            Environment.LIVE,
        )
        .with_reconciliation(True)
        .add_data_client(None, InteractiveBrokersDataClientFactory(), data_config)
        .add_exec_client(None, InteractiveBrokersExecutionClientFactory(), exec_config)
    )
    node = builder.build()
    node.add_strategy(ConductorIbkrStrategy())
    store.heartbeat(
        route_id,
        ready=False,
        account_id=route_cfg.route.account,
        details={"nautilus_version": installed, "preloaded_instruments": preload},
    )
    node.run()
