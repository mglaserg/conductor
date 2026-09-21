from __future__ import annotations

import argparse
import json
from decimal import Decimal

import pandas as pd
import streamlit as st

from conductor.config import load_runtime_config
from conductor.ledger import ConductorLedger


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config", default="conductor.toml")
    args, _ = parser.parse_known_args()
    return args


def _money(value: object) -> str:
    try:
        return f"${Decimal(str(value)):,.2f}"
    except Exception:
        return str(value)


def main() -> None:
    cfg = load_runtime_config(_args().config)
    ledger = ConductorLedger(cfg.state_db)
    st.set_page_config(page_title=f"Conductor — {cfg.node_id}", layout="wide")
    st.title(f"Conductor — {cfg.node_id}")
    st.caption("Read-only projection of durable Conductor state")

    runtime = {(x.strategy_id, x.book_id): x for x in ledger.runtime_intents()}
    cols = st.columns(4)
    cols[0].metric("Configured strategies", len(cfg.strategies))
    cols[1].metric("Virtual positions", len(ledger.virtual_positions()))
    recent = ledger.strategy_runs(limit=100)
    cols[2].metric("Runs recorded", len(recent))
    cols[3].metric(
        "Active runs", sum(row["status"] in {"starting", "running"} for row in recent)
    )

    tab1, tab2, tab3, tab4 = st.tabs(["Strategies", "Positions", "Runs", "Audit"])
    with tab1:
        rows = []
        for strategy_id, profile in sorted(cfg.strategies.items()):
            account = ledger.strategy_account(strategy_id, book_id=profile.book_id) or {}
            target = runtime.get((strategy_id, profile.book_id))
            rows.append(
                {
                    "strategy": strategy_id,
                    "book": profile.book_id,
                    "lifecycle": ledger.strategy_lifecycle(strategy_id, book_id=profile.book_id),
                    "route": profile.route_id,
                    "native mode": profile.result_mode.value,
                    "allocated capital": _money(account.get("allocated_capital", 0)),
                    "virtual cash": _money(account.get("cash", 0)),
                    "target revision": None if target is None else target.revision,
                    "target as-of": None if target is None else target.as_of.isoformat(),
                }
            )
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    with tab2:
        positions = ledger.virtual_positions()
        if positions:
            st.dataframe(pd.DataFrame(positions), use_container_width=True, hide_index=True)
        else:
            st.info("No virtual positions recorded.")

    with tab3:
        if recent:
            st.dataframe(pd.DataFrame(recent), use_container_width=True, hide_index=True)
        else:
            st.info("No strategy runs recorded.")

    with tab4:
        events = ledger.events()
        if events:
            df = pd.DataFrame(events)
            df["payload"] = df["payload_json"].map(
                lambda value: json.dumps(json.loads(value), sort_keys=True)
            )
            st.dataframe(
                df[["ts", "event_type", "payload"]].iloc[::-1],
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.info("No audit events recorded.")


if __name__ == "__main__":
    main()
