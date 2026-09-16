"""
Конфигурация сервиса.
Все значения читаются из переменных окружения (или .env при локальной разработке).
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Настройки приложения. Значения по умолчанию — для локальной разработки."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- База данных ---------------------------------------------------------
    database_url: str = Field(
        default="postgresql://app:app@localhost:5432/app",
        description="DSN для подключения к PostgreSQL",
    )
    db_pool_min_size: int = Field(default=2, ge=1)
    db_pool_max_size: int = Field(default=10, ge=1)

    # --- HTTP-сервер ---------------------------------------------------------
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8080, ge=1, le=65535)

    # --- Провайдер -----------------------------------------------------------
    provider_url: str = Field(
        default="http://localhost:8081",
        description="Базовый URL provider-simulator",
    )
    provider_timeout_s: float = Field(default=5.0, gt=0)
    provider_max_attempts: int = Field(
        default=5,
        ge=1,
        description="Максимум попыток вызова провайдера с одним Idempotency-Key",
    )
    provider_backoff_base_s: float = Field(default=0.5, gt=0)
    provider_backoff_cap_s: float = Field(default=10.0, gt=0)

    # --- Воркер --------------------------------------------------------------
    worker_poll_interval_ms: int = Field(
        default=300,
        ge=50,
        description="Как часто воркер опрашивает БД на наличие PROCESSING-операций",
    )
    worker_batch_size: int = Field(default=20, ge=1)

    # --- Логи ----------------------------------------------------------------
    log_level: str = Field(default="INFO")


@lru_cache
def get_settings() -> Settings:
    """Кэшированный доступ к настройкам (singleton)."""
    return Settings()


settings = get_settings()
