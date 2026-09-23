from __future__ import annotations

from decimal import Decimal

from conductor.adapters.nautilus_ibkr_worker import _resolve_account_net_liquidation


class FakeAccountId(str):
    pass


class FakeVenue:
    @classmethod
    def from_str(cls, value: str) -> str:
        return value


class FakeMoney:
    def __init__(self, value: str) -> None:
        self.value = value

    def as_decimal(self) -> Decimal:
        return Decimal(self.value)


class AccountScopedPortfolio:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def equity(self, *, account_id=None, venue=None):
        self.calls.append(("equity", account_id if account_id is not None else venue))
        if account_id == FakeAccountId("U_MAIN"):
            return {"USD": FakeMoney("250000.50")}
        raise AssertionError("venue fallback should not be used")

    def account(self, *, account_id=None):
        raise AssertionError("account fallback should not be used")


def test_nav_prefers_account_scoped_equity() -> None:
    portfolio = AccountScopedPortfolio()

    nav = _resolve_account_net_liquidation(
        portfolio,
        "U_MAIN",
        account_id_type=FakeAccountId,
        venue_type=FakeVenue,
    )

    assert nav == Decimal("250000.50")
    assert portfolio.calls == [("equity", FakeAccountId("U_MAIN"))]


class FakeAccount:
    def balance_total(self) -> FakeMoney:
        return FakeMoney("125000")


class AccountFallbackPortfolio:
    def equity(self, *, account_id=None, venue=None):
        if account_id is not None:
            return {}
        raise AssertionError("venue fallback should not be used")

    def account(self, *, account_id=None):
        assert account_id == FakeAccountId("U_TLAQ")
        return FakeAccount()


def test_nav_falls_back_to_account_balance() -> None:
    nav = _resolve_account_net_liquidation(
        AccountFallbackPortfolio(),
        "U_TLAQ",
        account_id_type=FakeAccountId,
        venue_type=FakeVenue,
    )

    assert nav == Decimal("125000")


class LegacyVenuePortfolio:
    def equity(self, *, account_id=None, venue=None):
        if account_id is not None:
            raise TypeError("legacy Nautilus signature")
        if venue == "SMART":
            return {"USD": FakeMoney("99000")}
        return {}

    def account(self, *, account_id=None):
        raise TypeError("legacy Nautilus signature")


def test_nav_retains_venue_fallback_for_older_nautilus() -> None:
    nav = _resolve_account_net_liquidation(
        LegacyVenuePortfolio(),
        "U_MAIN",
        account_id_type=FakeAccountId,
        venue_type=FakeVenue,
    )

    assert nav == Decimal("99000")
