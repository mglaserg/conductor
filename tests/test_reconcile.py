from decimal import Decimal

from conductor.adapters.paper import PaperExecutionAdapter
from conductor.domain.models import AggregateTarget, BrokerPosition, ExposureType
from conductor.reconcile import DesiredStateReconciler


def test_desired_state_reconciliation_is_idempotent_after_execution() -> None:
    adapter = PaperExecutionAdapter([BrokerPosition("AAPL", Decimal("137"))])
    desired = [AggregateTarget("AAPL", Decimal("140"), ExposureType.QUANTITY)]
    reconciler = DesiredStateReconciler()

    first = reconciler.reconcile(desired, adapter.positions())
    assert first[0].delta == Decimal("3")

    adapter.submit_deltas(first)
    second = reconciler.reconcile(desired, adapter.positions())
    assert second == []
