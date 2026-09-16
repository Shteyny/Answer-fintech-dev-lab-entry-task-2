"""
HTTP-роуты. Тонкий слой: валидация → вызов repository → сериализация.

Никакой бизнес-логики здесь нет. Всё, что меняет состояние, уже в repository.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Response, status

from src import repository
from src.models import (
    EventResponse,
    OperationCreateRequest,
    OperationResponse,
    ReceiptRequest,
    SubmitResponse,
)

log = logging.getLogger(__name__)
router = APIRouter()


# ===========================================================================
# Health
# ===========================================================================
@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


# ===========================================================================
# POST /operations
# ===========================================================================
@router.post(
    "/operations",
    status_code=status.HTTP_201_CREATED,
    response_model=OperationResponse,
)
async def create_operation(payload: OperationCreateRequest) -> OperationResponse:
    created = await repository.create_operation(
        operation_id=payload.operationId,
        amount=payload.amount,
        currency=payload.currency,
        description=payload.description,
    )
    if not created:
        log.info("operation.create.conflict", extra={"operationId": payload.operationId})
        raise HTTPException(status_code=409, detail="operation already exists")

    return OperationResponse(
        operationId=payload.operationId,
        amount=_format_amount(payload.amount),
        currency=payload.currency,
        description=payload.description,
        status="CREATED",
        providerPaymentId=None,
    )


# ===========================================================================
# POST /operations/{id}/submit
# ===========================================================================
@router.post(
    "/operations/{operation_id}/submit",
    response_model=SubmitResponse,
)
async def submit_operation(operation_id: str, response: Response) -> SubmitResponse:
    result = await repository.try_start_submit(operation_id)

    if not result.found:
        raise HTTPException(status_code=404, detail="operation not found")

    # 202 — именно этот вызов создал намерение; 200 — уже было.
    response.status_code = 202 if result.created_now else 200

    return SubmitResponse(
        operationId=operation_id,
        status=result.operation["status"],
        providerPaymentId=result.operation["provider_payment_id"],
    )


# ===========================================================================
# POST /receipts
# ===========================================================================
@router.post("/receipts", status_code=status.HTTP_204_NO_CONTENT)
async def receive_receipt(payload: ReceiptRequest) -> Response:
    outcome = await repository.apply_receipt(
        operation_id=payload.operationId,
        provider_payment_id=payload.providerPaymentId,
        result=payload.result,
        message=payload.message,
    )

    match outcome:
        case repository.ReceiptOutcome.APPLIED:
            log.info("receipt.applied", extra=_receipt_log_ctx(payload))
            return Response(status_code=204)
        case repository.ReceiptOutcome.DUPLICATE:
            log.info("receipt.duplicate", extra=_receipt_log_ctx(payload))
            return Response(status_code=204)
        case repository.ReceiptOutcome.IGNORED:
            log.warning("receipt.ignored", extra=_receipt_log_ctx(payload))
            return Response(status_code=204)
        case repository.ReceiptOutcome.CONFLICT:
            log.error("receipt.conflict", extra=_receipt_log_ctx(payload))
            raise HTTPException(status_code=409, detail="providerPaymentId mismatch")
        case repository.ReceiptOutcome.NOT_FOUND:
            log.error("receipt.not_found", extra=_receipt_log_ctx(payload))
            raise HTTPException(status_code=404, detail="operation not found")


# ===========================================================================
# GET /operations/{id}
# ===========================================================================
@router.get(
    "/operations/{operation_id}",
    response_model=OperationResponse,
)
async def get_operation(operation_id: str) -> OperationResponse:
    op = await repository.get_operation(operation_id)
    if op is None:
        raise HTTPException(status_code=404, detail="operation not found")

    return OperationResponse(
        operationId=op["operation_id"],
        amount=_format_amount(op["amount"]),
        currency=op["currency"],
        description=op["description"],
        status=op["status"],
        providerPaymentId=op["provider_payment_id"],
    )


# ===========================================================================
# GET /operations/{id}/events
# ===========================================================================
@router.get(
    "/operations/{operation_id}/events",
    response_model=list[EventResponse],
)
async def get_events(operation_id: str) -> list[EventResponse]:
    # Проверяем существование операции, чтобы отдать 404, а не пустой список.
    op = await repository.get_operation(operation_id)
    if op is None:
        raise HTTPException(status_code=404, detail="operation not found")

    events = await repository.list_events(operation_id)
    return [
        EventResponse(
            eventId=e["event_id"],
            type=e["type"],
            fromStatus=e["from_status"],
            toStatus=e["to_status"],
            message=e["message"],
            occurredAt=e["occurred_at"],
        )
        for e in events
    ]


# ===========================================================================
# Помощники
# ===========================================================================
def _format_amount(amount) -> str:
    """Decimal → '1000.00' (как требует контракт)."""
    return f"{amount:.2f}"


def _receipt_log_ctx(payload: ReceiptRequest) -> dict:
    return {
        "operationId": payload.operationId,
        "providerPaymentId": payload.providerPaymentId,
        "result": payload.result,
    }
