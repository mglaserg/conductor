from __future__ import annotations

import argparse
import json
from pathlib import Path

from pydantic import ValidationError

from conductor.cli import main as demo_main
from conductor.protocol.models import TARGET_SNAPSHOT_ADAPTER
from conductor.protocol.schema import write_json_schemas


def main() -> None:
    parser = argparse.ArgumentParser(prog="conductor")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("demo", help="run the V0.3 paper protocol demo")

    validate = sub.add_parser("validate", help="validate a target snapshot JSON file")
    validate.add_argument("path", type=Path)

    schemas = sub.add_parser("schemas", help="export the committed JSON Schemas")
    schemas.add_argument("directory", nargs="?", default="schemas", type=Path)

    args = parser.parse_args()
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
