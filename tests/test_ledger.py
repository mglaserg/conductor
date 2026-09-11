from decimal import Decimal

from conductor.domain.models import ExposureType, VirtualTarget
from conductor.ledger import ConductorLedger


def test_ledger_preserves_strategy_level_virtual_ownership(tmp_path) -> None:
    ledger = ConductorLedger(tmp_path / "conductor.sqlite")
    ledger.replace_virtual_targets(
        [
            VirtualTarget("ETSA", "equities", "AAPL", Decimal("120"), ExposureType.QUANTITY),
            VirtualTarget(
                "RPSchteroids", "equities", "AAPL", Decimal("30"), ExposureType.QUANTITY
            ),
        ]
    )
    rows = ledger.virtual_targets()

    assert [(row.strategy_id, row.instrument, row.target) for row in rows] == [
        ("ETSA", "AAPL", Decimal("120")),
        ("RPSchteroids", "AAPL", Decimal("30")),
    ]
