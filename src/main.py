"""
Сборка FastAPI-приложения: lifespan, воркер, логи, роуты.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime

from fastapi import FastAPI

from src.config import settings
from src.db import close_pool, init_pool
from src.routes import router
from src.worker import worker_loop


class _JsonFormatter(logging.Formatter):
    """
    Минимальный JSON-форматтер для стандартного logging.
    Все поля, переданные через extra={...}, попадают в JSON как есть.
    """

    _RESERVED = {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "message",
        "asctime",
        "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in self._RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def _configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(_JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.log_level)

    # uvicorn пишет в свои логгеры — перенаправляем их в root.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers.clear()
        uv_logger.propagate = True

    # Сторонние библиотеки — на WARNING, независимо от LOG_LEVEL.
    for name in ("httpcore", "httpx", "asyncio"):
        logging.getLogger(name).setLevel(logging.WARNING)


# ===========================================================================
# Lifespan: пул → воркер → работа → graceful shutdown
# ===========================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    _configure_logging()
    log = logging.getLogger(__name__)

    await init_pool()
    log.info("app.start", extra={"provider_url": settings.provider_url})

    shutdown = asyncio.Event()
    worker_task = asyncio.create_task(worker_loop(shutdown), name="worker")

    try:
        yield
    finally:
        log.info("app.shutdown.begin")
        shutdown.set()
        with suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(worker_task, timeout=5.0)
        await close_pool()
        log.info("app.shutdown.done")


# ===========================================================================
# Приложение
# ===========================================================================
app = FastAPI(
    title="Payment Operations Service",
    version="0.1.0",
    lifespan=lifespan,
)
app.include_router(router)
