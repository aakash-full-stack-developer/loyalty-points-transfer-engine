"""Reconciler worker entry point: `python -m app.workers.reconciler`.

Placeholder until step 8. It starts, logs that it is idle, and shuts down cleanly on
SIGTERM/SIGINT, so the `worker` compose service already behaves like the real one.
"""

import asyncio
import signal

import structlog

from app.config import get_settings
from app.observability.logging import configure_logging

logger = structlog.get_logger(__name__)


async def run() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    logger.info("reconciler_started", detail="placeholder: reconciliation is added in step 8")
    await stop.wait()
    logger.info("reconciler_stopped")


def main() -> None:
    settings = get_settings()
    configure_logging(level=settings.log_level, json_logs=settings.log_json)
    asyncio.run(run())


if __name__ == "__main__":
    main()
