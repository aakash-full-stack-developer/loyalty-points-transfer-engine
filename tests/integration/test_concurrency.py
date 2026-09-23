"""Correctness under concurrency and failure: the tests reviewers care most about.

Every test ends with the ledger invariants checked (no points created or destroyed). The
chaos test also checks the ledger against the partner: a transfer is COMPLETED exactly
when the partner holds its credit.
"""

import asyncio
import uuid
from collections import Counter
from collections.abc import AsyncIterator
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.models import LedgerJournal, Transfer, TransferEvent
from app.domain.enums import JournalType, TransferStatus
from app.partners.base import CreditOutcome, PartnerCreditResult
from app.partners.registry import PartnerRegistry, build_partner_client, build_partner_registry
from app.partners.resilience import CircuitBreakerRegistry
from app.services.reconciliation import (
    ReconcileResult,
    ReconciliationPolicy,
    ReconciliationService,
)
from app.services.transfer_service import TransferRequest, TransferService
from tests.integration.conftest import running_app
from tests.integration.factories import create_user
from tests.integration.helpers import (
    CARD_TO_AIRLINE,
    Clock,
    balances,
    set_partner_mode,
    transfer,
)

pytestmark = pytest.mark.usefixtures("seeded", "simulator", "ledger_stays_consistent")

Factory = async_sessionmaker[AsyncSession]
ALL_PROGRAMS = (
    "NOVA_REWARDS",
    "ZENITH_POINTS",
    "SKYWARD_MILES",
    "STAYWELL_POINTS",
    "HARBOR_CRUISE_POINTS",
)


async def journal_count(factory: Factory, journal_type: JournalType) -> int:
    async with factory() as session:
        return (
            await session.scalar(
                select(func.count())
                .select_from(LedgerJournal)
                .where(LedgerJournal.journal_type == journal_type)
            )
            or 0
        )


async def transfers_by_status(factory: Factory) -> Counter[str]:
    async with factory() as session:
        return Counter(str(status) for status in await session.scalars(select(Transfer.status)))


@pytest.fixture
async def partners(settings: Settings) -> AsyncIterator[PartnerRegistry]:
    client = build_partner_client(settings)
    yield build_partner_registry(settings, client, CircuitBreakerRegistry(1_000, 30))
    await client.aclose()


# ---------------------------------------------------------------- the three classics


async def test_twenty_concurrent_transfers_exceeding_the_balance_never_overdraw(
    client: httpx.AsyncClient, session_factory: Factory
) -> None:
    await create_user(session_factory, "user_racer", {"NOVA_REWARDS": 100_000, "SKYWARD_MILES": 0})

    responses = await asyncio.gather(
        *(transfer(client, *CARD_TO_AIRLINE, 10_000, user="user_racer") for _ in range(20))
    )

    outcomes = Counter(r.json().get("code") or r.json()["status"] for r in responses)
    assert outcomes == {"COMPLETED": 10, "INSUFFICIENT_BALANCE": 10}
    final = await balances(client, "user_racer")
    assert final["NOVA_REWARDS"] == 0  # exactly the available balance was spent, never more
    assert final["SKYWARD_MILES"] == 10 * 12_500
    assert await journal_count(session_factory, JournalType.TRANSFER_DEBIT) == 10


async def test_ten_concurrent_requests_with_one_key_create_exactly_one_transfer(
    client: httpx.AsyncClient, session_factory: Factory
) -> None:
    before = await balances(client)
    key = str(uuid.uuid4())

    responses = await asyncio.gather(
        *(transfer(client, *CARD_TO_AIRLINE, 10_000, key=key) for _ in range(10))
    )

    assert {r.status_code for r in responses} <= {201, 409}
    created_ids = {r.json()["id"] for r in responses if r.status_code == 201}
    assert len(created_ids) == 1  # the winner, plus any replays of it
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Transfer)) == 1
    assert await journal_count(session_factory, JournalType.TRANSFER_DEBIT) == 1
    assert (await balances(client))["NOVA_REWARDS"] == before["NOVA_REWARDS"] - 10_000


async def test_concurrent_opposite_direction_transfers_never_deadlock(
    client: httpx.AsyncClient, session_factory: Factory
) -> None:
    await create_user(
        session_factory, "user_pingpong", {"NOVA_REWARDS": 300_000, "SKYWARD_MILES": 300_000}
    )
    requests = [
        transfer(client, source, destination, 3_000, user="user_pingpong")
        for _ in range(10)
        for source, destination in (
            ("NOVA_REWARDS", "SKYWARD_MILES"),
            ("SKYWARD_MILES", "NOVA_REWARDS"),
        )
    ]

    # A deadlock would surface as a lock timeout (500) or hang past the timeout.
    responses = await asyncio.wait_for(asyncio.gather(*requests), timeout=60)

    assert {r.json()["status"] for r in responses} == {"COMPLETED"}
    final = await balances(client, "user_pingpong")
    assert final["NOVA_REWARDS"] == 300_000 - 10 * 3_000 + 10 * 1_000
    assert final["SKYWARD_MILES"] == 300_000 + 10 * 3_750 - 10 * 3_000


# ---------------------------------------------------------------- safety nets


async def test_duplicate_key_that_bypasses_the_idempotency_layer_returns_the_same_transfer(
    session_factory: Factory, partners: PartnerRegistry
) -> None:
    """The transfers table's UNIQUE (user_id, idempotency_key) is the last line of defence."""
    saga = TransferService(session_factory, partners, verification_delay=timedelta(seconds=10))
    request = TransferRequest(*CARD_TO_AIRLINE, 10_000)

    first = await saga.create_transfer("user_alice", request, "raw-key")
    second = await saga.create_transfer("user_alice", request, "raw-key")

    assert first == second
    assert await journal_count(session_factory, JournalType.TRANSFER_DEBIT) == 1
    assert await journal_count(session_factory, JournalType.TRANSFER_SETTLE) == 1


async def test_late_partner_answer_is_ignored_once_the_reconciler_resolved_the_transfer(
    session_factory: Factory, partners: PartnerRegistry
) -> None:
    """The API request stalls after calling the partner; the reconciler settles the transfer;
    then the API's answer finally arrives. It must not settle a second time."""
    saga = TransferService(session_factory, partners, verification_delay=timedelta(seconds=10))
    debited = await saga._debit_source("user_alice", TransferRequest(*CARD_TO_AIRLINE, 10_000), "k")
    await saga._mark_submitted(debited.transfer_id, debited.partner_code)
    answer = await partners.get("SKYWARD").credit_points(
        "SKYWARD", debited.transfer_id, debited.member_id, debited.destination_points
    )
    clock = Clock()
    clock.advance(hours=1)
    policy = ReconciliationPolicy(
        lease=timedelta(seconds=60),
        stuck_after=timedelta(seconds=60),
        not_found_grace=timedelta(seconds=60),
        max_attempts=3,
        backoff_base=timedelta(seconds=10),
        backoff_max=timedelta(seconds=60),
    )
    reconciler = ReconciliationService(session_factory, partners, policy, clock)
    assert await reconciler.reconcile(debited.transfer_id) == ReconcileResult.COMPLETED

    status = await saga._apply_partner_outcome(debited.transfer_id, answer)

    assert answer.outcome == CreditOutcome.SUCCESS
    assert status == TransferStatus.COMPLETED
    assert await journal_count(session_factory, JournalType.TRANSFER_SETTLE) == 1


async def test_late_failure_answer_cannot_reverse_a_completed_transfer(
    session_factory: Factory, partners: PartnerRegistry
) -> None:
    saga = TransferService(session_factory, partners, verification_delay=timedelta(seconds=10))
    transfer_id = await saga.create_transfer(
        "user_alice", TransferRequest(*CARD_TO_AIRLINE, 10_000), "k2"
    )

    status = await saga._apply_partner_outcome(
        transfer_id, PartnerCreditResult(CreditOutcome.REJECTED, "late", retryable=False)
    )

    assert status == TransferStatus.COMPLETED
    assert await journal_count(session_factory, JournalType.TRANSFER_REVERSAL) == 0


# ---------------------------------------------------------------- chaos


CHAOS_ROUTES = (
    ("NOVA_REWARDS", "SKYWARD_MILES", 2_000),
    ("NOVA_REWARDS", "STAYWELL_POINTS", 1_000),
    ("SKYWARD_MILES", "NOVA_REWARDS", 3_000),
    ("ZENITH_POINTS", "HARBOR_CRUISE_POINTS", 5_000),
    ("STAYWELL_POINTS", "NOVA_REWARDS", 5_000),
)
CHAOS_USERS = tuple(f"user_chaos_{n}" for n in range(5))
STARTING_BALANCE = 1_000_000


async def test_chaos_flaky_partners_and_a_live_reconciler_never_create_or_lose_points(
    settings: Settings,
    simulator: httpx.AsyncClient,
    session_factory: Factory,
    partners: PartnerRegistry,
) -> None:
    """50 concurrent transfers across 5 routes, 5 users and 4 misbehaving partners, with a
    reconciler running at the same time:
      SKYWARD, NOVA  flaky: half of all requests fail (credits and status checks alike)
      STAYWELL       applies credits but always answers too late (unknown outcomes)
      HARBOR         rejects every credit
    Every transfer must end COMPLETED or REVERSED, and the ledger must agree with the
    partners: COMPLETED exactly when the partner holds the credit."""
    for user in CHAOS_USERS:
        await create_user(session_factory, user, dict.fromkeys(ALL_PROGRAMS, STARTING_BALANCE))
    for partner in ("NOVA", "SKYWARD"):
        await set_partner_mode(simulator, partner, "flaky", failure_rate=0.5)
    await set_partner_mode(simulator, "STAYWELL", "timeout_after_commit", delay_ms=1_200)
    await set_partner_mode(simulator, "HARBOR", "reject")

    live_policy = ReconciliationPolicy(
        lease=timedelta(seconds=30),
        stuck_after=timedelta(seconds=60),
        not_found_grace=timedelta(seconds=60),
        max_attempts=100,
        backoff_base=timedelta(seconds=1),
        backoff_max=timedelta(seconds=2),
    )
    live_reconciler = ReconciliationService(session_factory, partners, live_policy)
    stop = asyncio.Event()

    async def reconcile_while_transfers_run() -> None:
        while not stop.is_set():
            await live_reconciler.run_once(batch_size=50)
            await asyncio.sleep(0.2)

    api_settings = settings.model_copy(update={"reconciliation_initial_delay_seconds": 1})
    async with running_app(api_settings) as (_, api):
        background = asyncio.create_task(reconcile_while_transfers_run())
        responses: list[httpx.Response] = []
        for batch in range(5):
            responses += await asyncio.gather(
                *(
                    transfer(api, source, destination, points, user=CHAOS_USERS[(batch + n) % 5])
                    for n, (source, destination, points) in enumerate(CHAOS_ROUTES * 2)
                )
            )
        stop.set()
        await background

    assert len(responses) == 50
    assert {r.status_code for r in responses} <= {201, 202}

    # Drain everything still unresolved: later, with the partners still flaky.
    clock = Clock()
    drain = ReconciliationService(session_factory, partners, live_policy, clock)
    for _ in range(30):
        pending = sum(
            count
            for status, count in (await transfers_by_status(session_factory)).items()
            if status not in ("COMPLETED", "REVERSED")
        )
        if not pending:
            break
        clock.advance(hours=1)
        await drain.run_once(batch_size=100)

    statuses = await transfers_by_status(session_factory)
    assert set(statuses) <= {"COMPLETED", "REVERSED"}, statuses
    assert sum(statuses.values()) == 50

    # The ledger agrees with the partners, transfer by transfer: a credit exists at the
    # partner exactly for the COMPLETED transfers, with exactly the destination points.
    credits = {c["reference"]: c for c in (await simulator.get("/simulator/credits")).json()}
    async with session_factory() as session:
        all_transfers = list(await session.scalars(select(Transfer)))
    completed = {t.id: t for t in all_transfers if t.status == TransferStatus.COMPLETED}
    assert set(credits) == set(completed)
    for transfer_id, credit in credits.items():
        assert credit["points"] == completed[transfer_id].destination_points

    # Every user's balances equal start - completed debits + completed credits; reversed
    # transfers leave no trace.
    expected = {user: dict.fromkeys(ALL_PROGRAMS, STARTING_BALANCE) for user in CHAOS_USERS}
    bodies = {r.json()["id"]: r.json() for r in responses}
    for transfer_id, completed_transfer in completed.items():
        body, owner = bodies[transfer_id], expected[completed_transfer.user_id]
        owner[body["source"]["program"]] -= body["source"]["points"]
        owner[body["destination"]["program"]] += body["destination"]["points"]
    async with running_app(settings) as (_, api):
        final_balances = {user: await balances(api, user) for user in CHAOS_USERS}
    assert final_balances == expected

    # The chaos really happened: every recovery path was exercised, so this test cannot
    # pass vacuously.
    async with session_factory() as session:
        reasons = Counter(
            await session.scalars(
                select(TransferEvent.reason).where(
                    TransferEvent.to_status.in_([TransferStatus.COMPLETED, TransferStatus.REVERSED])
                )
            )
        )
    for path in (
        "partner_confirmed_credit",  # completed during the request
        "reconciled_partner_has_credit",  # unknown outcome, reconciler completed it
        "reconciled_partner_has_no_credit",  # unknown outcome, reconciler reversed it
        "partner_rejected_credit",  # definitive failure, reversed immediately
    ):
        assert reasons[path] > 0, f"chaos never exercised {path}: {dict(reasons)}"
