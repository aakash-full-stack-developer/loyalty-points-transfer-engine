"""Loads route pricing configuration (programs, active rate, active bonus) from PostgreSQL."""

from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ConversionRate, Program, TransferBonus
from app.domain.errors import DomainError, ErrorCode
from app.services.rate_engine import BonusRule, ProgramRef, RateRule, RouteConfig


async def load_programs(session: AsyncSession, *codes: str) -> dict[str, ProgramRef]:
    """Return the requested programs by code; raise PROGRAM_NOT_FOUND for any unknown code."""
    rows = await session.execute(
        select(Program.id, Program.code, Program.active).where(Program.code.in_(codes))
    )
    programs = {code: ProgramRef(id=pid, code=code, active=active) for pid, code, active in rows}
    for code in codes:
        if code not in programs:
            raise DomainError(
                ErrorCode.PROGRAM_NOT_FOUND, f"Program {code} does not exist.", program=code
            )
    return programs


def to_rate_rule(row: ConversionRate) -> RateRule:
    return RateRule(
        rate_id=row.id,
        version=row.version,
        numerator=row.numerator,
        denominator=row.denominator,
        min_source_points=row.min_source_points,
        source_increment=row.source_increment,
        max_source_points=row.max_source_points,
        effective_to=row.effective_to,
    )


class RateRepository:
    """PostgreSQL implementation of RouteProvider (the source of truth)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_route(self, source_code: str, destination_code: str, at: datetime) -> RouteConfig:
        programs = await load_programs(self._session, source_code, destination_code)
        source, destination = programs[source_code], programs[destination_code]
        route_filter = (
            ConversionRate.source_program_id == source.id,
            ConversionRate.destination_program_id == destination.id,
        )

        rate_row = await self._session.scalar(
            select(ConversionRate)
            .where(
                *route_filter,
                ConversionRate.active,
                ConversionRate.effective_from <= at,
                or_(ConversionRate.effective_to.is_(None), ConversionRate.effective_to > at),
            )
            .order_by(ConversionRate.effective_from.desc())
            .limit(1)
        )

        bonus_filter = (
            TransferBonus.source_program_id == source.id,
            TransferBonus.destination_program_id == destination.id,
        )
        # The exclusion constraint guarantees at most one bonus is active at any moment.
        bonus_row = await self._session.scalar(
            select(TransferBonus)
            .where(*bonus_filter, TransferBonus.starts_at <= at, TransferBonus.ends_at > at)
            .limit(1)
        )
        next_bonus_start = await self._session.scalar(
            select(func.min(TransferBonus.starts_at)).where(
                *bonus_filter, TransferBonus.starts_at > at
            )
        )

        rate = to_rate_rule(rate_row) if rate_row else None
        bonus = (
            BonusRule(
                bonus_id=bonus_row.id,
                bonus_bps=bonus_row.bonus_bps,
                starts_at=bonus_row.starts_at,
                ends_at=bonus_row.ends_at,
            )
            if bonus_row
            else None
        )
        boundaries = [
            rate.effective_to if rate else None,
            bonus.ends_at if bonus else None,
            next_bonus_start,
        ]
        known = [moment for moment in boundaries if moment is not None]
        return RouteConfig(
            source=source,
            destination=destination,
            rate=rate,
            bonus=bonus,
            valid_until=min(known) if known else None,
        )
