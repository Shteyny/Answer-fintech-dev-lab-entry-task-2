"""
Общие фикстуры для тестов.

Postgres поднимаем напрямую через docker CLI — testcontainers с Ryuk
конфликтует с Docker Desktop (context: desktop-linux), а отключать
reaper через env не получается в текущей версии.
"""

import socket
import subprocess
import time
import uuid

import pytest
import pytest_asyncio
from src.config import settings as app_settings

# ---------------------------------------------------------------------------
# Настройки применяем при импорте conftest — гарантированно до тестов.
# Это убирает проблему с session-autouse фикстурой, которая в некоторых
# версиях pytest не срабатывает до первого запроса БД.
# ---------------------------------------------------------------------------
app_settings.provider_url = "http://provider.test"
app_settings.log_level = "WARNING"
app_settings.worker_poll_interval_ms = 50
app_settings.provider_backoff_base_s = 0.001
app_settings.provider_backoff_cap_s = 0.005


# ---------------------------------------------------------------------------
# Управление контейнером Postgres через docker CLI
# ---------------------------------------------------------------------------
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _wait_for_postgres(container: str, timeout: float = 30.0) -> None:
    start = time.time()
    while time.time() - start < timeout:
        r = subprocess.run(
            ["docker", "exec", container, "pg_isready", "-U", "app", "-d", "app"],
            capture_output=True,
            text=True,
        )
        if r.returncode == 0:
            return
        time.sleep(0.5)
    raise RuntimeError(f"Postgres container {container} did not become ready in {timeout}s")


@pytest.fixture(scope="session")
def postgres():
    """Postgres в отдельном контейнере, живёт всю сессию."""
    name = f"pg-test-{uuid.uuid4().hex[:8]}"
    port = _free_port()

    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-d",
            "--name",
            name,
            "-e",
            "POSTGRES_USER=app",
            "-e",
            "POSTGRES_PASSWORD=app",
            "-e",
            "POSTGRES_DB=app",
            "-p",
            f"{port}:5432",
            "postgres:16-alpine",
        ],
        check=True,
        capture_output=True,
    )

    try:
        _wait_for_postgres(name)
        app_settings.database_url = f"postgresql://app:app@localhost:{port}/app"
        yield
    finally:
        subprocess.run(["docker", "stop", name], capture_output=True)


@pytest_asyncio.fixture
async def db(postgres):
    """Свежий пул и пустые таблицы перед каждым тестом."""
    from src.db import close_pool, init_pool, pool

    await init_pool()
    async with pool().acquire() as conn:
        await conn.execute("TRUNCATE receipts_seen, events, operations CASCADE")

    yield

    await close_pool()
