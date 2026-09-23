"""The partner adapter interface and its normalized result types.

Business logic (the transfer saga, the reconciler) depends only on this module, never on
httpx or on a particular partner's API. Every partner outcome is classified into one of
four cases, because each one requires a different action from the saga:

    SUCCESS    the partner confirmed the credit              -> settle
    REJECTED   the partner definitively refused it           -> reverse (points returned)
    NOT_SENT   the request provably never reached the partner -> reverse (safe: nothing applied)
    UNKNOWN    the request may or may not have been applied  -> verify later, never guess

The crucial distinction is NOT_SENT vs UNKNOWN. A timeout after the request was sent is not
a failure: the partner may have applied the credit and only the response was lost.
Reversing then would give the user both the source points back and the destination points.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum


class CreditOutcome(StrEnum):
    SUCCESS = "SUCCESS"
    REJECTED = "REJECTED"
    NOT_SENT = "NOT_SENT"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class PartnerCreditResult:
    outcome: CreditOutcome
    reason: str
    retryable: bool
    confirmation_id: str | None = None
    http_status: int | None = None
    attempts: int = 1


class CreditStatus(StrEnum):
    COMPLETED = "COMPLETED"  # the partner has the credit
    NOT_FOUND = "NOT_FOUND"  # the partner has no record of the reference
    UNKNOWN = "UNKNOWN"  # could not find out (partner unreachable, 5xx, ...)


@dataclass(frozen=True)
class PartnerStatusResult:
    status: CreditStatus
    reason: str
    retryable: bool
    confirmation_id: str | None = None
    http_status: int | None = None
    attempts: int = 1


class PartnerAdapter(ABC):
    """Credits points at a partner. Implementations must be idempotent on `reference`:
    sending the same reference twice must never credit twice. That is what makes retries,
    and reconciliation after an unknown outcome, safe."""

    @abstractmethod
    async def credit_points(
        self, partner_code: str, reference: str, member_id: str, points: int
    ) -> PartnerCreditResult: ...

    @abstractmethod
    async def get_credit_status(self, partner_code: str, reference: str) -> PartnerStatusResult: ...
