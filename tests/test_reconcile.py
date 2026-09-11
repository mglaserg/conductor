from decimal import Decimal

from conductor.domain.models import AggregateTarget, BrokerPosition
from conductor.reconcile import DesiredStateReconciler


def test_reconcile_is_desired_minus_actual_and_idempotent() -> None:
    reconciler = DesiredStateReconciler()
    desired = [AggregateTarget("AAPL", Decimal("140"), Decimal("28000"))]
    actual = [BrokerPosition("AAPL", Decimal("137"))]
    delta = reconciler.reconcile(desired, actual)[0]
    assert delta.delta == Decimal("3")
    assert reconciler.is_reconciled(desired, [BrokerPosition("AAPL", Decimal("140"))])
