"""RPSchteroids adapter for its native absolute target-share output.

Set RPS_TARGET_CALLABLE to ``package.module:function``. The callable must return a
mapping of ticker -> absolute target shares. This keeps existing RPS portfolio logic
inside RPS; Conductor only normalizes/owns execution.
"""
from __future__ import annotations

import importlib
import os
from collections.abc import Mapping

from _emit import write


def load_callable(spec: str):
    module_name, separator, function_name = spec.partition(":")
    if not separator:
        raise RuntimeError("RPS_TARGET_CALLABLE must be package.module:function")
    return getattr(importlib.import_module(module_name), function_name)


def main() -> None:
    spec = os.environ.get("RPS_TARGET_CALLABLE")
    if not spec:
        raise RuntimeError("RPS_TARGET_CALLABLE is not configured")
    targets = load_callable(spec)()
    if not isinstance(targets, Mapping):
        raise TypeError("RPS target callable must return ticker -> target shares")
    write("targets", targets)


if __name__ == "__main__":
    main()
