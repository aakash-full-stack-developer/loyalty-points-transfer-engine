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
from pathlib import Path

import asyncpg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.base import Base
from app.db.session import create_engine, create_session_factory, unit_of_work
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


@pytest.fixture
def settings(test_database_url: str) -> Settings:
    return Settings(environment="test", database_url=test_database_url)


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
