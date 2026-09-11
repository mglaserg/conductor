from __future__ import annotations

from importlib import import_module
from importlib.metadata import PackageNotFoundError, version


class NautilusVersionError(RuntimeError):
    pass


def check_nautilus_v2() -> str:
    """Fail fast if the runtime is not the v2 API Conductor targets.

    We deliberately do not import concrete v2 execution classes in the Conductor
    domain package. The live bridge will live here and can evolve independently as
    NautilusTrader v2 stabilizes.
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
