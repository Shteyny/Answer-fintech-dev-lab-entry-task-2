"""
Pydantic-схемы для HTTP-контракта.
Отдельные модели на вход и на выход — не смешивай их.
"""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# POST /operations
# ---------------------------------------------------------------------------
class OperationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operationId: str = Field(min_length=1, max_length=128)
    amount: Decimal = Field(gt=0, max_digits=19, decimal_places=2)
    currency: Literal["RUB"]
    description: str | None = Field(default=None, max_length=512)


class OperationResponse(BaseModel):
    operationId: str
    amount: str  # отдаём строкой — как в задании
    currency: str
    description: str | None
    status: Literal["CREATED", "PROCESSING", "COMPLETED", "REJECTED"]
    providerPaymentId: str | None


# ---------------------------------------------------------------------------
# POST /operations/{id}/submit
# ---------------------------------------------------------------------------
class SubmitResponse(BaseModel):
    operationId: str
    status: Literal["CREATED", "PROCESSING", "COMPLETED", "REJECTED"]
    providerPaymentId: str | None = None


# ---------------------------------------------------------------------------
# POST /receipts
# ---------------------------------------------------------------------------
class ReceiptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    providerPaymentId: str = Field(min_length=1)
    operationId: str = Field(min_length=1)
    result: Literal["COMPLETED", "REJECTED"]
    message: str | None = None
    occurredAt: datetime


# ---------------------------------------------------------------------------
# GET /operations/{id}/events
# ---------------------------------------------------------------------------
class EventResponse(BaseModel):
    eventId: int
    type: str
    fromStatus: str | None
    toStatus: str | None
    message: str | None
    occurredAt: datetime


# ---------------------------------------------------------------------------
# Внутренний транспорт между repository и роутами (не сериализуется наружу)
# ---------------------------------------------------------------------------
class OperationRow(BaseModel):
    """Строка operations, как её читает repository."""

    model_config = ConfigDict(from_attributes=True)

    operation_id: str
    amount: Decimal
    currency: str
    description: str | None
    status: str
    provider_payment_id: str | None
    attempt_count: int
    next_attempt_at: datetime | None
    created_at: datetime
    updated_at: datetime


class EventRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    operation_id: str
    event_id: int
    type: str
    from_status: str | None
    to_status: str | None
    message: str | None
    occurred_at: datetime
