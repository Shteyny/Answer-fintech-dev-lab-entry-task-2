"""
HTTP-клиент к provider-simulator.

Правила:
- Один вызов = один POST /payments с Idempotency-Key = operationId.
- Повторы используют тот же ключ и то же тело — провайдер вернёт тот же платёж.
- 503 и сетевые ошибки → retry с backoff. Остальные 4xx/5xx → без retry.
- Успешный 202 возвращает providerPaymentId. Статус операции НЕ меняется —
  финал ставит только квитанция.
"""

from __future__ import annotations

import enum
import logging
import random
from dataclasses import dataclass

import httpx

from src.config import settings

log = logging.getLogger(__name__)


# ===========================================================================
# Результат вызова — то, что видит воркер
# ===========================================================================
class ProviderOutcome(enum.Enum):
    ACCEPTED = "accepted"  # 202, есть providerPaymentId
    RETRYABLE = "retryable"  # 503 / сеть / таймаут — повторить позже
    PERMANENT = "permanent"  # 4xx (кроме 429) — повторять бессмысленно
    UNEXPECTED = "unexpected"  # 5xx кроме 503, битый JSON и т.п.


@dataclass(frozen=True)
class ProviderResult:
    outcome: ProviderOutcome
    provider_payment_id: str | None = None
    http_status: int | None = None
    error: str | None = None


# ===========================================================================
# Публичный вызов
# ===========================================================================
async def submit_payment(
    operation_id: str,
    amount: str,
    currency: str,
) -> ProviderResult:
    """
    Отправить платёж провайдеру.

    amount приходит строкой (как хранится в БД) — не конвертируем во float,
    чтобы не потерять точность на проводе.
    """
    payload = {
        "operationId": operation_id,
        "amount": amount,
        "currency": currency,
    }
    headers = {
        "Idempotency-Key": operation_id,
        "X-Correlation-ID": operation_id,
    }

    last_error: str | None = None

    for attempt in range(1, settings.provider_max_attempts + 1):
        result = await _attempt_call(operation_id, payload, headers, attempt)

        if result.outcome is not ProviderOutcome.RETRYABLE:
            return result

        last_error = result.error
        if attempt < settings.provider_max_attempts:
            delay = _backoff_delay(attempt)
            log.warning(
                "provider.retry",
                extra={
                    "operationId": operation_id,
                    "attempt": attempt,
                    "delay_s": round(delay, 3),
                    "reason": result.error,
                },
            )
            await _sleep(delay)

    log.error(
        "provider.exhausted",
        extra={
            "operationId": operation_id,
            "attempts": settings.provider_max_attempts,
            "last_error": last_error,
        },
    )
    return ProviderResult(outcome=ProviderOutcome.RETRYABLE, error=last_error)


# ===========================================================================
# Одна попытка
# ===========================================================================
async def _attempt_call(
    operation_id: str,
    payload: dict,
    headers: dict,
    attempt: int,
) -> ProviderResult:
    """Один POST к провайдеру. Не бросает исключений наружу."""
    url = f"{settings.provider_url.rstrip('/')}/payments"

    try:
        async with httpx.AsyncClient(timeout=settings.provider_timeout_s) as client:
            response = await client.post(url, json=payload, headers=headers)
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        log.warning(
            "provider.network_error",
            extra={"operationId": operation_id, "attempt": attempt, "error": type(exc).__name__},
        )
        return ProviderResult(outcome=ProviderOutcome.RETRYABLE, error=type(exc).__name__)

    return _classify_response(operation_id, response, attempt)


def _classify_response(
    operation_id: str,
    response: httpx.Response,
    attempt: int,
) -> ProviderResult:
    """Превратить HTTP-ответ в ProviderResult. Один вход — один исход."""
    status = response.status_code

    if status == 202:
        return _parse_accepted(operation_id, response)

    if status == 503 or status == 429:
        log.warning(
            "provider.retryable_status",
            extra={"operationId": operation_id, "attempt": attempt, "status": status},
        )
        return ProviderResult(
            outcome=ProviderOutcome.RETRYABLE, http_status=status, error=f"HTTP {status}"
        )

    if 400 <= status < 500:
        log.error(
            "provider.permanent_error",
            extra={
                "operationId": operation_id,
                "attempt": attempt,
                "status": status,
                "body": response.text[:500],
            },
        )
        return ProviderResult(
            outcome=ProviderOutcome.PERMANENT, http_status=status, error=f"HTTP {status}"
        )

    log.error(
        "provider.unexpected_status",
        extra={"operationId": operation_id, "attempt": attempt, "status": status},
    )
    return ProviderResult(
        outcome=ProviderOutcome.UNEXPECTED, http_status=status, error=f"HTTP {status}"
    )


def _parse_accepted(operation_id: str, response: httpx.Response) -> ProviderResult:
    """Разобрать тело 202 и вытащить providerPaymentId."""
    try:
        body = response.json()
    except ValueError:
        log.error(
            "provider.bad_json", extra={"operationId": operation_id, "body": response.text[:500]}
        )
        return ProviderResult(
            outcome=ProviderOutcome.UNEXPECTED, http_status=202, error="invalid json"
        )

    provider_payment_id = body.get("providerPaymentId")
    if not isinstance(provider_payment_id, str) or not provider_payment_id:
        log.error("provider.missing_ppid", extra={"operationId": operation_id, "body": body})
        return ProviderResult(
            outcome=ProviderOutcome.UNEXPECTED, http_status=202, error="missing providerPaymentId"
        )

    log.info(
        "provider.accepted",
        extra={"operationId": operation_id, "providerPaymentId": provider_payment_id},
    )
    return ProviderResult(
        outcome=ProviderOutcome.ACCEPTED, provider_payment_id=provider_payment_id, http_status=202
    )


# ===========================================================================
# Backoff с jitter
# ===========================================================================
def _backoff_delay(attempt: int) -> float:
    """
    Экспоненциальный backoff с полным jitter.

    base * 2^(attempt-1), зажат сверху cap, и случайно размазан в [0, base_delay].
    Полный jitter защищает от синхронных "стад" при массовых сбоях.
    """
    raw = settings.provider_backoff_base_s * (2 ** (attempt - 1))
    capped = min(raw, settings.provider_backoff_cap_s)
    return random.uniform(0, capped)


async def _sleep(seconds: float) -> None:
    """Обёртка над asyncio.sleep — вынесена, чтобы легко мокать в тестах."""
    import asyncio

    await asyncio.sleep(seconds)
