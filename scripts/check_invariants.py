"""Verify the ledger invariants: `python -m scripts.check_invariants` (or `make check-invariants`).

Exits 0 when every invariant holds and 1 on any violation, so it can gate tests, the demo
and CI. All checks run in one REPEATABLE READ transaction, so they see a single consistent
snapshot even while transfers are running.
"""

import asyncio
import sys

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Account, LedgerJournal
from app.db.session import create_engine
from app.services.ledger_invariants import INVARIANTS, find_violations


async def _run() -> int:
    engine = create_engine(get_settings())
    try:
        async with engine.connect() as connection:
            connection = await connection.execution_options(isolation_level="REPEATABLE READ")
            async with AsyncSession(bind=connection) as session, session.begin():
                violations = await find_violations(session)
                accounts = await session.scalar(select(func.count()).select_from(Account))
                journals = await session.scalar(select(func.count()).select_from(LedgerJournal))
    finally:
        await engine.dispose()

    if violations:
        print(f"FAIL: {len(violations)} ledger invariant violation(s)")
        for violation in violations:
            print(f"  - {violation.invariant}: {violation.detail}")
        return 1

    print(
        f"OK: all {len(INVARIANTS)} ledger invariants hold "
        f"({accounts} accounts, {journals} journals)"
    )
    for name in INVARIANTS:
        print(f"  - {name}")
    return 0


def main() -> None:
    sys.exit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
