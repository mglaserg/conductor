from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from conductor.adapters.ibkr_account_summary import (
    IbkrAccountSummaryNavProvider,
    query_ibkr_stock_portfolio_snapshot,
)
from conductor.adapters.nautilus import check_nautilus_v2
from conductor.adapters.nautilus_bridge import NautilusBridgeStore, execution_report_payload
from conductor.config import RuntimeConfig, load_runtime_config
from conductor.domain.models import BrokerPosition, ExecutionReport, ZERO
from conductor.ledger import ConductorLedger


class NautilusWorkerConfigError(RuntimeError):
    pass


def canonical_us_equity_id(value: str) -> str:
    """Normalize an IB/native US stock symbol to Conductor's canonical equity ID.

    IB position callbacks report native symbols such as ``AEP`` while strategy/runtime state uses
    canonical IDs such as ``EQ.US.AEP``. Startup reconciliation must compare one identity scheme,
    otherwise the same 39 holdings can appear as 39 missing plus 39 extra positions.
    """
    instrument = str(value).strip().upper()
    if not instrument:
        raise ValueError("empty canonical instrument")
    if instrument.startswith("EQ.US."):
        symbol = instrument.removeprefix("EQ.US.")
    elif "." not in instrument:
        symbol = instrument
    else:
        raise ValueError(
            f"V0.4 IB worker only resolves US equities; unsupported canonical ID {value!r}"
        )
    if not symbol or any(ch.isspace() for ch in symbol):
        raise ValueError(f"invalid US equity symbol {symbol!r}")
    return f"EQ.US.{symbol}"


def canonical_to_ib_raw(canonical_id: str) -> str:
    """Map Conductor's initial US-equity IDs to Nautilus IB RAW symbology.

    V0.4's production migration is equities-only. Futures/options will get explicit parsers rather
    than guessing contract semantics from a string.
    """
    canonical = canonical_us_equity_id(canonical_id)
    symbol = canonical.removeprefix("EQ.US.")
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


def _raw_account_number(account_id: Any) -> str:
    """Return the broker-native account number from a Nautilus AccountId-like object."""
    getter = getattr(account_id, "get_id", None)
    if callable(getter):
        try:
            return str(getter())
        except Exception:
            pass
    value = str(account_id)
    return value.rsplit("-", 1)[-1]


def _resolve_cached_account_id(
    cache: Any,
    configured_account: str,
    *,
    venue_type: Any,
) -> Any | None:
    """Find Nautilus' namespaced AccountId for one configured IB account.

    Interactive Brokers config accepts the native account number (for example ``U123``),
    while Nautilus stores accounts under a namespaced ``AccountId`` such as ``IB-U123``.
    Never construct that namespace ourselves: ask the live cache for the authoritative ID.
    """
    for venue_name in ("IB", "INTERACTIVE_BROKERS", "SMART"):
        try:
            account_id = cache.account_id(venue_type.from_str(venue_name))
        except Exception:
            continue
        if account_id is None:
            continue
        if _raw_account_number(account_id) == configured_account:
            return account_id
    return None


def _account_event_net_liquidation(account: Any) -> Decimal | None:
    """Read venue-reported NetLiquidation without invoking portfolio valuation.

    IB's v2 adapter can publish an initial margin ``AccountState`` with no typed balances while
    still preserving the raw account-summary fields in ``event.info``. Calling
    ``Portfolio.equity`` against that half-initialized state can enter Rust valuation code which
    requires a currency and abort the process. Prefer the venue's raw NetLiquidation field when
    it is available.
    """
    try:
        event = getattr(account, "last_event", None)
        if callable(event):
            event = event()
        if event is None:
            return None
        info = getattr(event, "info", None)
        if callable(info):
            info = info()
        if not info:
            return None
        for key in ("NetLiquidation", "NET_LIQUIDATION", "net_liquidation"):
            try:
                value = info.get(key)
            except AttributeError:
                try:
                    value = info[key]
                except Exception:
                    continue
            if value is not None:
                nav = _decimal(value)
                if nav is not None:
                    return nav
    except Exception:
        return None
    return None


def _account_has_typed_balances(account: Any) -> bool | None:
    """Return whether Nautilus has usable typed balances, or ``None`` if unknown."""
    try:
        balances = getattr(account, "balances", None)
        if balances is None:
            return None
        balances = balances() if callable(balances) else balances
        return bool(balances)
    except Exception:
        return None


def _resolve_account_net_liquidation(
    portfolio: Any,
    configured_account: str,
    *,
    account_id_type: Any,
    venue_type: Any,
    cache: Any | None = None,
) -> tuple[Decimal | None, Any | None, str | None]:
    """Resolve NAV for one configured broker account without crossing account boundaries.

    Prefer the authoritative account ID already registered in Nautilus' cache. IB accepts
    native account numbers in its adapter config, but Nautilus namespaces the resulting
    ``AccountId`` with the execution-client issuer (for example ``IB-U123``). Constructing
    ``AccountId("U123")`` is therefore both invalid and unsafe for multi-account routing.

    A live IB worker may briefly expose an account object before typed balances are populated.
    In that state we read the raw venue-reported ``NetLiquidation`` from the account event when
    possible and otherwise return not-ready. We deliberately do *not* call ``Portfolio.equity``
    on an explicitly empty account because Nautilus 2.0.0rc4 can abort in Rust when currency
    metadata is not yet available.
    """
    account_id = None
    if cache is not None:
        account_id = _resolve_cached_account_id(
            cache,
            configured_account,
            venue_type=venue_type,
        )

    if account_id is None and cache is not None:
        # Current live workers must never fall back to venue-wide equity when the configured
        # account cannot be resolved. That could silently size one account from another account's
        # NAV on a multi-account broker connection.
        return None, None, None

    if account_id is None:
        # Unit-test / legacy helper path only. Real workers use the cached namespaced ID above.
        try:
            account_id = account_id_type(configured_account)
        except Exception:
            account_id = None

    if account_id is not None:
        account = None
        account_lookup_succeeded = False
        try:
            account = portfolio.account(account_id=account_id)
            account_lookup_succeeded = account is not None
        except Exception:
            account = None

        if account is not None:
            nav = _account_event_net_liquidation(account)
            if nav is not None:
                return nav, account_id, "portfolio.account.last_event.info.NetLiquidation"

            has_balances = _account_has_typed_balances(account)
            if has_balances is False:
                # Explicitly empty account state: do not enter Nautilus valuation code yet.
                return None, account_id, None

            if has_balances is not False:
                try:
                    nav = _select_equity_value(portfolio.equity(account_id=account_id))
                    if nav is not None:
                        return nav, account_id, "portfolio.equity(account_id)"
                except Exception:
                    pass

                try:
                    nav = _decimal(account.balance_total())
                    if nav is not None:
                        return nav, account_id, "portfolio.account.balance_total"
                except Exception:
                    pass

        if not account_lookup_succeeded:
            # Compatibility path for older/mocked Portfolio implementations which expose equity
            # but not account(). Real workers normally take the safer account-first path above.
            try:
                nav = _select_equity_value(portfolio.equity(account_id=account_id))
                if nav is not None:
                    return nav, account_id, "portfolio.equity(account_id)"
            except Exception:
                pass

        if cache is not None:
            # A live worker resolved the correct account but still could not obtain account-scoped
            # NAV. Never fall back to venue-wide equity here: on a multi-account TWS connection
            # that could silently size this route from another account's capital.
            return None, account_id, None

    for venue_name in ("IB", "INTERACTIVE_BROKERS", "SMART"):
        try:
            venue = venue_type.from_str(venue_name)
            nav = _select_equity_value(portfolio.equity(venue=venue))
            if nav is not None:
                return nav, account_id, f"portfolio.equity(venue={venue_name})"
        except Exception:
            continue
    return None, account_id, None


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
        account_summary_nav_provider: IbkrAccountSummaryNavProvider | None = None,
        startup_expected_positions: dict[str, Decimal] | None = None,
        startup_bootstrap_error: str | None = None,
    ) -> None:
        self._bridge_store = store
        self._bridge_route_id = route_id
        self._bridge_account = account
        self._bridge_live = live_orders_enabled
        self._order_timeout_seconds = order_timeout_seconds
        self._account_summary_nav_provider = account_summary_nav_provider
        self._pending_resolves: dict[str, tuple[str, Any]] = {}
        self._pending_warms: dict[str, dict[str, Any]] = {}
        self._instrument_requests_inflight: set[str] = set()
        self._quote_subscriptions: set[str] = set()
        self._startup_expected_positions = dict(startup_expected_positions or {})
        self._startup_bootstrap_error = startup_bootstrap_error
        self._startup_reconciled = (
            not bool(self._startup_expected_positions) and not startup_bootstrap_error
        )
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
            self._refresh_pending_warms()
            self._enforce_execution_deadlines()
            for request in self._bridge_store.claim_pending(self._bridge_route_id):
                try:
                    if request["kind"] == "resolve_instrument":
                        self._handle_resolve(request)
                    elif request["kind"] == "warm_instruments":
                        self._handle_warm(request)
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
                self._ensure_quote_subscription(instrument_id)

    def _instrument_key(self, instrument_id: Any) -> str:
        return str(instrument_id)

    def _ensure_instrument_request(self, instrument_id: Any) -> None:
        key = self._instrument_key(instrument_id)
        if self.cache.instrument(instrument_id) is not None:
            self._instrument_requests_inflight.discard(key)
            return
        if key in self._instrument_requests_inflight:
            return
        self._instrument_requests_inflight.add(key)
        try:
            self.request_instrument(instrument_id)
        except Exception:
            self._instrument_requests_inflight.discard(key)
            raise

    def _ensure_quote_subscription(self, instrument_id: Any) -> None:
        key = self._instrument_key(instrument_id)
        if key in self._quote_subscriptions:
            return
        self._quote_subscriptions.add(key)
        try:
            self.subscribe_quotes(instrument_id)
        except Exception:
            self._quote_subscriptions.discard(key)
            raise

    def _handle_resolve(self, request: dict) -> None:
        InstrumentId = _import_nautilus()["InstrumentId"]
        canonical = str(request["payload"]["instrument"])
        nautilus_id = canonical_to_ib_raw(canonical)
        instrument_id = InstrumentId.from_str(nautilus_id)
        if self.cache.instrument(instrument_id) is None:
            self._ensure_instrument_request(instrument_id)
        else:
            self._ensure_quote_subscription(instrument_id)
        self._pending_resolves[request["request_id"]] = (canonical, instrument_id)
        self._refresh_pending_resolves()

    def _handle_warm(self, request: dict) -> None:
        InstrumentId = _import_nautilus()["InstrumentId"]
        pending: dict[str, Any] = {}
        for value in request["payload"].get("instruments", []):
            canonical = str(value)
            instrument_id = InstrumentId.from_str(canonical_to_ib_raw(canonical))
            pending[canonical] = instrument_id
            if self.cache.instrument(instrument_id) is None:
                self._ensure_instrument_request(instrument_id)
            else:
                self._ensure_quote_subscription(instrument_id)
        self._pending_warms[request["request_id"]] = pending
        self._refresh_pending_warms()

    def _refresh_pending_resolves(self) -> None:
        PriceType = _import_nautilus()["PriceType"]
        for request_id, (canonical, instrument_id) in list(self._pending_resolves.items()):
            instrument = self.cache.instrument(instrument_id)
            if instrument is None:
                continue
            # Once loaded, keep a live top-of-book mark available for sizing.
            self._instrument_requests_inflight.discard(self._instrument_key(instrument_id))
            self._ensure_quote_subscription(instrument_id)
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

    def _refresh_pending_warms(self) -> None:
        PriceType = _import_nautilus()["PriceType"]
        for request_id, pending in list(self._pending_warms.items()):
            unresolved: list[str] = []
            for canonical, instrument_id in pending.items():
                instrument = self.cache.instrument(instrument_id)
                if instrument is None:
                    unresolved.append(canonical)
                    continue
                self._instrument_requests_inflight.discard(self._instrument_key(instrument_id))
                self._ensure_quote_subscription(instrument_id)
                price = self.cache.price(instrument_id, PriceType.MID)
                if price is None:
                    price = self.cache.price(instrument_id, PriceType.LAST)
                decimal_price = _decimal(price)
                multiplier = _decimal(getattr(instrument, "multiplier", None)) or Decimal("1")
                size_increment = (
                    _decimal(getattr(instrument, "size_increment", None)) or Decimal("1")
                )
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
                if decimal_price is None:
                    unresolved.append(canonical)
            if not unresolved:
                self._bridge_store.complete(
                    request_id,
                    {"instruments": sorted(pending), "count": len(pending)},
                )
                del self._pending_warms[request_id]

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
            row["nautilus_instrument_id"]: canonical_us_equity_id(row["instrument"])
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
        live_position_map = {position.instrument: position.quantity for position in positions}
        if not self._startup_reconciled:
            if self._startup_bootstrap_error:
                ready = False
                error = error or self._startup_bootstrap_error
            elif live_position_map == self._startup_expected_positions:
                self._startup_reconciled = True
            else:
                ready = False
                expected = len(self._startup_expected_positions)
                actual = len(live_position_map)
                missing = sorted(set(self._startup_expected_positions) - set(live_position_map))
                extra = sorted(set(live_position_map) - set(self._startup_expected_positions))
                details = []
                if missing:
                    details.append("missing=" + ",".join(missing[:8]))
                if extra:
                    details.append("extra=" + ",".join(extra[:8]))
                suffix = ("; " + "; ".join(details)) if details else ""
                error = error or (
                    f"startup broker-position reconciliation incomplete: expected {expected} held "
                    f"instruments, Nautilus has {actual}{suffix}"
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
        nav, nautilus_account_id, nav_source = _resolve_account_net_liquidation(
            self.portfolio,
            self._bridge_account,
            account_id_type=AccountId,
            venue_type=Venue,
            cache=self.cache,
        )
        direct_nav_currency = None
        direct_nav_age_seconds = None
        direct_nav_error = None
        if nav is None and self._account_summary_nav_provider is not None:
            self._account_summary_nav_provider.ensure_refresh()
            direct_nav, direct_nav_currency, direct_nav_error, direct_nav_age_seconds = (
                self._account_summary_nav_provider.current()
            )
            if direct_nav is not None:
                nav = direct_nav
                nav_source = "ibkr.reqAccountSummary(NetLiquidation)"

        if nav is None and ready:
            ready = False
            if direct_nav_error:
                error = error or direct_nav_error
            elif nautilus_account_id is None:
                error = error or (
                    f"Nautilus cache has no account ID matching configured IB account "
                    f"{self._bridge_account}"
                )
            else:
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
                "nautilus_account_id": (
                    None if nautilus_account_id is None else str(nautilus_account_id)
                ),
                "nav_source": nav_source,
                "direct_nav_currency": direct_nav_currency,
                "direct_nav_age_seconds": direct_nav_age_seconds,
                "direct_nav_error": direct_nav_error,
                "startup_reconciled": self._startup_reconciled,
                "startup_expected_position_count": len(self._startup_expected_positions),
            },
        )


def _import_nautilus() -> dict[str, Any]:
    # Keep imports local so non-worker code paths do not eagerly import the heavy Nautilus runtime.
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


def _seed_startup_positions(
    store: NautilusBridgeStore,
    route_id: str,
    positions: dict[str, Decimal],
    *,
    prices: dict[str, Decimal] | None = None,
) -> dict[str, Decimal]:
    """Seed exact broker-held stocks using Conductor canonical IDs.

    IB portfolio callbacks return native symbols. Normalize them at this ingress boundary so
    startup expectations, bridge mappings, strategy targets, and live Nautilus positions all
    compare the same economic identity. Broker-reported marks are persisted at the same time so
    ownership bootstrap can value existing holdings without a second quote warm-up cycle.
    """
    canonical_positions: dict[str, Decimal] = {}
    prices = prices or {}
    for instrument, quantity in positions.items():
        canonical = canonical_us_equity_id(instrument)
        canonical_positions[canonical] = canonical_positions.get(canonical, ZERO) + quantity
        nautilus_id = canonical_to_ib_raw(canonical)
        store.upsert_instrument(
            route_id,
            canonical,
            nautilus_instrument_id=nautilus_id,
            price=prices.get(instrument),
            asset_class="equity",
            venue="SMART",
        )
    canonical_positions = {
        instrument: quantity
        for instrument, quantity in canonical_positions.items()
        if quantity != ZERO
    }
    store.replace_positions(
        route_id,
        [
            BrokerPosition(canonical, quantity, route_id)
            for canonical, quantity in sorted(canonical_positions.items())
        ],
    )
    return canonical_positions


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
    startup_expected_positions: dict[str, Decimal] = {}
    startup_bootstrap_error: str | None = None
    try:
        startup_snapshot = query_ibkr_stock_portfolio_snapshot(
            host=route_cfg.host,
            port=route_cfg.port,
            client_id=route_cfg.account_summary_client_id,
            account=route_cfg.route.account,
            timeout_seconds=max(10.0, float(route_cfg.account_summary_timeout_seconds)),
        )
        startup_expected_positions = _seed_startup_positions(
            store,
            route_id,
            startup_snapshot.positions,
            prices=startup_snapshot.prices,
        )
    except Exception as exc:  # fail closed: startup reconciliation must be exhaustive
        startup_bootstrap_error = f"IBKR position bootstrap failed: {exc}"

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
    account_summary_nav_provider = (
        IbkrAccountSummaryNavProvider(
            host=route_cfg.host,
            port=route_cfg.port,
            client_id=route_cfg.account_summary_client_id,
            account=route_cfg.route.account,
            refresh_seconds=route_cfg.account_summary_refresh_seconds,
            stale_after_seconds=route_cfg.account_summary_stale_after_seconds,
            timeout_seconds=route_cfg.account_summary_timeout_seconds,
        )
        if route_cfg.account_summary_fallback_enabled
        else None
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
                account_summary_nav_provider=account_summary_nav_provider,
                startup_expected_positions=startup_expected_positions,
                startup_bootstrap_error=startup_bootstrap_error,
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
        error=startup_bootstrap_error,
        details={
            "nautilus_version": installed,
            "preloaded_instruments": preload,
            "startup_expected_position_count": len(startup_expected_positions),
        },
    )
    node.run()
