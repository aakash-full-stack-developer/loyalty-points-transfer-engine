"""Rate administration: versioned rates and time-bound bonuses.

Rates are never updated in place. Creating a rate for a route closes the current version
(effective_to = now) and inserts version + 1 (effective_from = the same instant), in one
transaction. The two versions are contiguous, so at any moment exactly one applies, and
every transfer's rate_id keeps pointing at the unchanged terms it was priced with.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Integer, cast, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from app.db.errors import violated_constraint
from app.db.models import ConversionRate, Program, TransferBonus
from app.db.session import unit_of_work
from app.domain.errors import DomainError, ErrorCode
from app.services.rate_engine import ensure_different_programs
from app.services.rate_repository import load_programs

CacheInvalidator = Callable[[str, str], Awaitable[None]]


@dataclass(frozen=True)
class NewRate:
    source_program: str
    destination_program: str
    numerator: int
    denominator: int
    min_source_points: int
    source_increment: int
    max_source_points: int | None


@dataclass(frozen=True)
class NewBonus:
    source_program: str
    destination_program: str
    bonus_bps: int
    starts_at: datetime
    ends_at: datetime


@dataclass(frozen=True)
class RateVersion:
    id: int
    source_program: str
    destination_program: str
    numerator: int
    denominator: int
    min_source_points: int
    source_increment: int
    max_source_points: int | None
    version: int
    effective_from: datetime
    effective_to: datetime | None
    active: bool

    @property
    def is_current(self) -> bool:
        return self.active and self.effective_to is None


@dataclass(frozen=True)
class BonusView:
    id: int
    source_program: str
    destination_program: str
    bonus_bps: int
    starts_at: datetime
    ends_at: datetime


def _validate_rate(rate: NewRate) -> None:
    ensure_different_programs(rate.source_program, rate.destination_program)
    if rate.max_source_points is not None and rate.max_source_points < rate.min_source_points:
        raise DomainError(ErrorCode.INVALID_RATE, "max_source_points must be >= min_source_points.")
    if rate.min_source_points % rate.source_increment != 0:
        # Otherwise the minimum itself would be rejected as an invalid increment.
        raise DomainError(
            ErrorCode.INVALID_RATE, "min_source_points must be a multiple of source_increment."
        )


class RateAdminService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        invalidate_cache: CacheInvalidator,
    ) -> None:
        self._session_factory = session_factory
        self._invalidate_cache = invalidate_cache

    async def list_rates(
        self, source_program: str | None = None, destination_program: str | None = None
    ) -> list[RateVersion]:
        source = aliased(Program)
        destination = aliased(Program)
        query = (
            select(ConversionRate, source.code, destination.code)
            .join(source, source.id == ConversionRate.source_program_id)
            .join(destination, destination.id == ConversionRate.destination_program_id)
            .order_by(source.code, destination.code, ConversionRate.version.desc())
        )
        if source_program:
            query = query.where(source.code == source_program)
        if destination_program:
            query = query.where(destination.code == destination_program)

        async with self._session_factory() as session:
            rows = (await session.execute(query)).tuples().all()
        return [_to_rate_version(row, src, dst) for row, src, dst in rows]

    async def create_rate_version(self, new_rate: NewRate) -> RateVersion:
        _validate_rate(new_rate)
        async with unit_of_work(self._session_factory) as session:
            programs = await load_programs(
                session, new_rate.source_program, new_rate.destination_program
            )
            source_id = programs[new_rate.source_program].id
            destination_id = programs[new_rate.destination_program].id

            # Serialize version changes per route. Two admins changing the same route at
            # once both succeed, one after the other, with consecutive version numbers.
            await session.execute(
                select(
                    func.pg_advisory_xact_lock(
                        cast(source_id, Integer), cast(destination_id, Integer)
                    )
                )
            )
            # clock_timestamp(), not now(): now() is the transaction *start* time, which can be
            # earlier than a version committed while this transaction waited for the lock.
            changed_at = await session.scalar(select(func.clock_timestamp()))
            if changed_at is None:
                raise RuntimeError("clock_timestamp() returned NULL")

            route = (
                ConversionRate.source_program_id == source_id,
                ConversionRate.destination_program_id == destination_id,
            )
            current = await session.scalar(
                select(ConversionRate).where(
                    *route, ConversionRate.effective_to.is_(None), ConversionRate.active
                )
            )
            latest_version = await session.scalar(
                select(func.coalesce(func.max(ConversionRate.version), 0)).where(*route)
            )
            if current is not None:
                current.effective_to = changed_at
                await session.flush()  # close before insert: one open version per route

            created = ConversionRate(
                source_program_id=source_id,
                destination_program_id=destination_id,
                numerator=new_rate.numerator,
                denominator=new_rate.denominator,
                min_source_points=new_rate.min_source_points,
                source_increment=new_rate.source_increment,
                max_source_points=new_rate.max_source_points,
                version=(latest_version or 0) + 1,
                effective_from=changed_at,
            )
            session.add(created)
            await session.flush()
            await session.refresh(created)

        await self._invalidate_cache(new_rate.source_program, new_rate.destination_program)
        return _to_rate_version(created, new_rate.source_program, new_rate.destination_program)

    async def create_bonus(self, new_bonus: NewBonus, now: datetime) -> BonusView:
        ensure_different_programs(new_bonus.source_program, new_bonus.destination_program)
        if new_bonus.ends_at <= new_bonus.starts_at:
            raise DomainError(ErrorCode.INVALID_BONUS, "ends_at must be after starts_at.")
        if new_bonus.ends_at <= now:
            raise DomainError(ErrorCode.INVALID_BONUS, "ends_at must be in the future.")

        async with unit_of_work(self._session_factory) as session:
            programs = await load_programs(
                session, new_bonus.source_program, new_bonus.destination_program
            )
            source_id = programs[new_bonus.source_program].id
            destination_id = programs[new_bonus.destination_program].id
            has_current_rate = await session.scalar(
                select(ConversionRate.id).where(
                    ConversionRate.source_program_id == source_id,
                    ConversionRate.destination_program_id == destination_id,
                    ConversionRate.effective_to.is_(None),
                    ConversionRate.active,
                )
            )
            if has_current_rate is None:
                raise DomainError(
                    ErrorCode.ROUTE_NOT_SUPPORTED,
                    "A bonus needs a supported route; create a rate for it first.",
                )

            bonus = TransferBonus(
                source_program_id=source_id,
                destination_program_id=destination_id,
                bonus_bps=new_bonus.bonus_bps,
                starts_at=new_bonus.starts_at,
                ends_at=new_bonus.ends_at,
            )
            session.add(bonus)
            try:
                await session.flush()
            except IntegrityError as exc:
                if violated_constraint(exc) == "ex_transfer_bonuses_no_overlap":
                    raise DomainError(
                        ErrorCode.BONUS_OVERLAP,
                        "Another bonus on this route overlaps the requested period.",
                    ) from exc
                raise

        await self._invalidate_cache(new_bonus.source_program, new_bonus.destination_program)
        return BonusView(
            id=bonus.id,
            source_program=new_bonus.source_program,
            destination_program=new_bonus.destination_program,
            bonus_bps=bonus.bonus_bps,
            starts_at=bonus.starts_at,
            ends_at=bonus.ends_at,
        )


def _to_rate_version(row: ConversionRate, source_code: str, destination_code: str) -> RateVersion:
    return RateVersion(
        id=row.id,
        source_program=source_code,
        destination_program=destination_code,
        numerator=row.numerator,
        denominator=row.denominator,
        min_source_points=row.min_source_points,
        source_increment=row.source_increment,
        max_source_points=row.max_source_points,
        version=row.version,
        effective_from=row.effective_from,
        effective_to=row.effective_to,
        active=row.active,
    )
