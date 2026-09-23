"""Helpers for integration tests that work directly with the ledger."""

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Account, Program
from app.domain.enums import AccountType, OwnerType
from app.domain.ids import new_transfer_id


async def user_account(session: AsyncSession, user_id: str, program_code: str) -> Account:
    account = await session.scalar(
        select(Account)
        .join(Program, Program.id == Account.program_id)
        .where(Account.user_id == user_id, Program.code == program_code)
    )
    assert account is not None, f"no {program_code} account for {user_id}"
    return account


async def system_account(
    session: AsyncSession, program_code: str, account_type: AccountType
) -> Account:
    account = await session.scalar(
        select(Account)
        .join(Program, Program.id == Account.program_id)
        .where(
            Account.owner_type == OwnerType.SYSTEM,
            Account.account_type == account_type,
            Program.code == program_code,
        )
    )
    assert account is not None
    return account


async def insert_transfer_row(
    session: AsyncSession,
    user_id: str = "user_alice",
    source: str = "NOVA_REWARDS",
    destination: str = "SKYWARD_MILES",
    points: int = 1_000,
) -> str:
    """Insert a minimal PENDING transfer so transfer journals have something to reference.
    The real transfer flow arrives in step 7."""
    transfer_id = new_transfer_id()
    await session.execute(
        text(
            "INSERT INTO transfers (id, user_id, idempotency_key, source_program_id, "
            "destination_program_id, source_account_id, destination_account_id, source_points, "
            "base_points, bonus_points, destination_points, rate_id, rate_snapshot, status) "
            "SELECT :id, :user_id, :id, s.id, d.id, src.id, dst.id, :points, :points, 0, "
            ":points, r.id, '{}'::jsonb, 'PENDING' "
            "FROM programs s "
            "JOIN programs d ON d.code = :destination "
            "JOIN conversion_rates r "
            "  ON r.source_program_id = s.id AND r.destination_program_id = d.id "
            "JOIN accounts src ON src.program_id = s.id AND src.user_id = :user_id "
            "JOIN accounts dst ON dst.program_id = d.id AND dst.user_id = :user_id "
            "WHERE s.code = :source "
            "LIMIT 1"
        ),
        {
            "id": transfer_id,
            "user_id": user_id,
            "source": source,
            "destination": destination,
            "points": points,
        },
    )
    return transfer_id
