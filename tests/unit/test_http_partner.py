from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from app.partners.base import CreditOutcome, CreditStatus
from app.partners.http_partner import HttpPartnerAdapter

BASE_URL = "http://partner.test"
CREDITS = "/partners/SKYWARD/credits"


@pytest.fixture
async def adapter() -> AsyncIterator[HttpPartnerAdapter]:
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        yield HttpPartnerAdapter(client)


@pytest.mark.parametrize(
    ("mock", "outcome", "retryable"),
    [
        pytest.param(
            httpx.Response(201, json={"confirmation_id": "SKY-1"}),
            CreditOutcome.SUCCESS,
            False,
            id="201_created",
        ),
        pytest.param(
            httpx.Response(200, json={"confirmation_id": "SKY-1"}),
            CreditOutcome.SUCCESS,
            False,
            id="200_idempotent_replay",
        ),
        pytest.param(
            httpx.Response(201, text="<html>oops</html>"),
            CreditOutcome.UNKNOWN,
            True,
            id="201_without_confirmation",
        ),
        pytest.param(
            httpx.Response(422, json={"error": "MEMBER_NOT_FOUND"}),
            CreditOutcome.REJECTED,
            False,
            id="422_rejected",
        ),
        pytest.param(
            httpx.Response(409, json={"error": "REFERENCE_CONFLICT"}),
            CreditOutcome.REJECTED,
            False,
            id="409_conflict",
        ),
        pytest.param(httpx.Response(429), CreditOutcome.NOT_SENT, True, id="429_throttled"),
        pytest.param(httpx.Response(500), CreditOutcome.UNKNOWN, True, id="500_error"),
        pytest.param(httpx.Response(503), CreditOutcome.UNKNOWN, True, id="503_unavailable"),
        pytest.param(
            httpx.ConnectError("connection refused"),
            CreditOutcome.NOT_SENT,
            True,
            id="connect_error",
        ),
        pytest.param(
            httpx.ConnectTimeout("connect timeout"),
            CreditOutcome.NOT_SENT,
            True,
            id="connect_timeout",
        ),
        pytest.param(
            httpx.ReadTimeout("read timeout"), CreditOutcome.UNKNOWN, True, id="read_timeout"
        ),
        pytest.param(
            httpx.RemoteProtocolError("server disconnected"),
            CreditOutcome.UNKNOWN,
            True,
            id="connection_dropped",
        ),
    ],
)
async def test_credit_outcomes_are_classified(
    adapter: HttpPartnerAdapter,
    mock: httpx.Response | Exception,
    outcome: CreditOutcome,
    retryable: bool,
) -> None:
    with respx.mock(base_url=BASE_URL) as router:
        route = router.post(CREDITS)
        if isinstance(mock, Exception):
            route.side_effect = mock
        else:
            route.return_value = mock

        result = await adapter.credit_points("SKYWARD", "tr_1", "SKY100200301", 1_000)

    assert (result.outcome, result.retryable) == (outcome, retryable)


async def test_rejection_reason_comes_from_partner_body(adapter: HttpPartnerAdapter) -> None:
    with respx.mock(base_url=BASE_URL) as router:
        router.post(CREDITS).return_value = httpx.Response(422, json={"error": "MEMBER_NOT_FOUND"})

        result = await adapter.credit_points("SKYWARD", "tr_1", "SKY100200301", 1_000)

    assert result.reason == "MEMBER_NOT_FOUND"
    assert result.http_status == 422


async def test_credit_request_carries_reference_member_and_points(
    adapter: HttpPartnerAdapter,
) -> None:
    with respx.mock(base_url=BASE_URL) as router:
        route = router.post(CREDITS)
        route.return_value = httpx.Response(201, json={"confirmation_id": "SKY-1"})

        await adapter.credit_points("SKYWARD", "tr_ABC", "SKY100200301", 1_250)

    sent = route.calls.last.request
    assert sent.read() == b'{"reference":"tr_ABC","member_id":"SKY100200301","points":1250}'


@pytest.mark.parametrize(
    ("mock", "status"),
    [
        pytest.param(
            httpx.Response(200, json={"confirmation_id": "SKY-1"}),
            CreditStatus.COMPLETED,
            id="found",
        ),
        pytest.param(httpx.Response(404), CreditStatus.NOT_FOUND, id="not_found"),
        pytest.param(httpx.Response(500), CreditStatus.UNKNOWN, id="error"),
        pytest.param(httpx.ConnectError("refused"), CreditStatus.UNKNOWN, id="unreachable"),
    ],
)
async def test_status_outcomes_are_classified(
    adapter: HttpPartnerAdapter, mock: httpx.Response | Exception, status: CreditStatus
) -> None:
    with respx.mock(base_url=BASE_URL) as router:
        route = router.get(f"{CREDITS}/tr_1")
        if isinstance(mock, Exception):
            route.side_effect = mock
        else:
            route.return_value = mock

        result = await adapter.get_credit_status("SKYWARD", "tr_1")

    assert result.status == status
