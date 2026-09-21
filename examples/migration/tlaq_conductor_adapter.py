"""TLAQ adapter for native share-delta output.

Set TLAQ_DELTA_CALLABLE to ``package.module:function``. The callable receives the
strategy's Conductor account-state dict (its positions, virtual cash and allocated
capital) and returns ticker -> share delta. Negative virtual cash is preserved.
"""
from __future__ import annotations

import importlib
import os
from collections.abc import Mapping

from _emit import account_state, write


def load_callable(spec: str):
    module_name, separator, function_name = spec.partition(":")
    if not separator:
        raise RuntimeError("TLAQ_DELTA_CALLABLE must be package.module:function")
    return getattr(importlib.import_module(module_name), function_name)


def main() -> None:
    spec = os.environ.get("TLAQ_DELTA_CALLABLE")
    if not spec:
        raise RuntimeError("TLAQ_DELTA_CALLABLE is not configured")
    deltas = load_callable(spec)(account_state())
    if not isinstance(deltas, Mapping):
        raise TypeError("TLAQ delta callable must return ticker -> share delta")
    write("deltas", deltas)


if __name__ == "__main__":
    main()
