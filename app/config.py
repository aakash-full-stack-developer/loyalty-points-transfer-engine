"""Application settings, loaded from environment variables (and `.env` when present).

Every setting is documented in `.env.example`. Durations use explicit unit suffixes
(`_SECONDS`, `_MS`) so a value can never be misread. Points and rates are never configured
here; they live in the database.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, PositiveFloat, PositiveInt, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
Environment = Literal["local", "docker", "test", "production"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "loyalty-transfer-engine"
    environment: Environment = "local"
    log_level: LogLevel = "INFO"
    log_json: bool = False

    # PostgreSQL
    database_url: str = "postgresql+asyncpg://loyalty:loyalty@localhost:5432/loyalty"
    db_pool_size: PositiveInt = 10
    db_max_overflow: int = Field(default=5, ge=0)
    db_pool_timeout_seconds: PositiveFloat = 5.0
    db_statement_timeout_ms: PositiveInt = 5000
    db_lock_timeout_ms: PositiveInt = 3000

    # Redis
    redis_url: str = "redis://localhost:6379/0"
    redis_socket_timeout_seconds: PositiveFloat = 1.0

    # Partner APIs
    partner_base_url: str = "http://localhost:8001"
    partner_connect_timeout_seconds: PositiveFloat = 1.0
    partner_read_timeout_seconds: PositiveFloat = 3.0
    partner_retry_max_attempts: PositiveInt = 3
    partner_retry_base_delay_ms: PositiveInt = 100
    partner_retry_max_delay_ms: PositiveInt = 1000
    partner_total_deadline_seconds: PositiveFloat = 8.0
    circuit_breaker_failure_threshold: PositiveInt = 5
    circuit_breaker_cooldown_seconds: PositiveFloat = 30.0

    # Rate limiting (POST /v1/transfers, per user, fixed window)
    rate_limit_transfers_per_window: PositiveInt = 30
    rate_limit_window_seconds: PositiveInt = 60

    # Idempotency and caching
    idempotency_ttl_seconds: PositiveInt = 86_400
    idempotency_lock_ttl_ms: PositiveInt = 30_000
    rate_cache_ttl_seconds: PositiveInt = 60

    # Security
    admin_api_key: SecretStr = SecretStr("change-me-admin-key")

    # Health checks
    health_check_timeout_seconds: PositiveFloat = 2.0


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings (read once, then cached)."""
    return Settings()
