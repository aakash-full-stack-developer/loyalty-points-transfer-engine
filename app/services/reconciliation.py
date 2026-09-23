"""Reconciliation: resolve transfers whose partner outcome is unknown, or whose saga was
interrupted by a crash. This is what makes "automatic rollback" trustworthy: every transfer
eventually reaches COMPLETED, REVERSED, or (if the partner cannot be reached at all)
MANUAL_REVIEW, and points never stay in limbo.

What gets picked up
- PENDING_VERIFICATION whose next_verification_at is due (a timeout / 5xx outcome).
- SOURCE_DEBITED or PARTNER_SUBMITTED older than the stuck window: the request that owned
  it died. The window exceeds the partner deadline, so a live request is never touched.

How each is resolved
- SOURCE_DEBITED: the partner was never called (the call happens only after
  PARTNER_SUBMITTED is committed), so reversing is safe.         -> REVERSED
- Otherwise ask the partner for the credit by reference (outside any transaction):
    COMPLETED  the partner has it                                 -> settle, COMPLETED
    NOT_FOUND  and older than the not-found grace                  -> REVERSED
               (the partner is idempotent by reference and the grace exceeds the maximum
               in-flight time, so no earlier request can still land)
    NOT_FOUND  but still young                                    -> check again, at the
               latest when the grace ends (does not count as a failed attempt)
    UNKNOWN    the partner could not be asked (down, circuit open) -> retry with exponential
               backoff; after max attempts                        -> MANUAL_REVIEW
  MANUAL_REVIEW is deliberate: when the system cannot prove what the partner did, guessing
  either way could lose points or pay twice. A person decides, with the full event trail.

Concurrency
Claiming uses SELECT ... FOR UPDATE SKIP LOCKED in a short transaction that also pushes
next_verification_at forward by a lease. Workers skip rows another worker is claiming and
ignore leased rows, so several workers never process the same transfer. The partner is
then called with no transaction open, and each resolution re-locks the transfer and
re-checks its status, so a transfer is never settled or reversed twice, even if the lease
expired or the API request finished first. The unique (transfer, journal type) constraint
is the last line of defence.

Residual risk, documented: a partner that applied a credit it received long after our
client gave up (beyond the not-found grace) would conflict with a reversal. Real partners
bound this with a request expiry or a "void by reference" API.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

import structlog
from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.models import Program, Transfer
from app.db.session import unit_of_work
from app.domain.enums import TransferStatus
from app.domain.transfer_state import TransferFailureCode, transition
from app.partners.base import CreditStatus, PartnerStatusResult
from app.partners.registry import PartnerRegistry
from app.services.transfer_service import lock_transfer, reverse_transfer, settle_transfer

logger = structlog.get_logger(__name__)

S = TransferStatus
_STUCK_STATUSES = (S.SOURCE_DEBITED, S.PARTNER_SUBMITTED)
_VERIFIABLE_STATUSES = (S.PARTNER_SUBMITTED, S.PENDING_VERIFICATION)


class ReconcileResult(StrEnum):
    COMPLETED = "COMPLETED"
    REVERSED = "REVERSED"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    NOT_DUE = "NOT_DUE"  # too young to touch safely (a live request may own it)
    ALREADY_RESOLVED = "ALREADY_RESOLVED"  # final, in manual review, or moved on meanwhile


@dataclass(frozen=True)
class ReconciliationPolicy:
    lease: timedelta
    stuck_after: timedelta
    not_found_grace: timedelta
    max_attempts: int
    backoff_base: timedelta
    backoff_max: timedelta

    @classmethod
    def from_settings(cls, settings: Settings) -> "ReconciliationPolicy":
        return cls(
            lease=timedelta(seconds=settings.reconciliation_lease_seconds),
            stuck_after=timedelta(seconds=settings.reconciliation_stuck_after_seconds),
            not_found_grace=timedelta(seconds=settings.reconciliation_not_found_grace_seconds),
            max_attempts=settings.reconciliation_max_attempts,
            backoff_base=timedelta(seconds=settings.reconciliation_backoff_base_seconds),
            backoff_max=timedelta(seconds=settings.reconciliation_backoff_max_seconds),
        )

    def backoff(self, attempts: int) -> timedelta:
        """Delay before the next check after `attempts` inconclusive checks (1-based)."""
        delay: timedelta = self.backoff_base * (2 ** (attempts - 1))
        return min(self.backoff_max, delay)


@dataclass(frozen=True)
class _Snapshot:
    """What the reconciler needs to know about a transfer before calling the partner."""

    status: TransferStatus
    created_at: datetime
    partner_code: str
    reference: str


def utc_now() -> datetime:
    return datetime.now(UTC)


class ReconciliationService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        partners: PartnerRegistry,
        policy: ReconciliationPolicy,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._session_factory = session_factory
        self._partners = partners
        self._policy = policy
        self._clock = clock

    # ------------------------------------------------------------ batch (the worker)

    async def run_once(
        self, batch_size: int, should_stop: Callable[[], bool] = lambda: False
    ) -> dict[ReconcileResult, int]:
        """Claim a batch of due transfers and resolve them one by one. Stops between
        transfers (never in the middle of one) when `should_stop()` becomes true."""
        summary: dict[ReconcileResult, int] = {}
        for transfer_id in await self.claim_due(batch_size):
            if should_stop():
                break  # unprocessed claims simply expire with their lease
            result = await self.reconcile(transfer_id)
            summary[result] = summary.get(result, 0) + 1
        return summary

    async def claim_due(self, batch_size: int) -> list[str]:
        now = self._clock()
        due = or_(
            and_(Transfer.status == S.PENDING_VERIFICATION, Transfer.next_verification_at <= now),
            and_(
                Transfer.status.in_(_STUCK_STATUSES),
                Transfer.created_at <= now - self._policy.stuck_after,
                or_(Transfer.next_verification_at.is_(None), Transfer.next_verification_at <= now),
            ),
        )
        async with unit_of_work(self._session_factory) as session:
            claimed = list(
                await session.scalars(
                    select(Transfer.id)
                    .where(due)
                    .order_by(Transfer.next_verification_at.asc().nulls_first(), Transfer.id)
                    .limit(batch_size)
                    .with_for_update(skip_locked=True)
                )
            )
            if claimed:
                await session.execute(
                    update(Transfer)
                    .where(Transfer.id.in_(claimed))
                    # Keep updated_at: a lease is not a business change.
                    .values(
                        next_verification_at=now + self._policy.lease,
                        updated_at=Transfer.updated_at,
                    )
                )
        if claimed:
            logger.info("reconciliation_batch_claimed", count=len(claimed))
        return claimed

    # ------------------------------------------------------------ one transfer

    async def reconcile(self, transfer_id: str) -> ReconcileResult:
        """Resolve one transfer if it is safe to do so (also used by the admin endpoint)."""
        log = logger.bind(transfer_id=transfer_id)
        snapshot = await self._snapshot(transfer_id)
        now = self._clock()

        if snapshot.status not in (*_STUCK_STATUSES, S.PENDING_VERIFICATION):
            return ReconcileResult.ALREADY_RESOLVED
        if (
            snapshot.status in _STUCK_STATUSES
            and snapshot.created_at > now - self._policy.stuck_after
        ):
            return ReconcileResult.NOT_DUE

        if snapshot.status == S.SOURCE_DEBITED:
            return await self._reverse_interrupted(transfer_id, log)

        # Outside any transaction: the partner call may take seconds.
        partner_status = await self._partners.get(snapshot.partner_code).get_credit_status(
            snapshot.partner_code, snapshot.reference
        )
        log.info(
            "reconciliation_partner_status",
            partner_status=partner_status.status,
            reason=partner_status.reason,
        )
        return await self._apply(transfer_id, partner_status, log)

    async def _snapshot(self, transfer_id: str) -> _Snapshot:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(
                        Transfer.status,
                        Transfer.created_at,
                        Program.partner_code,
                        Transfer.partner_reference,
                    )
                    .join(Program, Program.id == Transfer.destination_program_id)
                    .where(Transfer.id == transfer_id)
                )
            ).one_or_none()
        if row is None:
            raise LookupError(f"transfer {transfer_id} does not exist")
        status, created_at, partner_code, reference = row
        return _Snapshot(S(status), created_at, partner_code, reference or transfer_id)

    async def _reverse_interrupted(
        self, transfer_id: str, log: structlog.typing.FilteringBoundLogger
    ) -> ReconcileResult:
        async with unit_of_work(self._session_factory) as session:
            transfer = await lock_transfer(session, transfer_id)
            if transfer.status != S.SOURCE_DEBITED:
                return ReconcileResult.ALREADY_RESOLVED
            await reverse_transfer(
                session,
                transfer,
                TransferFailureCode.TRANSFER_INTERRUPTED,
                "Processing was interrupted before the partner was contacted. "
                "Points were returned.",
                "reconciled_interrupted_before_partner",
                {"source": "reconciler"},
            )
        log.warning("reconciliation_reversed_interrupted_transfer")
        return ReconcileResult.REVERSED

    async def _apply(
        self,
        transfer_id: str,
        partner_status: PartnerStatusResult,
        log: structlog.typing.FilteringBoundLogger,
    ) -> ReconcileResult:
        now = self._clock()
        async with unit_of_work(self._session_factory) as session:
            transfer = await lock_transfer(session, transfer_id)
            if transfer.status not in _VERIFIABLE_STATUSES:
                return ReconcileResult.ALREADY_RESOLVED  # someone else resolved it meanwhile

            metadata: dict[str, Any] = {
                "source": "reconciler",
                "partner_status": partner_status.status,
                "partner_reason": partner_status.reason,
                "verification_attempts": transfer.verification_attempts,
            }

            if partner_status.status == CreditStatus.COMPLETED and partner_status.confirmation_id:
                await settle_transfer(
                    session,
                    transfer,
                    partner_status.confirmation_id,
                    "reconciled_partner_has_credit",
                    metadata,
                )
                log.info("reconciliation_completed")
                return ReconcileResult.COMPLETED

            if partner_status.status == CreditStatus.NOT_FOUND:
                decidable_at = transfer.created_at + self._policy.not_found_grace
                if now >= decidable_at:
                    await reverse_transfer(
                        session,
                        transfer,
                        TransferFailureCode.PARTNER_NOT_RECEIVED,
                        "The partner never received the credit. Points were returned.",
                        "reconciled_partner_has_no_credit",
                        metadata,
                    )
                    log.info("reconciliation_reversed")
                    return ReconcileResult.REVERSED
                return self._recheck_after_grace(session, transfer, metadata, now, decidable_at)

            return self._retry_or_escalate(session, transfer, metadata, now, log)

    def _recheck_after_grace(
        self,
        session: AsyncSession,
        transfer: Transfer,
        metadata: dict[str, Any],
        now: datetime,
        decidable_at: datetime,
    ) -> ReconcileResult:
        """The partner answered 'not found', but a request could still be in flight. That is
        a clear answer, not a failure, so it does not count toward max attempts (which is
        reserved for an unreachable partner). Check again soon, and no later than the moment
        the answer becomes decisive."""
        if transfer.status == S.PARTNER_SUBMITTED:
            transition(session, transfer, S.PENDING_VERIFICATION, "reconciler_verifying", metadata)
        transfer.next_verification_at = min(now + self._policy.backoff_base, decidable_at)
        return ReconcileResult.RETRY_SCHEDULED

    def _retry_or_escalate(
        self,
        session: AsyncSession,
        transfer: Transfer,
        metadata: dict[str, Any],
        now: datetime,
        log: structlog.typing.FilteringBoundLogger,
    ) -> ReconcileResult:
        """Inconclusive check: schedule the next one, or hand over to a human."""
        if transfer.status == S.PARTNER_SUBMITTED:
            transition(session, transfer, S.PENDING_VERIFICATION, "reconciler_verifying", metadata)

        transfer.verification_attempts += 1
        attempts = transfer.verification_attempts
        metadata = {**metadata, "verification_attempts": attempts}

        if attempts >= self._policy.max_attempts:
            transfer.failure_code = TransferFailureCode.VERIFICATION_EXHAUSTED
            transfer.failure_message = (
                f"Partner outcome still unknown after {attempts} verification attempts. "
                "Points are held in clearing until an operator resolves the transfer."
            )
            transfer.next_verification_at = None
            transition(session, transfer, S.MANUAL_REVIEW, "verification_exhausted", metadata)
            log.error(
                "reconciliation_manual_review_required",
                attempts=attempts,
                partner_reason=metadata["partner_reason"],
            )
            return ReconcileResult.MANUAL_REVIEW

        delay = self._policy.backoff(attempts)
        transfer.next_verification_at = now + delay
        log.info(
            "reconciliation_retry_scheduled",
            attempts=attempts,
            next_check_in_seconds=int(delay.total_seconds()),
        )
        return ReconcileResult.RETRY_SCHEDULED
