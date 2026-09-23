from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain.errors import DomainError, ErrorCode
from app.services.rate_engine import (
    BonusRule,
    ProgramRef,
    QuoteService,
    RateRule,
    RouteConfig,
    calculate_conversion,
    ensure_different_programs,
    ensure_route_usable,
)

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def make_rate(
    numerator: int = 1,
    denominator: int = 1,
    min_source_points: int = 1_000,
    source_increment: int = 1_000,
    max_source_points: int | None = 500_000,
) -> RateRule:
    return RateRule(
        rate_id=7,
        version=3,
        numerator=numerator,
        denominator=denominator,
        min_source_points=min_source_points,
        source_increment=source_increment,
        max_source_points=max_source_points,
        effective_to=None,
    )


def make_bonus(
    bps: int = 2_500,
    starts_at: datetime = NOW - timedelta(days=1),
    ends_at: datetime = NOW + timedelta(days=1),
) -> BonusRule:
    return BonusRule(bonus_id=11, bonus_bps=bps, starts_at=starts_at, ends_at=ends_at)


@pytest.mark.parametrize(
    ("rate", "bonus", "source_points", "expected"),
    [
        pytest.param(make_rate(1, 1), None, 10_000, (10_000, 0, 10_000), id="one_to_one"),
        pytest.param(make_rate(2, 1), None, 10_000, (20_000, 0, 20_000), id="one_to_two"),
        pytest.param(
            make_rate(1, 3, min_source_points=3_000, source_increment=3_000),
            None,
            9_000,
            (3_000, 0, 3_000),
            id="three_to_one_reverse",
        ),
        pytest.param(make_rate(2, 3), None, 1_000, (666, 0, 666), id="uneven_ratio_rounds_down"),
        pytest.param(make_rate(2, 3), None, 5_000, (3_333, 0, 3_333), id="uneven_ratio_5000"),
        pytest.param(
            make_rate(1, 1), make_bonus(), 10_000, (10_000, 2_500, 12_500), id="bonus_on_base"
        ),
        pytest.param(make_rate(2, 3), make_bonus(), 1_000, (666, 166, 832), id="bonus_rounds_down"),
        pytest.param(
            make_rate(1, 1),
            make_bonus(ends_at=NOW),
            10_000,
            (10_000, 0, 10_000),
            id="bonus_expired_end_is_exclusive",
        ),
        pytest.param(
            make_rate(1, 1),
            make_bonus(starts_at=NOW + timedelta(seconds=1)),
            10_000,
            (10_000, 0, 10_000),
            id="bonus_not_started",
        ),
    ],
)
def test_conversion_amounts(
    rate: RateRule, bonus: BonusRule | None, source_points: int, expected: tuple[int, int, int]
) -> None:
    conversion = calculate_conversion(rate, source_points, bonus, at=NOW)

    assert (
        conversion.base_points,
        conversion.bonus_points,
        conversion.destination_points,
    ) == expected


def test_snapshot_records_rate_terms_and_applied_bonus() -> None:
    conversion = calculate_conversion(make_rate(2, 3), 3_000, make_bonus(), at=NOW)

    assert conversion.snapshot.to_dict() == {
        "rate_id": 7,
        "version": 3,
        "numerator": 2,
        "denominator": 3,
        "bonus_id": 11,
        "bonus_bps": 2_500,
    }


def test_snapshot_has_no_bonus_when_bonus_is_inactive() -> None:
    conversion = calculate_conversion(make_rate(), 1_000, make_bonus(ends_at=NOW), at=NOW)

    assert conversion.snapshot.bonus_id is None
    assert conversion.snapshot.bonus_bps is None


@pytest.mark.parametrize(
    ("rate", "source_points", "code"),
    [
        pytest.param(make_rate(), 999, ErrorCode.BELOW_MINIMUM, id="below_minimum"),
        pytest.param(make_rate(), 501_000, ErrorCode.ABOVE_MAXIMUM, id="above_maximum"),
        pytest.param(make_rate(), 1_500, ErrorCode.INVALID_INCREMENT, id="invalid_increment"),
        pytest.param(
            make_rate(1, 1_000_000, min_source_points=1, source_increment=1),
            999,
            ErrorCode.ZERO_DESTINATION_POINTS,
            id="zero_destination",
        ),
    ],
)
def test_amount_rules_are_enforced(rate: RateRule, source_points: int, code: ErrorCode) -> None:
    with pytest.raises(DomainError) as raised:
        calculate_conversion(rate, source_points, None, at=NOW)

    assert raised.value.code == code


def test_amount_errors_expose_the_limit_for_clients() -> None:
    with pytest.raises(DomainError) as raised:
        calculate_conversion(make_rate(), 999, None, at=NOW)

    assert raised.value.extra == {"min_source_points": 1_000}


def _route(
    rate: RateRule | None = None, source_active: bool = True, destination_active: bool = True
) -> RouteConfig:
    return RouteConfig(
        source=ProgramRef(1, "NOVA_REWARDS", source_active),
        destination=ProgramRef(2, "SKYWARD_MILES", destination_active),
        rate=rate,
        bonus=None,
        valid_until=None,
    )


def test_route_without_rate_is_not_supported() -> None:
    with pytest.raises(DomainError) as raised:
        ensure_route_usable(_route(rate=None))

    assert raised.value.code == ErrorCode.ROUTE_NOT_SUPPORTED


@pytest.mark.parametrize(("source_active", "destination_active"), [(False, True), (True, False)])
def test_inactive_program_is_rejected(source_active: bool, destination_active: bool) -> None:
    with pytest.raises(DomainError) as raised:
        ensure_route_usable(_route(make_rate(), source_active, destination_active))

    assert raised.value.code == ErrorCode.PROGRAM_INACTIVE


def test_same_program_is_rejected() -> None:
    with pytest.raises(DomainError) as raised:
        ensure_different_programs("NOVA_REWARDS", "NOVA_REWARDS")

    assert raised.value.code == ErrorCode.SAME_PROGRAM


class _FixedRoutes:
    def __init__(self, route: RouteConfig) -> None:
        self.route = route
        self.calls = 0

    async def get_route(self, source_code: str, destination_code: str, at: datetime) -> RouteConfig:
        self.calls += 1
        return self.route


async def test_quote_service_prices_route_and_sets_expiry() -> None:
    service = QuoteService(_FixedRoutes(_route(make_rate())), timedelta(seconds=60), lambda: NOW)

    quote = await service.quote("NOVA_REWARDS", "SKYWARD_MILES", 5_000)

    assert quote.conversion.destination_points == 5_000
    assert quote.expires_at == NOW + timedelta(seconds=60)


async def test_quote_service_rejects_same_program_before_any_lookup() -> None:
    routes = _FixedRoutes(_route(make_rate()))
    service = QuoteService(routes, timedelta(seconds=60), lambda: NOW)

    with pytest.raises(DomainError):
        await service.quote("NOVA_REWARDS", "NOVA_REWARDS", 5_000)
    assert routes.calls == 0


@given(
    numerator=st.integers(min_value=1, max_value=1_000),
    denominator=st.integers(min_value=1, max_value=1_000),
    bonus_bps=st.integers(min_value=0, max_value=10_000),
    source_points=st.integers(min_value=1, max_value=10**9),
)
def test_destination_is_never_negative_and_never_exceeds_exact_value(
    numerator: int, denominator: int, bonus_bps: int, source_points: int
) -> None:
    rate = make_rate(
        numerator, denominator, min_source_points=1, source_increment=1, max_source_points=None
    )
    bonus = make_bonus(bps=bonus_bps) if bonus_bps else None
    if source_points * numerator < denominator:
        # Less than one whole destination point: rejected rather than rounded to zero.
        with pytest.raises(DomainError, match="zero destination points"):
            calculate_conversion(rate, source_points, bonus, at=NOW)
        return

    conversion = calculate_conversion(rate, source_points, bonus, at=NOW)
    exact_scaled = source_points * numerator * (10_000 + bonus_bps)  # x denominator x 10_000
    assert conversion.destination_points > 0
    # Upper bound: rounding never creates points.
    assert conversion.destination_points * denominator * 10_000 <= exact_scaled
    # Lower bound: base loses less than one point to rounding.
    assert conversion.base_points * denominator > source_points * numerator - denominator
    assert conversion.destination_points == conversion.base_points + conversion.bonus_points
