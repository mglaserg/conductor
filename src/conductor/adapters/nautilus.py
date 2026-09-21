from __future__ import annotations

from importlib import import_module
from importlib.metadata import PackageNotFoundError, version


class NautilusVersionError(RuntimeError):
    pass


def check_nautilus_v2() -> str:
    """Fail fast unless the optional NautilusTrader v2 runtime is installed.

    Conductor deliberately keeps Nautilus types outside the domain model. This
    module is the anti-corruption boundary where the concrete live/sandbox bridge
    will evolve while the NautilusTrader v2 API stabilizes.
    """
    try:
        installed = version("nautilus_trader")
    except PackageNotFoundError as exc:
        raise NautilusVersionError(
            "NautilusTrader is not installed. Install the optional 'nautilus' extra."
        ) from exc

    if not installed.startswith("2."):
        raise NautilusVersionError(
            f"Conductor targets NautilusTrader v2; found {installed}. "
            "Do not bind Conductor to the v1 API."
        )

    import_module("nautilus_trader")
    return installed


def main() -> None:
    try:
        installed = check_nautilus_v2()
    except NautilusVersionError as exc:
        print(f"NAUTILUS BRIDGE: NOT READY — {exc}")
        raise SystemExit(2) from exc
    print(f"NAUTILUS BRIDGE: RUNTIME FOUND — nautilus_trader {installed}")
    print("Conductor domain model remains independent of Nautilus runtime types.")
    print("Use paper/shadow mode first; V0.4 can route through the persistent Nautilus worker.")
