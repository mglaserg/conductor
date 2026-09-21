from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from conductor.ledger import ConductorLedger


@dataclass(frozen=True, slots=True)
class ExecutionRoute:
    route_id: str
    node_id: str
    adapter: str
    account: str
    venue: str | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("route_id", self.route_id),
            ("node_id", self.node_id),
            ("adapter", self.adapter),
            ("account", self.account),
        ):
            if not value.strip():
                raise ValueError(f"{name} must be non-empty")


class RouteRegistry:
    def __init__(self, routes: Mapping[str, ExecutionRoute]) -> None:
        self.routes = dict(routes)
        if len(self.routes) != len(set(self.routes)):
            raise ValueError("route IDs must be unique")

    def get(self, route_id: str) -> ExecutionRoute:
        try:
            return self.routes[route_id]
        except KeyError as exc:
            raise KeyError(f"unknown execution route: {route_id}") from exc


@dataclass(frozen=True, slots=True)
class ResolvedInstrument:
    canonical_id: str
    route_id: str
    symbol: str
    asset_class: str
    venue: str | None
    currency: str | None
    broker_id: str | None = None
    metadata: Mapping[str, object] | None = None


class CanonicalInstrumentResolver:
    """Programmatic resolver for canonical IDs, backed by the SQLite identity cache.

    V0.4 directly supports the migration universe (US equities) and Hyperliquid IDs.
    Broker qualification can enrich the cached broker_id later without changing IDs.
    """

    def __init__(self, ledger: ConductorLedger) -> None:
        self.ledger = ledger

    def resolve(self, canonical_id: str, route_id: str) -> ResolvedInstrument:
        cached = self.ledger.instrument_cache_entry(canonical_id, route_id)
        if cached is not None:
            import json

            return ResolvedInstrument(
                canonical_id=canonical_id,
                route_id=route_id,
                symbol=cached["symbol"],
                asset_class=cached["asset_class"],
                venue=cached["venue"],
                currency=cached["currency"],
                broker_id=cached["broker_id"],
                metadata=json.loads(cached["metadata_json"]),
            )

        if canonical_id.startswith("EQ.US."):
            symbol = canonical_id.removeprefix("EQ.US.")
            if not symbol:
                raise ValueError(f"invalid US equity canonical ID: {canonical_id}")
            resolved = ResolvedInstrument(
                canonical_id=canonical_id,
                route_id=route_id,
                symbol=symbol,
                asset_class="equity",
                venue="SMART",
                currency="USD",
            )
        elif canonical_id.startswith("CRYPTO.HL."):
            symbol = canonical_id.removeprefix("CRYPTO.HL.")
            if not symbol:
                raise ValueError(f"invalid Hyperliquid canonical ID: {canonical_id}")
            resolved = ResolvedInstrument(
                canonical_id=canonical_id,
                route_id=route_id,
                symbol=symbol,
                asset_class="crypto_perp" if symbol.endswith("-PERP") else "crypto",
                venue="HYPERLIQUID",
                currency="USDC",
            )
        else:
            raise ValueError(
                f"no programmatic resolver registered for canonical instrument {canonical_id}"
            )

        self.ledger.upsert_instrument_cache(
            canonical_id=resolved.canonical_id,
            route_id=resolved.route_id,
            symbol=resolved.symbol,
            asset_class=resolved.asset_class,
            broker_id=resolved.broker_id,
            venue=resolved.venue,
            currency=resolved.currency,
            metadata=dict(resolved.metadata or {}),
        )
        return resolved
