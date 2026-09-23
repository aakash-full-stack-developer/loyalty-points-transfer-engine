from typing import Any, cast

from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from app.cache.rate_limiter import FixedWindowRateLimiter


class DownRedis:
    def pipeline(self, *args: Any, **kwargs: Any) -> Any:
        raise RedisConnectionError("connection refused")


async def test_redis_outage_fails_open() -> None:
    limiter = FixedWindowRateLimiter(
        cast(Redis, DownRedis()), scope="transfers", limit=1, window_seconds=60
    )

    decisions = [await limiter.hit("user_alice") for _ in range(5)]

    assert all(decision.allowed for decision in decisions)
