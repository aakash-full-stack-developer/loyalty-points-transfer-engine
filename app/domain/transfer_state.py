"""Transfer state machine.

    PENDING -> SOURCE_DEBITED -> PARTNER_SUBMITTED -> COMPLETED
    SOURCE_DEBITED       -> REVERSED              partner never called (crash before submit)
    PARTNER_SUBMITTED    -> REVERSED              definitive failure (rejected / not sent)
    PARTNER_SUBMITTED    -> PENDING_VERIFICATION  outcome unknown (timeout, 5xx)
    PENDING_VERIFICATION -> COMPLETED | REVERSED | MANUAL_REVIEW

COMPLETED and REVERSED are final: the ledger is settled and nothing will change.
MANUAL_REVIEW is terminal for automation: a human must decide, because the system could not
prove what the partner did.

Every transition is validated against ALLOWED_TRANSITIONS and recorded as an append-only
transfer_events row in the same database transaction as the status change, so the audit
trail can never disagree with the transfer.
"""

from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Transfer, TransferEvent
from app.domain.enums import TransferStatus

S = TransferStatus

ALLOWED_TRANSITIONS: Mapping[TransferStatus, frozenset[TransferStatus]] = {
    S.PENDING: frozenset({S.SOURCE_DEBITED}),
    S.SOURCE_DEBITED: frozenset({S.PARTNER_SUBMITTED, S.REVERSED}),
    S.PARTNER_SUBMITTED: frozenset({S.COMPLETED, S.REVERSED, S.PENDING_VERIFICATION}),
    S.PENDING_VERIFICATION: frozenset({S.COMPLETED, S.REVERSED, S.MANUAL_REVIEW}),
    S.COMPLETED: frozenset(),
    S.REVERSED: frozenset(),
    S.MANUAL_REVIEW: frozenset(),
}

FINAL_STATUSES = frozenset({S.COMPLETED, S.REVERSED})


class TransferFailureCode(StrEnum):
    """Why a transfer was reversed or needs review (stored on the transfer)."""

    PARTNER_REJECTED = "PARTNER_REJECTED"  # partner definitively refused the credit
    PARTNER_UNAVAILABLE = "PARTNER_UNAVAILABLE"  # request provably never reached the partner
    PARTNER_NOT_RECEIVED = "PARTNER_NOT_RECEIVED"  # reconciler: partner has no such credit
    TRANSFER_INTERRUPTED = "TRANSFER_INTERRUPTED"  # crashed before the partner was called
    VERIFICATION_EXHAUSTED = "VERIFICATION_EXHAUSTED"  # reconciler gave up: manual review


class InvalidStateTransitionError(Exception):
    def __init__(self, transfer_id: str, from_status: TransferStatus, to_status: TransferStatus):
        super().__init__(f"transfer {transfer_id}: {from_status} -> {to_status} is not allowed")
        self.transfer_id = transfer_id
        self.from_status = from_status
        self.to_status = to_status


def can_transition(from_status: TransferStatus, to_status: TransferStatus) -> bool:
    return to_status in ALLOWED_TRANSITIONS[from_status]


def record_creation(session: AsyncSession, transfer: Transfer, reason: str) -> None:
    """Audit event for a new transfer (None -> PENDING)."""
    session.add(
        TransferEvent(
            transfer_id=transfer.id,
            from_status=None,
            to_status=transfer.status,
            reason=reason,
            event_metadata={},
        )
    )


def transition(
    session: AsyncSession,
    transfer: Transfer,
    to_status: TransferStatus,
    reason: str,
    metadata: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> None:
    """Move `transfer` to `to_status` and append the audit event, in the caller's transaction.

    Raises InvalidStateTransitionError if the move is not in ALLOWED_TRANSITIONS.
    """
    from_status = TransferStatus(transfer.status)
    if not can_transition(from_status, to_status):
        raise InvalidStateTransitionError(transfer.id, from_status, to_status)

    moment = now or datetime.now(UTC)
    transfer.status = to_status
    transfer.updated_at = moment
    if to_status in FINAL_STATUSES:
        transfer.completed_at = moment
    session.add(
        TransferEvent(
            transfer_id=transfer.id,
            from_status=from_status,
            to_status=to_status,
            reason=reason,
            event_metadata=metadata or {},
        )
    )
