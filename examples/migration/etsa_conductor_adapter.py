"""ETSA producer adapter: copy beside `_emit.py` into the existing ETSA repo.

This intentionally reuses the existing RobotWealth fetch + ETSA weight calculation;
only Dagster orchestration/broker execution is removed.
"""
from __future__ import annotations

import pandas as pd

from _emit import write
from yautja_orchestra.equities_triangulated_stat_arb.get_data import (
    get_triangulated_equities_stat_arb,
)
from yautja_orchestra.equities_triangulated_stat_arb.triangulated_stat_arb import (
    latest_tri_stat_arb_weights,
)


def main() -> None:
    payload = get_triangulated_equities_stat_arb()
    rows = payload if isinstance(payload, list) else payload.get("rows", [])
    if not rows:
        raise RuntimeError("ETSA RobotWealth payload contains no rows")
    weights = latest_tri_stat_arb_weights(
        pd.DataFrame(rows),
        universe_size=100,
        quality_threshold=90.0,
        weighting_scheme="cons_quality",
        signal_threshold=1.0,
    )
    if not weights:
        raise RuntimeError("ETSA produced no non-zero weights")
    write("targets", weights)


if __name__ == "__main__":
    main()
