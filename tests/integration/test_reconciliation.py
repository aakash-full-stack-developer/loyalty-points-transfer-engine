"""The reconciler resolves every transfer the API could not finish.

Unknown partner outcomes are produced for real: the API runs with retries disabled against
the simulator's `timeout` (credit not applied) and `timeout_after_commit` (credit applied)
modes. Crashes are simulated by running only the first saga steps. The reconciler uses a
controllable clock, so "one hour later" takes no time.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.models import LedgerJournal, Transfer
from app.domain.enums import JournalType, TransferStatus
from app.partners.registry import (
    PartnerRegistry,
    build_partner_client,
    build_partner_registry,
)
from app.partners.resilience import CircuitBreakerRegistry
from app.services.reconciliation import (
    ReconcileResult,
    ReconciliationPolicy,
    ReconciliationService,
)
from app.services.transfer_service import TransferRequest, TransferService
from app.workers.reconciler import run as run_worker
from tests.integration.conftest import running_app
from tests.integration.helpers import (
    CARD_TO_AIRLINE,
    balances,
    clearing_balance,
    partner_credits,
    set_partner_mode,
    transfer,
)

pytestmark = pytest.mark.usefixtures("seeded", "simulator", "ledger_stays_consistent")

Factory = async_sessionmaker[AsyncSession]
R = ReconcileResult

POLICY = ReconciliationPolicy(
    lease=timedelta(seconds=60),
    stuck_after=timedelta(seconds=60),
    not_found_grace=timedelta(seconds=60),
    max_attempts=3,
    backoff_base=timedelta(seconds=10),
    backoff_max=timedelta(seconds=600),
)


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
async def partners(settings: Settings) -> AsyncIterator[PartnerRegistry]:
    client = build_partner_client(settings)
    # A high breaker threshold keeps the circuit out of the way unless a test wants it.
    yield build_partner_registry(settings, client, CircuitBreakerRegistry(100, 30))
    await client.aclose()


@pytest.fixture
def reconciler(
    session_factory: Factory, partners: PartnerRegistry, clock: Clock
) -> ReconciliationService:
    return ReconciliationService(session_factory, partners, POLICY, clock)


@pytest.fixture
async def api(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    """The API with partner retries disabled, so a timeout stays an unknown outcome."""
    async with running_app(settings.model_copy(update={"partner_retry_max_attempts": 1})) as (
        _,
        client,
    ):
        yield client


async def pending_transfer(
    api: httpx.AsyncClient, simulator: httpx.AsyncClient, mode: str, points: int = 10_000
) -> str:
    await set_partner_mode(simulator, "SKYWARD", mode, delay_ms=1_500)
    response = await transfer(api, *CARD_TO_AIRLINE, points)
    assert response.json()["status"] == "PENDING_VERIFICATION", response.text
    return str(response.json()["id"])


async def load(factory: Factory, transfer_id: str) -> Transfer:
    async with factory() as session:
        loaded = await session.get(Transfer, transfer_id)
    assert loaded is not None
    return loaded


async def last_event_reason(api: httpx.AsyncClient, transfer_id: str) -> str:
    body = (
        await api.get(f"/v1/transfers/{transfer_id}", headers={"X-User-Id": "user_alice"})
    ).json()
    return str(body["events"][-1]["reason"])


# ---------------------------------------------------------------- unknown outcomes


async def test_credit_applied_before_the_timeout_is_completed_and_credited_once(
    api: httpx.AsyncClient,
    simulator: httpx.AsyncClient,
    reconciler: ReconciliationService,
    clock: Clock,
    session_factory: Factory,
) -> None:
    before = await balances(api)
    transfer_id = await pending_transfer(api, simulator, "timeout_after_commit")

    clock.advance(hours=1)
    summary = await reconciler.run_once(batch_size=10)

    assert summary == {R.COMPLETED: 1}
    assert (await load(session_factory, transfer_id)).status == TransferStatus.COMPLETED
    after = await balances(api)
    assert after["NOVA_REWARDS"] == before["NOVA_REWARDS"] - 10_000
    assert after["SKYWARD_MILES"] == before["SKYWARD_MILES"] + 12_500  # exactly once
    assert await clearing_balance(session_factory, "NOVA_REWARDS") == 0
    assert len(await partner_credits(simulator)) == 1
    assert await last_event_reason(api, transfer_id) == "reconciled_partner_has_credit"
    assert await reconciler.run_once(batch_size=10) == {}  # nothing left to do


async def test_credit_never_received_is_reversed_after_the_grace_period(
    api: httpx.AsyncClient,
    simulator: httpx.AsyncClient,
    reconciler: ReconciliationService,
    clock: Clock,
    session_factory: Factory,
) -> None:
    before = await balances(api)
    transfer_id = await pending_transfer(api, simulator, "timeout")

    clock.advance(hours=1)
    summary = await reconciler.run_once(batch_size=10)

    assert summary == {R.REVERSED: 1}
    reversed_transfer = await load(session_factory, transfer_id)
    assert reversed_transfer.status == TransferStatus.REVERSED
    assert reversed_transfer.failure_code == "PARTNER_NOT_RECEIVED"
    assert await balances(api) == before
    assert await partner_credits(simulator) == []


async def test_missing_credit_inside_the_grace_period_is_rechecked_not_reversed(
    api: httpx.AsyncClient,
    simulator: httpx.AsyncClient,
    reconciler: ReconciliationService,
    clock: Clock,
    session_factory: Factory,
) -> None:
    """A request can still be in flight to the partner: 'not found' is not yet proof."""
    transfer_id = await pending_transfer(api, simulator, "timeout")

    clock.advance(seconds=15)  # due for its first check, but younger than the grace
    summary = await reconciler.run_once(batch_size=10)

    assert summary == {R.RETRY_SCHEDULED: 1}
    pending = await load(session_factory, transfer_id)
    assert pending.status == TransferStatus.PENDING_VERIFICATION
    assert pending.verification_attempts == 0  # a clear answer, not a failed attempt
    assert pending.next_verification_at == clock.now + timedelta(seconds=10)


async def test_repeated_not_found_answers_never_escalate_to_manual_review(
    api: httpx.AsyncClient,
    simulator: httpx.AsyncClient,
    reconciler: ReconciliationService,
    clock: Clock,
    session_factory: Factory,
) -> None:
    """More 'not found yet' answers than max_attempts, all inside the grace: the partner is
    reachable and answering, so this must end in a reversal, not in manual review."""
    transfer_id = await pending_transfer(api, simulator, "timeout")
    created = (await load(session_factory, transfer_id)).created_at
    clock.now = created + timedelta(seconds=11)

    results = []
    for _ in range(POLICY.max_attempts + 2):
        results.append(await reconciler.reconcile(transfer_id))
        state = await load(session_factory, transfer_id)
        assert state.next_verification_at is not None
        assert state.next_verification_at <= created + POLICY.not_found_grace
        clock.now = state.next_verification_at

    assert set(results) == {R.RETRY_SCHEDULED}
    assert await reconciler.reconcile(transfer_id) == R.REVERSED


async def test_unreachable_partner_escalates_to_manual_review_after_max_attempts(
    api: httpx.AsyncClient,
    simulator: httpx.AsyncClient,
    reconciler: ReconciliationService,
    clock: Clock,
    session_factory: Factory,
) -> None:
    transfer_id = await pending_transfer(api, simulator, "timeout")
    await set_partner_mode(simulator, "SKYWARD", "unavailable")

    results = []
    delays = []
    for _ in range(POLICY.max_attempts):
        clock.advance(hours=1)
        results.append(await reconciler.reconcile(transfer_id))
        state = await load(session_factory, transfer_id)
        if state.next_verification_at is not None:
            delays.append(state.next_verification_at - clock.now)

    assert results == [R.RETRY_SCHEDULED, R.RETRY_SCHEDULED, R.MANUAL_REVIEW]
    assert delays == [timedelta(seconds=10), timedelta(seconds=20)]  # exponential backoff
    review = await load(session_factory, transfer_id)
    assert review.status == TransferStatus.MANUAL_REVIEW
    assert review.failure_code == "VERIFICATION_EXHAUSTED"
    # Nothing was guessed: the points are still held in clearing for an operator.
    assert await clearing_balance(session_factory, "NOVA_REWARDS") == 10_000
    clock.advance(hours=1)
    assert await reconciler.run_once(batch_size=10) == {}


# ---------------------------------------------------------------- concurrency


async def test_concurrent_claims_never_overlap(
    api: httpx.AsyncClient,
    simulator: httpx.AsyncClient,
    reconciler: ReconciliationService,
    session_factory: Factory,
    partners: PartnerRegistry,
    clock: Clock,
) -> None:
    await set_partner_mode(simulator, "SKYWARD", "timeout_after_commit", delay_ms=1_500)
    responses = await asyncio.gather(*(transfer(api, *CARD_TO_AIRLINE, 1_000) for _ in range(6)))
    assert {r.json()["status"] for r in responses} == {"PENDING_VERIFICATION"}
    other = ReconciliationService(session_factory, partners, POLICY, clock)

    clock.advance(hours=1)
    first, second = await asyncio.gather(reconciler.claim_due(10), other.claim_due(10))

    assert not set(first) & set(second)  # SKIP LOCKED + lease: disjoint claims
    assert len(first) + len(second) == 6
    assert await reconciler.claim_due(10) == []  # leased: nobody can claim them again


async def test_two_reconcilers_running_together_settle_each_transfer_once(
    api: httpx.AsyncClient,
    simulator: httpx.AsyncClient,
    reconciler: ReconciliationService,
    session_factory: Factory,
    partners: PartnerRegistry,
    clock: Clock,
) -> None:
    await set_partner_mode(simulator, "SKYWARD", "timeout_after_commit", delay_ms=1_500)
    responses = await asyncio.gather(*(transfer(api, *CARD_TO_AIRLINE, 1_000) for _ in range(6)))
    ids = {response.json()["id"] for response in responses}
    other = ReconciliationService(session_factory, partners, POLICY, clock)

    clock.advance(hours=1)
    first, second = await asyncio.gather(
        reconciler.run_once(batch_size=3), other.run_once(batch_size=3)
    )
    leftovers = await reconciler.run_once(batch_size=10)

    total = sum(first.values()) + sum(second.values()) + sum(leftovers.values())
    assert total == 6
    async with session_factory() as session:
        settles = await session.scalar(
            select(func.count())
            .select_from(LedgerJournal)
            .where(LedgerJournal.journal_type == JournalType.TRANSFER_SETTLE)
        )
        statuses = set(await session.scalars(select(Transfer.status).where(Transfer.id.in_(ids))))
    assert settles == 6
    assert statuses == {TransferStatus.COMPLETED}


# ---------------------------------------------------------------- crash recovery


@pytest.fixture
def saga(session_factory: Factory, partners: PartnerRegistry) -> TransferService:
    return TransferService(session_factory, partners, verification_delay=timedelta(seconds=10))


async def test_crash_before_the_partner_call_is_reversed_after_the_stuck_window(
    api: httpx.AsyncClient,
    simulator: httpx.AsyncClient,
    saga: TransferService,
    reconciler: ReconciliationService,
    clock: Clock,
    session_factory: Factory,
) -> None:
    before = await balances(api)
    # Simulated crash: only step 1 (the debit) ran. Private step methods are used on purpose.
    debited = await saga._debit_source(
        "user_alice", TransferRequest(*CARD_TO_AIRLINE, 10_000), "crash-1"
    )

    assert await reconciler.reconcile(debited.transfer_id) == R.NOT_DUE  # may still be live
    assert await reconciler.claim_due(10) == []

    clock.advance(minutes=5)
    assert await reconciler.run_once(batch_size=10) == {R.REVERSED: 1}
    crashed = await load(session_factory, debited.transfer_id)
    assert (crashed.status, crashed.failure_code) == (
        TransferStatus.REVERSED,
        "TRANSFER_INTERRUPTED",
    )
    assert await balances(api) == before
    assert await partner_credits(simulator) == []


@pytest.mark.parametrize(
    ("partner_got_it", "expected"),
    [(True, R.COMPLETED), (False, R.REVERSED)],
    ids=["crash_after_partner_call", "crash_before_partner_call"],
)
async def test_crash_after_submission_is_resolved_by_asking_the_partner(
    simulator: httpx.AsyncClient,
    saga: TransferService,
    reconciler: ReconciliationService,
    partners: PartnerRegistry,
    clock: Clock,
    session_factory: Factory,
    partner_got_it: bool,
    expected: ReconcileResult,
) -> None:
    debited = await saga._debit_source(
        "user_alice", TransferRequest(*CARD_TO_AIRLINE, 10_000), "crash-2"
    )
    await saga._mark_submitted(debited.transfer_id, debited.partner_code)
    if partner_got_it:
        await partners.get("SKYWARD").credit_points(
            "SKYWARD", debited.transfer_id, debited.member_id, debited.destination_points
        )
    # ...and the process died before recording the outcome.

    clock.advance(hours=1)
    assert await reconciler.run_once(batch_size=10) == {expected: 1}
    assert (await load(session_factory, debited.transfer_id)).status == expected.value


# ---------------------------------------------------------------- admin endpoint and worker


async def test_admin_can_reconcile_a_transfer_on_demand(
    api: httpx.AsyncClient, simulator: httpx.AsyncClient, settings: Settings
) -> None:
    transfer_id = await pending_transfer(api, simulator, "timeout_after_commit")
    admin = {"X-Admin-Key": settings.admin_api_key.get_secret_value()}

    first = await api.post(f"/v1/admin/transfers/{transfer_id}/reconcile", headers=admin)
    again = await api.post(f"/v1/admin/transfers/{transfer_id}/reconcile", headers=admin)
    unknown = await api.post(
        "/v1/admin/transfers/tr_01HZZZZZZZZZZZZZZZZZZZZZZZ/reconcile", headers=admin
    )
    anonymous = await api.post(f"/v1/admin/transfers/{transfer_id}/reconcile")

    assert first.status_code == 200
    assert first.json()["result"] == "COMPLETED"
    assert first.json()["transfer"]["status"] == "COMPLETED"
    assert again.json()["result"] == "ALREADY_RESOLVED"
    assert unknown.status_code == 404
    assert anonymous.status_code == 401


async def test_worker_resolves_pending_transfers_and_stops_gracefully(
    simulator: httpx.AsyncClient, settings: Settings, session_factory: Factory
) -> None:
    fast = settings.model_copy(
        update={
            "partner_retry_max_attempts": 1,
            "reconciliation_initial_delay_seconds": 1,
            "reconciler_interval_seconds": 0.2,
        }
    )
    async with running_app(fast) as (_, api):
        transfer_id = await pending_transfer(api, simulator, "timeout_after_commit")

    stop = asyncio.Event()
    worker = asyncio.create_task(run_worker(fast, stop))
    try:
        for _ in range(50):
            if (await load(session_factory, transfer_id)).status == TransferStatus.COMPLETED:
                break
            await asyncio.sleep(0.2)
        assert (await load(session_factory, transfer_id)).status == TransferStatus.COMPLETED
    finally:
        stop.set()
        await asyncio.wait_for(worker, timeout=5)  # exits promptly on the stop signal
