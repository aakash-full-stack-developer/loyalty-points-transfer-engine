"""The HTTP adapter against the real partner simulator container, over real HTTP.

These tests reset the simulator's in-memory state, which also clears any simulator modes
set by hand in the dev stack.
"""

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.partners.base import CreditOutcome, CreditStatus
from app.partners.http_partner import HttpPartnerAdapter, build_http_client
from app.partners.resilience import CircuitBreakerRegistry, ResilientPartnerAdapter, RetryPolicy

MEMBER = "SKY100200301"


@pytest.fixture
async def adapter(settings: Settings) -> AsyncIterator[HttpPartnerAdapter]:
    # A short read timeout keeps the timeout scenarios fast.
    client = build_http_client(settings.partner_base_url, connect_timeout=1.0, read_timeout=0.5)
    async with client:
        yield HttpPartnerAdapter(client)


async def set_mode(simulator: httpx.AsyncClient, mode: str, **extra: Any) -> None:
    response = await simulator.post(
        "/simulator/config", json={"partner_code": "SKYWARD", "mode": mode, **extra}
    )
    assert response.status_code == 200


async def test_successful_credit_is_confirmed_and_visible_by_reference(
    simulator: httpx.AsyncClient, adapter: HttpPartnerAdapter
) -> None:
    result = await adapter.credit_points("SKYWARD", "tr_ok", MEMBER, 1_000)
    status = await adapter.get_credit_status("SKYWARD", "tr_ok")

    assert result.outcome == CreditOutcome.SUCCESS
    assert status.status == CreditStatus.COMPLETED
    assert status.confirmation_id == result.confirmation_id


async def test_retrying_the_same_reference_never_credits_twice(
    simulator: httpx.AsyncClient, adapter: HttpPartnerAdapter
) -> None:
    first = await adapter.credit_points("SKYWARD", "tr_retry", MEMBER, 1_000)
    second = await adapter.credit_points("SKYWARD", "tr_retry", MEMBER, 1_000)

    assert first.confirmation_id == second.confirmation_id
    credits = (await simulator.get("/simulator/credits")).json()
    assert len(credits) == 1


async def test_rejection_is_definitive(
    simulator: httpx.AsyncClient, adapter: HttpPartnerAdapter
) -> None:
    await set_mode(simulator, "reject")

    result = await adapter.credit_points("SKYWARD", "tr_rej", MEMBER, 1_000)

    assert (result.outcome, result.reason) == (CreditOutcome.REJECTED, "MEMBER_NOT_FOUND")


async def test_timeout_without_commit_is_unknown_and_partner_has_no_credit(
    simulator: httpx.AsyncClient, adapter: HttpPartnerAdapter
) -> None:
    await set_mode(simulator, "timeout", delay_ms=1_500)

    result = await adapter.credit_points("SKYWARD", "tr_to", MEMBER, 1_000)
    status = await adapter.get_credit_status("SKYWARD", "tr_to")

    assert result.outcome == CreditOutcome.UNKNOWN
    assert status.status == CreditStatus.NOT_FOUND


async def test_timeout_after_commit_is_unknown_but_partner_has_the_credit(
    simulator: httpx.AsyncClient, adapter: HttpPartnerAdapter
) -> None:
    """Why a timeout must be UNKNOWN, not FAILED: here the credit was applied."""
    await set_mode(simulator, "timeout_after_commit", delay_ms=1_500)

    result = await adapter.credit_points("SKYWARD", "tr_tac", MEMBER, 1_000)
    status = await adapter.get_credit_status("SKYWARD", "tr_tac")

    assert result.outcome == CreditOutcome.UNKNOWN
    assert status.status == CreditStatus.COMPLETED


async def test_retrying_an_applied_reference_returns_the_original_confirmation(
    simulator: httpx.AsyncClient, adapter: HttpPartnerAdapter
) -> None:
    """Once the partner answers again, a retry of the same reference is a safe replay:
    the original confirmation, and still one credit."""
    await set_mode(simulator, "timeout_after_commit", delay_ms=1_500)
    first = await adapter.credit_points("SKYWARD", "tr_replay", MEMBER, 1_000)
    await set_mode(simulator, "success")

    retry = await adapter.credit_points("SKYWARD", "tr_replay", MEMBER, 1_000)
    status = await adapter.get_credit_status("SKYWARD", "tr_replay")

    assert first.outcome == CreditOutcome.UNKNOWN
    assert retry.outcome == CreditOutcome.SUCCESS
    assert retry.confirmation_id == status.confirmation_id
    assert len((await simulator.get("/simulator/credits")).json()) == 1


async def test_unreachable_partner_is_not_sent() -> None:
    client = build_http_client("http://127.0.0.1:1", connect_timeout=0.5, read_timeout=0.5)
    async with client:
        result = await HttpPartnerAdapter(client).credit_points("SKYWARD", "tr_x", MEMBER, 1)

    assert result.outcome == CreditOutcome.NOT_SENT


async def test_resilient_adapter_retries_an_outage_then_opens_the_circuit(
    simulator: httpx.AsyncClient, adapter: HttpPartnerAdapter
) -> None:
    await set_mode(simulator, "unavailable")
    resilient = ResilientPartnerAdapter(
        adapter,
        retry=RetryPolicy(max_attempts=3, base_delay_seconds=0.01, max_delay_seconds=0.02),
        breakers=CircuitBreakerRegistry(failure_threshold=3, cooldown_seconds=30),
        deadline_seconds=5,
    )

    first = await resilient.credit_points("SKYWARD", "tr_down", MEMBER, 1_000)
    second = await resilient.credit_points("SKYWARD", "tr_down2", MEMBER, 1_000)

    assert (first.outcome, first.attempts) == (CreditOutcome.UNKNOWN, 3)
    assert (second.outcome, second.reason) == (CreditOutcome.NOT_SENT, "circuit_open")
