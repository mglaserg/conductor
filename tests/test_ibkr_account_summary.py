from __future__ import annotations

import threading
import time
from decimal import Decimal

import pytest

from conductor.adapters.ibkr_account_summary import (
    IbkrAccountSummaryError,
    IbkrAccountSummaryNavProvider,
    query_ibkr_net_liquidation,
)


class FakeSummaryApp:
    def __init__(self, account: str, rows: list[tuple[str, str, str, str]]) -> None:
        self.account = account
        self.rows = rows
        self.ready_event = threading.Event()
        self.summary_done_event = threading.Event()
        self.matches: list[tuple[Decimal, str | None]] = []
        self.observed_accounts: set[str] = set()
        self.fatal_errors: list[str] = []
        self.connected = None
        self.cancelled = False
        self.disconnected = False

    def connect(self, host: str, port: int, clientId: int) -> None:  # noqa: N803
        self.connected = (host, port, clientId)
        self.ready_event.set()

    def run(self) -> None:
        return

    def reqAccountSummary(self, req_id: int, group: str, tags: str) -> None:  # noqa: N802
        assert group == "All"
        assert tags == "NetLiquidation"
        for account, tag, value, currency in self.rows:
            self.observed_accounts.add(account)
            if account == self.account and tag == "NetLiquidation":
                self.matches.append((Decimal(value), currency or None))
        self.summary_done_event.set()

    def cancelAccountSummary(self, req_id: int) -> None:  # noqa: N802
        self.cancelled = True

    def disconnect(self) -> None:
        self.disconnected = True


def test_direct_summary_accepts_only_exact_configured_account() -> None:
    app = FakeSummaryApp(
        "U_TLAQ",
        [
            ("U_MAIN", "NetLiquidation", "250000", "USD"),
            ("U_TLAQ", "NetLiquidation", "75000.25", "USD"),
        ],
    )

    nav, currency = query_ibkr_net_liquidation(
        host="127.0.0.1",
        port=7496,
        client_id=11312,
        account="U_TLAQ",
        app_factory=lambda _account: app,
    )

    assert nav == Decimal("75000.25")
    assert currency == "USD"
    assert app.connected == ("127.0.0.1", 7496, 11312)
    assert app.cancelled is True
    assert app.disconnected is True


def test_direct_summary_fails_closed_when_only_other_account_is_returned() -> None:
    app = FakeSummaryApp(
        "U_TLAQ",
        [("U_MAIN", "NetLiquidation", "250000", "USD")],
    )

    with pytest.raises(IbkrAccountSummaryError, match="no NetLiquidation for U_TLAQ"):
        query_ibkr_net_liquidation(
            host="127.0.0.1",
            port=7496,
            client_id=11312,
            account="U_TLAQ",
            app_factory=lambda _account: app,
        )


def test_direct_summary_rejects_conflicting_values_for_same_account() -> None:
    app = FakeSummaryApp(
        "U_TLAQ",
        [
            ("U_TLAQ", "NetLiquidation", "75000", "USD"),
            ("U_TLAQ", "NetLiquidation", "76000", "USD"),
        ],
    )

    with pytest.raises(IbkrAccountSummaryError, match="conflicting NetLiquidation"):
        query_ibkr_net_liquidation(
            host="127.0.0.1",
            port=7496,
            client_id=11312,
            account="U_TLAQ",
            app_factory=lambda _account: app,
        )


def test_nav_provider_refreshes_off_thread_and_exposes_fresh_value() -> None:
    calls: list[dict] = []

    def query(**kwargs):
        calls.append(kwargs)
        return Decimal("81234.56"), "USD"

    provider = IbkrAccountSummaryNavProvider(
        host="127.0.0.1",
        port=7496,
        client_id=11312,
        account="U_TLAQ",
        refresh_seconds=0.05,
        stale_after_seconds=0.2,
        timeout_seconds=0.1,
        query=query,
    )
    provider.ensure_refresh()
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        nav, currency, error, age = provider.current()
        if nav is not None:
            break
        time.sleep(0.01)

    assert nav == Decimal("81234.56")
    assert currency == "USD"
    assert error is None
    assert age is not None and age < 0.2
    assert calls[0]["account"] == "U_TLAQ"
    assert calls[0]["client_id"] == 11312
