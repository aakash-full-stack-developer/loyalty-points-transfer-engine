"""Conversion rate engine: value objects, the pure conversion calculation, and quoting.

The calculation is a pure function (no I/O, no clock): given a rate, an optional bonus, an
amount and a point in time, it always returns the same result. That makes the money math
trivial to test exhaustively and impossible to break by a slow database or a stale cache.

Integer math only. For a rate numerator/denominator (destination points per source point):

    base  = floor(source_points * numerator / denominator)
    bonus = floor(base * bonus_bps / 10_000)            if a bonus is active
    destination = base + bonus

Both steps round down, so fractional points are never created: rounding always favours the
platform by less than one point, which is the conservative choice for a currency-like asset.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from app.domain.errors import DomainError, ErrorCode

BPS_DENOMINATOR = 10_000


@dataclass(frozen=True)
class ProgramRef:
    id: int
    code: str
    active: bool


@dataclass(frozen=True)
class RateRule:
    rate_id: int
    version: int
    numerator: int
    denominator: int
    min_source_points: int
    source_increment: int
    max_source_points: int | None
    effective_to: datetime | None


@dataclass(frozen=True)
class BonusRule:
    bonus_id: int
    bonus_bps: int
    starts_at: datetime
    ends_at: datetime

    def applies_at(self, at: datetime) -> bool:
        return self.starts_at <= at < self.ends_at


@dataclass(frozen=True)
class RouteConfig:
    """Everything needed to price a route at a moment in time.

    `valid_until` is the earliest moment this configuration changes by itself (the rate
    version ends, the bonus ends, or a scheduled bonus starts). Caches must not keep it
    longer than that.
    """

    source: ProgramRef
    destination: ProgramRef
    rate: RateRule | None
    bonus: BonusRule | None
    valid_until: datetime | None


@dataclass(frozen=True)
class RateSnapshot:
    """The exact terms a conversion used. Stored on every transfer so history never changes."""

    rate_id: int
    version: int
    numerator: int
    denominator: int
    bonus_id: int | None
    bonus_bps: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "rate_id": self.rate_id,
            "version": self.version,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "bonus_id": self.bonus_id,
            "bonus_bps": self.bonus_bps,
        }


@dataclass(frozen=True)
class Conversion:
    source_points: int
    base_points: int
    bonus_points: int
    destination_points: int
    snapshot: RateSnapshot


@dataclass(frozen=True)
class Quote:
    source_program: str
    destination_program: str
    conversion: Conversion
    quoted_at: datetime
    expires_at: datetime


def calculate_conversion(
    rate: RateRule, source_points: int, bonus: BonusRule | None, *, at: datetime
) -> Conversion:
    """Convert `source_points` under `rate` (and `bonus` if active at `at`). Pure."""
    if source_points < rate.min_source_points:
        raise DomainError(
            ErrorCode.BELOW_MINIMUM,
            f"Minimum transfer for this route is {rate.min_source_points} points.",
            min_source_points=rate.min_source_points,
        )
    if rate.max_source_points is not None and source_points > rate.max_source_points:
        raise DomainError(
            ErrorCode.ABOVE_MAXIMUM,
            f"Maximum transfer for this route is {rate.max_source_points} points.",
            max_source_points=rate.max_source_points,
        )
    if source_points % rate.source_increment != 0:
        raise DomainError(
            ErrorCode.INVALID_INCREMENT,
            f"Points must be a multiple of {rate.source_increment} for this route.",
            source_increment=rate.source_increment,
        )

    base_points = source_points * rate.numerator // rate.denominator
    applied = bonus if bonus is not None and bonus.applies_at(at) else None
    bonus_points = base_points * applied.bonus_bps // BPS_DENOMINATOR if applied else 0
    destination_points = base_points + bonus_points
    if destination_points == 0:
        raise DomainError(
            ErrorCode.ZERO_DESTINATION_POINTS,
            "This amount converts to zero destination points.",
        )

    return Conversion(
        source_points=source_points,
        base_points=base_points,
        bonus_points=bonus_points,
        destination_points=destination_points,
        snapshot=RateSnapshot(
            rate_id=rate.rate_id,
            version=rate.version,
            numerator=rate.numerator,
            denominator=rate.denominator,
            bonus_id=applied.bonus_id if applied else None,
            bonus_bps=applied.bonus_bps if applied else None,
        ),
    )


def ensure_route_usable(route: RouteConfig) -> RateRule:
    """Validate the programs and route; return the rate to use. Pure."""
    for program in (route.source, route.destination):
        if not program.active:
            raise DomainError(
                ErrorCode.PROGRAM_INACTIVE,
                f"Program {program.code} is not accepting transfers.",
                program=program.code,
            )
    if route.rate is None:
        raise DomainError(
            ErrorCode.ROUTE_NOT_SUPPORTED,
            f"Transfers from {route.source.code} to {route.destination.code} are not supported.",
            source_program=route.source.code,
            destination_program=route.destination.code,
        )
    return route.rate


def ensure_different_programs(source_code: str, destination_code: str) -> None:
    if source_code == destination_code:
        raise DomainError(
            ErrorCode.SAME_PROGRAM, "Source and destination programs must be different."
        )


class RouteProvider(Protocol):
    async def get_route(
        self, source_code: str, destination_code: str, at: datetime
    ) -> RouteConfig: ...


def utc_now() -> datetime:
    return datetime.now(UTC)


class QuoteService:
    """Prices a transfer without side effects."""

    def __init__(
        self,
        routes: RouteProvider,
        quote_ttl: timedelta,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._routes = routes
        self._quote_ttl = quote_ttl
        self._clock = clock

    async def quote(self, source_code: str, destination_code: str, source_points: int) -> Quote:
        ensure_different_programs(source_code, destination_code)
        now = self._clock()
        route = await self._routes.get_route(source_code, destination_code, at=now)
        rate = ensure_route_usable(route)
        conversion = calculate_conversion(rate, source_points, route.bonus, at=now)
        return Quote(
            source_program=source_code,
            destination_program=destination_code,
            conversion=conversion,
            quoted_at=now,
            expires_at=now + self._quote_ttl,
        )
