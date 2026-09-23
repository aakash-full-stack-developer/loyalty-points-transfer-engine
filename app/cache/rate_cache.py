"""Redis cache for route pricing configuration.

The cache is an optimisation, never a dependency:
- Any Redis error is logged and the request falls back to PostgreSQL (fail open).
- Entries live for at most RATE_CACHE_TTL_SECONDS, and never past the route's
  `valid_until` (rate version end, bonus end, or scheduled bonus start), so a time-based
  change is visible immediately rather than after the TTL.
- Admin changes to a rate or bonus delete the route's entry after the change commits.
  A reader that loaded the old values just before the commit could re-populate the entry;
  that window is bounded by the TTL. Transfers do not rely on this: they re-read the rate
  from PostgreSQL inside their own transaction (step 7).
"""

import json
from dataclasses import asdict
from datetime import datetime
from typing import Any

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.services.rate_engine import (
    BonusRule,
    ProgramRef,
    RateRule,
    RouteConfig,
    RouteProvider,
)

logger = structlog.get_logger(__name__)

# Bump the version when the cached structure changes, so old entries are ignored.
_KEY_PREFIX = "rate-engine:v1:route"


def route_cache_key(source_code: str, destination_code: str) -> str:
    return f"{_KEY_PREFIX}:{source_code}:{destination_code}"


def serialize_route(route: RouteConfig) -> str:
    def encode(value: Any) -> Any:
        if isinstance(value, datetime):
            return value.isoformat()
        raise TypeError(f"cannot serialize {type(value).__name__}")

    return json.dumps(asdict(route), default=encode)


def deserialize_route(payload: str) -> RouteConfig:
    data = json.loads(payload)

    def moment(value: str | None) -> datetime | None:
        return datetime.fromisoformat(value) if value is not None else None

    rate = data["rate"]
    bonus = data["bonus"]
    return RouteConfig(
        source=ProgramRef(**data["source"]),
        destination=ProgramRef(**data["destination"]),
        rate=RateRule(**{**rate, "effective_to": moment(rate["effective_to"])}) if rate else None,
        bonus=(
            BonusRule(
                bonus_id=bonus["bonus_id"],
                bonus_bps=bonus["bonus_bps"],
                starts_at=datetime.fromisoformat(bonus["starts_at"]),
                ends_at=datetime.fromisoformat(bonus["ends_at"]),
            )
            if bonus
            else None
        ),
        valid_until=moment(data["valid_until"]),
    )


class RouteCache:
    def __init__(self, redis: Redis, ttl_seconds: int) -> None:
        self._redis = redis
        self._ttl_seconds = ttl_seconds

    async def get(self, source_code: str, destination_code: str) -> RouteConfig | None:
        key = route_cache_key(source_code, destination_code)
        try:
            payload = await self._redis.get(key)
        except RedisError as exc:
            logger.warning("rate_cache_unavailable", operation="get", key=key, error=repr(exc))
            return None
        if payload is None:
            return None
        try:
            return deserialize_route(payload)
        except (ValueError, KeyError, TypeError) as exc:
            logger.warning("rate_cache_entry_invalid", key=key, error=repr(exc))
            return None

    async def set(self, route: RouteConfig, at: datetime) -> None:
        ttl_ms = self._ttl_seconds * 1000
        if route.valid_until is not None:
            ttl_ms = min(ttl_ms, int((route.valid_until - at).total_seconds() * 1000))
        if ttl_ms <= 0:
            return
        key = route_cache_key(route.source.code, route.destination.code)
        try:
            await self._redis.set(key, serialize_route(route), px=ttl_ms)
        except RedisError as exc:
            logger.warning("rate_cache_unavailable", operation="set", key=key, error=repr(exc))

    async def invalidate(self, source_code: str, destination_code: str) -> None:
        key = route_cache_key(source_code, destination_code)
        try:
            await self._redis.delete(key)
        except RedisError as exc:
            # Not fatal: the entry expires within the TTL. Logged as an error because
            # quotes may show the previous rate until then.
            logger.error("rate_cache_invalidation_failed", key=key, error=repr(exc))


class CachedRouteProvider:
    """RouteProvider that serves from Redis when possible and PostgreSQL otherwise."""

    def __init__(self, inner: RouteProvider, cache: RouteCache) -> None:
        self._inner = inner
        self._cache = cache

    async def get_route(self, source_code: str, destination_code: str, at: datetime) -> RouteConfig:
        cached = await self._cache.get(source_code, destination_code)
        if cached is not None:
            return cached
        route = await self._inner.get_route(source_code, destination_code, at)
        await self._cache.set(route, at)
        return route
