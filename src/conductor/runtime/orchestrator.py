from __future__ import annotations

import json
import os
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping
from uuid import uuid4

from conductor.accounting import VirtualAccountingEngine
from conductor.domain.models import ExposureType, RunResult, RunState, StrategyIntent
from conductor.engine import ConductorEngine
from conductor.ledger import ConductorLedger
from conductor.runtime.models import StrategyRunnerProfile
from conductor.runtime.native import adapt_native_result, load_native_result


@dataclass(frozen=True, slots=True)
class StrategyRunOutcome:
    run_id: str
    strategy_id: str
    book_id: str
    status: str
    revision: int | None = None
    portfolio_result: RunResult | None = None
    error: str | None = None


class StrategyRunOrchestrator:
    """Cron/Task-Scheduler entrypoint for existing strategy repositories.

    The operating system decides *when* to call Conductor. Conductor owns the run
    lock, input account snapshot, subprocess, native-result normalization, durable
    desired state, portfolio construction/risk, execution, reconciliation, and audit.
    """

    def __init__(
        self,
        *,
        ledger: ConductorLedger,
        accounting: VirtualAccountingEngine,
        engine: ConductorEngine,
        run_root: str | Path,
        profiles: Mapping[str, StrategyRunnerProfile] | None = None,
    ) -> None:
        self.ledger = ledger
        self.accounting = accounting
        self.engine = engine
        self.run_root = Path(run_root)
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.profiles = dict(profiles or {})

    @staticmethod
    def _write_account_state(path: Path, account) -> None:
        payload = asdict(account)
        payload["allocated_capital"] = str(account.allocated_capital)
        payload["cash"] = str(account.cash)
        payload["equity"] = str(account.equity)
        payload["gross_exposure"] = str(account.gross_exposure)
        payload["net_exposure"] = str(account.net_exposure)
        payload["as_of"] = account.as_of.isoformat()
        payload["positions"] = {k: str(v) for k, v in account.positions.items()}
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def _portfolio_intents(self, route_id: str | None = None) -> list[StrategyIntent]:
        """Latest desired books plus hold-current intents for one capital pool.

        Strategies sharing a route/account must remain present when one of them runs, but an
        independent account must not become an execution dependency or accidental flatten target.
        ``route_id=None`` retains the legacy all-route view for direct callers.
        """
        intents = [
            intent
            for intent in self.ledger.runtime_intents()
            if route_id is None or intent.route_id == route_id
        ]
        present = {(intent.strategy_id, intent.book_id) for intent in intents}
        for profile in self.profiles.values():
            if route_id is not None and profile.route_id != route_id:
                continue
            key = (profile.strategy_id, profile.book_id)
            if key in present:
                continue
            lifecycle = self.ledger.strategy_lifecycle(
                profile.strategy_id, book_id=profile.book_id
            )
            if lifecycle == "retired":
                targets = {}
            else:
                targets = self.ledger.strategy_positions(
                    profile.strategy_id, book_id=profile.book_id
                )
            intents.append(
                StrategyIntent(
                    strategy_id=profile.strategy_id,
                    book_id=profile.book_id,
                    sleeve_id=profile.sleeve_id,
                    route_id=profile.route_id,
                    targets=targets,
                    exposure_type=ExposureType.QUANTITY,
                    revision=1,
                    metadata={"producer": "conductor-hold-current"},
                )
            )
        return sorted(intents, key=lambda item: (item.strategy_id, item.book_id))

    def activate(self, profile: StrategyRunnerProfile) -> None:
        self.ledger.set_strategy_lifecycle(
            profile.strategy_id, "active", book_id=profile.book_id
        )

    def disable(self, profile: StrategyRunnerProfile) -> None:
        """Stop future strategy runs while preserving current ownership."""
        self.ledger.set_strategy_lifecycle(
            profile.strategy_id, "disabled", book_id=profile.book_id
        )

    def retire(self, profile: StrategyRunnerProfile) -> RunResult:
        """Safely flatten a strategy book, then mark it retired; history is retained."""
        self.ledger.set_strategy_lifecycle(
            profile.strategy_id, "retiring", book_id=profile.book_id
        )
        revision = self.ledger.next_runtime_revision(
            profile.strategy_id, book_id=profile.book_id
        )
        run_id = uuid4().hex
        intent = StrategyIntent(
            strategy_id=profile.strategy_id,
            book_id=profile.book_id,
            sleeve_id=profile.sleeve_id,
            route_id=profile.route_id,
            targets={},
            exposure_type=ExposureType.QUANTITY,
            revision=revision,
            intent_id=run_id,
            metadata={"producer": "conductor-retirement", "run_id": run_id},
        )
        self.ledger.replace_runtime_intent(intent, run_id=run_id)
        result = self.engine.run_cycle(
            self._portfolio_intents(profile.route_id), run_id=run_id
        )
        remaining = self.ledger.strategy_positions(
            profile.strategy_id, book_id=profile.book_id
        )
        if result.reconciled and not remaining:
            self.ledger.set_strategy_lifecycle(
                profile.strategy_id, "retired", book_id=profile.book_id
            )
        return result

    def run(
        self,
        profile: StrategyRunnerProfile,
        *,
        trigger: str = "manual",
        scheduled_for: datetime | None = None,
    ) -> StrategyRunOutcome:
        run_id = uuid4().hex
        revision = self.ledger.next_runtime_revision(profile.strategy_id, book_id=profile.book_id)
        account = self.accounting.account_view(profile.strategy_id, book_id=profile.book_id)

        run_dir = self.run_root / profile.strategy_id / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        input_path = run_dir / "account_state.json"
        output_path = run_dir / "native_result.json"
        stdout_path = run_dir / "stdout.log"
        stderr_path = run_dir / "stderr.log"
        self._write_account_state(input_path, account)

        scheduled_text = scheduled_for.isoformat() if scheduled_for else None
        acquired = self.ledger.begin_strategy_run(
            run_id=run_id,
            strategy_id=profile.strategy_id,
            book_id=profile.book_id,
            trigger=trigger,
            scheduled_for=scheduled_text,
            command=profile.command,
            cwd=str(profile.cwd),
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
            input_state_path=str(input_path),
            output_path=str(output_path),
        )
        if not acquired:
            return StrategyRunOutcome(
                run_id=run_id,
                strategy_id=profile.strategy_id,
                book_id=profile.book_id,
                status="rejected",
                error="strategy is not active or another run is already active",
            )

        env = os.environ.copy()
        env.update(profile.environment)
        env.update(
            {
                "CONDUCTOR_RUN_ID": run_id,
                "CONDUCTOR_STRATEGY_ID": profile.strategy_id,
                "CONDUCTOR_BOOK_ID": profile.book_id,
                "CONDUCTOR_INPUT_STATE": str(input_path),
                "CONDUCTOR_OUTPUT": str(output_path),
                "CONDUCTOR_NATIVE_RESULT_MODE": profile.result_mode.value,
                "CONDUCTOR_REVISION": str(revision),
            }
        )
        self.ledger.mark_strategy_run_running(run_id)

        try:
            with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open(
                "w", encoding="utf-8"
            ) as stderr:
                completed = subprocess.run(
                    profile.command,
                    cwd=profile.cwd,
                    env=env,
                    stdout=stdout,
                    stderr=stderr,
                    timeout=profile.timeout_seconds,
                    check=False,
                )
        except subprocess.TimeoutExpired as exc:
            error = f"strategy exceeded {profile.timeout_seconds}s timeout"
            self.ledger.finish_strategy_run(run_id, status="timed_out", error=error)
            return StrategyRunOutcome(
                run_id=run_id,
                strategy_id=profile.strategy_id,
                book_id=profile.book_id,
                status="timed_out",
                error=error,
            )
        except Exception as exc:  # noqa: BLE001 - subprocess boundary must be audited
            error = f"strategy launch failed: {exc}"
            self.ledger.finish_strategy_run(run_id, status="failed", error=error)
            return StrategyRunOutcome(
                run_id=run_id,
                strategy_id=profile.strategy_id,
                book_id=profile.book_id,
                status="failed",
                error=error,
            )

        if completed.returncode != 0:
            error = f"strategy exited with code {completed.returncode}"
            self.ledger.finish_strategy_run(
                run_id, status="failed", exit_code=completed.returncode, error=error
            )
            return StrategyRunOutcome(
                run_id=run_id,
                strategy_id=profile.strategy_id,
                book_id=profile.book_id,
                status="failed",
                error=error,
            )

        try:
            payload = load_native_result(output_path)
            intent = adapt_native_result(
                payload,
                profile=profile,
                account=account,
                revision=revision,
                as_of=datetime.now(timezone.utc),
                run_id=run_id,
            )
            self.ledger.replace_runtime_intent(intent, run_id=run_id)
            all_intents = self._portfolio_intents(profile.route_id)
            portfolio_result = self.engine.run_cycle(all_intents, run_id=run_id)
        except Exception as exc:  # noqa: BLE001 - preserve desired-state failure audit
            error = f"Conductor normalization/portfolio cycle failed: {exc}"
            self.ledger.finish_strategy_run(
                run_id,
                status="failed",
                exit_code=completed.returncode,
                error=error,
                canonical_revision=revision,
            )
            return StrategyRunOutcome(
                run_id=run_id,
                strategy_id=profile.strategy_id,
                book_id=profile.book_id,
                status="failed",
                revision=revision,
                error=error,
            )

        if portfolio_result.state is RunState.RECONCILED:
            outcome_status = "succeeded"
            outcome_error = None
        elif portfolio_result.state in {RunState.PLANNED, RunState.SUBMITTED}:
            outcome_status = "submitted"
            outcome_error = None
        else:
            outcome_status = "blocked"
            outcome_error = "portfolio cycle blocked by terminal execution outcome"

        self.ledger.finish_strategy_run(
            run_id,
            status=outcome_status,
            exit_code=completed.returncode,
            error=outcome_error,
            canonical_revision=revision,
        )
        return StrategyRunOutcome(
            run_id=run_id,
            strategy_id=profile.strategy_id,
            book_id=profile.book_id,
            status=outcome_status,
            revision=revision,
            portfolio_result=portfolio_result,
            error=outcome_error,
        )
