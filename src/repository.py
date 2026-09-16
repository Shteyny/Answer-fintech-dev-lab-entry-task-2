"""
Слой доступа к данным.

Правила:
- Каждая публичная функция делает одну вещь и возвращает простой результат.
- Все изменения — только внутри transaction() из db.py.
- SQL-запросы читаются сверху вниз, без неявных состояний.
- Никаких глобальных переменных и сайд-эффектов, кроме записи в БД.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import asyncpg

from src.db import transaction

# ---------------------------------------------------------------------------
# Набор колонок operations. Единый, чтобы SELECT-ы не расходились.
# ---------------------------------------------------------------------------
_OPERATION_COLUMNS = """
    operation_id,
    amount,
    currency,
    description,
    status,
    provider_payment_id,
    attempt_count,
    next_attempt_at,
    created_at,
    updated_at
"""


# ===========================================================================
# Публичные типы результатов
# ===========================================================================
@dataclass(frozen=True)
class SubmitResult:
    """
    Результат попытки перевести операцию в PROCESSING.

    found       — операция существует.
    created_now — именно этот вызов создал намерение отправки (202).
                  Если False — намерение уже было, отдаём 200.
    operation   — текущее состояние операции (None, если не найдена).
    """

    found: bool
    created_now: bool
    operation: dict | None


class ReceiptOutcome(enum.Enum):
    """Что сделал обработчик квитанции. Маппится в HTTP-статус в роуте."""

    APPLIED = "applied"  # 204 — первая валидная квитанция
    DUPLICATE = "duplicate"  # 204 — повтор той же квитанции
    IGNORED = "ignored"  # 204 — поздняя противоположная
    CONFLICT = "conflict"  # 409 — чужой provider_payment_id
    NOT_FOUND = "not_found"  # 404 — операции нет


# ===========================================================================
# Создание и чтение
# ===========================================================================
async def create_operation(
    operation_id: str,
    amount: Decimal,
    currency: str,
    description: str | None,
) -> bool:
    """
    Создать новую операцию. Возвращает True, если создана.
    False означает, что operation_id уже существует (роут отдаст 409).
    """
    async with transaction() as conn:
        inserted = await conn.fetchval(
            """
            INSERT INTO operations (operation_id, amount, currency, description, status)
            VALUES ($1, $2, $3, $4, 'CREATED')
            ON CONFLICT (operation_id) DO NOTHING
            RETURNING operation_id
            """,
            operation_id,
            amount,
            currency,
            description,
        )
        if inserted is None:
            return False

        await _insert_event(
            conn,
            operation_id=operation_id,
            event_type="CREATED",
            from_status=None,
            to_status="CREATED",
            message="Operation created",
        )
        return True


async def get_operation(operation_id: str) -> dict | None:
    """Прочитать операцию. None, если не найдена."""
    async with transaction() as conn:
        return await _fetch_operation(conn, operation_id, for_update=False)


# ===========================================================================
# Submit — перевод CREATED -> PROCESSING с сохранением намерения
# ===========================================================================
async def try_start_submit(operation_id: str) -> SubmitResult:
    """
    Атомарная попытка начать отправку.

    Внутри одной транзакции с FOR UPDATE:
      - читает операцию;
      - если статус CREATED — переводит в PROCESSING и пишет событие;
      - иначе возвращает текущее состояние без изменений.
    """
    async with transaction() as conn:
        operation = await _fetch_operation(conn, operation_id, for_update=True)

        if operation is None:
            return SubmitResult(found=False, created_now=False, operation=None)

        if operation["status"] != "CREATED":
            return SubmitResult(found=True, created_now=False, operation=operation)

        await conn.execute(
            """
            UPDATE operations
               SET status = 'PROCESSING',
                   next_attempt_at = now(),
                   updated_at = now()
             WHERE operation_id = $1
            """,
            operation_id,
        )
        await _insert_event(
            conn,
            operation_id=operation_id,
            event_type="SUBMITTED",
            from_status="CREATED",
            to_status="PROCESSING",
            message="Submit intent persisted",
        )

        operation["status"] = "PROCESSING"
        operation["next_attempt_at"] = "now"
        return SubmitResult(found=True, created_now=True, operation=operation)


# ===========================================================================
# Работа воркера
# ===========================================================================
async def find_due_processing(limit: int) -> list[dict]:
    """Операции, которые пора (пере)отправить провайдеру."""
    async with transaction() as conn:
        rows = await conn.fetch(
            f"""
            SELECT {_OPERATION_COLUMNS}
              FROM operations
             WHERE status = 'PROCESSING'
               AND next_attempt_at IS NOT NULL
               AND next_attempt_at <= now()
             ORDER BY next_attempt_at
             LIMIT $1
            """,
            limit,
        )
        return [dict(r) for r in rows]


async def save_provider_payment_id(operation_id: str, provider_payment_id: str) -> None:
    """
    Сохранить providerPaymentId, полученный в 202 от провайдера.
    Статус НЕ меняется — финал ставит только квитанция.
    Если операция уже финальна — ничего не делаем.
    Поле next_attempt_at не трогаем: worker сам решает, когда вернуться
    с follow-up вызовом.
    """
    async with transaction() as conn:
        row = await conn.fetchrow(
            "SELECT status, provider_payment_id FROM operations WHERE operation_id = $1 FOR UPDATE",
            operation_id,
        )
        if row is None:
            return
        if row["status"] in ("COMPLETED", "REJECTED"):
            return

        await conn.execute(
            """
            UPDATE operations
               SET provider_payment_id = COALESCE(provider_payment_id, $2),
                   updated_at = now()
             WHERE operation_id = $1
            """,
            operation_id,
            provider_payment_id,
        )


async def schedule_retry(operation_id: str, next_attempt_at: datetime) -> None:
    """Отложить следующую попытку отправки (после 5xx/сетевой ошибки)."""
    async with transaction() as conn:
        await conn.execute(
            """
            UPDATE operations
               SET attempt_count = attempt_count + 1,
                   next_attempt_at = $2,
                   updated_at = now()
             WHERE operation_id = $1
               AND status = 'PROCESSING'
            """,
            operation_id,
            next_attempt_at,
        )


# ===========================================================================
# Обработка квитанции
# ===========================================================================
async def apply_receipt(
    operation_id: str,
    provider_payment_id: str,
    result: str,
    message: str | None,
) -> ReceiptOutcome:
    """
    Обработать callback-квитанцию. Всё в одной транзакции с FOR UPDATE.

    Порядок проверок:
      1. Операции нет                          → NOT_FOUND
      2. Чужой provider_payment_id             → CONFLICT
      3. Финальный статус совпадает с result   → DUPLICATE
      4. Финальный статус противоположен       → IGNORED (записываем ignored)
      5. Первая валидная квитанция             → APPLIED (ставим финал)
    """
    async with transaction() as conn:
        row = await conn.fetchrow(
            "SELECT status, provider_payment_id FROM operations WHERE operation_id = $1 FOR UPDATE",
            operation_id,
        )
        if row is None:
            return ReceiptOutcome.NOT_FOUND

        current_status = row["status"]
        stored_ppid = row["provider_payment_id"]

        if stored_ppid is not None and stored_ppid != provider_payment_id:
            return ReceiptOutcome.CONFLICT

        if current_status in ("COMPLETED", "REJECTED"):
            if current_status == result:
                return ReceiptOutcome.DUPLICATE

            await _record_receipt(conn, operation_id, provider_payment_id, result, ignored=True)
            return ReceiptOutcome.IGNORED

        await conn.execute(
            """
            UPDATE operations
               SET status = $2,
                   provider_payment_id = COALESCE(provider_payment_id, $3),
                   next_attempt_at = NULL,
                   updated_at = now()
             WHERE operation_id = $1
            """,
            operation_id,
            result,
            provider_payment_id,
        )
        await _record_receipt(conn, operation_id, provider_payment_id, result, ignored=False)
        await _insert_event(
            conn,
            operation_id=operation_id,
            event_type=result,
            from_status="PROCESSING",
            to_status=result,
            message=message or f"Receipt: {result}",
        )
        return ReceiptOutcome.APPLIED


# ===========================================================================
# История
# ===========================================================================
async def list_events(operation_id: str) -> list[dict]:
    """События операции в порядке event_id."""
    async with transaction() as conn:
        rows = await conn.fetch(
            """
            SELECT event_id, type, from_status, to_status, message, occurred_at
              FROM events
             WHERE operation_id = $1
             ORDER BY event_id
            """,
            operation_id,
        )
        return [dict(r) for r in rows]


# ===========================================================================
# Приватные помощники (используются только внутри транзакции)
# ===========================================================================
async def _fetch_operation(
    conn: asyncpg.Connection,
    operation_id: str,
    for_update: bool,
) -> dict | None:
    """Читает операцию. for_update=True добавляет блокировку строки."""
    lock = "FOR UPDATE" if for_update else ""
    row = await conn.fetchrow(
        f"SELECT {_OPERATION_COLUMNS} FROM operations WHERE operation_id = $1 {lock}",
        operation_id,
    )
    return dict(row) if row else None


async def _insert_event(
    conn: asyncpg.Connection,
    operation_id: str,
    event_type: str,
    from_status: str | None,
    to_status: str | None,
    message: str | None,
) -> None:
    """Вставляет событие с монотонным event_id в пределах операции."""
    next_id = await conn.fetchval(
        "SELECT COALESCE(MAX(event_id), 0) + 1 FROM events WHERE operation_id = $1",
        operation_id,
    )
    await conn.execute(
        """
        INSERT INTO events (operation_id, event_id, type, from_status, to_status, message)
        VALUES ($1, $2, $3, $4, $5, $6)
        """,
        operation_id,
        next_id,
        event_type,
        from_status,
        to_status,
        message,
    )


async def _record_receipt(
    conn: asyncpg.Connection,
    operation_id: str,
    provider_payment_id: str,
    result: str,
    ignored: bool,
) -> None:
    """Помечает квитанцию как обработанную (или проигнорированную). Идемпотентно."""
    await conn.execute(
        """
        INSERT INTO receipts_seen (operation_id, provider_payment_id, result, ignored)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (operation_id, provider_payment_id, result) DO NOTHING
        """,
        operation_id,
        provider_payment_id,
        result,
        ignored,
    )
