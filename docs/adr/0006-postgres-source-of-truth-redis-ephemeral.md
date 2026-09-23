# ADR 0006: PostgreSQL as the source of truth; Redis only for ephemeral concerns

Status: accepted

## Context

The stack includes both PostgreSQL and Redis. Redis is fast, but by default it can lose
recent writes on restart and it has no multi-key constraints. Balances, transfers and
their history must never be lost or inconsistent.

## Decision

- **PostgreSQL** holds everything that matters: programs, rates, accounts, the ledger,
  transfers, their events and idempotency records. Correctness rules live in constraints
  and triggers where possible.
- **Redis** holds only data that is safe to lose, and every use has a defined behaviour
  when Redis is down:

| Use | If Redis is down |
| --- | --- |
| Idempotency lock | Falls back to the PostgreSQL unique constraint (still exactly-once) |
| Active-rate cache for quotes | Falls back to PostgreSQL; transfers never use the cache |
| Per-user rate limiting | Fails open: requests are allowed and a warning is logged |

## Consequences

- A Redis outage degrades performance and abuse protection, never correctness; tests
  cover each case.
- Cache entries expire at the earlier of the TTL and the moment the route's rate or bonus
  changes on its own, and are deleted after admin changes.
- The rate limiter protects capacity, not money, which is why it fails open while
  idempotency falls back to PostgreSQL rather than failing open.

## Alternatives considered

- **Balances in Redis for speed.** Fast, but a restart or failover could lose or duplicate
  points.
- **No Redis.** Workable (PostgreSQL covers every guarantee), but concurrent duplicate
  requests and quote traffic would all hit the database.
