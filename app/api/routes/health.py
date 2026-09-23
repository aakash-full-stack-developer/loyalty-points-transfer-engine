"""Liveness and readiness probes.

- /health/live: the process is up and serving HTTP. No dependency checks, so a database
  outage never causes an orchestrator to restart healthy API processes.
- /health/ready: PostgreSQL and Redis both answer within the timeout. Returns 503 otherwise,
  so a load balancer stops routing traffic to this instance.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Literal

import structlog
from fastapi import APIRouter, Response, status
from pydantic import BaseModel
from sqlalchemy import text

from app.api.deps import EngineDep, RedisDep, SettingsDep

CheckStatus = Literal["ok", "unavailable"]

router = APIRouter(tags=["health"])
logger = structlog.get_logger(__name__)


class LivenessResponse(BaseModel):
    status: Literal["ok"] = "ok"


class ReadinessResponse(BaseModel):
    status: CheckStatus
    checks: dict[str, CheckStatus]


@router.get("/health/live")
async def live() -> LivenessResponse:
    return LivenessResponse()


@router.get(
    "/health/ready",
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadinessResponse}},
)
async def ready(
    response: Response, engine: EngineDep, redis: RedisDep, settings: SettingsDep
) -> ReadinessResponse:
    async def check_database() -> None:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def check_redis() -> None:
        await redis.ping()

    budget = settings.health_check_timeout_seconds
    database, redis_status = await asyncio.gather(
        _run_check("database", check_database, budget),
        _run_check("redis", check_redis, budget),
    )
    checks = {"database": database, "redis": redis_status}
    overall: CheckStatus = "ok" if all(v == "ok" for v in checks.values()) else "unavailable"
    if overall != "ok":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse(status=overall, checks=checks)


async def _run_check(
    name: str, check: Callable[[], Awaitable[None]], budget_seconds: float
) -> CheckStatus:
    try:
        async with asyncio.timeout(budget_seconds):
            await check()
    except Exception as exc:
        # Details go to the log only; the response never exposes internal errors.
        logger.warning("readiness_check_failed", check=name, error=repr(exc))
        return "unavailable"
    return "ok"
