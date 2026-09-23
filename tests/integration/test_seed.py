from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import (
    Account,
    ConversionRate,
    LedgerEntry,
    LedgerJournal,
    Program,
    TransferBonus,
    User,
)
from app.db.session import unit_of_work
from app.domain.enums import AccountType, OwnerType
from app.services.ledger_invariants import find_violations
from scripts.seed import USERS, SeedReport, seed


async def _row_counts(session: AsyncSession) -> dict[str, int]:
    counts: dict[str, int] = {}
    models = (Program, User, Account, ConversionRate, TransferBonus, LedgerJournal, LedgerEntry)
    for model in models:
        count = await session.scalar(select(func.count()).select_from(model))
        counts[model.__tablename__] = count or 0
    return counts


async def test_seed_creates_expected_reference_data(
    seeded: SeedReport, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    assert seeded.created == {
        "programs": 5,
        "system_accounts": 10,
        "rates": 6,
        "bonuses": 1,
        "users": 2,
        "user_accounts": 10,
    }
    async with session_factory() as session:
        assert await _row_counts(session) == {
            "programs": 5,
            "users": 2,
            "accounts": 20,
            "conversion_rates": 6,
            "transfer_bonuses": 1,
            "ledger_journals": 10,
            "ledger_entries": 20,
        }


async def test_seed_is_idempotent(
    seeded: SeedReport, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session:
        before = await _row_counts(session)

    async with unit_of_work(session_factory) as session:
        second = await seed(session)

    assert set(second.created.values()) == {0}
    async with session_factory() as session:
        assert await _row_counts(session) == before


async def test_seeded_ledger_satisfies_all_invariants(
    seeded: SeedReport, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session:
        assert await find_violations(session) == []


async def test_opening_balances_are_mirrored_by_negative_settlement_accounts(
    seeded: SeedReport, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    expected_user_totals: dict[str, int] = {}
    for user in USERS:
        for program_code, (balance, _member_id) in user.accounts.items():
            expected_user_totals[program_code] = expected_user_totals.get(program_code, 0) + balance

    async with session_factory() as session:
        rows = await session.execute(
            select(Program.code, Account.owner_type, Account.account_type, Account.balance).join(
                Program, Program.id == Account.program_id
            )
        )
        user_totals: dict[str, int] = {}
        settlement: dict[str, int] = {}
        clearing: dict[str, int] = {}
        for code, owner_type, account_type, balance in rows.tuples():
            if owner_type == OwnerType.USER:
                user_totals[code] = user_totals.get(code, 0) + balance
            elif account_type == AccountType.PARTNER_SETTLEMENT:
                settlement[code] = balance
            else:
                clearing[code] = balance

    assert user_totals == expected_user_totals
    assert settlement == {code: -total for code, total in expected_user_totals.items()}
    assert set(clearing.values()) == {0}


async def test_seeded_routes_are_directional_and_skyward_to_zenith_is_unsupported(
    seeded: SeedReport, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session:
        ids = dict((await session.execute(select(Program.code, Program.id))).tuples().all())
        routes = {
            (source, destination): (numerator, denominator)
            for source, destination, numerator, denominator in (
                await session.execute(
                    select(
                        ConversionRate.source_program_id,
                        ConversionRate.destination_program_id,
                        ConversionRate.numerator,
                        ConversionRate.denominator,
                    )
                )
            ).tuples()
        }

    # Forward 1:1, reverse 3:1 (worse): separate rows with separate rules.
    assert routes[(ids["NOVA_REWARDS"], ids["SKYWARD_MILES"])] == (1, 1)
    assert routes[(ids["SKYWARD_MILES"], ids["NOVA_REWARDS"])] == (1, 3)
    assert (ids["SKYWARD_MILES"], ids["ZENITH_POINTS"]) not in routes
