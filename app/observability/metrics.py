"""Prometheus metrics. Every metric the service exports is defined here, in one place.

The API exposes them at GET /metrics. The reconciler worker is a separate process, so it
serves its own /metrics on RECONCILER_METRICS_PORT (reconciliation counters and the backlog
gauges live there).

Suggested alerts (see README):
- transfers_manual_review > 0                          a person must resolve a transfer
- transfers_pending_verification rising for 15 min     a partner is not settling
- circuit_breaker_state == 2 for 5 min                 a partner is down
- rate(transfers_total{status="REVERSED"}[5m]) spike   partner rejections
- rate(partner_requests_total{outcome="UNKNOWN"}[5m])  timeouts / 5xx from a partner

Label values are bounded (statuses, program codes, partner codes, outcomes), never ids,
so the number of time series stays small.
"""

from prometheus_client import Counter, Gauge, Histogram

TRANSFERS = Counter(
    "transfers",
    "Transfers reaching an outcome state (COMPLETED, REVERSED, PENDING_VERIFICATION, "
    "MANUAL_REVIEW), by route",
    ["status", "route"],
)
TRANSFER_DURATION = Histogram(
    "transfer_duration_seconds",
    "Duration of the synchronous transfer saga in POST /v1/transfers",
    ["status"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8, 16),
)
PARTNER_REQUESTS = Counter(
    "partner_requests",
    "Partner API attempts by outcome (credit: SUCCESS/REJECTED/NOT_SENT/UNKNOWN; "
    "status: COMPLETED/NOT_FOUND/UNKNOWN; CIRCUIT_OPEN when failed fast)",
    ["partner", "operation", "outcome"],
)
PARTNER_REQUEST_DURATION = Histogram(
    "partner_request_duration_seconds",
    "Duration of one partner API attempt",
    ["partner", "operation"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8),
)
CIRCUIT_BREAKER_STATE = Gauge(
    "circuit_breaker_state",
    "Circuit breaker state per partner: 0 closed, 1 half-open, 2 open",
    ["partner"],
)
IDEMPOTENT_REPLAYS = Counter(
    "idempotent_replays", "Requests answered from a stored idempotent response"
)
IDEMPOTENCY_CONFLICTS = Counter(
    "idempotency_conflicts",
    "Idempotency-Key conflicts (in_progress: 409, key_reused: 422)",
    ["reason"],
)
RATE_LIMIT_REJECTIONS = Counter(
    "rate_limit_rejections", "POST /v1/transfers requests rejected with 429"
)
RECONCILIATION_RESOLVED = Counter(
    "reconciliation_resolved", "Reconciliation attempts by result", ["result"]
)
TRANSFERS_PENDING_VERIFICATION = Gauge(
    "transfers_pending_verification", "Transfers waiting for partner verification"
)
TRANSFERS_MANUAL_REVIEW = Gauge("transfers_manual_review", "Transfers waiting for an operator")

# Keyed by BreakerState value (a str enum); not imported, to keep this module dependency-free.
_BREAKER_VALUES = {"CLOSED": 0, "HALF_OPEN": 1, "OPEN": 2}


def route_label(source_program: str, destination_program: str) -> str:
    return f"{source_program}->{destination_program}"


def record_breaker_state(partner: str, state: str) -> None:
    CIRCUIT_BREAKER_STATE.labels(partner=partner).set(_BREAKER_VALUES[state])
