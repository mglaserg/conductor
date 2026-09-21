import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier

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


def test_strategy_run_acquisition_is_atomic_across_process_connections(tmp_path) -> None:
    path = tmp_path / "conductor.sqlite"
    ledgers = (ConductorLedger(path), ConductorLedger(path))
    ledgers[0].ensure_strategy("ETSA")
    ready = Barrier(2)

    def acquire(ledger: ConductorLedger, run_id: str) -> bool:
        ready.wait()
        return ledger.begin_strategy_run(
            run_id=run_id,
            strategy_id="ETSA",
            book_id="main",
            trigger="test",
            scheduled_for=None,
            command=("strategy",),
            cwd=str(tmp_path),
            stdout_path=str(tmp_path / f"{run_id}.stdout"),
            stderr_path=str(tmp_path / f"{run_id}.stderr"),
            input_state_path=str(tmp_path / f"{run_id}.input.json"),
            output_path=str(tmp_path / f"{run_id}.output.json"),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(acquire, ledger, f"run-{index}")
            for index, ledger in enumerate(ledgers)
        ]
        results = [future.result() for future in futures]

    assert sorted(results) == [False, True]
    assert len(ledgers[0].strategy_runs()) == 1
    rejection = next(
        event for event in ledgers[0].events() if event["event_type"] == "strategy.run_rejected"
    )
    assert json.loads(rejection["payload_json"])["reason"] == "concurrent_run"
