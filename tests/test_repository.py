"""Инварианты слоя данных. Без HTTP, только repository."""

import asyncio
from decimal import Decimal

from src import repository as repo


async def test_concurrent_submit_creates_single_intent(db):
    """10 параллельных submit — ровно один created_now, одно SUBMITTED."""
    await repo.create_operation("op-1", Decimal("100.00"), "RUB", None)

    results = await asyncio.gather(*(repo.try_start_submit("op-1") for _ in range(10)))

    assert sum(1 for r in results if r.created_now) == 1

    events = await repo.list_events("op-1")
    assert [e["type"] for e in events] == ["CREATED", "SUBMITTED"]


async def test_duplicate_receipt_does_not_create_transition(db):
    await repo.create_operation("op-2", Decimal("50.00"), "RUB", None)
    await repo.try_start_submit("op-2")

    assert (
        await repo.apply_receipt("op-2", "pp-1", "COMPLETED", "ok") is repo.ReceiptOutcome.APPLIED
    )
    assert (
        await repo.apply_receipt("op-2", "pp-1", "COMPLETED", "ok") is repo.ReceiptOutcome.DUPLICATE
    )

    events = await repo.list_events("op-2")
    assert len(events) == 3


async def test_late_opposite_receipt_is_ignored(db):
    await repo.create_operation("op-3", Decimal("50.00"), "RUB", None)
    await repo.try_start_submit("op-3")
    await repo.apply_receipt("op-3", "pp-1", "COMPLETED", "ok")

    assert (
        await repo.apply_receipt("op-3", "pp-1", "REJECTED", "late") is repo.ReceiptOutcome.IGNORED
    )

    op = await repo.get_operation("op-3")
    assert op["status"] == "COMPLETED"


async def test_foreign_provider_payment_id_rejected(db):
    await repo.create_operation("op-4", Decimal("50.00"), "RUB", None)
    await repo.try_start_submit("op-4")
    await repo.apply_receipt("op-4", "pp-1", "COMPLETED", "ok")

    assert (
        await repo.apply_receipt("op-4", "pp-2", "COMPLETED", "x") is repo.ReceiptOutcome.CONFLICT
    )


async def test_receipt_for_unknown_operation(db):
    outcome = await repo.apply_receipt("nope", "pp-1", "COMPLETED", "x")
    assert outcome is repo.ReceiptOutcome.NOT_FOUND


async def test_second_create_returns_false(db):
    first = await repo.create_operation("op-5", Decimal("10.00"), "RUB", None)
    second = await repo.create_operation("op-5", Decimal("10.00"), "RUB", None)
    assert first is True
    assert second is False
