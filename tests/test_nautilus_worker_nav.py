from __future__ import annotations

from decimal import Decimal

from conductor.adapters.nautilus_ibkr_worker import _resolve_account_net_liquidation


class FakeAccountId(str):
    def get_id(self) -> str:
        return str(self).rsplit("-", 1)[-1]


class FakeVenue:
    @classmethod
    def from_str(cls, value: str) -> str:
        return value


class FakeMoney:
    def __init__(self, value: str) -> None:
        self.value = value

    def as_decimal(self) -> Decimal:
        return Decimal(self.value)


class FakeCache:
    def __init__(self, mapping: dict[str, FakeAccountId | None]) -> None:
        self.mapping = mapping

    def account_id(self, venue: str):
        return self.mapping.get(venue)


class AccountScopedPortfolio:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def equity(self, *, account_id=None, venue=None):
        self.calls.append(("equity", account_id if account_id is not None else venue))
        if account_id == FakeAccountId("IB-U_MAIN"):
            return {"USD": FakeMoney("250000.50")}
        raise AssertionError("venue fallback should not be used")

    def account(self, *, account_id=None):
        raise AssertionError("account fallback should not be used")


def test_nav_prefers_cached_namespaced_account_id() -> None:
    portfolio = AccountScopedPortfolio()
    cache = FakeCache({"IB": FakeAccountId("IB-U_MAIN")})

    nav, account_id, source = _resolve_account_net_liquidation(
        portfolio,
        "U_MAIN",
        account_id_type=FakeAccountId,
        venue_type=FakeVenue,
        cache=cache,
    )

    assert nav == Decimal("250000.50")
    assert account_id == FakeAccountId("IB-U_MAIN")
    assert source == "portfolio.equity(account_id)"
    assert portfolio.calls == [("equity", FakeAccountId("IB-U_MAIN"))]


class FakeAccount:
    def balance_total(self) -> FakeMoney:
        return FakeMoney("125000")


class AccountFallbackPortfolio:
    def equity(self, *, account_id=None, venue=None):
        if account_id is not None:
            return {}
        raise AssertionError("venue fallback should not be used")

    def account(self, *, account_id=None):
        assert account_id == FakeAccountId("IB-LIVE-U_TLAQ")
        return FakeAccount()


def test_nav_falls_back_to_account_balance() -> None:
    nav, account_id, source = _resolve_account_net_liquidation(
        AccountFallbackPortfolio(),
        "U_TLAQ",
        account_id_type=FakeAccountId,
        venue_type=FakeVenue,
        cache=FakeCache({"IB": FakeAccountId("IB-LIVE-U_TLAQ")}),
    )

    assert nav == Decimal("125000")
    assert account_id == FakeAccountId("IB-LIVE-U_TLAQ")
    assert source == "portfolio.account.balance_total"


class LegacyVenuePortfolio:
    def equity(self, *, account_id=None, venue=None):
        if account_id is not None:
            raise TypeError("legacy Nautilus signature")
        if venue == "SMART":
            return {"USD": FakeMoney("99000")}
        return {}

    def account(self, *, account_id=None):
        raise TypeError("legacy Nautilus signature")


def test_nav_retains_venue_fallback_for_older_nautilus_without_cache() -> None:
    nav, account_id, source = _resolve_account_net_liquidation(
        LegacyVenuePortfolio(),
        "U_MAIN",
        account_id_type=FakeAccountId,
        venue_type=FakeVenue,
    )

    assert nav == Decimal("99000")
    assert account_id == FakeAccountId("U_MAIN")
    assert source == "portfolio.equity(venue=SMART)"


def test_live_cache_account_mismatch_fails_closed() -> None:
    nav, account_id, source = _resolve_account_net_liquidation(
        LegacyVenuePortfolio(),
        "U_MAIN",
        account_id_type=FakeAccountId,
        venue_type=FakeVenue,
        cache=FakeCache({"IB": FakeAccountId("IB-U_SOMEONE_ELSE")}),
    )

    assert nav is None
    assert account_id is None
    assert source is None


class FakeEvent:
    def __init__(self, info) -> None:
        self.info = info


class EmptyIbAccountWithInfo:
    base_currency = None

    @property
    def last_event(self):
        return FakeEvent({"NetLiquidation": "75000.12"})

    def balances(self):
        return []

    def balance_total(self):
        raise AssertionError("must not touch balance_total for empty account state")


class EmptyIbAccountWithoutInfo:
    base_currency = None

    @property
    def last_event(self):
        return FakeEvent({})

    def balances(self):
        return []

    def balance_total(self):
        raise AssertionError("must not touch balance_total for empty account state")


class EmptyAccountPortfolio:
    def __init__(self, account) -> None:
        self._account = account
        self.equity_called = False

    def account(self, *, account_id=None):
        assert account_id == FakeAccountId("IB-U_TLAQ")
        return self._account

    def equity(self, *, account_id=None, venue=None):
        self.equity_called = True
        raise AssertionError("must not enter Nautilus equity valuation for empty account state")


def test_nav_reads_reported_net_liquidation_before_portfolio_valuation() -> None:
    portfolio = EmptyAccountPortfolio(EmptyIbAccountWithInfo())

    nav, account_id, source = _resolve_account_net_liquidation(
        portfolio,
        "U_TLAQ",
        account_id_type=FakeAccountId,
        venue_type=FakeVenue,
        cache=FakeCache({"IB": FakeAccountId("IB-U_TLAQ")}),
    )

    assert nav == Decimal("75000.12")
    assert account_id == FakeAccountId("IB-U_TLAQ")
    assert source == "portfolio.account.last_event.info.NetLiquidation"
    assert portfolio.equity_called is False


def test_empty_account_state_returns_not_ready_without_entering_rust_equity() -> None:
    portfolio = EmptyAccountPortfolio(EmptyIbAccountWithoutInfo())

    nav, account_id, source = _resolve_account_net_liquidation(
        portfolio,
        "U_TLAQ",
        account_id_type=FakeAccountId,
        venue_type=FakeVenue,
        cache=FakeCache({"IB": FakeAccountId("IB-U_TLAQ")}),
    )

    assert nav is None
    assert account_id == FakeAccountId("IB-U_TLAQ")
    assert source is None
    assert portfolio.equity_called is False


class LiveNoNavPortfolio:
    def account(self, *, account_id=None):
        return None

    def equity(self, *, account_id=None, venue=None):
        if account_id is not None:
            return {}
        raise AssertionError("live worker must never use venue-wide NAV fallback")


def test_live_worker_never_uses_venue_wide_nav_after_account_resolution() -> None:
    nav, account_id, source = _resolve_account_net_liquidation(
        LiveNoNavPortfolio(),
        "U_TLAQ",
        account_id_type=FakeAccountId,
        venue_type=FakeVenue,
        cache=FakeCache({"IB": FakeAccountId("IB-U_TLAQ")}),
    )

    assert nav is None
    assert account_id == FakeAccountId("IB-U_TLAQ")
    assert source is None
