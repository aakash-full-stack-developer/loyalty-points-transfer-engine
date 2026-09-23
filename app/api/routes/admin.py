"""Admin endpoints (X-Admin-Key required): rate versions, bonuses, on-demand reconciliation."""

from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import (
    RateAdminServiceDep,
    ReconciliationServiceDep,
    SessionFactoryDep,
    require_admin,
)
from app.api.presenters import TRANSFER_ID_PATTERN, transfer_not_found, transfer_out
from app.api.schemas import (
    PROBLEM_RESPONSES,
    BonusCreateRequest,
    BonusOut,
    ProblemDetails,
    ProgramCode,
    RateCreateRequest,
    RateList,
    RateOut,
    ReconcileOut,
)
from app.services.rate_admin import NewBonus, NewRate, RateVersion
from app.services.rate_engine import utc_now
from app.services.transfer_queries import get_transfer_details

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


@router.post(
    "/transfers/{transfer_id}/reconcile",
    summary="Reconcile one transfer now (asks the partner if the outcome is unknown)",
    responses={404: {"model": ProblemDetails, "description": "Unknown transfer"}},
)
async def reconcile_transfer(
    transfer_id: str,
    reconciliation: ReconciliationServiceDep,
    session_factory: SessionFactoryDep,
) -> ReconcileOut:
    """Skips the wait for next_verification_at, but never the safety windows: a transfer
    that a live request may still own is reported as NOT_DUE and left alone."""
    if not TRANSFER_ID_PATTERN.fullmatch(transfer_id):
        raise transfer_not_found()
    try:
        result = await reconciliation.reconcile(transfer_id)
    except LookupError as exc:
        raise transfer_not_found() from exc
    async with session_factory() as session:
        details = await get_transfer_details(session, transfer_id, user_id=None)
    if details is None:
        raise transfer_not_found()
    return ReconcileOut(result=result, transfer=transfer_out(details))
