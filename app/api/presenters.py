"""Turn domain objects into API response models (shared by several routers)."""

import re

from app.api.schemas import (
    RateSnapshotOut,
    TransferDestinationOut,
    TransferEventOut,
    TransferFailureOut,
    TransferOut,
    TransferSourceOut,
    TransferSummaryOut,
)
from app.domain.errors import DomainError, ErrorCode
from app.services.transfer_queries import TransferDetails

TRANSFER_ID_PATTERN = re.compile(r"tr_[0-9A-HJKMNP-TV-Z]{26}")


def transfer_not_found() -> DomainError:
    return DomainError(ErrorCode.TRANSFER_NOT_FOUND, "No such transfer.")


def transfer_summary_out(details: TransferDetails) -> TransferSummaryOut:
    transfer = details.transfer
    return TransferSummaryOut(
        id=transfer.id,
        status=transfer.status,
        source=TransferSourceOut(program=details.source_program, points=transfer.source_points),
        destination=TransferDestinationOut(
            program=details.destination_program,
            points=transfer.destination_points,
            base_points=transfer.base_points,
            bonus_points=transfer.bonus_points,
        ),
        rate=RateSnapshotOut(**transfer.rate_snapshot),
        failure=(
            TransferFailureOut(code=transfer.failure_code, message=transfer.failure_message)
            if transfer.failure_code
            else None
        ),
        partner_confirmation_id=transfer.partner_confirmation_id,
        created_at=transfer.created_at,
        updated_at=transfer.updated_at,
        completed_at=transfer.completed_at,
    )


def transfer_out(details: TransferDetails) -> TransferOut:
    return TransferOut(
        **transfer_summary_out(details).model_dump(),
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
