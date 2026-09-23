"""Retries, circuit breaking and an overall deadline around any PartnerAdapter.

Implemented here (small and fully tested) rather than with a library, so every rule that
affects money is visible in one place.

Retry: exponential backoff with full jitter, delay = uniform(0, min(max, base * 2^(n-1))).
Jitter spreads retries from many clients so a recovering partner is not hit in lockstep.
Only retryable outcomes are retried, and only because the partner is idempotent on
`reference`: a retry after an UNKNOWN can never credit twice.

Circuit breaker, one per partner:
    CLOSED    --N consecutive failures-->  OPEN
    OPEN      --cooldown elapsed-------->  HALF_OPEN (one trial request at a time)
    HALF_OPEN --trial succeeds---------->  CLOSED
    HALF_OPEN --trial fails------------->  OPEN
While OPEN, calls fail fast with NOT_SENT: we stop adding load to a partner that is down,
and users get an immediate answer instead of waiting for timeouts. A business rejection
(REJECTED) counts as success for the breaker: the partner is healthy, it just said no.
The breaker state is in-process. With several API instances each has its own breaker,
which is acceptable (each still protects itself); shared state could move to Redis.

Deadline: an overall time budget for one logical call including all retries, so a transfer
request never waits longer than PARTNER_TOTAL_DEADLINE_SECONDS.

Outcome safety: once any attempt ended UNKNOWN (the credit may have been applied), the
final result is never downgraded to NOT_SENT, even if a later attempt fails fast (circuit
open, deadline). Otherwise the saga would reverse points the partner may have credited.
"""

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from enum import StrEnum

import structlog

from app.observability import metrics
from app.partners.base import (
    CreditOutcome,
    CreditStatus,
    PartnerAdapter,
    PartnerCreditResult,
    PartnerStatusResult,
)

logger = structlog.get_logger(__name__)

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int
    base_delay_seconds: float
    max_delay_seconds: float

    def backoff(self, attempt: int, rand: Callable[[], float] = random.random) -> float:
        """Delay after the `attempt`-th failed attempt (1-based), with full jitter."""
        ceiling = min(self.max_delay_seconds, self.base_delay_seconds * 2.0 ** (attempt - 1))
        return rand() * ceiling


class BreakerState(StrEnum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        failure_threshold: int,
        cooldown_seconds: float,
        clock: Clock = time.monotonic,
    ) -> None:
        self.name = name
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._state = BreakerState.CLOSED
        self._consecutive_failures = 0
        self._opened_at = 0.0
        self._trial_in_flight = False
        metrics.record_breaker_state(name, self._state)

    @property
    def state(self) -> BreakerState:
        if (
            self._state == BreakerState.OPEN
            and self._clock() - self._opened_at >= self._cooldown_seconds
        ):
            self._transition(BreakerState.HALF_OPEN)
        return self._state

    def allow_request(self) -> bool:
        state = self.state
        if state == BreakerState.CLOSED:
            return True
        if state == BreakerState.HALF_OPEN and not self._trial_in_flight:
            self._trial_in_flight = True
            return True
        return False

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._trial_in_flight = False
        if self._state != BreakerState.CLOSED:
            self._transition(BreakerState.CLOSED)

    def record_failure(self) -> None:
        self._trial_in_flight = False
        if self._state == BreakerState.HALF_OPEN:
            self._open()
            return
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._failure_threshold:
            self._open()

    def _open(self) -> None:
        self._opened_at = self._clock()
        self._transition(BreakerState.OPEN)

    def _transition(self, new_state: BreakerState) -> None:
        if new_state != self._state:
            logger.warning(
                "circuit_breaker_state_changed",
                partner=self.name,
                from_state=self._state,
                to_state=new_state,
            )
            self._state = new_state
            metrics.record_breaker_state(self.name, new_state)


class CircuitBreakerRegistry:
    """One breaker per partner code, created on first use."""

    def __init__(
        self, failure_threshold: int, cooldown_seconds: float, clock: Clock = time.monotonic
    ) -> None:
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._breakers: dict[str, CircuitBreaker] = {}

    def get(self, partner_code: str) -> CircuitBreaker:
        breaker = self._breakers.get(partner_code)
        if breaker is None:
            breaker = CircuitBreaker(
                partner_code, self._failure_threshold, self._cooldown_seconds, self._clock
            )
            self._breakers[partner_code] = breaker
        return breaker

    def states(self) -> dict[str, BreakerState]:
        return {code: breaker.state for code, breaker in self._breakers.items()}


_CIRCUIT_OPEN = "circuit_open"


def _record_attempt(partner: str, operation: str, outcome: str, duration: float) -> None:
    metrics.PARTNER_REQUESTS.labels(partner=partner, operation=operation, outcome=outcome).inc()
    metrics.PARTNER_REQUEST_DURATION.labels(partner=partner, operation=operation).observe(duration)


def _count_fast_fail(partner: str, operation: str) -> None:
    metrics.PARTNER_REQUESTS.labels(
        partner=partner, operation=operation, outcome="CIRCUIT_OPEN"
    ).inc()


class ResilientPartnerAdapter(PartnerAdapter):
    """Wraps another adapter with retries, a per-partner circuit breaker and a deadline."""

    def __init__(
        self,
        inner: PartnerAdapter,
        retry: RetryPolicy,
        breakers: CircuitBreakerRegistry,
        deadline_seconds: float,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
        rand: Callable[[], float] = random.random,
    ) -> None:
        self._inner = inner
        self._retry = retry
        self._breakers = breakers
        self._deadline_seconds = deadline_seconds
        self._clock = clock
        self._sleep = sleep
        self._rand = rand

    async def credit_points(
        self, partner_code: str, reference: str, member_id: str, points: int
    ) -> PartnerCreditResult:
        breaker = self._breakers.get(partner_code)
        deadline = self._clock() + self._deadline_seconds
        log = logger.bind(partner=partner_code, reference=reference)
        possibly_applied = False
        attempts = 0
        result: PartnerCreditResult | None = None

        while True:
            if not breaker.allow_request():
                _count_fast_fail(partner_code, "credit")
                result = PartnerCreditResult(CreditOutcome.NOT_SENT, _CIRCUIT_OPEN, retryable=False)
                break
            remaining = deadline - self._clock()
            if remaining <= 0:
                result = result or PartnerCreditResult(
                    CreditOutcome.NOT_SENT, "deadline_exceeded", retryable=False
                )
                break

            attempts += 1
            started = time.perf_counter()
            try:
                async with asyncio.timeout(remaining):
                    result = await self._inner.credit_points(
                        partner_code, reference, member_id, points
                    )
            except TimeoutError:
                # The request may have been sent before the budget ran out.
                result = PartnerCreditResult(
                    CreditOutcome.UNKNOWN, "deadline_exceeded", retryable=False
                )
            duration = time.perf_counter() - started
            _record_attempt(partner_code, "credit", result.outcome, duration)

            if result.outcome in (CreditOutcome.SUCCESS, CreditOutcome.REJECTED):
                breaker.record_success()
            else:
                breaker.record_failure()
            if result.outcome == CreditOutcome.UNKNOWN:
                possibly_applied = True
            # The member id is deliberately not logged; the reference identifies the credit.
            log.info(
                "partner_credit_attempt",
                attempt=attempts,
                outcome=result.outcome,
                reason=result.reason,
                duration_ms=round(duration * 1000, 1),
            )

            if not result.retryable or attempts >= self._retry.max_attempts:
                break
            delay = self._retry.backoff(attempts, self._rand)
            if self._clock() + delay >= deadline:
                break
            await self._sleep(delay)

        if possibly_applied and result.outcome == CreditOutcome.NOT_SENT:
            # Never downgrade: an earlier attempt may have been applied by the partner.
            result = PartnerCreditResult(
                CreditOutcome.UNKNOWN,
                f"earlier_attempt_unknown_then_{result.reason}",
                retryable=False,
            )
        return replace(result, attempts=attempts)

    async def get_credit_status(self, partner_code: str, reference: str) -> PartnerStatusResult:
        breaker = self._breakers.get(partner_code)
        deadline = self._clock() + self._deadline_seconds
        attempts = 0
        result = PartnerStatusResult(CreditStatus.UNKNOWN, _CIRCUIT_OPEN, retryable=False)

        while True:
            if not breaker.allow_request():
                _count_fast_fail(partner_code, "status")
                result = PartnerStatusResult(CreditStatus.UNKNOWN, _CIRCUIT_OPEN, retryable=False)
                break
            remaining = deadline - self._clock()
            if remaining <= 0:
                break
            attempts += 1
            started = time.perf_counter()
            try:
                async with asyncio.timeout(remaining):
                    result = await self._inner.get_credit_status(partner_code, reference)
            except TimeoutError:
                result = PartnerStatusResult(
                    CreditStatus.UNKNOWN, "deadline_exceeded", retryable=False
                )
            _record_attempt(partner_code, "status", result.status, time.perf_counter() - started)

            if result.status == CreditStatus.UNKNOWN:
                breaker.record_failure()
            else:
                breaker.record_success()
            if not result.retryable or attempts >= self._retry.max_attempts:
                break
            delay = self._retry.backoff(attempts, self._rand)
            if self._clock() + delay >= deadline:
                break
            await self._sleep(delay)

        return replace(result, attempts=attempts)
