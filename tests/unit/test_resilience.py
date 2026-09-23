import asyncio
import time

import pytest

from app.partners.base import (
    CreditOutcome,
    CreditStatus,
    PartnerAdapter,
    PartnerCreditResult,
    PartnerStatusResult,
)
from app.partners.resilience import (
    BreakerState,
    CircuitBreaker,
    CircuitBreakerRegistry,
    ResilientPartnerAdapter,
    RetryPolicy,
)

SUCCESS = PartnerCreditResult(CreditOutcome.SUCCESS, "credited", False, confirmation_id="C-1")
REJECTED = PartnerCreditResult(CreditOutcome.REJECTED, "MEMBER_NOT_FOUND", False)
UNKNOWN = PartnerCreditResult(CreditOutcome.UNKNOWN, "partner_error_500", True)
NOT_SENT = PartnerCreditResult(CreditOutcome.NOT_SENT, "connection_failed", True)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


class ScriptedPartner(PartnerAdapter):
    """Returns the scripted results in order, repeating the last one."""

    def __init__(
        self,
        credits: list[PartnerCreditResult] | None = None,
        statuses: list[PartnerStatusResult] | None = None,
    ) -> None:
        self.credits = credits or [SUCCESS]
        self.statuses = statuses or []
        self.credit_calls = 0
        self.status_calls = 0

    async def credit_points(
        self, partner_code: str, reference: str, member_id: str, points: int
    ) -> PartnerCreditResult:
        self.credit_calls += 1
        return self.credits[min(self.credit_calls, len(self.credits)) - 1]

    async def get_credit_status(self, partner_code: str, reference: str) -> PartnerStatusResult:
        self.status_calls += 1
        return self.statuses[min(self.status_calls, len(self.statuses)) - 1]


def resilient(
    inner: PartnerAdapter,
    clock: FakeClock,
    *,
    max_attempts: int = 3,
    threshold: int = 100,
    cooldown: float = 30.0,
    deadline: float = 60.0,
    base_delay: float = 0.1,
) -> ResilientPartnerAdapter:
    return ResilientPartnerAdapter(
        inner,
        retry=RetryPolicy(max_attempts, base_delay_seconds=base_delay, max_delay_seconds=1.0),
        breakers=CircuitBreakerRegistry(threshold, cooldown, clock=clock),
        deadline_seconds=deadline,
        clock=clock,
        sleep=clock.sleep,
        rand=lambda: 1.0,  # take the full backoff ceiling: deterministic delays
    )


async def credit(adapter: ResilientPartnerAdapter) -> PartnerCreditResult:
    return await adapter.credit_points("SKYWARD", "tr_1", "SKY100200301", 1_000)


# ---------------------------------------------------------------- backoff


@pytest.mark.parametrize(("attempt", "ceiling"), [(1, 0.1), (2, 0.2), (3, 0.4), (4, 0.8), (5, 1.0)])
def test_backoff_is_exponential_and_capped(attempt: int, ceiling: float) -> None:
    policy = RetryPolicy(max_attempts=5, base_delay_seconds=0.1, max_delay_seconds=1.0)

    assert policy.backoff(attempt, rand=lambda: 1.0) == pytest.approx(ceiling)
    assert policy.backoff(attempt, rand=lambda: 0.0) == 0.0  # full jitter spans [0, ceiling]


# ---------------------------------------------------------------- retries


async def test_retryable_failures_are_retried_until_success() -> None:
    clock = FakeClock()
    inner = ScriptedPartner([UNKNOWN, NOT_SENT, SUCCESS])

    result = await credit(resilient(inner, clock))

    assert result.outcome == CreditOutcome.SUCCESS
    assert result.attempts == 3
    assert clock.now == pytest.approx(1_000.0 + 0.1 + 0.2)  # two backoff sleeps


async def test_retries_stop_at_max_attempts() -> None:
    inner = ScriptedPartner([UNKNOWN])

    result = await credit(resilient(inner, FakeClock(), max_attempts=3))

    assert result.outcome == CreditOutcome.UNKNOWN
    assert inner.credit_calls == 3
    assert result.attempts == 3


async def test_rejection_is_final_and_never_retried() -> None:
    inner = ScriptedPartner([REJECTED])

    result = await credit(resilient(inner, FakeClock()))

    assert result.outcome == CreditOutcome.REJECTED
    assert inner.credit_calls == 1


async def test_not_sent_after_retries_stays_not_sent() -> None:
    inner = ScriptedPartner([NOT_SENT])

    result = await credit(resilient(inner, FakeClock()))

    assert result.outcome == CreditOutcome.NOT_SENT


async def test_unknown_is_never_downgraded_to_not_sent() -> None:
    """Attempt 1 may have been applied by the partner. Even though attempt 2 provably never
    left, the overall outcome must stay UNKNOWN, or the saga would wrongly reverse."""
    inner = ScriptedPartner([UNKNOWN, NOT_SENT])

    result = await credit(resilient(inner, FakeClock(), max_attempts=2))

    assert result.outcome == CreditOutcome.UNKNOWN


async def test_unknown_followed_by_open_circuit_stays_unknown() -> None:
    inner = ScriptedPartner([UNKNOWN])

    result = await credit(resilient(inner, FakeClock(), threshold=1))

    assert result.outcome == CreditOutcome.UNKNOWN
    assert "circuit_open" in result.reason
    assert inner.credit_calls == 1


async def test_retries_stop_when_the_next_backoff_would_pass_the_deadline() -> None:
    clock = FakeClock()
    inner = ScriptedPartner([UNKNOWN])

    result = await credit(resilient(inner, clock, max_attempts=10, deadline=1.0, base_delay=0.4))

    # Sleeps 0.4 then 0.8 would reach 1.2 s > 1.0 s budget: only 2 attempts fit.
    assert inner.credit_calls == 2
    assert result.outcome == CreditOutcome.UNKNOWN
    assert clock.now - 1_000.0 < 1.0


async def test_attempt_cut_off_by_the_deadline_is_unknown() -> None:
    class HangingPartner(ScriptedPartner):
        async def credit_points(
            self, partner_code: str, reference: str, member_id: str, points: int
        ) -> PartnerCreditResult:
            await asyncio.sleep(5)
            return SUCCESS

    adapter = ResilientPartnerAdapter(
        HangingPartner(),
        retry=RetryPolicy(3, 0.01, 0.01),
        breakers=CircuitBreakerRegistry(5, 30),
        deadline_seconds=0.05,
        clock=time.monotonic,
    )

    result = await credit(adapter)

    assert result.outcome == CreditOutcome.UNKNOWN
    assert result.reason == "deadline_exceeded"


# ---------------------------------------------------------------- circuit breaker


async def test_circuit_opens_after_threshold_and_fails_fast() -> None:
    clock = FakeClock()
    inner = ScriptedPartner([NOT_SENT])
    adapter = resilient(inner, clock, max_attempts=1, threshold=3)

    for _ in range(3):
        await credit(adapter)
    fast = await credit(adapter)

    assert inner.credit_calls == 3  # the fourth call never reached the partner
    assert fast.outcome == CreditOutcome.NOT_SENT
    assert fast.reason == "circuit_open"
    assert fast.attempts == 0


async def test_circuit_half_opens_after_cooldown_and_closes_on_success() -> None:
    clock = FakeClock()
    inner = ScriptedPartner([NOT_SENT, SUCCESS])
    adapter = resilient(inner, clock, max_attempts=1, threshold=1, cooldown=30)
    await credit(adapter)  # opens the circuit

    clock.now += 30
    result = await credit(adapter)

    assert result.outcome == CreditOutcome.SUCCESS
    assert adapter._breakers.get("SKYWARD").state == BreakerState.CLOSED


def test_failed_trial_in_half_open_reopens_the_circuit() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker("SKYWARD", failure_threshold=1, cooldown_seconds=30, clock=clock)
    breaker.record_failure()
    clock.now += 30

    assert breaker.allow_request()  # the single trial
    breaker.record_failure()

    assert breaker.state == BreakerState.OPEN
    assert not breaker.allow_request()


def test_half_open_allows_only_one_trial_at_a_time() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker("SKYWARD", failure_threshold=1, cooldown_seconds=30, clock=clock)
    breaker.record_failure()
    clock.now += 30

    assert breaker.allow_request()
    assert not breaker.allow_request()


async def test_business_rejections_do_not_open_the_circuit() -> None:
    inner = ScriptedPartner([REJECTED])
    adapter = resilient(inner, FakeClock(), threshold=2)

    for _ in range(5):
        await credit(adapter)

    assert adapter._breakers.get("SKYWARD").state == BreakerState.CLOSED
    assert inner.credit_calls == 5


async def test_breakers_are_independent_per_partner() -> None:
    inner = ScriptedPartner([NOT_SENT])
    adapter = resilient(inner, FakeClock(), max_attempts=1, threshold=1)

    await adapter.credit_points("SKYWARD", "tr_1", "m", 1)

    assert adapter._breakers.get("SKYWARD").state == BreakerState.OPEN
    assert adapter._breakers.get("STAYWELL").state == BreakerState.CLOSED


# ---------------------------------------------------------------- status checks


async def test_status_check_retries_until_answer() -> None:
    inner = ScriptedPartner(
        statuses=[
            PartnerStatusResult(CreditStatus.UNKNOWN, "status_check_failed_503", True),
            PartnerStatusResult(CreditStatus.COMPLETED, "credit_found", False, "C-1"),
        ]
    )

    result = await resilient(inner, FakeClock()).get_credit_status("SKYWARD", "tr_1")

    assert result.status == CreditStatus.COMPLETED
    assert result.attempts == 2


async def test_status_check_fails_fast_when_circuit_is_open() -> None:
    inner = ScriptedPartner(
        credits=[NOT_SENT],
        statuses=[PartnerStatusResult(CreditStatus.COMPLETED, "credit_found", False, "C-1")],
    )
    adapter = resilient(inner, FakeClock(), max_attempts=1, threshold=1)
    await credit(adapter)

    result = await adapter.get_credit_status("SKYWARD", "tr_1")

    assert result.status == CreditStatus.UNKNOWN
    assert result.reason == "circuit_open"
    assert inner.status_calls == 0
