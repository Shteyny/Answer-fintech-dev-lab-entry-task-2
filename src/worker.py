"""
Фоновый воркер.

Раз в POLL_INTERVAL просыпается, берёт порцию PROCESSING-операций с наступившим
next_attempt_at и пытается отправить их провайдеру. Всё состояние — в БД,
никаких очередей в памяти.

Инварианты:
- воркер НЕ ставит финальный статус — это делает только apply_receipt;
- после рестарта воркер сам подхватывает незавершённые PROCESSING-операции;
- падение одной операции не валит остальные.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import UTC, datetime, timedelta

from src import provider, repository
from src.config import settings

log = logging.getLogger(__name__)

FOLLOWUP_DELAY_S = 30  # перезапрос провайдера, если callback не пришёл


# ===========================================================================
# Публичный вход
# ===========================================================================
async def worker_loop(shutdown: asyncio.Event) -> None:
    """
    Главный цикл воркера. Работает, пока shutdown не установлен.

    Запускается один раз из lifespan. Останавливается через shutdown.set().
    """
    log.info("worker.start", extra={"poll_ms": settings.worker_poll_interval_ms})

    while not shutdown.is_set():
        try:
            processed = await _process_batch()
            if processed == 0:
                await _wait_or_shutdown(shutdown, settings.worker_poll_interval_ms / 1000)
            # Если что-то обработали — сразу идём за следующей порцией,
            # не ждём poll_interval. Так бэклог рассасывается быстрее.
        except Exception:
            # Воркер не должен умирать от одной ошибки.
            log.exception("worker.loop_error")
            await _wait_or_shutdown(shutdown, 1.0)

    log.info("worker.stop")


# ===========================================================================
# Обработка порции
# ===========================================================================
async def _process_batch() -> int:
    """Взять порцию due-операций и обработать. Возвращает число обработанных."""
    operations = await repository.find_due_processing(settings.worker_batch_size)
    if not operations:
        return 0

    for op in operations:
        # Обрабатываем последовательно — операции независимы, но так проще
        # рассуждать и не раздувать пул соединений. Для нагрузки хватит.
        await _handle_operation(op)

    return len(operations)


async def _handle_operation(op: dict) -> None:
    """
    Одна операция. Ловим все исключения, чтобы одна упавшая не сломала цикл.

    Вызываем провайдера всегда, когда операция попала в due-очередь:
    - если ppid ещё нет — это первая отправка;
    - если ppid уже есть, но callback не пришёл — это follow-up.
      Idempotency-Key гарантирует, что провайдер вернёт тот же платёж.
    """
    operation_id = op["operation_id"]
    try:
        await _try_submit(op)
    except Exception:
        log.exception("worker.operation_error", extra={"operationId": operation_id})


# ===========================================================================
# Ветки: отправка и «тихое» ожидание
# ===========================================================================
async def _try_submit(op: dict) -> None:
    """Вызвать провайдера и разложить результат по инвариантам."""
    operation_id = op["operation_id"]
    log.info(
        "worker.submit",
        extra={
            "operationId": operation_id,
            "attempt": op["attempt_count"] + 1,
        },
    )

    result = await provider.submit_payment(
        operation_id=operation_id,
        amount=_format_amount(op["amount"]),
        currency=op["currency"],
    )

    await _apply_provider_result(op, result)


async def _apply_provider_result(op: dict, result: provider.ProviderResult) -> None:
    """
    Реакция воркера на исход вызова. Единственное правило:
    финальный статус здесь не ставится никогда.
    """
    operation_id = op["operation_id"]
    now = datetime.now(UTC)
    next_attempt = op["attempt_count"] + 1

    match result.outcome:
        case provider.ProviderOutcome.ACCEPTED:
            await repository.save_provider_payment_id(operation_id, result.provider_payment_id)
            # Планируем follow-up: если callback не придёт, вернёмся через
            # FOLLOWUP_DELAY_S и дёрнем провайдера снова. Idempotency-Key
            # защитит от второго платежа.
            await repository.schedule_retry(operation_id, now + timedelta(seconds=FOLLOWUP_DELAY_S))
            log.info(
                "worker.accepted",
                extra={
                    "operationId": operation_id,
                    "providerPaymentId": result.provider_payment_id,
                },
            )

        case provider.ProviderOutcome.RETRYABLE:
            delay = _backoff_for(next_attempt, base=2.0, cap=60.0)
            await repository.schedule_retry(operation_id, now + timedelta(seconds=delay))

        case provider.ProviderOutcome.PERMANENT | provider.ProviderOutcome.UNEXPECTED:
            # Провайдер мог уже принять платёж — НЕ помечаем REJECTED,
            # ждём квитанцию. Просто тормозим попытки сильнее.
            delay = _backoff_for(next_attempt, base=5.0, cap=120.0)
            await repository.schedule_retry(operation_id, now + timedelta(seconds=delay))


def _backoff_for(attempt: int, base: float, cap: float) -> float:
    """
    Экспоненциальный backoff с полным jitter.

    Идея: 2^(attempt-1) * base, зажато сверху cap, и случайно размазано
    в [0, capped]. Полный jitter разводит «стадо» одновременных retry.
    """
    raw = base * (2 ** (attempt - 1))
    capped = min(raw, cap)
    return random.uniform(0, capped)


# ===========================================================================
# Помощники
# ===========================================================================
def _format_amount(amount) -> str:
    """Привести Decimal/строку из БД к строке вида '1000.00'."""
    if isinstance(amount, str):
        return amount
    return f"{amount:.2f}"


async def _wait_or_shutdown(shutdown: asyncio.Event, seconds: float) -> None:
    """Спать, но проснуться раньше, если пришёл shutdown."""
    try:
        await asyncio.wait_for(shutdown.wait(), timeout=seconds)
    except TimeoutError:
        pass
