"""Admin endpoints (X-Admin-Key required): rate versions and bonuses."""

from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import RateAdminServiceDep, require_admin
from app.api.schemas import (
    PROBLEM_RESPONSES,
    BonusCreateRequest,
    BonusOut,
    ProblemDetails,
    ProgramCode,
    RateCreateRequest,
    RateList,
    RateOut,
)
from app.services.rate_admin import NewBonus, NewRate, RateVersion
from app.services.rate_engine import utc_now

router = APIRouter(
    prefix="/v1/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
    responses={401: {"model": ProblemDetails, "description": "Missing or invalid admin key"}},
)


def _rate_out(rate: RateVersion) -> RateOut:
    return RateOut(**asdict(rate), is_current=rate.is_current)


@router.get("/rates", summary="List current and historical rate versions")
async def list_rates(
    admin: RateAdminServiceDep,
    source_program: Annotated[ProgramCode | None, Query()] = None,
    destination_program: Annotated[ProgramCode | None, Query()] = None,
) -> RateList:
    rates = await admin.list_rates(source_program, destination_program)
    return RateList(data=[_rate_out(rate) for rate in rates])


@router.post(
    "/rates",
    status_code=status.HTTP_201_CREATED,
    summary="Create a new rate version for a route (closes the current version)",
    responses=PROBLEM_RESPONSES,
)
async def create_rate(body: RateCreateRequest, admin: RateAdminServiceDep) -> RateOut:
    rate = await admin.create_rate_version(NewRate(**body.model_dump()))
    return _rate_out(rate)


@router.post(
    "/bonuses",
    status_code=status.HTTP_201_CREATED,
    summary="Create a time-bound transfer bonus for a route",
    responses={
        **PROBLEM_RESPONSES,
        409: {"model": ProblemDetails, "description": "Overlaps an existing bonus"},
    },
)
async def create_bonus(body: BonusCreateRequest, admin: RateAdminServiceDep) -> BonusOut:
    bonus = await admin.create_bonus(NewBonus(**body.model_dump()), now=utc_now())
    return BonusOut(**asdict(bonus))
