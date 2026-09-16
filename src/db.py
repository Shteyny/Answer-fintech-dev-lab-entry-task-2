"""
Асинхронный пул соединений с PostgreSQL + helper для транзакций.
Всё, что меняет состояние, должно идти через transaction().
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg

from src.config import settings

log = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


# ---------------------------------------------------------------------------
# Жизненный цикл пула
# ---------------------------------------------------------------------------
async def init_pool() -> None:
    """Создаёт пул и идемпотентно применяет схему. Вызывается один раз в lifespan."""
    global _pool
    if _pool is not None:
        return

    log.info("db.pool.creating", extra={"dsn": _mask_dsn(settings.database_url)})
    _pool = await asyncpg.create_pool(
        dsn=settings.database_url,
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
        command_timeout=10,
        # Отключаем prepared statement cache для совместимости с pgbouncer,
        # если он появится в будущем. Небольшой оверхед, зато предсказуемо.
        statement_cache_size=0,
    )

    ddl = SCHEMA_PATH.read_text(encoding="utf-8")
    async with _pool.acquire() as conn:
        await conn.execute(ddl)
    log.info("db.schema.applied")


async def close_pool() -> None:
    """Закрывает пул. Вызывается в конце lifespan."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        log.info("db.pool.closed")


def pool() -> asyncpg.Pool:
    """Доступ к пулу. Падает, если пул не инициализирован — это баг, а не runtime-ситуация."""
    if _pool is None:
        raise RuntimeError("DB pool is not initialized. Call init_pool() first.")
    return _pool


# ---------------------------------------------------------------------------
# Транзакции
# ---------------------------------------------------------------------------
@asynccontextmanager
async def transaction() -> AsyncIterator[asyncpg.Connection]:
    """
    Контекстный менеджер транзакции.

    Использование:
        async with transaction() as conn:
            await conn.execute(...)
            await conn.fetchrow(...)

    Внутри — BEGIN ... COMMIT. При исключении — ROLLBACK.
    Вложенные вызовы transaction() создадут SAVEPOINT (asyncpg это умеет).
    """
    async with pool().acquire() as conn:
        async with conn.transaction():
            yield conn


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------
def _mask_dsn(dsn: str) -> str:
    """Прячет пароль в логах."""
    if "@" not in dsn:
        return dsn
    scheme, rest = dsn.split("://", 1)
    creds, host = rest.split("@", 1)
    if ":" in creds:
        user, _ = creds.split(":", 1)
        return f"{scheme}://{user}:***@{host}"
    return dsn
