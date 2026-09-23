"""Delete expired idempotency keys: `python -m scripts.cleanup_idempotency`.

Expired keys are already ignored (treated as new) by the idempotency service, so this is
housekeeping only. In production it would run periodically (cron, or a scheduled task in
the worker). Deletes in batches so a large backlog never holds long locks.
"""

import asyncio

from sqlalchemy import text

from app.config import get_settings
from app.db.session import create_engine, create_session_factory, unit_of_work

BATCH_SIZE = 5_000

_DELETE_BATCH = text(
    """
    DELETE FROM idempotency_keys
    WHERE id IN (
        SELECT id FROM idempotency_keys
        WHERE expires_at <= now()
        LIMIT :batch_size
        FOR UPDATE SKIP LOCKED
    )
    """
)


async def delete_expired_keys(engine_url: str | None = None) -> int:
    settings = get_settings()
    engine = create_engine(
        settings if engine_url is None else settings.model_copy(update={"database_url": engine_url})
    )
    factory = create_session_factory(engine)
    total = 0
    try:
        while True:
            async with unit_of_work(factory) as session:
                result = await session.execute(_DELETE_BATCH, {"batch_size": BATCH_SIZE})
                deleted = int(result.rowcount)  # type: ignore[attr-defined]
            total += deleted
            if deleted < BATCH_SIZE:
                return total
    finally:
        await engine.dispose()


def main() -> None:
    deleted = asyncio.run(delete_expired_keys())
    print(f"deleted {deleted} expired idempotency key(s)")


if __name__ == "__main__":
    main()
