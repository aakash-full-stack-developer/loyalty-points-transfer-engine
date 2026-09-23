"""The transfer saga: move points from a source program to a destination program.

A database transaction cannot include a partner's API, so a transfer is a saga: a series of
short local transactions, each leaving a durable state, with a compensating step (reversal)
when the partner definitively fails.

    1. TX "debit"     quote with the current rate, lock the source account, check the
                      balance, create the transfer and post TRANSFER_DEBIT
                      (user source -> source TRANSFER_CLEARING).       status SOURCE_DEBITED
    2. TX "submit"    record that the partner is about to be called.   status PARTNER_SUBMITTED
    3. partner call   OUTSIDE any transaction, reference = transfer id (idempotent at partner)
    4. TX "outcome"   SUCCESS  -> TRANSFER_SETTLE journal                status COMPLETED
                      REJECTED -> TRANSFER_REVERSAL (points returned)    status REVERSED
                      NOT_SENT -> TRANSFER_REVERSAL (points returned)    status REVERSED
                      UNKNOWN  -> leave points in clearing              status PENDING_VERIFICATION

"Automatic rollback" therefore means compensation: the debit was committed in step 1, and
a failure is undone by a reversal journal, never by deleting history. An unknown outcome is
never guessed; the reconciler (step 8) asks the partner and completes or reverses.

Every intermediate state is committed before the next external action, so a crash at any
point leaves a transfer the reconciler can pick up (SOURCE_DEBITED, PARTNER_SUBMITTED or
PENDING_VERIFICATION) and points are never lost: they wait in the clearing account.

Locking order, in every saga transaction: the transfer row first, then every account the
transaction changes, in ascending id order. One global order means no deadlocks.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.errors import violated_constraint
from app.db.models import Account, Program, Transfer
from app.db.session import unit_of_work
from app.domain.enums import AccountType, JournalType, TransferStatus
from app.domain.errors import DomainError, ErrorCode
from app.domain.ids import new_transfer_id
from app.domain.transfer_state import TransferFailureCode, record_creation, transition
from app.partners.base import CreditOutcome, PartnerCreditResult
from app.partners.registry import PartnerRegistry
from app.services.ledger import (
    Posting,
    get_system_account,
    get_user_account,
    lock_accounts_for_update,
    post_journal,
)
from app.services.rate_engine import (
    calculate_conversion,
    ensure_different_programs,
    ensure_route_usable,
)
from app.services.rate_repository import RateRepository

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class TransferRequest:
    source_program: str
    destination_program: str
    source_points: int


@dataclass(frozen=True)
class _DebitedTransfer:
    transfer_id: str
    partner_code: str
    member_id: str
    destination_points: int


def utc_now() -> datetime:
    return datetime.now(UTC)


class TransferService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        partners: PartnerRegistry,
        verification_delay: timedelta,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._session_factory = session_factory
        self._partners = partners
        self._verification_delay = verification_delay
        self._clock = clock

    async def create_transfer(
        self, user_id: str, request: TransferRequest, idempotency_key: str
    ) -> str:
        """Run the saga; return the transfer id. Business errors raise DomainError and leave
        nothing behind (the debit transaction rolls back)."""
        ensure_different_programs(request.source_program, request.destination_program)
        try:
            debited = await self._debit_source(user_id, request, idempotency_key)
        except IntegrityError as exc:
            if violated_constraint(exc) != "uq_transfers_user_idempotency_key":
                raise
            # Last line of defence behind the idempotency layer: this key already created a
            # transfer (for example after an abandoned request was taken over). Return it.
            existing_id = await self._find_by_idempotency_key(user_id, idempotency_key)
            logger.warning("transfer_already_exists_for_key", transfer_id=existing_id)
            return existing_id

        structlog.contextvars.bind_contextvars(transfer_id=debited.transfer_id)
        logger.info("transfer_source_debited", destination_points=debited.destination_points)

        await self._mark_submitted(debited.transfer_id, debited.partner_code)

        # No database transaction or row lock is held here, on purpose. The partner call can
        # take seconds (timeouts, retries); holding locks would block every other transfer on
        # these accounts, and a transaction cannot roll back what the partner did anyway.
        adapter = self._partners.get(debited.partner_code)
        result = await adapter.credit_points(
            debited.partner_code,
            reference=debited.transfer_id,
            member_id=debited.member_id,
            points=debited.destination_points,
        )

        await self._apply_partner_outcome(debited.transfer_id, result)
        return debited.transfer_id

    # ------------------------------------------------------------ step 1: debit

    async def _debit_source(
        self, user_id: str, request: TransferRequest, idempotency_key: str
    ) -> _DebitedTransfer:
        now = self._clock()
        async with unit_of_work(self._session_factory) as session:
            # Price from PostgreSQL inside this transaction, never from the cache, so the
            # transfer always uses the rate version that is current as it is created.
            route = await RateRepository(session).get_route(
                request.source_program, request.destination_program, at=now
            )
            rate = ensure_route_usable(route)
            conversion = calculate_conversion(rate, request.source_points, route.bonus, at=now)

            source = await self._user_account(session, user_id, route.source.id, route.source.code)
            destination = await self._user_account(
                session, user_id, route.destination.id, route.destination.code
            )
            destination_program = await session.get(Program, route.destination.id)
            if destination_program is None or destination.external_member_id is None:
                raise RuntimeError("destination program or member id missing")
            clearing = await get_system_account(
                session, route.source.id, AccountType.TRANSFER_CLEARING
            )

            locked = await lock_accounts_for_update(session, [source.id, clearing.id])
            if locked[source.id].balance < request.source_points:
                raise DomainError(
                    ErrorCode.INSUFFICIENT_BALANCE,
                    f"Your {route.source.code} balance is lower than {request.source_points}.",
                )

            transfer_id = new_transfer_id()
            transfer = Transfer(
                id=transfer_id,
                user_id=user_id,
                idempotency_key=idempotency_key,
                source_program_id=route.source.id,
                destination_program_id=route.destination.id,
                source_account_id=source.id,
                destination_account_id=destination.id,
                source_points=conversion.source_points,
                base_points=conversion.base_points,
                bonus_points=conversion.bonus_points,
                destination_points=conversion.destination_points,
                rate_id=rate.rate_id,
                rate_snapshot=conversion.snapshot.to_dict(),
                status=TransferStatus.PENDING,
                partner_reference=transfer_id,
            )
            session.add(transfer)
            await session.flush()
            record_creation(session, transfer, "transfer_requested")

            journal = await post_journal(
                session,
                JournalType.TRANSFER_DEBIT,
                [
                    Posting.debit(locked[source.id], request.source_points),
                    Posting.credit(locked[clearing.id], request.source_points),
                ],
                transfer_id=transfer_id,
            )
            transition(
                session,
                transfer,
                TransferStatus.SOURCE_DEBITED,
                "source_points_debited",
                {"journal_id": journal.id},
                now,
            )

        return _DebitedTransfer(
            transfer_id=transfer_id,
            partner_code=destination_program.partner_code,
            member_id=destination.external_member_id,
            destination_points=conversion.destination_points,
        )

    @staticmethod
    async def _user_account(
        session: AsyncSession, user_id: str, program_id: int, program_code: str
    ) -> Account:
        account = await get_user_account(session, user_id, program_id)
        if account is None:
            raise DomainError(
                ErrorCode.ACCOUNT_NOT_FOUND,
                f"You have no {program_code} account linked.",
                program=program_code,
            )
        return account

    async def _find_by_idempotency_key(self, user_id: str, idempotency_key: str) -> str:
        async with self._session_factory() as session:
            transfer_id = await session.scalar(
                select(Transfer.id).where(
                    Transfer.user_id == user_id, Transfer.idempotency_key == idempotency_key
                )
            )
        if transfer_id is None:
            raise RuntimeError("unique violation on idempotency key but no transfer found")
        return transfer_id

    # ------------------------------------------------------------ step 2: submit

    async def _mark_submitted(self, transfer_id: str, partner_code: str) -> None:
        async with unit_of_work(self._session_factory) as session:
            transfer = await lock_transfer(session, transfer_id)
            transition(
                session,
                transfer,
                TransferStatus.PARTNER_SUBMITTED,
                "partner_credit_requested",
                {"partner": partner_code},
                self._clock(),
            )

    # ------------------------------------------------------------ step 4: outcome

    async def _apply_partner_outcome(self, transfer_id: str, result: PartnerCreditResult) -> None:
        now = self._clock()
        metadata: dict[str, Any] = {
            "partner_outcome": result.outcome,
            "partner_reason": result.reason,
            "attempts": result.attempts,
            "http_status": result.http_status,
        }
        logger.info("partner_credit_outcome", **metadata)

        async with unit_of_work(self._session_factory) as session:
            transfer = await lock_transfer(session, transfer_id)
            if transfer.status != TransferStatus.PARTNER_SUBMITTED:
                # Another actor (the reconciler, an admin) already moved it on. Never apply
                # an outcome twice.
                logger.warning("partner_outcome_ignored", current_status=transfer.status)
                return

            if result.outcome == CreditOutcome.SUCCESS and result.confirmation_id:
                await settle_transfer(
                    session, transfer, result.confirmation_id, "partner_confirmed_credit", metadata
                )
            elif result.outcome == CreditOutcome.REJECTED:
                await reverse_transfer(
                    session,
                    transfer,
                    TransferFailureCode.PARTNER_REJECTED,
                    f"The partner rejected the credit ({result.reason}). Points were returned.",
                    "partner_rejected_credit",
                    metadata,
                )
            elif result.outcome == CreditOutcome.NOT_SENT:
                await reverse_transfer(
                    session,
                    transfer,
                    TransferFailureCode.PARTNER_UNAVAILABLE,
                    "The partner could not be reached. Points were returned.",
                    "partner_not_reached",
                    metadata,
                )
            else:
                # UNKNOWN: the partner may have applied the credit. Keep the points in
                # clearing and let the reconciler find out; never guess.
                transfer.next_verification_at = now + self._verification_delay
                transition(
                    session,
                    transfer,
                    TransferStatus.PENDING_VERIFICATION,
                    "partner_outcome_unknown",
                    metadata,
                    now,
                )


# ---------------------------------------------------------------- shared saga steps
# Used by the transfer service and the reconciler. The caller holds the transfer row lock.


async def lock_transfer(session: AsyncSession, transfer_id: str) -> Transfer:
    """Lock the transfer row (FOR NO KEY UPDATE, fresh values) before changing its state."""
    transfer = await session.scalar(
        select(Transfer)
        .where(Transfer.id == transfer_id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    )
    if transfer is None:
        raise LookupError(f"transfer {transfer_id} does not exist")
    return transfer


async def settle_transfer(
    session: AsyncSession,
    transfer: Transfer,
    confirmation_id: str,
    reason: str,
    metadata: dict[str, Any],
) -> None:
    """COMPLETED: source clearing -> source settlement; destination settlement -> user."""
    source_clearing = await get_system_account(
        session, transfer.source_program_id, AccountType.TRANSFER_CLEARING
    )
    source_settlement = await get_system_account(
        session, transfer.source_program_id, AccountType.PARTNER_SETTLEMENT
    )
    destination_settlement = await get_system_account(
        session, transfer.destination_program_id, AccountType.PARTNER_SETTLEMENT
    )
    accounts = await lock_accounts_for_update(
        session,
        [
            source_clearing.id,
            source_settlement.id,
            destination_settlement.id,
            transfer.destination_account_id,
        ],
    )
    await post_journal(
        session,
        JournalType.TRANSFER_SETTLE,
        [
            Posting.debit(accounts[source_clearing.id], transfer.source_points),
            Posting.credit(accounts[source_settlement.id], transfer.source_points),
            Posting.debit(accounts[destination_settlement.id], transfer.destination_points),
            Posting.credit(accounts[transfer.destination_account_id], transfer.destination_points),
        ],
        transfer_id=transfer.id,
    )
    transfer.partner_confirmation_id = confirmation_id
    transfer.next_verification_at = None
    transition(
        session,
        transfer,
        TransferStatus.COMPLETED,
        reason,
        {**metadata, "confirmation_id": confirmation_id},
    )


async def reverse_transfer(
    session: AsyncSession,
    transfer: Transfer,
    failure_code: TransferFailureCode,
    failure_message: str,
    reason: str,
    metadata: dict[str, Any],
) -> None:
    """REVERSED (compensation): source clearing -> user source account, in full."""
    clearing = await get_system_account(
        session, transfer.source_program_id, AccountType.TRANSFER_CLEARING
    )
    accounts = await lock_accounts_for_update(session, [clearing.id, transfer.source_account_id])
    await post_journal(
        session,
        JournalType.TRANSFER_REVERSAL,
        [
            Posting.debit(accounts[clearing.id], transfer.source_points),
            Posting.credit(accounts[transfer.source_account_id], transfer.source_points),
        ],
        transfer_id=transfer.id,
    )
    transfer.failure_code = failure_code
    transfer.failure_message = failure_message
    transfer.next_verification_at = None
    transition(
        session,
        transfer,
        TransferStatus.REVERSED,
        reason,
        {**metadata, "failure_code": failure_code},
    )
