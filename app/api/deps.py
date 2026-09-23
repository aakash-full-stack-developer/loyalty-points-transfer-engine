"""FastAPI dependencies: process-wide resources from the app lifespan, and service wiring."""

import hmac
import re
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Annotated, cast

from fastapi import Depends, Header, Request
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.cache.rate_cache import CachedRouteProvider, RouteCache
from app.cache.rate_limiter import FixedWindowRateLimiter
from app.config import Settings
from app.db.models import User
from app.domain.errors import DomainError, ErrorCode
from app.partners.registry import PartnerRegistry
from app.services.idempotency import IdempotencyService
from app.services.rate_admin import RateAdminService
from app.services.rate_engine import QuoteService
from app.services.rate_repository import RateRepository
from app.services.reconciliation import ReconciliationPolicy, ReconciliationService
from app.services.transfer_service import TransferService


def get_settings_dep(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def get_engine(request: Request) -> AsyncEngine:
    return cast(AsyncEngine, request.app.state.db_engine)


def get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)


async def get_session(
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> AsyncIterator[AsyncSession]:
    """Request-scoped session for read-only endpoints. It never commits: anything that
    writes uses `unit_of_work` so its transaction boundary is explicit."""
    async with session_factory() as session:
        yield session


def get_redis(request: Request) -> Redis:
    return cast(Redis, request.app.state.redis)


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
EngineDep = Annotated[AsyncEngine, Depends(get_engine)]
SessionFactoryDep = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
RedisDep = Annotated[Redis, Depends(get_redis)]


def get_route_cache(redis: RedisDep, settings: SettingsDep) -> RouteCache:
    return RouteCache(redis, ttl_seconds=settings.rate_cache_ttl_seconds)


RouteCacheDep = Annotated[RouteCache, Depends(get_route_cache)]


def get_quote_service(
    session: SessionDep, cache: RouteCacheDep, settings: SettingsDep
) -> QuoteService:
    return QuoteService(
        routes=CachedRouteProvider(RateRepository(session), cache),
        quote_ttl=timedelta(seconds=settings.quote_ttl_seconds),
    )


def get_rate_admin_service(
    session_factory: SessionFactoryDep, cache: RouteCacheDep
) -> RateAdminService:
    return RateAdminService(session_factory, invalidate_cache=cache.invalidate)


def get_idempotency_service(
    session_factory: SessionFactoryDep, redis: RedisDep, settings: SettingsDep
) -> IdempotencyService:
    return IdempotencyService(
        session_factory,
        redis,
        key_ttl=timedelta(seconds=settings.idempotency_ttl_seconds),
        lock_ttl=timedelta(milliseconds=settings.idempotency_lock_ttl_ms),
    )


IdempotencyServiceDep = Annotated[IdempotencyService, Depends(get_idempotency_service)]


def get_partner_registry(request: Request) -> PartnerRegistry:
    return cast(PartnerRegistry, request.app.state.partners)


def get_transfer_service(
    session_factory: SessionFactoryDep,
    partners: Annotated[PartnerRegistry, Depends(get_partner_registry)],
    settings: SettingsDep,
) -> TransferService:
    return TransferService(
        session_factory,
        partners,
        verification_delay=timedelta(seconds=settings.reconciliation_initial_delay_seconds),
    )


TransferServiceDep = Annotated[TransferService, Depends(get_transfer_service)]


def get_reconciliation_service(
    session_factory: SessionFactoryDep,
    partners: Annotated[PartnerRegistry, Depends(get_partner_registry)],
    settings: SettingsDep,
) -> ReconciliationService:
    return ReconciliationService(
        session_factory, partners, ReconciliationPolicy.from_settings(settings)
    )


ReconciliationServiceDep = Annotated[ReconciliationService, Depends(get_reconciliation_service)]
QuoteServiceDep = Annotated[QuoteService, Depends(get_quote_service)]
RateAdminServiceDep = Annotated[RateAdminService, Depends(get_rate_admin_service)]


_USER_ID_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,64}")


async def get_current_user_id(
    session_factory: SessionFactoryDep,
    x_user_id: Annotated[
        str | None,
        Header(description="Stand-in for an authenticated identity (auth is out of scope)"),
    ] = None,
) -> str:
    """The caller's user id. In production this would come from a verified token; here the
    header must name an existing user, as a token would. An unknown identity is treated as
    unauthenticated (401), the same answer a real authentication layer would give."""
    if x_user_id is None or not _USER_ID_PATTERN.fullmatch(x_user_id):
        raise DomainError(
            ErrorCode.AUTHENTICATION_REQUIRED, "Missing or malformed X-User-Id header."
        )
    async with session_factory() as session:
        exists = await session.scalar(select(User.id).where(User.id == x_user_id))
    if exists is None:
        raise DomainError(ErrorCode.AUTHENTICATION_REQUIRED, "Unknown user.")
    return x_user_id


CurrentUserId = Annotated[str, Depends(get_current_user_id)]


async def enforce_transfer_rate_limit(
    user_id: CurrentUserId, redis: RedisDep, settings: SettingsDep
) -> None:
    """Per-user fixed-window limit on transfer creation (fails open if Redis is down)."""
    limiter = FixedWindowRateLimiter(
        redis,
        scope="transfers",
        limit=settings.rate_limit_transfers_per_window,
        window_seconds=settings.rate_limit_window_seconds,
    )
    decision = await limiter.hit(user_id)
    if not decision.allowed:
        raise DomainError(
            ErrorCode.RATE_LIMITED,
            f"At most {decision.limit} transfers per {settings.rate_limit_window_seconds} s.",
            headers={"Retry-After": str(decision.retry_after_seconds)},
            retry_after_seconds=decision.retry_after_seconds,
        )


async def require_admin(
    settings: SettingsDep,
    x_admin_key: Annotated[str | None, Header(description="Admin API key")] = None,
) -> None:
    """Admin endpoints need X-Admin-Key. Compared in constant time so response timing
    reveals nothing about how much of a guessed key was correct."""
    expected = settings.admin_api_key.get_secret_value().encode()
    provided = (x_admin_key or "").encode()
    if not hmac.compare_digest(provided, expected):
        raise DomainError(ErrorCode.ADMIN_AUTH_REQUIRED, "Missing or invalid X-Admin-Key header.")
