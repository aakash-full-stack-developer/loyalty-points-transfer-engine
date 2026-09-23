from datetime import UTC, datetime, timedelta
from typing import Any, cast

from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from app.cache.rate_cache import (
    CachedRouteProvider,
    RouteCache,
    deserialize_route,
    route_cache_key,
    serialize_route,
)
from app.services.rate_engine import BonusRule, ProgramRef, RateRule, RouteConfig

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def make_route(valid_until: datetime | None = None) -> RouteConfig:
    return RouteConfig(
        source=ProgramRef(1, "NOVA_REWARDS", True),
        destination=ProgramRef(3, "SKYWARD_MILES", True),
        rate=RateRule(1, 1, 1, 1, 1_000, 1_000, 500_000, None),
        bonus=BonusRule(1, 2_500, NOW - timedelta(days=1), NOW + timedelta(days=29)),
        valid_until=valid_until,
    )


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttls_ms: dict[str, int] = {}

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: str, px: int) -> None:
        self.store[key] = value
        self.ttls_ms[key] = px

    async def delete(self, key: str) -> None:
        self.store.pop(key, None)


class DownRedis:
    async def get(self, *args: Any, **kwargs: Any) -> None:
        raise RedisConnectionError("connection refused")

    set = get
    delete = get


class CountingRoutes:
    def __init__(self, route: RouteConfig) -> None:
        self.route = route
        self.calls = 0

    async def get_route(self, source_code: str, destination_code: str, at: datetime) -> RouteConfig:
        self.calls += 1
        return self.route


def test_route_survives_serialization_round_trip() -> None:
    route = make_route(valid_until=NOW + timedelta(minutes=5))

    assert deserialize_route(serialize_route(route)) == route


async def test_second_lookup_is_served_from_cache() -> None:
    inner = CountingRoutes(make_route())
    provider = CachedRouteProvider(inner, RouteCache(cast(Redis, FakeRedis()), ttl_seconds=60))

    first = await provider.get_route("NOVA_REWARDS", "SKYWARD_MILES", NOW)
    second = await provider.get_route("NOVA_REWARDS", "SKYWARD_MILES", NOW)

    assert first == second
    assert inner.calls == 1


async def test_redis_outage_falls_back_to_database_without_error() -> None:
    inner = CountingRoutes(make_route())
    provider = CachedRouteProvider(inner, RouteCache(cast(Redis, DownRedis()), ttl_seconds=60))

    route = await provider.get_route("NOVA_REWARDS", "SKYWARD_MILES", NOW)
    await provider.get_route("NOVA_REWARDS", "SKYWARD_MILES", NOW)

    assert route == inner.route
    assert inner.calls == 2


async def test_invalidation_during_redis_outage_does_not_raise() -> None:
    await RouteCache(cast(Redis, DownRedis()), ttl_seconds=60).invalidate("A_B", "C_D")


async def test_ttl_never_outlives_the_route_configuration() -> None:
    redis = FakeRedis()
    cache = RouteCache(cast(Redis, redis), ttl_seconds=60)

    await cache.set(make_route(valid_until=NOW + timedelta(seconds=5)), at=NOW)

    assert redis.ttls_ms[route_cache_key("NOVA_REWARDS", "SKYWARD_MILES")] == 5_000


async def test_already_expired_configuration_is_not_cached() -> None:
    redis = FakeRedis()
    cache = RouteCache(cast(Redis, redis), ttl_seconds=60)

    await cache.set(make_route(valid_until=NOW), at=NOW)

    assert redis.store == {}


async def test_corrupt_cache_entry_is_ignored() -> None:
    redis = FakeRedis()
    redis.store[route_cache_key("NOVA_REWARDS", "SKYWARD_MILES")] = "{not json"

    cached = await RouteCache(cast(Redis, redis), 60).get("NOVA_REWARDS", "SKYWARD_MILES")

    assert cached is None
