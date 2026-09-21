"""Tiny producer-side helpers for existing strategy repositories.

These helpers intentionally know nothing about brokers or portfolio risk. Existing
strategies read the Conductor account snapshot, run their current logic, and write
one native result file whose semantics are declared in conductor.toml.
"""
from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Mapping


def _env_path(name: str) -> Path:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is not set; strategy must be launched by Conductor")
    return Path(value)


def read_account_state() -> dict:
    return json.loads(_env_path("CONDUCTOR_INPUT_STATE").read_text(encoding="utf-8"))


def _write(payload: dict) -> None:
    path = _env_path("CONDUCTOR_OUTPUT")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _numbers(values: Mapping[str, object]) -> dict[str, str]:
    return {str(key): str(Decimal(str(value))) for key, value in values.items()}


def write_target_weights(targets: Mapping[str, object]) -> None:
    _write({"targets": _numbers(targets)})


def write_target_quantities(targets: Mapping[str, object]) -> None:
    _write({"targets": _numbers(targets)})


def write_position_deltas(deltas: Mapping[str, object]) -> None:
    _write({"deltas": _numbers(deltas)})


def canonical_us_equity(symbol: str) -> str:
    symbol = str(symbol).strip().upper()
    if not symbol:
        raise ValueError("symbol must be non-empty")
    return f"EQ.US.{symbol}"


def canonicalize_us_equities(values: Mapping[str, object]) -> dict[str, object]:
    return {canonical_us_equity(symbol): value for symbol, value in values.items()}
