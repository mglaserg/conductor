"""Vendored zero-dependency Conductor output helpers for strategy repositories."""
from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Mapping


def account_state() -> dict:
    return json.loads(Path(os.environ["CONDUCTOR_INPUT_STATE"]).read_text(encoding="utf-8"))


def canonical(symbol: str) -> str:
    text = str(symbol).strip().upper()
    if text.startswith("EQ.US."):
        return text
    if not text:
        raise ValueError("empty equity symbol")
    return f"EQ.US.{text}"


def write(field: str, values: Mapping[str, object]) -> None:
    output = Path(os.environ["CONDUCTOR_OUTPUT"])
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {field: {canonical(k): str(Decimal(str(v))) for k, v in values.items()}}
    tmp = output.with_suffix(output.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(output)
