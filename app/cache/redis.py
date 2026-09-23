"""Redis client factory.

Redis holds only ephemeral data (idempotency locks, rate cache, rate-limit counters).
Short socket timeouts keep a slow or unreachable Redis from stalling requests; callers
decide whether to fail open or fall back to PostgreSQL.
"""

from redis.asyncio import Redis

from app.config import Settings


def create_redis(settings: Settings) -> Redis:
    # redis-py types `from_url` as returning Any; the annotation restores the real type.
    client: Redis = Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_timeout=settings.redis_socket_timeout_seconds,
        socket_connect_timeout=settings.redis_socket_timeout_seconds,
        health_check_interval=30,
    )
    return client
