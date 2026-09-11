from decimal import Decimal

from conductor.domain.models import ExposureType, VirtualTarget
from conductor.ledger import ConductorLedger


def test_ledger_persists_targets_and_committed_virtual_ownership(tmp_path) -> None:
    ledger = ConductorLedger(tmp_path / "conductor.sqlite")
    targets = [
        VirtualTarget(
            "ETSA",
            "equities",
            "AAPL",
            Decimal("120"),
            Decimal("24000"),
            source_exposure_type=ExposureType.NAV_WEIGHT,
        ),
        VirtualTarget(
            "RPS",
            "equities",
            "AAPL",
            Decimal("20"),
            Decimal("4000"),
            source_exposure_type=ExposureType.NAV_WEIGHT,
        ),
    ]
    ledger.replace_virtual_targets(targets)
    ledger.replace_virtual_positions(targets)

    assert [(x.strategy_id, x.target) for x in ledger.virtual_targets()] == [
        ("ETSA", Decimal("120")),
        ("RPS", Decimal("20")),
    ]
    ownership = ledger.virtual_positions()
    assert [(x["strategy_id"], x["quantity"]) for x in ownership] == [
        ("ETSA", "120"),
        ("RPS", "20"),
    ]
