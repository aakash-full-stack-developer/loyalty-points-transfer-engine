"""Fixed-window rate limiter in Redis (per user, for POST /v1/transfers).

Each window is one Redis key, `ratelimit:<scope>:<subject>:<window number>`, incremented
with INCR and given a TTL so old windows clean themselves up. Fixed windows allow up to 2x
the limit across a window boundary; that is acceptable for abuse protection and far
simpler than a sliding window.

Fails open: if Redis is unavailable the request is allowed and a warning is logged. The
limiter protects capacity, not correctness (idempotency and the ledger do that), so an
outage should not block every transfer.
"""

import math
import time
from collections.abc import Callable
from dataclasses import dataclass

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: int


class FixedWindowRateLimiter:
    def __init__(
        self,
        redis: Redis,
        scope: str,
        limit: int,
        window_seconds: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._redis = redis
        self._scope = scope
        self._limit = limit
        self._window_seconds = window_seconds
        self._clock = clock

    async def hit(self, subject: str) -> RateLimitDecision:
        now = self._clock()
        window = int(now // self._window_seconds)
        key = f"ratelimit:{self._scope}:{subject}:{window}"
        retry_after = max(1, math.ceil((window + 1) * self._window_seconds - now))
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.incr(key)
                pipe.expire(key, self._window_seconds + 1)
                count, _ = await pipe.execute()
        except RedisError as exc:
            logger.warning("rate_limiter_unavailable_failing_open", error=repr(exc))
            return RateLimitDecision(True, self._limit, self._limit, 0)

        count = int(count)
        return RateLimitDecision(
            allowed=count <= self._limit,
            limit=self._limit,
            remaining=max(0, self._limit - count),
            retry_after_seconds=retry_after,
        )
