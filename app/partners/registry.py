"""Maps a program's partner_code to the adapter that talks to that partner.

Adding a partner that speaks the standard credit API is a data change: give its program a
partner_code and the default adapter serves it. A partner with a different API gets its own
PartnerAdapter implementation, registered here; the transfer saga does not change.
"""

import httpx

from app.config import Settings
from app.partners.base import PartnerAdapter
from app.partners.http_partner import HttpPartnerAdapter, build_http_client
from app.partners.resilience import CircuitBreakerRegistry, ResilientPartnerAdapter, RetryPolicy


class UnknownPartnerError(LookupError):
    pass


class PartnerRegistry:
    def __init__(
        self,
        default: PartnerAdapter | None = None,
        adapters: dict[str, PartnerAdapter] | None = None,
    ) -> None:
        self._default = default
        self._adapters = dict(adapters or {})

    def register(self, partner_code: str, adapter: PartnerAdapter) -> None:
        self._adapters[partner_code] = adapter

    def get(self, partner_code: str) -> PartnerAdapter:
        adapter = self._adapters.get(partner_code, self._default)
        if adapter is None:
            raise UnknownPartnerError(f"no adapter registered for partner {partner_code}")
        return adapter


def build_partner_registry(
    settings: Settings, client: httpx.AsyncClient, breakers: CircuitBreakerRegistry
) -> PartnerRegistry:
    resilient_http = ResilientPartnerAdapter(
        HttpPartnerAdapter(client),
        retry=RetryPolicy(
            max_attempts=settings.partner_retry_max_attempts,
            base_delay_seconds=settings.partner_retry_base_delay_ms / 1000,
            max_delay_seconds=settings.partner_retry_max_delay_ms / 1000,
        ),
        breakers=breakers,
        deadline_seconds=settings.partner_total_deadline_seconds,
    )
    return PartnerRegistry(default=resilient_http)


def build_partner_client(settings: Settings) -> httpx.AsyncClient:
    return build_http_client(
        settings.partner_base_url,
        connect_timeout=settings.partner_connect_timeout_seconds,
        read_timeout=settings.partner_read_timeout_seconds,
    )
