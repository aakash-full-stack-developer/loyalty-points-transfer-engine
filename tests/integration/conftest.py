"""Integration test infrastructure.

Integration tests never touch the development database. At the start of a test session a
separate database (`<dev database>_test`, or TEST_DATABASE_URL if set) is dropped, created
and migrated from scratch with Alembic, which also proves the migrations apply cleanly.
Every test starts from empty tables.
"""

import asyncio
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
import httpx
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.cache.redis import create_redis
from app.config import Settings
from app.db.base import Base
from app.db.session import create_engine, create_session_factory, unit_of_work
from app.main import create_app
from app.services.ledger_invariants import find_violations
from scripts.seed import SeedReport, seed

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SAFE_DATABASE_NAME = re.compile(r"[a-z0-9_]+")


def _derive_test_database_url() -> str:
    explicit = os.environ.get("TEST_DATABASE_URL")
    if explicit:
        return explicit
    url = make_url(Settings().database_url)
    return url.set(database=f"{url.database}_test").render_as_string(hide_password=False)


async def _recreate_database(database_url: str) -> None:
    url = make_url(database_url)
    name = url.database or ""
    if not _SAFE_DATABASE_NAME.fullmatch(name) or not name.endswith("_test"):
        raise RuntimeError(f"refusing to recreate database {name!r}: must end with _test")
    maintenance_dsn = url.set(drivername="postgresql", database="postgres").render_as_string(
        hide_password=False
    )
    connection = await asyncpg.connect(maintenance_dsn)
    try:
        await connection.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await connection.execute(f'CREATE DATABASE "{name}"')
    finally:
        await connection.close()


def _migrate(database_url: str) -> None:
    config = AlembicConfig(str(_PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_PROJECT_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    config.attributes["configure_logging"] = False
    command.upgrade(config, "head")


@pytest.fixture(scope="session")
def test_database_url() -> str:
    database_url = _derive_test_database_url()
    asyncio.run(_recreate_database(database_url))
    _migrate(database_url)
    return database_url


def _test_redis_url() -> str:
    """Integration tests use Redis logical database 1, so they never touch the dev cache (0)."""
    explicit = os.environ.get("TEST_REDIS_URL")
    if explicit:
        return explicit
    base = Settings().redis_url
    return re.sub(r"/\d+$", "", base) + "/1"


@pytest.fixture
def settings(test_database_url: str) -> Settings:
    return Settings(
        environment="test",
        database_url=test_database_url,
        redis_url=_test_redis_url(),
        # Short partner timeouts keep timeout scenarios around one second each.
        partner_connect_timeout_seconds=0.5,
        partner_read_timeout_seconds=1.0,
        partner_retry_max_attempts=2,
        partner_retry_base_delay_ms=10,
        partner_retry_max_delay_ms=50,
        partner_total_deadline_seconds=3.0,
        # Tests create many transfers per user; the limiter has its own test.
        rate_limit_transfers_per_window=10_000,
    )


@pytest.fixture
async def simulator(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    """Client for the partner simulator container, reset before and after the test.
    Resetting also clears modes set by hand in the dev stack."""
    async with httpx.AsyncClient(base_url=settings.partner_base_url) as client:
        await client.post("/simulator/reset")
        yield client
        await client.post("/simulator/reset")


@asynccontextmanager
async def running_app(settings: Settings) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient]]:
    """A separately configured app with its lifespan running (for per-test settings)."""
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield app, client


@pytest.fixture(autouse=True)
async def clean_redis(settings: Settings) -> AsyncIterator[Redis]:
    redis = create_redis(settings)
    await redis.flushdb()
    yield redis
    await redis.aclose()


@pytest.fixture
async def db_engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(settings)
    yield engine
    await engine.dispose()


@pytest.fixture
def session_factory(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(db_engine)


@pytest.fixture(autouse=True)
async def clean_database(db_engine: AsyncEngine) -> None:
    """Empty every table before each test. TRUNCATE does not fire the row-level
    append-only triggers, so it is the one way to reset the ledger."""
    tables = ", ".join(table.name for table in Base.metadata.sorted_tables)
    async with db_engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@pytest.fixture
async def seeded(session_factory: async_sessionmaker[AsyncSession]) -> SeedReport:
    async with unit_of_work(session_factory) as session:
        return await seed(session)


@pytest.fixture
async def ledger_stays_consistent(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    """Use in tests that move points: after the test, every ledger invariant must hold."""
    yield
    async with session_factory() as session:
        assert await find_violations(session) == []
