"""HTTP partner adapter (httpx) with strict outcome classification.

Classification rules, credit (POST /partners/{code}/credits):
    200, 201 with a confirmation_id        SUCCESS
    200, 201 without a usable body         UNKNOWN   (applied, but we cannot prove it)
    429                                    NOT_SENT  (throttled before processing), retryable
    other 4xx (422 rejection, 409, 400...) REJECTED  (definitive business/client error)
    5xx (500, 502, 503, 504)               UNKNOWN   retryable
    connect error / connect or pool timeout NOT_SENT retryable (the request never left)
    read / write timeout, broken connection UNKNOWN  retryable (it may have been processed)

Retrying an UNKNOWN is safe only because the partner is idempotent on `reference`.
"""

from typing import Any

import httpx

from app.partners.base import (
    CreditOutcome,
    CreditStatus,
    PartnerAdapter,
    PartnerCreditResult,
    PartnerStatusResult,
)

# Raised before any byte of the request reached the partner.
_NOT_SENT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


class HttpPartnerAdapter(PartnerAdapter):
    def __init__(self, client: httpx.AsyncClient) -> None:
        """`client` carries the base URL and the connect/read timeouts."""
        self._client = client

    async def credit_points(
        self, partner_code: str, reference: str, member_id: str, points: int
    ) -> PartnerCreditResult:
        try:
            response = await self._client.post(
                f"/partners/{partner_code}/credits",
                json={"reference": reference, "member_id": member_id, "points": points},
            )
        except _NOT_SENT_ERRORS as exc:
            return PartnerCreditResult(
                CreditOutcome.NOT_SENT, f"connection_failed: {type(exc).__name__}", retryable=True
            )
        except httpx.TransportError as exc:  # read/write timeout, reset, protocol error
            return PartnerCreditResult(
                CreditOutcome.UNKNOWN, f"no_response: {type(exc).__name__}", retryable=True
            )
        return classify_credit_response(response)

    async def get_credit_status(self, partner_code: str, reference: str) -> PartnerStatusResult:
        try:
            response = await self._client.get(f"/partners/{partner_code}/credits/{reference}")
        except httpx.TransportError as exc:
            return PartnerStatusResult(
                CreditStatus.UNKNOWN, f"no_response: {type(exc).__name__}", retryable=True
            )
        return classify_status_response(response)


def classify_credit_response(response: httpx.Response) -> PartnerCreditResult:
    code = response.status_code
    body = _json(response)
    if code in (200, 201):
        confirmation_id = body.get("confirmation_id")
        if isinstance(confirmation_id, str) and confirmation_id:
            return PartnerCreditResult(
                CreditOutcome.SUCCESS,
                "credited",
                retryable=False,
                confirmation_id=confirmation_id,
                http_status=code,
            )
        return PartnerCreditResult(
            CreditOutcome.UNKNOWN, "success_without_confirmation", retryable=True, http_status=code
        )
    if code == 429:
        return PartnerCreditResult(
            CreditOutcome.NOT_SENT, "throttled", retryable=True, http_status=code
        )
    if 400 <= code < 500:
        reason = str(body.get("error") or f"http_{code}")
        return PartnerCreditResult(
            CreditOutcome.REJECTED, reason, retryable=False, http_status=code
        )
    return PartnerCreditResult(
        CreditOutcome.UNKNOWN, f"partner_error_{code}", retryable=True, http_status=code
    )


def classify_status_response(response: httpx.Response) -> PartnerStatusResult:
    code = response.status_code
    if code == 200:
        confirmation_id = _json(response).get("confirmation_id")
        if isinstance(confirmation_id, str) and confirmation_id:
            return PartnerStatusResult(
                CreditStatus.COMPLETED,
                "credit_found",
                retryable=False,
                confirmation_id=confirmation_id,
                http_status=code,
            )
    if code == 404:
        return PartnerStatusResult(
            CreditStatus.NOT_FOUND, "credit_not_found", retryable=False, http_status=code
        )
    return PartnerStatusResult(
        CreditStatus.UNKNOWN, f"status_check_failed_{code}", retryable=True, http_status=code
    )


def build_http_client(
    base_url: str, connect_timeout: float, read_timeout: float
) -> httpx.AsyncClient:
    """One shared client per process: connection pooling, separate connect/read timeouts."""
    return httpx.AsyncClient(
        base_url=base_url,
        timeout=httpx.Timeout(
            connect=connect_timeout, read=read_timeout, write=read_timeout, pool=connect_timeout
        ),
        headers={"User-Agent": "loyalty-transfer-engine"},
    )
