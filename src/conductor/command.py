from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from conductor.cli import main as demo_main
from conductor.protocol.models import TARGET_SNAPSHOT_ADAPTER
from conductor.protocol.schema import write_json_schemas
from conductor.runtime.app import ConductorRuntimeApp


def main() -> None:
    parser = argparse.ArgumentParser(prog="conductor")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("demo", help="run the V0.3 paper protocol demo")

    validate = sub.add_parser("validate", help="validate a target snapshot JSON file")
    validate.add_argument("path", type=Path)

    schemas = sub.add_parser("schemas", help="export the committed JSON Schemas")
    schemas.add_argument("directory", nargs="?", default="schemas", type=Path)

    run = sub.add_parser("run", help="run one configured strategy through Conductor")
    run.add_argument("strategy_id")
    run.add_argument("--config", default="conductor.toml", type=Path)
    run.add_argument("--trigger", default="manual")
    run.add_argument(
        "--paper",
        action="store_true",
        help="use the separate offline synthetic-broker runtime and paper database",
    )

    status = sub.add_parser("status", help="show node, strategy, ownership and recent-run state")
    status.add_argument("--config", default="conductor.toml", type=Path)
    status.add_argument(
        "--paper",
        action="store_true",
        help="inspect the separate offline paper database",
    )

    doctor = sub.add_parser(
        "doctor", help="read-only bootstrap check: virtual ownership must equal broker positions"
    )
    doctor.add_argument("--config", default="conductor.toml", type=Path)

    dashboard = sub.add_parser("dashboard", help="launch the read-only local Streamlit board")
    dashboard.add_argument("--config", default="conductor.toml", type=Path)

    worker = sub.add_parser(
        "nautilus-worker",
        help="run the persistent NautilusTrader execution worker for one configured route",
    )
    worker.add_argument("route_id")
    worker.add_argument("--config", default="conductor.toml", type=Path)

    worker_status = sub.add_parser(
        "worker-status",
        help="show persistent Nautilus worker heartbeat/readiness without starting the runtime",
    )
    worker_status.add_argument("route_id")
    worker_status.add_argument("--config", default="conductor.toml", type=Path)

    for action in ("activate", "disable"):
        action_parser = sub.add_parser(action, help=f"{action} a configured strategy")
        action_parser.add_argument("strategy_id")
        action_parser.add_argument("--config", default="conductor.toml", type=Path)

    retire = sub.add_parser("retire", help="safely flatten and retire a strategy")
    retire.add_argument("strategy_id")
    retire.add_argument("--config", default="conductor.toml", type=Path)
    retire.add_argument(
        "--confirm",
        help="must exactly match strategy_id because retirement can submit flattening orders",
    )

    args = parser.parse_args()
    if args.command == "worker-status":
        from conductor.adapters.nautilus_bridge import NautilusBridgeStore
        from conductor.config import load_runtime_config

        config = load_runtime_config(args.config)
        route_cfg = config.routes.get(args.route_id)
        if route_cfg is None:
            raise SystemExit(f"unknown route: {args.route_id}")
        if route_cfg.bridge_db is None:
            raise SystemExit(f"route has no bridge_db: {args.route_id}")
        state = NautilusBridgeStore(route_cfg.bridge_db).worker_state(args.route_id)
        if state is None:
            print(json.dumps({"route_id": args.route_id, "state": "never_seen"}, indent=2))
            raise SystemExit(2)
        heartbeat = datetime.fromisoformat(state["heartbeat_at"])
        age = max(0.0, (datetime.now(timezone.utc) - heartbeat).total_seconds())
        details = json.loads(state.get("details_json") or "{}")
        configured_account = route_cfg.route.account
        actual_account = state.get("account_id")
        account_matches = actual_account == configured_account
        payload = {
            "route_id": args.route_id,
            "ready": bool(state["ready"]),
            "heartbeat_at": state["heartbeat_at"],
            "heartbeat_age_seconds": round(age, 3),
            "stale_after_seconds": route_cfg.worker_stale_after_seconds,
            "stale": age > route_cfg.worker_stale_after_seconds,
            "net_liquidation": state.get("net_liquidation"),
            "account_id": actual_account,
            "configured_account_id": configured_account,
            "account_matches": account_matches,
            "error": state.get("error"),
            "details": details,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        if payload["stale"] or not payload["ready"] or not account_matches:
            raise SystemExit(3)
        return
    if args.command == "nautilus-worker":
        from conductor.adapters.nautilus_ibkr_worker import run_nautilus_ibkr_worker

        run_nautilus_ibkr_worker(args.config, args.route_id)
        return
    if args.command == "dashboard":
        try:
            import streamlit  # noqa: F401
        except ImportError as exc:
            raise SystemExit(
                "dashboard extra is not installed; install conductor-trading[dashboard]"
            ) from exc
        module_path = Path(__file__).with_name("dashboard") / "app.py"
        raise SystemExit(
            subprocess.call(
                [
                    sys.executable,
                    "-m",
                    "streamlit",
                    "run",
                    str(module_path),
                    "--",
                    "--config",
                    str(args.config),
                ]
            )
        )

    if args.command in {"run", "status", "doctor", "activate", "disable", "retire"}:
        if args.command == "retire" and args.confirm != args.strategy_id:
            raise SystemExit(
                "REFUSED: retirement can flatten positions; pass --confirm with the exact "
                "strategy ID"
            )
        route_scope = None
        if args.command in {"run", "activate", "disable", "retire"}:
            from conductor.config import load_runtime_config

            config = load_runtime_config(args.config)
            matches = [
                configured
                for configured in config.strategies
                if configured.casefold() == args.strategy_id.casefold()
            ]
            if len(matches) != 1:
                raise SystemExit(f"unknown configured strategy: {args.strategy_id}")
            route_scope = {config.strategies[matches[0]].route_id}

        try:
            app = ConductorRuntimeApp.from_path(
                args.config,
                paper=bool(getattr(args, "paper", False)),
                route_scope=route_scope,
            )
        except Exception as exc:
            from conductor.adapters.nautilus_bridge import NautilusBridgeError

            if isinstance(exc, (NautilusBridgeError, ValueError)):
                raise SystemExit(f"REFUSED: {exc}") from exc
            raise
        try:
            if args.command == "run":
                try:
                    outcome = app.run_strategy(args.strategy_id, trigger=args.trigger)
                except KeyError as exc:
                    raise SystemExit(str(exc)) from exc
                payload = {
                    "run_id": outcome.run_id,
                    "strategy_id": outcome.strategy_id,
                    "book_id": outcome.book_id,
                    "status": outcome.status,
                    "revision": outcome.revision,
                    "error": outcome.error,
                    "portfolio_state": (
                        None
                        if outcome.portfolio_result is None
                        else outcome.portfolio_result.state.value
                    ),
                    "reconciled": (
                        None
                        if outcome.portfolio_result is None
                        else outcome.portfolio_result.reconciled
                    ),
                    "trade_count": (
                        None
                        if outcome.portfolio_result is None
                        else len(outcome.portfolio_result.deltas)
                    ),
                }
                print(json.dumps(payload, indent=2, sort_keys=True))
                if outcome.status not in {"succeeded", "submitted"}:
                    raise SystemExit(2)
                return
            if args.command == "status":
                print(json.dumps(app.status(), indent=2, sort_keys=True))
                return
            if args.command == "doctor":
                result = app.bootstrap_reconciliation()
                print(json.dumps(result, indent=2, sort_keys=True))
                if not result["reconciled"]:
                    raise SystemExit(3)
                return

            try:
                canonical_id = app.resolve_strategy_id(args.strategy_id)
            except KeyError as exc:
                raise SystemExit(str(exc)) from exc
            profile = app.config.strategies[canonical_id]
            if args.command == "activate":
                app.orchestrator.activate(profile)
                print(f"ACTIVE {canonical_id}/{profile.book_id}")
                return
            if args.command == "disable":
                app.orchestrator.disable(profile)
                print(f"DISABLED {canonical_id}/{profile.book_id}")
                return
            if args.command == "retire":
                result = app.orchestrator.retire(profile)
                print(
                    json.dumps(
                        {
                            "strategy_id": canonical_id,
                            "state": result.state.value,
                            "reconciled": result.reconciled,
                            "trade_count": len(result.deltas),
                            "lifecycle": app.ledger.strategy_lifecycle(
                                canonical_id, book_id=profile.book_id
                            ),
                        },
                        indent=2,
                        sort_keys=True,
                    )
                )
                if not result.reconciled:
                    raise SystemExit(4)
                return
        finally:
            app.close()

    if args.command == "demo":
        demo_main()
        return
    if args.command == "schemas":
        paths = write_json_schemas(args.directory)
        for path in paths:
            print(path)
        return
    if args.command == "validate":
        try:
            snapshot = TARGET_SNAPSHOT_ADAPTER.validate_json(args.path.read_text(encoding="utf-8"))
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            raise SystemExit(f"INVALID: {exc}") from exc
        print(
            f"VALID {snapshot.type} {snapshot.data.strategy_id}/{snapshot.data.book_id} "
            f"revision={snapshot.data.revision} id={snapshot.id}"
        )
        return
