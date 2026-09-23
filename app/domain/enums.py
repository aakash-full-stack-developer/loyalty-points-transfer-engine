"""Domain enumerations.

These are the single source of truth for the allowed values stored in the database: the
CHECK constraints in `app/db/models.py` are generated from them, so code and schema can't
disagree.
"""

from enum import StrEnum


class ProgramType(StrEnum):
    CARD = "CARD"
    LOYALTY = "LOYALTY"


class OwnerType(StrEnum):
    USER = "USER"
    SYSTEM = "SYSTEM"


class AccountType(StrEnum):
    USER_BALANCE = "USER_BALANCE"
    # Holds debited source points while the partner credit is in flight.
    TRANSFER_CLEARING = "TRANSFER_CLEARING"
    # Our position with the partner that operates the program (may be negative).
    PARTNER_SETTLEMENT = "PARTNER_SETTLEMENT"


class EntryDirection(StrEnum):
    DEBIT = "DEBIT"
    CREDIT = "CREDIT"


class JournalType(StrEnum):
    SEED = "SEED"
    TRANSFER_DEBIT = "TRANSFER_DEBIT"
    TRANSFER_SETTLE = "TRANSFER_SETTLE"
    TRANSFER_REVERSAL = "TRANSFER_REVERSAL"


class TransferStatus(StrEnum):
    PENDING = "PENDING"
    SOURCE_DEBITED = "SOURCE_DEBITED"
    PARTNER_SUBMITTED = "PARTNER_SUBMITTED"
    PENDING_VERIFICATION = "PENDING_VERIFICATION"
    COMPLETED = "COMPLETED"
    REVERSED = "REVERSED"
    MANUAL_REVIEW = "MANUAL_REVIEW"


# Statuses the reconciler may need to pick up (a crash can leave a transfer in any of them).
RECONCILABLE_STATUSES: tuple[TransferStatus, ...] = (
    TransferStatus.SOURCE_DEBITED,
    TransferStatus.PARTNER_SUBMITTED,
    TransferStatus.PENDING_VERIFICATION,
)


class IdempotencyStatus(StrEnum):
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
