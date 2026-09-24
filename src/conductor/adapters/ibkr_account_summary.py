from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable


class IbkrAccountSummaryError(RuntimeError):
    """Raised when the direct TWS account-summary fallback cannot produce safe NAV."""




@dataclass(frozen=True, slots=True)
class IbkrStockPortfolioSnapshot:
    positions: dict[str, Decimal]
    prices: dict[str, Decimal]

@dataclass(frozen=True, slots=True)
class IbkrNavSnapshot:
    net_liquidation: Decimal | None
    currency: str | None
    error: str | None
    fetched_at_monotonic: float


def _parse_decimal(value: Any) -> Decimal:
    text = str(value).replace(",", "").strip()
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise IbkrAccountSummaryError(f"invalid IB NetLiquidation value {value!r}") from exc


def _make_ibapi_app(configured_account: str):
    # Keep imports local so unit tests and non-IB runtimes do not need to import the TWS API.
    from ibapi.client import EClient
    from ibapi.wrapper import EWrapper

    class AccountSummaryApp(EWrapper, EClient):
        def __init__(self) -> None:
            EWrapper.__init__(self)
            EClient.__init__(self, self)
            self.ready_event = threading.Event()
            self.summary_done_event = threading.Event()
            self.matches: list[tuple[Decimal, str | None]] = []
            self.observed_accounts: set[str] = set()
            self.fatal_errors: list[str] = []

        def nextValidId(self, orderId: int) -> None:  # noqa: N802 - IB callback API
            self.ready_event.set()

        def accountSummary(  # noqa: N802 - IB callback API
            self,
            reqId: int,
            account: str,
            tag: str,
            value: str,
            currency: str,
        ) -> None:
            self.observed_accounts.add(str(account))
            if str(account) != configured_account or str(tag) != "NetLiquidation":
                return
            try:
                nav = _parse_decimal(value)
            except IbkrAccountSummaryError as exc:
                self.fatal_errors.append(str(exc))
                return
            self.matches.append((nav, str(currency) or None))

        def accountSummaryEnd(self, reqId: int) -> None:  # noqa: N802 - IB callback API
            self.summary_done_event.set()

        def error(self, reqId: int, errorCode: int, errorString: str, *args: Any) -> None:
            # Connectivity/info codes are noisy and not request failures. These codes can prevent
            # the fallback from connecting or completing and should fail closed.
            if int(errorCode) in {321, 326, 502, 504, 1100, 1300}:
                self.fatal_errors.append(f"IB error {errorCode}: {errorString}")
                if int(errorCode) in {321, 326, 502, 504, 1300}:
                    self.ready_event.set()
                    self.summary_done_event.set()

    return AccountSummaryApp()


def query_ibkr_net_liquidation(
    *,
    host: str,
    port: int,
    client_id: int,
    account: str,
    timeout_seconds: float = 5.0,
    app_factory: Callable[[str], Any] | None = None,
) -> tuple[Decimal, str | None]:
    """Read exact-account NetLiquidation through a separate TWS API connection.

    The request is deliberately ``group='All'`` and then filtered by the callback's *native*
    account code. This is safer than asking Nautilus' namespaced account object for venue-wide
    equity on a linked/multi-account TWS login.
    """
    if not account or account == "paper":
        raise IbkrAccountSummaryError("a native IB account code is required")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    factory = app_factory or _make_ibapi_app
    app = factory(account)
    request_id = 900001
    thread: threading.Thread | None = None
    try:
        app.connect(host, int(port), clientId=int(client_id))
        thread = threading.Thread(
            target=app.run,
            name=f"conductor-ib-summary-{account}",
            daemon=True,
        )
        thread.start()
        if not app.ready_event.wait(timeout_seconds):
            raise IbkrAccountSummaryError(
                f"timed out waiting for TWS API readiness on {host}:{port} client_id={client_id}"
            )
        if app.fatal_errors:
            raise IbkrAccountSummaryError(app.fatal_errors[-1])

        app.reqAccountSummary(request_id, "All", "NetLiquidation")
        if not app.summary_done_event.wait(timeout_seconds):
            raise IbkrAccountSummaryError(
                f"timed out waiting for NetLiquidation account summary for {account}"
            )
        if app.fatal_errors:
            raise IbkrAccountSummaryError(app.fatal_errors[-1])

        matches = list(app.matches)
        if not matches:
            observed = ", ".join(sorted(app.observed_accounts)) or "none"
            raise IbkrAccountSummaryError(
                f"TWS account summary returned no NetLiquidation for {account}; "
                f"observed accounts: {observed}"
            )
        values = {nav for nav, _currency in matches}
        if len(values) != 1:
            raise IbkrAccountSummaryError(
                f"conflicting NetLiquidation values returned for {account}: "
                + ", ".join(str(value) for value in sorted(values))
            )
        nav = next(iter(values))
        currency = next((currency for _value, currency in matches if currency), None)
        return nav, currency
    finally:
        try:
            app.cancelAccountSummary(request_id)
        except Exception:
            pass
        try:
            app.disconnect()
        except Exception:
            pass
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)



def query_ibkr_stock_portfolio_snapshot(
    *,
    host: str,
    port: int,
    client_id: int,
    account: str,
    timeout_seconds: float = 10.0,
    app_factory: Callable[[str], Any] | None = None,
) -> IbkrStockPortfolioSnapshot:
    """Read exact-account stock positions and broker portfolio marks in one TWS snapshot.

    ``reqAccountUpdates`` supplies ``updatePortfolio`` callbacks with the native account's
    current position and market value.  Using those broker-reported marks lets Conductor seed the
    bridge before Nautilus starts, so one-time ownership bootstrap does not need to subscribe to
    dozens of quotes merely to back-solve virtual cash.
    """
    if not account or account == "paper":
        raise IbkrAccountSummaryError("a native IB account code is required")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    if app_factory is None:
        from ibapi.client import EClient
        from ibapi.wrapper import EWrapper

        class PortfolioApp(EWrapper, EClient):
            def __init__(self) -> None:
                EWrapper.__init__(self)
                EClient.__init__(self, self)
                self.ready_event = threading.Event()
                self.download_done_event = threading.Event()
                self.positions: dict[str, Decimal] = {}
                self.market_values: dict[str, Decimal] = {}
                self.market_prices: dict[str, Decimal] = {}
                self.unsupported: list[str] = []
                self.fatal_errors: list[str] = []

            def nextValidId(self, orderId: int) -> None:  # noqa: N802 - IB callback API
                self.ready_event.set()

            def updatePortfolio(  # noqa: N802 - IB callback API
                self,
                contract,
                position,
                marketPrice,
                marketValue,
                averageCost,
                unrealizedPNL,
                realizedPNL,
                accountName,
            ) -> None:
                if str(accountName) != account:
                    return
                qty = _parse_decimal(position)
                if qty == 0:
                    return
                sec_type = str(getattr(contract, "secType", "") or "").upper()
                symbol = str(getattr(contract, "symbol", "") or "").strip()
                if sec_type != "STK" or not symbol:
                    self.unsupported.append(
                        f"{symbol or '<unknown>'}:{sec_type or '<unknown>'}"
                    )
                    return
                self.positions[symbol] = self.positions.get(symbol, Decimal("0")) + qty
                value = _parse_decimal(marketValue)
                self.market_values[symbol] = self.market_values.get(
                    symbol, Decimal("0")
                ) + value
                price = _parse_decimal(marketPrice)
                if price > 0:
                    self.market_prices[symbol] = price

            def accountDownloadEnd(self, accountName: str) -> None:  # noqa: N802 - IB callback API
                if str(accountName) == account:
                    self.download_done_event.set()

            def error(self, reqId: int, errorCode: int, errorString: str, *args: Any) -> None:
                if int(errorCode) in {321, 326, 502, 504, 1100, 1300}:
                    self.fatal_errors.append(f"IB error {errorCode}: {errorString}")
                    if int(errorCode) in {321, 326, 502, 504, 1300}:
                        self.ready_event.set()
                        self.download_done_event.set()

        app = PortfolioApp()
    else:
        app = app_factory(account)

    thread: threading.Thread | None = None
    try:
        app.connect(host, int(port), clientId=int(client_id))
        thread = threading.Thread(
            target=app.run,
            name=f"conductor-ib-portfolio-{account}",
            daemon=True,
        )
        thread.start()
        if not app.ready_event.wait(timeout_seconds):
            raise IbkrAccountSummaryError(
                f"timed out waiting for TWS API readiness on {host}:{port} client_id={client_id}"
            )
        if app.fatal_errors:
            raise IbkrAccountSummaryError(app.fatal_errors[-1])

        app.reqAccountUpdates(True, account)
        if not app.download_done_event.wait(timeout_seconds):
            raise IbkrAccountSummaryError(
                f"timed out waiting for broker portfolio snapshot for {account}"
            )
        if app.fatal_errors:
            raise IbkrAccountSummaryError(app.fatal_errors[-1])
        unsupported = list(getattr(app, "unsupported", []))
        if unsupported:
            raise IbkrAccountSummaryError(
                "IB account contains unsupported non-stock positions for the equities-only "
                f"worker: {', '.join(sorted(unsupported))}"
            )

        positions = {
            str(symbol): Decimal(str(qty))
            for symbol, qty in dict(getattr(app, "positions", {})).items()
            if Decimal(str(qty)) != 0
        }
        market_values = {
            str(symbol): Decimal(str(value))
            for symbol, value in dict(getattr(app, "market_values", {})).items()
        }
        reported_prices = {
            str(symbol): Decimal(str(price))
            for symbol, price in dict(getattr(app, "market_prices", {})).items()
        }
        prices: dict[str, Decimal] = {}
        for symbol, qty in positions.items():
            market_value = market_values.get(symbol)
            if market_value is not None and market_value != 0:
                derived = market_value / qty
                if derived > 0:
                    prices[symbol] = derived
                    continue
            reported = reported_prices.get(symbol)
            if reported is not None and reported > 0:
                prices[symbol] = reported

        return IbkrStockPortfolioSnapshot(positions=positions, prices=prices)
    finally:
        try:
            app.reqAccountUpdates(False, account)
        except Exception:
            pass
        try:
            app.disconnect()
        except Exception:
            pass
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)


def query_ibkr_stock_positions(
    *,
    host: str,
    port: int,
    client_id: int,
    account: str,
    timeout_seconds: float = 10.0,
    app_factory: Callable[[str], Any] | None = None,
) -> dict[str, Decimal]:
    """Read the exact account's non-zero IB stock positions before Nautilus startup.

    Nautilus performs execution reconciliation before Python strategy callbacks run. If held
    instruments are absent from its instrument provider cache, those broker positions are skipped
    during reconciliation. This lightweight preflight discovers the held stock symbols first so the
    worker can include them in ``load_ids`` before building the live node.
    """
    if not account or account == "paper":
        raise IbkrAccountSummaryError("a native IB account code is required")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    if app_factory is None:
        from ibapi.client import EClient
        from ibapi.wrapper import EWrapper

        class PositionApp(EWrapper, EClient):
            def __init__(self) -> None:
                EWrapper.__init__(self)
                EClient.__init__(self, self)
                self.ready_event = threading.Event()
                self.positions_done_event = threading.Event()
                self.positions: dict[str, Decimal] = {}
                self.unsupported: list[str] = []
                self.fatal_errors: list[str] = []

            def nextValidId(self, orderId: int) -> None:  # noqa: N802 - IB callback API
                self.ready_event.set()

            def position(self, callback_account, contract, position, avgCost) -> None:  # noqa: N802
                if str(callback_account) != account:
                    return
                qty = _parse_decimal(position)
                if qty == 0:
                    return
                sec_type = str(getattr(contract, "secType", "") or "").upper()
                symbol = str(getattr(contract, "symbol", "") or "").strip()
                if sec_type != "STK" or not symbol:
                    self.unsupported.append(
                        f"{symbol or '<unknown>'}:{sec_type or '<unknown>'}"
                    )
                    return
                self.positions[symbol] = self.positions.get(symbol, Decimal("0")) + qty

            def positionEnd(self) -> None:  # noqa: N802 - IB callback API
                self.positions_done_event.set()

            def error(self, reqId: int, errorCode: int, errorString: str, *args: Any) -> None:
                if int(errorCode) in {321, 326, 502, 504, 1100, 1300}:
                    self.fatal_errors.append(f"IB error {errorCode}: {errorString}")
                    if int(errorCode) in {321, 326, 502, 504, 1300}:
                        self.ready_event.set()
                        self.positions_done_event.set()

        app = PositionApp()
    else:
        app = app_factory(account)

    thread: threading.Thread | None = None
    try:
        app.connect(host, int(port), clientId=int(client_id))
        thread = threading.Thread(
            target=app.run,
            name=f"conductor-ib-positions-{account}",
            daemon=True,
        )
        thread.start()
        if not app.ready_event.wait(timeout_seconds):
            raise IbkrAccountSummaryError(
                f"timed out waiting for TWS API readiness on {host}:{port} client_id={client_id}"
            )
        if app.fatal_errors:
            raise IbkrAccountSummaryError(app.fatal_errors[-1])
        app.reqPositions()
        if not app.positions_done_event.wait(timeout_seconds):
            raise IbkrAccountSummaryError(
                f"timed out waiting for broker positions for {account}"
            )
        if app.fatal_errors:
            raise IbkrAccountSummaryError(app.fatal_errors[-1])
        unsupported = list(getattr(app, "unsupported", []))
        if unsupported:
            raise IbkrAccountSummaryError(
                "IB account contains unsupported non-stock positions for the equities-only "
                f"worker: {', '.join(sorted(unsupported))}"
            )
        return {
            str(symbol): Decimal(str(qty))
            for symbol, qty in dict(getattr(app, "positions", {})).items()
            if Decimal(str(qty)) != 0
        }
    finally:
        try:
            app.cancelPositions()
        except Exception:
            pass
        try:
            app.disconnect()
        except Exception:
            pass
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)


class IbkrAccountSummaryNavProvider:
    """Non-blocking, expiring cache for direct IB account-summary NAV.

    The Nautilus strategy callback is latency-sensitive, so network I/O happens on a daemon
    thread. A successful value is usable only for ``stale_after_seconds``; failed refreshes do not
    keep an old value alive forever.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        client_id: int,
        account: str,
        refresh_seconds: float = 10.0,
        stale_after_seconds: float = 30.0,
        timeout_seconds: float = 5.0,
        query: Callable[..., tuple[Decimal, str | None]] = query_ibkr_net_liquidation,
    ) -> None:
        if refresh_seconds <= 0:
            raise ValueError("refresh_seconds must be positive")
        if stale_after_seconds <= refresh_seconds:
            raise ValueError("stale_after_seconds must exceed refresh_seconds")
        self.host = host
        self.port = int(port)
        self.client_id = int(client_id)
        self.account = account
        self.refresh_seconds = float(refresh_seconds)
        self.stale_after_seconds = float(stale_after_seconds)
        self.timeout_seconds = float(timeout_seconds)
        self._query = query
        self._lock = threading.Lock()
        self._refreshing = False
        self._last_attempt = 0.0
        self._snapshot: IbkrNavSnapshot | None = None

    def ensure_refresh(self) -> None:
        now = time.monotonic()
        with self._lock:
            if self._refreshing:
                return
            if self._last_attempt and now - self._last_attempt < self.refresh_seconds:
                return
            self._refreshing = True
            self._last_attempt = now
        threading.Thread(
            target=self._refresh,
            name=f"conductor-ib-nav-{self.account}",
            daemon=True,
        ).start()

    def _refresh(self) -> None:
        now = time.monotonic()
        try:
            nav, currency = self._query(
                host=self.host,
                port=self.port,
                client_id=self.client_id,
                account=self.account,
                timeout_seconds=self.timeout_seconds,
            )
            snapshot = IbkrNavSnapshot(nav, currency, None, now)
        except Exception as exc:
            snapshot = IbkrNavSnapshot(None, None, str(exc), now)
        with self._lock:
            self._snapshot = snapshot
            self._refreshing = False

    def current(self) -> tuple[Decimal | None, str | None, str | None, float | None]:
        now = time.monotonic()
        with self._lock:
            snapshot = self._snapshot
            refreshing = self._refreshing
        if snapshot is None:
            return None, None, "direct IB account summary pending", None
        age = max(0.0, now - snapshot.fetched_at_monotonic)
        if snapshot.net_liquidation is not None and age <= self.stale_after_seconds:
            return snapshot.net_liquidation, snapshot.currency, None, age
        if snapshot.net_liquidation is not None:
            return None, snapshot.currency, "direct IB account summary is stale", age
        if refreshing and snapshot.error:
            return None, None, f"refreshing after: {snapshot.error}", age
        return None, None, snapshot.error or "direct IB account summary unavailable", age
