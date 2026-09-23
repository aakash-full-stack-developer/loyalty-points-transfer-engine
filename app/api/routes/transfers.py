"""Transfer endpoints.

POST returns 201 when the transfer reached a final state (COMPLETED or REVERSED; the
`status` field says which) and 202 when it is still being resolved (PENDING_VERIFICATION,
or any other non-final state). Business errors (4xx) mean no transfer was created.

Reading someone else's transfer returns 404, not 403, so ids cannot be probed for existence.
"""

import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import JSONResponse

from app.api.deps import (
    CurrentUserId,
    IdempotencyServiceDep,
    SessionDep,
    SessionFactoryDep,
    TransferServiceDep,
    enforce_transfer_rate_limit,
)
from app.api.idempotency import HandlerResult, IdempotencyKeyHeader, run_idempotent
from app.api.schemas import (
    PROBLEM_RESPONSES,
    ProblemDetails,
    RateSnapshotOut,
    TransferCreateRequest,
    TransferDestinationOut,
    TransferEventOut,
    TransferFailureOut,
    TransferList,
    TransferOut,
    TransferSourceOut,
    TransferSummaryOut,
)
from app.domain.enums import TransferStatus
from app.domain.errors import DomainError, ErrorCode
from app.domain.transfer_state import FINAL_STATUSES
from app.services.idempotency import compute_fingerprint
from app.services.transfer_queries import (
    TransferCursor,
    TransferDetails,
    get_transfer_details,
    list_transfers,
)
from app.services.transfer_service import TransferRequest

router = APIRouter(prefix="/v1/transfers", tags=["transfers"])

_TRANSFER_ID = re.compile(r"tr_[0-9A-HJKMNP-TV-Z]{26}")


def http_status_for(transfer_status: TransferStatus) -> int:
    if transfer_status in FINAL_STATUSES:
        return status.HTTP_201_CREATED
    return status.HTTP_202_ACCEPTED


def _summary_fields(details: TransferDetails) -> dict[str, Any]:
    transfer = details.transfer
    return {
        "id": transfer.id,
        "status": transfer.status,
        "source": TransferSourceOut(program=details.source_program, points=transfer.source_points),
        "destination": TransferDestinationOut(
            program=details.destination_program,
            points=transfer.destination_points,
            base_points=transfer.base_points,
            bonus_points=transfer.bonus_points,
        ),
        "rate": RateSnapshotOut(**transfer.rate_snapshot),
        "failure": (
            TransferFailureOut(code=transfer.failure_code, message=transfer.failure_message)
            if transfer.failure_code
            else None
        ),
        "partner_confirmation_id": transfer.partner_confirmation_id,
        "created_at": transfer.created_at,
        "updated_at": transfer.updated_at,
        "completed_at": transfer.completed_at,
    }


def transfer_out(details: TransferDetails) -> TransferOut:
    return TransferOut(
        **_summary_fields(details),
        events=[
            TransferEventOut(
                from_status=event.from_status,
                to_status=event.to_status,
                reason=event.reason,
                metadata=event.event_metadata,
                created_at=event.created_at,
            )
            for event in details.events
        ],
    )


def _not_found() -> DomainError:
    return DomainError(ErrorCode.TRANSFER_NOT_FOUND, "No such transfer.")


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=TransferOut,
    summary="Transfer points between programs (Idempotency-Key required)",
    dependencies=[Depends(enforce_transfer_rate_limit)],
    responses={
        202: {"model": TransferOut, "description": "Accepted; partner outcome being verified"},
        400: {"model": ProblemDetails, "description": "Missing or invalid Idempotency-Key"},
        409: {"model": ProblemDetails, "description": "Same key still in progress"},
        429: {"model": ProblemDetails, "description": "Rate limited (see Retry-After)"},
        **PROBLEM_RESPONSES,
    },
)
async def create_transfer(
    request: Request,
    body: TransferCreateRequest,
    user_id: CurrentUserId,
    idempotency_key: IdempotencyKeyHeader,
    idempotency: IdempotencyServiceDep,
    transfers: TransferServiceDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def handler() -> HandlerResult:
        transfer_id = await transfers.create_transfer(
            user_id,
            TransferRequest(body.source_program, body.destination_program, body.source_points),
            idempotency_key,
        )
        async with session_factory() as session:
            details = await get_transfer_details(session, transfer_id, user_id)
        if details is None:
            raise RuntimeError(f"transfer {transfer_id} vanished after creation")
        return HandlerResult(
            http_status_for(details.transfer.status),
            transfer_out(details).model_dump(mode="json"),
            resource_id=transfer_id,
        )

    return await run_idempotent(
        idempotency,
        request,
        user_id=user_id,
        key=idempotency_key,
        fingerprint=compute_fingerprint("POST", request.url.path, body.model_dump(mode="json")),
        handler=handler,
    )


@router.get(
    "/{transfer_id}",
    summary="A transfer with its full status timeline",
    responses={404: {"model": ProblemDetails, "description": "Unknown or not yours"}},
)
async def get_transfer(
    transfer_id: str, user_id: CurrentUserId, session: SessionDep
) -> TransferOut:
    if not _TRANSFER_ID.fullmatch(transfer_id):
        raise _not_found()
    details = await get_transfer_details(session, transfer_id, user_id)
    if details is None:
        raise _not_found()
    return transfer_out(details)


@router.get(
    "",
    summary="The current user's transfers, newest first (cursor pagination)",
    responses={400: {"model": ProblemDetails, "description": "Invalid cursor"}},
)
async def get_transfers(
    user_id: CurrentUserId,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
) -> TransferList:
    decoded = TransferCursor.decode(cursor) if cursor else None
    page, next_cursor = await list_transfers(session, user_id, limit, decoded)
    return TransferList(
        data=[TransferSummaryOut(**_summary_fields(details)) for details in page],
        next_cursor=next_cursor,
    )
