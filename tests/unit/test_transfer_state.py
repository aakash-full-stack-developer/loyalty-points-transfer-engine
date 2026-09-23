from datetime import UTC, datetime
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Transfer, TransferEvent
from app.domain.enums import TransferStatus
from app.domain.transfer_state import (
    ALLOWED_TRANSITIONS,
    FINAL_STATUSES,
    InvalidStateTransitionError,
    can_transition,
    transition,
)

S = TransferStatus
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)

# The brief's state machine, written out independently of the implementation.
EXPECTED = {
    (S.PENDING, S.SOURCE_DEBITED),
    (S.SOURCE_DEBITED, S.PARTNER_SUBMITTED),
    (S.SOURCE_DEBITED, S.REVERSED),
    (S.PARTNER_SUBMITTED, S.COMPLETED),
    (S.PARTNER_SUBMITTED, S.REVERSED),
    (S.PARTNER_SUBMITTED, S.PENDING_VERIFICATION),
    (S.PENDING_VERIFICATION, S.COMPLETED),
    (S.PENDING_VERIFICATION, S.REVERSED),
    (S.PENDING_VERIFICATION, S.MANUAL_REVIEW),
}


class RecordingSession:
    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, obj: Any) -> None:
        self.added.append(obj)


def make_transfer(status: TransferStatus) -> Transfer:
    return Transfer(id="tr_01HZZZZZZZZZZZZZZZZZZZZZZZ", status=status)


@pytest.mark.parametrize("from_status", list(S))
@pytest.mark.parametrize("to_status", list(S))
def test_transition_table_matches_the_brief_exactly(
    from_status: TransferStatus, to_status: TransferStatus
) -> None:
    assert can_transition(from_status, to_status) == ((from_status, to_status) in EXPECTED)


def test_final_statuses_have_no_way_out() -> None:
    for status in (*FINAL_STATUSES, S.MANUAL_REVIEW):
        assert ALLOWED_TRANSITIONS[status] == frozenset()


def test_transition_updates_status_and_appends_an_audit_event() -> None:
    session = RecordingSession()
    transfer = make_transfer(S.PARTNER_SUBMITTED)

    transition(
        cast(AsyncSession, session),
        transfer,
        S.PENDING_VERIFICATION,
        "partner_outcome_unknown",
        {"attempts": 2},
        NOW,
    )

    assert transfer.status == S.PENDING_VERIFICATION
    assert transfer.completed_at is None
    [event] = session.added
    assert isinstance(event, TransferEvent)
    assert (event.from_status, event.to_status, event.reason) == (
        S.PARTNER_SUBMITTED,
        S.PENDING_VERIFICATION,
        "partner_outcome_unknown",
    )
    assert event.event_metadata == {"attempts": 2}


@pytest.mark.parametrize("final", sorted(FINAL_STATUSES))
def test_reaching_a_final_status_sets_completed_at(final: TransferStatus) -> None:
    transfer = make_transfer(S.PARTNER_SUBMITTED)

    transition(cast(AsyncSession, RecordingSession()), transfer, final, "done", now=NOW)

    assert transfer.completed_at == NOW


@pytest.mark.parametrize(
    ("from_status", "to_status"),
    [
        (S.PENDING, S.COMPLETED),  # skipping the debit
        (S.SOURCE_DEBITED, S.COMPLETED),  # settling without asking the partner
        (S.COMPLETED, S.REVERSED),  # reversing a finished transfer
        (S.REVERSED, S.COMPLETED),
        (S.MANUAL_REVIEW, S.COMPLETED),  # automation must not touch manual review
    ],
)
def test_invalid_transitions_raise_and_change_nothing(
    from_status: TransferStatus, to_status: TransferStatus
) -> None:
    session = RecordingSession()
    transfer = make_transfer(from_status)

    with pytest.raises(InvalidStateTransitionError):
        transition(cast(AsyncSession, session), transfer, to_status, "nope", now=NOW)

    assert transfer.status == from_status
    assert session.added == []
