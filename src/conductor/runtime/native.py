from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Mapping

from conductor.domain.models import ExposureType, StrategyIntent, ZERO
from conductor.runtime.models import NativeResultMode, StrategyAccountView, StrategyRunnerProfile


class NativeResultError(ValueError):
    pass


def _decimal_map(raw: object, field: str) -> dict[str, Decimal]:
    if not isinstance(raw, dict):
        raise NativeResultError(f"{field} must be an object mapping instrument to numeric value")
    out: dict[str, Decimal] = {}
    for instrument, value in raw.items():
        if not isinstance(instrument, str) or not instrument.strip():
            raise NativeResultError(f"{field} contains an invalid instrument")
        try:
            number = Decimal(str(value))
        except Exception as exc:  # noqa: BLE001 - normalize external strategy output
            raise NativeResultError(f"{field}[{instrument!r}] is not numeric") from exc
        if not number.is_finite():
            raise NativeResultError(f"{field}[{instrument!r}] must be finite")
        out[instrument] = number
    return out


def load_native_result(path: str | Path) -> dict:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NativeResultError(f"cannot load native strategy result: {exc}") from exc
    if not isinstance(payload, dict):
        raise NativeResultError("native strategy result must be a JSON object")
    return payload


def adapt_native_result(
    payload: Mapping[str, object],
    *,
    profile: StrategyRunnerProfile,
    account: StrategyAccountView,
    revision: int,
    as_of: datetime | None = None,
    run_id: str,
) -> StrategyIntent:
    """Normalize legacy/native strategy output into absolute Conductor desired state.

    Delta semantics exist only here. The returned StrategyIntent is always an absolute
    complete desired state for the strategy/book, so replaying the resolved intent is
    idempotent.
    """

    as_of = as_of or datetime.now(timezone.utc)
    mode = profile.result_mode

    if mode is NativeResultMode.TARGET_WEIGHTS:
        targets = _decimal_map(payload.get("targets"), "targets")
        exposure_type = ExposureType.NAV_WEIGHT
    elif mode is NativeResultMode.TARGET_QUANTITIES:
        targets = _decimal_map(payload.get("targets"), "targets")
        exposure_type = ExposureType.QUANTITY
    elif mode is NativeResultMode.POSITION_DELTAS:
        deltas = _decimal_map(payload.get("deltas"), "deltas")
        targets = {instrument: Decimal(str(qty)) for instrument, qty in account.positions.items()}
        for instrument, delta in deltas.items():
            targets[instrument] = targets.get(instrument, ZERO) + delta
        # Complete absolute state: zero quantities need not be emitted.
        targets = {instrument: qty for instrument, qty in targets.items() if qty != ZERO}
        exposure_type = ExposureType.QUANTITY
    else:  # defensive against future enum additions
        raise NativeResultError(f"unsupported native result mode: {mode}")

    return StrategyIntent(
        strategy_id=profile.strategy_id,
        book_id=profile.book_id,
        sleeve_id=profile.sleeve_id,
        route_id=profile.route_id,
        targets=targets,
        exposure_type=exposure_type,
        as_of=as_of,
        revision=revision,
        intent_id=run_id,
        metadata={
            "producer": "conductor-runtime",
            "native_result_mode": mode.value,
            "run_id": run_id,
        },
    )
