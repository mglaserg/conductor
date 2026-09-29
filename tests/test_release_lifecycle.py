from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from conductor.command import main as command_main
from conductor.ledger import ConductorLedger
from conductor.protocol.release import StrategyRelease, StrategyReleaseState


def _release_config(tmp_path: Path) -> Path:
    config = tmp_path / "conductor.toml"
    cwd = json.dumps(str(tmp_path))
    command = json.dumps([sys.executable, "-c", "print('unused')"])
    config.write_text(
        f"""
[node]
id = "release-test"
state_db = "data/conductor.sqlite"
run_root = "data/runs"
portfolio_nav = 100000

[portfolio]
allocator = "static"
[portfolio.static.weights]
ETSA = 1.0

[risk]
max_gross_leverage = 2
max_instrument_nav = 1

[execution]
min_trade_nav_bps = 0

[routes.shared]
adapter = "paper"
live_orders_enabled = false

[strategies.ETSA]
result_mode = "target_weights"
route_id = "shared"
cwd = {cwd}
command = {command}
version = "1.0.0"
release_state = "research"
evidence = [{{ producer = "clockwork", artifact_type = "research", location = "evidence/etsa.json" }}]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return config


def test_release_state_machine_is_forward_and_fail_closed() -> None:
    release = StrategyRelease("ETSA", "1.0.0")
    assert [state.value for state in release.allowed_transitions()] == ["killed", "validated"]
    assert release.transitioned("validated").state is StrategyReleaseState.VALIDATED

    reviewed = StrategyRelease("ETSA", "1.0.0", state=StrategyReleaseState.REVIEW)
    assert [state.value for state in reviewed.allowed_transitions()] == ["killed", "validated"]

    killed = StrategyRelease("ETSA", "1.0.0", state=StrategyReleaseState.KILLED)
    assert killed.allowed_transitions() == ()
    with pytest.raises(ValueError, match="illegal strategy release transition"):
        killed.transitioned("research")


def test_release_transitions_are_persisted_audited_and_idempotent(tmp_path) -> None:
    ledger = ConductorLedger(tmp_path / "conductor.sqlite")
    ledger.ensure_strategy_release(
        StrategyRelease(
            "ETSA",
            "1.0.0",
            evidence_ids=("clockwork:research:evidence/etsa.json",),
        )
    )

    validated = ledger.transition_strategy_release(
        "ETSA", "1.0.0", "validated", reason="EdgeLab validation passed", actor="test"
    )
    assert validated.state is StrategyReleaseState.VALIDATED
    assert ledger.transition_strategy_release(
        "ETSA", "1.0.0", "validated", reason="repeat", actor="test"
    ) == validated

    history = ledger.strategy_release_transitions("ETSA", "1.0.0")
    assert len(history) == 1
    assert history[0]["from_state"] == "research"
    assert history[0]["to_state"] == "validated"
    assert history[0]["reason"] == "EdgeLab validation passed"
    assert history[0]["actor"] == "test"

    event = [
        row for row in ledger.events() if row["event_type"] == "strategy.release_transitioned"
    ]
    assert len(event) == 1

    with pytest.raises(ValueError, match="validated -> live"):
        ledger.transition_strategy_release(
            "ETSA", "1.0.0", "live", reason="skip shadow", actor="test"
        )


def test_release_cli_transitions_without_starting_execution_runtime(
    tmp_path, monkeypatch, capsys
) -> None:
    config = _release_config(tmp_path)

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "conductor",
            "release-transition",
            "etsa",
            "validated",
            "--config",
            str(config),
            "--reason",
            "validation complete",
            "--confirm",
            "ETSA:validated",
        ],
    )
    command_main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["strategy_id"] == "ETSA"
    assert payload["state"] == "validated"
    assert payload["allowed_transitions"] == ["killed", "review", "shadow"]
    assert payload["transitions"][0]["reason"] == "validation complete"

    monkeypatch.setattr(
        sys,
        "argv",
        ["conductor", "release-status", "ETSA", "--config", str(config)],
    )
    command_main()
    persisted = json.loads(capsys.readouterr().out)
    assert persisted["state"] == "validated"


def test_release_cli_refuses_missing_exact_confirmation(tmp_path, monkeypatch) -> None:
    config = _release_config(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "conductor",
            "release-transition",
            "ETSA",
            "validated",
            "--config",
            str(config),
            "--reason",
            "validation complete",
        ],
    )
    with pytest.raises(SystemExit, match="pass --confirm ETSA:validated"):
        command_main()


def test_review_transition_disables_operational_runs(tmp_path, monkeypatch, capsys) -> None:
    config = _release_config(tmp_path)
    for state, reason in (("validated", "validated"), ("review", "needs review")):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "conductor",
                "release-transition",
                "ETSA",
                state,
                "--config",
                str(config),
                "--reason",
                reason,
                "--confirm",
                f"ETSA:{state}",
            ],
        )
        command_main()
        payload = json.loads(capsys.readouterr().out)

    assert payload["state"] == "review"
    assert payload["operational_lifecycle"] == "disabled"


def test_live_authority_requires_every_route_release_live(tmp_path) -> None:
    from conductor.runtime.app import ConductorRuntimeApp

    config = _release_config(tmp_path)
    text = config.read_text(encoding="utf-8").replace(
        "live_orders_enabled = false", "live_orders_enabled = true"
    )
    config.write_text(text, encoding="utf-8")

    with pytest.raises(ValueError, match="live_orders_enabled=true.*not LIVE"):
        ConductorRuntimeApp.from_path(config)

    ledger = ConductorLedger(tmp_path / "data" / "conductor.sqlite")
    release = ledger.ensure_strategy_release(
        StrategyRelease(
            "ETSA",
            "1.0.0",
            evidence_ids=("clockwork:research:evidence/etsa.json",),
        )
    )
    for target in ("validated", "shadow", "live"):
        release = ledger.transition_strategy_release(
            "ETSA", release.version, target, reason=f"advance to {target}", actor="test"
        )

    app = ConductorRuntimeApp.from_path(config)
    try:
        status = app.status()
        assert status["strategies"][0]["release"]["state"] == "live"
    finally:
        app.close()


def test_review_requires_explicit_revalidation_and_reactivation(tmp_path, monkeypatch, capsys) -> None:
    from conductor.runtime.app import ConductorRuntimeApp

    config = _release_config(tmp_path)
    for state in ("validated", "review"):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "conductor",
                "release-transition",
                "ETSA",
                state,
                "--config",
                str(config),
                "--reason",
                f"advance to {state}",
                "--confirm",
                f"ETSA:{state}",
            ],
        )
        command_main()
        capsys.readouterr()

    app = ConductorRuntimeApp.from_path(config)
    try:
        with pytest.raises(ValueError, match="is REVIEW"):
            app.activate_strategy("ETSA")
    finally:
        app.close()

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "conductor",
            "release-transition",
            "ETSA",
            "validated",
            "--config",
            str(config),
            "--reason",
            "review cleared; validation accepted again",
            "--confirm",
            "ETSA:validated",
        ],
    )
    command_main()
    capsys.readouterr()

    app = ConductorRuntimeApp.from_path(config)
    try:
        assert app.ledger.strategy_lifecycle("ETSA") == "disabled"
        app.activate_strategy("ETSA")
        assert app.ledger.strategy_lifecycle("ETSA") == "active"
    finally:
        app.close()
