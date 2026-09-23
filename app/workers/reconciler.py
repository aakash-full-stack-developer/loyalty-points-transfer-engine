"""Reconciler worker: `python -m app.workers.reconciler` (the `worker` compose service).

Every RECONCILER_INTERVAL_SECONDS it claims a batch of due transfers and resolves them (see
app/services/reconciliation.py). Several copies can run at once: claiming uses
SELECT ... FOR UPDATE SKIP LOCKED plus a lease, so no transfer is processed twice.

Shutdown: SIGTERM / SIGINT set a stop flag. The worker finishes the transfer it is working
on, releases nothing it holds (unprocessed claims expire with their lease), closes its
connections and exits. A failing round (database or Redis down) is logged and retried on
the next tick instead of crashing the process.
"""

import asyncio
import contextlib
import signal

import structlog
from prometheus_client import start_http_server

from app.config import Settings, get_settings
from app.db.session import create_engine, create_session_factory
from app.domain.enums import TransferStatus
from app.observability import metrics
from app.observability.logging import configure_logging
from app.partners.registry import build_partner_client, build_partner_registry
from app.partners.resilience import CircuitBreakerRegistry
from app.services.reconciliation import (
    ReconciliationPolicy,
    ReconciliationService,
    count_backlog,
)

logger = structlog.get_logger(__name__)


async def run(settings: Settings, stop: asyncio.Event) -> None:
    engine = create_engine(settings)
    partner_client = build_partner_client(settings)
    breakers = CircuitBreakerRegistry(
        failure_threshold=settings.circuit_breaker_failure_threshold,
        cooldown_seconds=settings.circuit_breaker_cooldown_seconds,
    )
    session_factory = create_session_factory(engine)
    service = ReconciliationService(
        session_factory,
        build_partner_registry(settings, partner_client, breakers),
        ReconciliationPolicy.from_settings(settings),
    )
    if settings.reconciler_metrics_port:
        # The worker has no HTTP API, so it serves its own Prometheus endpoint.
        start_http_server(settings.reconciler_metrics_port)
    logger.info(
        "reconciler_started",
        interval_seconds=settings.reconciler_interval_seconds,
        batch_size=settings.reconciler_batch_size,
        metrics_port=settings.reconciler_metrics_port or None,
    )
    try:
        while not stop.is_set():
            try:
                summary = await service.run_once(settings.reconciler_batch_size, stop.is_set)
                if summary:
                    counts = {result.value: count for result, count in summary.items()}
                    logger.info("reconciliation_round_finished", **counts)
                async with session_factory() as session:
                    backlog = await count_backlog(session)
                metrics.TRANSFERS_PENDING_VERIFICATION.set(
                    backlog[TransferStatus.PENDING_VERIFICATION]
                )
                metrics.TRANSFERS_MANUAL_REVIEW.set(backlog[TransferStatus.MANUAL_REVIEW])
            except Exception:
                logger.exception("reconciliation_round_failed")
            # Sleep until the next tick, waking immediately on shutdown.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=settings.reconciler_interval_seconds)
    finally:
        await partner_client.aclose()
        await engine.dispose()
        logger.info("reconciler_stopped")


async def _main() -> None:
    settings = get_settings()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await run(settings, stop)


def main() -> None:
    settings = get_settings()
    configure_logging(level=settings.log_level, json_logs=settings.log_json)
    asyncio.run(_main())


if __name__ == "__main__":
    main()
