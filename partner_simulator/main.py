"""Partner simulator: stands in for external card-issuer and loyalty-program APIs.

It runs as its own service and is reached over real HTTP, so the engine exercises real
timeouts, connection errors and status codes. Credit endpoints and failure modes are
added in step 5.
"""

import os

from fastapi import FastAPI
from pydantic import BaseModel

from app.observability.logging import configure_logging
from app.observability.middleware import RequestContextMiddleware


class HealthResponse(BaseModel):
    status: str = "ok"
    service: str = "partner-simulator"


def create_app() -> FastAPI:
    configure_logging(
        level=os.getenv("LOG_LEVEL", "INFO"),
        json_logs=os.getenv("LOG_JSON", "false").lower() == "true",
    )
    app = FastAPI(title="Partner Simulator", version="0.1.0")
    app.add_middleware(RequestContextMiddleware)

    @app.get("/health", tags=["health"])
    async def health() -> HealthResponse:
        return HealthResponse()

    return app


app = create_app()
