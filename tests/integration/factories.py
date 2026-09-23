"""Factories for integration tests that need their own data on top of the seed."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Account, Program, User
from app.db.session import unit_of_work
from app.domain.enums import AccountType, JournalType, OwnerType
from app.services.ledger import Posting, get_system_account, post_journal


async def create_user(
    factory: async_sessionmaker[AsyncSession], user_id: str, balances: dict[str, int]
) -> None:
    """A user with an account in each given program, funded through balanced SEED journals
    (so the ledger invariants hold, exactly as for seeded users)."""
    async with unit_of_work(factory) as session:
        session.add(User(id=user_id))
        await session.flush()
        for code, balance in balances.items():
            program = await session.scalar(select(Program).where(Program.code == code))
            assert program is not None, code
            account = Account(
                owner_type=OwnerType.USER,
                user_id=user_id,
                program_id=program.id,
                account_type=AccountType.USER_BALANCE,
                external_member_id=f"{user_id}-{code}",
                balance=0,
            )
            session.add(account)
            await session.flush()
            if balance:
                settlement = await get_system_account(
                    session, program.id, AccountType.PARTNER_SETTLEMENT
                )
                await post_journal(
                    session,
                    JournalType.SEED,
                    [Posting.credit(account, balance), Posting.debit(settlement, balance)],
                )
