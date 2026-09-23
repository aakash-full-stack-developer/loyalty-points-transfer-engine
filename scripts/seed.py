"""Seed programs, rates, bonuses, system accounts and demo users: `python -m scripts.seed`.

Idempotent: every step inserts only what is missing, so running it twice changes nothing.
The whole seed runs in one transaction under an advisory lock, so two concurrent runs can't
interleave.

Opening balances are never written directly. Each is posted through the ledger service as a
balanced SEED journal (CREDIT the user's account, DEBIT the program's PARTNER_SETTLEMENT
account), so the invariant "sum of balances per program == 0" holds from the very first row.

All programs, companies and member ids are fictional.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Account, ConversionRate, Program, TransferBonus, User
from app.db.session import create_engine, create_session_factory, unit_of_work
from app.domain.enums import AccountType, EntryDirection, JournalType, OwnerType, ProgramType
from app.services.ledger import Posting, post_journal

# Arbitrary constant identifying the seed's advisory lock.
_SEED_LOCK_ID = 7_400_001


@dataclass(frozen=True)
class ProgramSeed:
    code: str
    name: str
    type: ProgramType
    partner_code: str


@dataclass(frozen=True)
class RateSeed:
    source: str
    destination: str
    numerator: int  # destination points per source point = numerator / denominator
    denominator: int
    min_source_points: int
    source_increment: int
    max_source_points: int | None


@dataclass(frozen=True)
class BonusSeed:
    source: str
    destination: str
    bonus_bps: int
    duration: timedelta


@dataclass(frozen=True)
class UserSeed:
    user_id: str
    # program code -> (opening balance, external member id)
    accounts: dict[str, tuple[int, str]]


PROGRAMS = (
    ProgramSeed("NOVA_REWARDS", "Nova Rewards", ProgramType.CARD, "NOVA"),
    ProgramSeed("ZENITH_POINTS", "Zenith Points", ProgramType.CARD, "ZENITH"),
    ProgramSeed("SKYWARD_MILES", "Skyward Miles", ProgramType.LOYALTY, "SKYWARD"),
    ProgramSeed("STAYWELL_POINTS", "Staywell Points", ProgramType.LOYALTY, "STAYWELL"),
    ProgramSeed("HARBOR_CRUISE_POINTS", "Harbor Cruise Points", ProgramType.LOYALTY, "HARBOR"),
)

# Reverse routes (loyalty -> card) are deliberately worse than forward routes.
# There is intentionally no SKYWARD_MILES -> ZENITH_POINTS route (unsupported-route demo).
RATES = (
    RateSeed("NOVA_REWARDS", "SKYWARD_MILES", 1, 1, 1_000, 1_000, 500_000),
    RateSeed("NOVA_REWARDS", "STAYWELL_POINTS", 2, 1, 1_000, 1_000, 500_000),
    RateSeed("ZENITH_POINTS", "SKYWARD_MILES", 1, 1, 1_000, 1_000, 500_000),
    # 2:3 makes floor rounding visible: 5,000 -> 3,333 (not 3,333.33).
    RateSeed("ZENITH_POINTS", "HARBOR_CRUISE_POINTS", 2, 3, 5_000, 1_000, 300_000),
    RateSeed("SKYWARD_MILES", "NOVA_REWARDS", 1, 3, 3_000, 3_000, 300_000),
    RateSeed("STAYWELL_POINTS", "NOVA_REWARDS", 1, 5, 5_000, 5_000, 500_000),
)

BONUSES = (BonusSeed("NOVA_REWARDS", "SKYWARD_MILES", 2_500, timedelta(days=30)),)

USERS = (
    UserSeed(
        "user_alice",
        {
            "NOVA_REWARDS": (250_000, "NOVA-4410-7730-0001"),
            "ZENITH_POINTS": (120_000, "ZEN-5521-0001"),
            "SKYWARD_MILES": (45_000, "SKY100200301"),
            "STAYWELL_POINTS": (80_000, "SW-8812-0001"),
            "HARBOR_CRUISE_POINTS": (20_000, "HCP-3001-0001"),
        },
    ),
    UserSeed(
        "user_bob",
        {
            "NOVA_REWARDS": (50_000, "NOVA-4410-7730-0002"),
            "ZENITH_POINTS": (300_000, "ZEN-5521-0002"),
            "SKYWARD_MILES": (150_000, "SKY100200302"),
            "STAYWELL_POINTS": (10_000, "SW-8812-0002"),
            "HARBOR_CRUISE_POINTS": (60_000, "HCP-3001-0002"),
        },
    ),
)


@dataclass
class SeedReport:
    created: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys(
            ("programs", "system_accounts", "rates", "bonuses", "users", "user_accounts"), 0
        )
    )

    def summary(self) -> str:
        parts = ", ".join(f"{name}={count}" for name, count in self.created.items())
        return f"seed complete, created: {parts}"


async def seed(session: AsyncSession, now: datetime | None = None) -> SeedReport:
    """Insert all missing seed data using the caller's transaction."""
    now = now or datetime.now(UTC)
    report = SeedReport()
    await session.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": _SEED_LOCK_ID})

    program_ids = await _seed_programs(session, report)
    settlement_ids = await _seed_system_accounts(session, program_ids, report)
    await _seed_rates(session, program_ids, now, report)
    await _seed_bonuses(session, program_ids, now, report)
    await _seed_users(session, program_ids, settlement_ids, report)
    return report


async def _seed_programs(session: AsyncSession, report: SeedReport) -> dict[str, int]:
    for program in PROGRAMS:
        result = await session.execute(
            insert(Program)
            .values(
                code=program.code,
                name=program.name,
                type=program.type,
                partner_code=program.partner_code,
            )
            .on_conflict_do_nothing(index_elements=["code"])
            .returning(Program.id)
        )
        if result.scalar_one_or_none() is not None:
            report.created["programs"] += 1

    rows = await session.execute(select(Program.code, Program.id))
    return dict(rows.tuples().all())


async def _seed_system_accounts(
    session: AsyncSession, program_ids: dict[str, int], report: SeedReport
) -> dict[str, int]:
    """Create TRANSFER_CLEARING and PARTNER_SETTLEMENT for every program.
    Returns program code -> PARTNER_SETTLEMENT account id."""
    for program_id in program_ids.values():
        for account_type in (AccountType.TRANSFER_CLEARING, AccountType.PARTNER_SETTLEMENT):
            result = await session.execute(
                insert(Account)
                .values(
                    owner_type=OwnerType.SYSTEM, program_id=program_id, account_type=account_type
                )
                .on_conflict_do_nothing(
                    index_elements=["program_id", "account_type"],
                    index_where=text("owner_type = 'SYSTEM'"),
                )
                .returning(Account.id)
            )
            if result.scalar_one_or_none() is not None:
                report.created["system_accounts"] += 1

    rows = await session.execute(
        select(Account.program_id, Account.id).where(
            Account.account_type == AccountType.PARTNER_SETTLEMENT
        )
    )
    settlement_by_program_id = dict(rows.tuples().all())
    return {code: settlement_by_program_id[pid] for code, pid in program_ids.items()}


async def _seed_rates(
    session: AsyncSession, program_ids: dict[str, int], now: datetime, report: SeedReport
) -> None:
    """Create version 1 for routes that have no rate history. Routes changed later through
    the admin API are left alone."""
    existing_routes = set(
        (
            await session.execute(
                select(ConversionRate.source_program_id, ConversionRate.destination_program_id)
            )
        )
        .tuples()
        .all()
    )
    for rate in RATES:
        route = (program_ids[rate.source], program_ids[rate.destination])
        if route in existing_routes:
            continue
        session.add(
            ConversionRate(
                source_program_id=route[0],
                destination_program_id=route[1],
                numerator=rate.numerator,
                denominator=rate.denominator,
                min_source_points=rate.min_source_points,
                source_increment=rate.source_increment,
                max_source_points=rate.max_source_points,
                version=1,
                effective_from=now,
            )
        )
        report.created["rates"] += 1
    await session.flush()


async def _seed_bonuses(
    session: AsyncSession, program_ids: dict[str, int], now: datetime, report: SeedReport
) -> None:
    """Create a bonus for routes that have never had one."""
    existing_routes = set(
        (
            await session.execute(
                select(TransferBonus.source_program_id, TransferBonus.destination_program_id)
            )
        )
        .tuples()
        .all()
    )
    for bonus in BONUSES:
        route = (program_ids[bonus.source], program_ids[bonus.destination])
        if route in existing_routes:
            continue
        session.add(
            TransferBonus(
                source_program_id=route[0],
                destination_program_id=route[1],
                bonus_bps=bonus.bonus_bps,
                starts_at=now,
                ends_at=now + bonus.duration,
            )
        )
        report.created["bonuses"] += 1
    await session.flush()


async def _seed_users(
    session: AsyncSession,
    program_ids: dict[str, int],
    settlement_ids: dict[str, int],
    report: SeedReport,
) -> None:
    for user in USERS:
        new_user_id = await session.scalar(
            insert(User)
            .values(id=user.user_id)
            .on_conflict_do_nothing(index_elements=["id"])
            .returning(User.id)
        )
        if new_user_id is not None:
            report.created["users"] += 1

        for program_code, (opening_balance, member_id) in user.accounts.items():
            program_id = program_ids[program_code]
            account_id = await session.scalar(
                insert(Account)
                .values(
                    owner_type=OwnerType.USER,
                    user_id=user.user_id,
                    program_id=program_id,
                    account_type=AccountType.USER_BALANCE,
                    external_member_id=member_id,
                )
                .on_conflict_do_nothing(index_elements=["user_id", "program_id"])
                .returning(Account.id)
            )
            if account_id is None:
                continue  # already seeded, together with its opening-balance journal
            report.created["user_accounts"] += 1
            await _post_opening_balance(
                session, program_id, account_id, settlement_ids[program_code], opening_balance
            )


async def _post_opening_balance(
    session: AsyncSession,
    program_id: int,
    user_account_id: int,
    settlement_account_id: int,
    amount: int,
) -> None:
    """SEED journal through the ledger: CREDIT the user, DEBIT the program's settlement."""
    await post_journal(
        session,
        JournalType.SEED,
        [
            Posting(user_account_id, program_id, EntryDirection.CREDIT, amount),
            Posting(settlement_account_id, program_id, EntryDirection.DEBIT, amount),
        ],
    )


async def _main() -> None:
    engine = create_engine(get_settings())
    try:
        async with unit_of_work(create_session_factory(engine)) as session:
            report = await seed(session)
    finally:
        await engine.dispose()
    print(report.summary())


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
