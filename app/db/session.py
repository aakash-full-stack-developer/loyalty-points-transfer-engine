"""Async SQLAlchemy engine, session factory and transaction helper.

Statement and lock timeouts are set server-side on every connection, so a stuck query or a
lock wait can never hang a request indefinitely.

Transaction boundaries are explicit: services open a short transaction with
`unit_of_work(session_factory)` for each step. The transfer saga relies on this, because it
must commit the debit *before* calling a partner and never hold a transaction open across
that network call.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_seconds,
        pool_pre_ping=True,
        connect_args={
            "server_settings": {
                "application_name": settings.app_name,
                "statement_timeout": str(settings.db_statement_timeout_ms),
                "lock_timeout": str(settings.db_lock_timeout_ms),
            }
        },
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: objects stay readable after commit, which the transfer saga
    # needs because it commits between steps and keeps working with the same transfer.
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def unit_of_work(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """One database transaction: commits if the block succeeds, rolls back if it raises.

    Usage:
        async with unit_of_work(session_factory) as session:
            ...  # all statements here are atomic
    """
    async with session_factory() as session, session.begin():
        yield session
