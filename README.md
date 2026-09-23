# Loyalty Points Transfer Engine

[![CI](https://github.com/aakash-full-stack-developer/loyalty-points-transfer-engine/actions/workflows/ci.yml/badge.svg)](https://github.com/aakash-full-stack-developer/loyalty-points-transfer-engine/actions/workflows/ci.yml)

A microservice that moves points between credit-card reward programs and travel loyalty
programs, in both directions: card points to airline miles or hotel points, and back.
Loyalty points behave like money, so every transfer is priced with auditable integer math,
recorded in a double-entry ledger, protected by idempotency keys, and completed through a
saga that reverses automatically when a partner fails. A reconciler resolves the cases where
the partner's answer is unknown, so points are never lost, duplicated or left in limbo.

Python 3.12 · FastAPI · PostgreSQL 16 · Redis 7 · REST · Docker Compose

## For reviewers: start here

1. **Run it:** `cp .env.example .env && make reset && make demo`: 11 scripted scenarios,
   each checked (transfers both ways, idempotent retries, a partner rejection reversed
   automatically, a partner timeout resolved by the reconciler, ledger invariants).
2. **Architecture:** [docs/architecture.md](docs/architecture.md) (component, sequence,
   state and ER diagrams, a worked ledger example) and seven [ADRs](docs/adr/).
3. **What happens when things go wrong:** [docs/failure-scenarios.md](docs/failure-scenarios.md),
   34 scenarios, each linked to the test that proves it.
4. **Most interesting code:** [transfer_service.py](app/services/transfer_service.py) (the saga),
   [reconciliation.py](app/services/reconciliation.py),
   [idempotency.py](app/services/idempotency.py),
   [rate_engine.py](app/services/rate_engine.py), [ledger.py](app/services/ledger.py),
   [resilience.py](app/partners/resilience.py).
5. **Proof:** 363 tests, 96% branch coverage, including concurrency tests and a chaos test
   ([test_concurrency.py](tests/integration/test_concurrency.py)).

## Table of contents

1. [Features](#features)
2. [Architecture overview](#architecture-overview)
3. [Tech stack](#tech-stack)
4. [Project structure](#project-structure)
5. [Prerequisites](#prerequisites)
6. [Quick start (Docker)](#quick-start-docker)
7. [Running the API without Docker](#running-the-api-without-docker)
8. [Make commands](#make-commands)
9. [Configuration](#configuration)
10. [API reference](#api-reference)
11. [Error codes](#error-codes)
12. [Conversion rate engine](#conversion-rate-engine)
13. [Transfer lifecycle](#transfer-lifecycle)
14. [Atomicity and rollback](#atomicity-and-rollback)
15. [Idempotency](#idempotency)
16. [Partner integration](#partner-integration)
17. [Concurrency and consistency guarantees](#concurrency-and-consistency-guarantees)
18. [Observability](#observability)
19. [Testing](#testing)
20. [Database](#database)
21. [Developer guide](#developer-guide)
22. [Assumptions](#assumptions)
23. [Trade-offs and limitations](#trade-offs-and-limitations)
24. [Production readiness and future improvements](#production-readiness-and-future-improvements)
25. [Troubleshooting](#troubleshooting)
26. [Author](#author)

## Features

| Requirement | How it is implemented |
| --- | --- |
| **Bi-directional conversion** | Rates are keyed by *directed* route: card → airline and airline → card are separate rows with separate rules (the seeded reverse rates are deliberately worse). A missing route is rejected with `ROUTE_NOT_SUPPORTED`. Every program has ledger accounts and a partner adapter, so both directions use the same code path. [rate_repository.py](app/services/rate_repository.py) |
| **Flexible conversion rate engine** | Integer numerator/denominator ratios, minimums, increments, maximums and time-bound bonuses; pure-function math that always rounds down; rates are versioned (never updated) and every transfer stores a snapshot of the terms it used; admin API to change rates; Redis cache that fails open. [rate_engine.py](app/services/rate_engine.py), [rate_admin.py](app/services/rate_admin.py) |
| **Atomic transactions with automatic rollback** | A saga of short local transactions: the debit commits first, the partner is called with no transaction open, and a definitive failure posts a compensating reversal journal. Unknown outcomes (timeouts) are verified by a reconciler instead of guessed. A double-entry ledger with database-enforced invariants records every movement. [transfer_service.py](app/services/transfer_service.py), [ledger.py](app/services/ledger.py) |
| **Idempotency keys** | `Idempotency-Key` required on `POST /v1/transfers`; request fingerprinting; Redis lock for concurrent duplicates; PostgreSQL record for replay; transfers are also unique per (user, key); partner calls use the transfer id as an idempotent reference. [idempotency.py](app/services/idempotency.py) |
| **Partner API integration** | Adapter interface with four normalized outcomes (SUCCESS, REJECTED, NOT_SENT, UNKNOWN); HTTP adapter with separate connect/read timeouts; retries with exponential backoff and full jitter; a circuit breaker per partner; an overall deadline; a partner simulator with eight failure modes reached over real HTTP. [app/partners/](app/partners/), [partner_simulator/](partner_simulator/) |
| **PostgreSQL** | The source of truth: CHECK constraints, composite foreign keys, append-only triggers, a deferred balanced-journal trigger, an exclusion constraint, `SELECT ... FOR NO KEY UPDATE`, `SKIP LOCKED`, Alembic migrations. |
| **Redis** | Only for data that is safe to lose: idempotency locks, the rate cache, per-user rate limiting. Every use has a tested fallback. |
| **REST** | Versioned `/v1` endpoints, 201/202 semantics, RFC 7807 `application/problem+json` errors with stable codes, cursor pagination, OpenAPI docs at `/docs`. |
| **Architecture diagram** | [docs/architecture.md](docs/architecture.md), exported images in [docs/images/](docs/images/). |

## Architecture overview

![Component diagram](docs/images/component.svg)

Two processes share one codebase and one Docker image. The **API** runs a transfer
synchronously: it debits the source into a clearing account, calls the partner with no
database transaction open, and settles or reverses. The **reconciler worker** resolves
anything the API could not finish: partner timeouts and transfers interrupted by a crash.
PostgreSQL is the only source of truth; Redis holds only data that is safe to lose. Partners
sit behind an adapter interface; in development they are a separate simulator service,
reached over real HTTP so that timeouts and errors are real.

Full details: [docs/architecture.md](docs/architecture.md) (sequence diagrams for the success
and failure paths, the state machine, the database, a worked ledger example) and the
[ADRs](docs/adr/).

## Tech stack

| Component | Why |
| --- | --- |
| Python 3.12 | Allowed by the brief; mature async ecosystem, strict typing with mypy |
| FastAPI + uvicorn | Async, typed request validation, generated OpenAPI docs |
| Pydantic v2 + pydantic-settings | Strict input validation (no float points), typed settings from the environment |
| SQLAlchemy 2.0 (async) + asyncpg | Explicit transactions and row locking with full SQL control |
| Alembic | Reviewed, reversible schema migrations |
| PostgreSQL 16 | Transactions, constraints and locking that enforce ledger rules in the database |
| Redis 7 (redis-py asyncio) | Fast ephemeral state: locks, cache, rate-limit counters |
| httpx | Async HTTP client with separate connect/read timeouts, connection pooling |
| structlog | Structured JSON logs with request, user and transfer context |
| prometheus-client | Metrics for transfers, partners, idempotency, reconciliation |
| pytest, pytest-asyncio, hypothesis, respx, pytest-cov | Unit, integration, property-based and HTTP-mocked tests, coverage |
| ruff, mypy (strict) | Lint, format and static types |
| Docker Compose + Make | One-command setup; every developer task is a make target |

## Project structure

```text
.
├── app/
│   ├── main.py                    # FastAPI app factory and lifespan (pools, clients)
│   ├── config.py                  # every setting, validated (see Configuration)
│   ├── api/
│   │   ├── routes/                # health, metrics, programs, accounts, quotes, transfers, admin
│   │   ├── deps.py                # dependency wiring: identity, services, rate limiting
│   │   ├── idempotency.py         # reusable idempotent-endpoint wrapper
│   │   ├── errors.py              # RFC 7807 problem+json responses
│   │   ├── presenters.py          # domain objects -> response models
│   │   └── schemas.py             # request/response models (strict integers)
│   ├── domain/
│   │   ├── errors.py              # every error code with its HTTP status and title
│   │   ├── transfer_state.py      # state machine and audit events
│   │   ├── enums.py, ids.py, masking.py
│   ├── services/
│   │   ├── transfer_service.py    # the transfer saga
│   │   ├── reconciliation.py      # resolves unknown outcomes and interrupted transfers
│   │   ├── ledger.py              # double-entry journals, row locking
│   │   ├── ledger_invariants.py   # invariant queries (make check-invariants)
│   │   ├── idempotency.py         # idempotency keys (Redis lock + PostgreSQL record)
│   │   ├── rate_engine.py         # pure conversion math, quotes
│   │   ├── rate_repository.py     # active rate and bonus lookup
│   │   ├── rate_admin.py          # rate versions and bonuses
│   │   └── transfer_queries.py    # transfer detail and cursor pagination
│   ├── partners/
│   │   ├── base.py                # adapter interface, four outcomes
│   │   ├── http_partner.py        # httpx adapter and outcome classification
│   │   ├── resilience.py          # retries, circuit breaker, deadline
│   │   └── registry.py            # partner_code -> adapter
│   ├── cache/                     # Redis client, rate cache, rate limiter
│   ├── db/                        # models (the schema), session, error helpers
│   ├── observability/             # logging, request middleware, Prometheus metrics
│   └── workers/reconciler.py      # the reconciler process
├── partner_simulator/             # mock partner APIs with failure modes (separate service)
├── migrations/                    # Alembic environment and versions
├── scripts/                       # seed, check_invariants, cleanup_idempotency, demo
├── tests/
│   ├── unit/                      # no services needed
│   └── integration/               # real PostgreSQL, Redis and simulator
├── docs/                          # architecture, ADRs, failure scenarios, testing, examples
├── .github/workflows/ci.yml       # lint, typecheck, tests, coverage, Docker build
├── docker-compose.yml, Dockerfile, Makefile, pyproject.toml, .env.example
```

## Prerequisites

| Tool | Version | Needed for |
| --- | --- | --- |
| Docker | 24+ (Docker Desktop, or Engine on Linux) | everything |
| Docker Compose | v2 (`docker compose`, not `docker-compose`) | everything |
| make | any (GNU make 3.81+) | every command |
| Python | 3.12 | only for running the API outside Docker |
| Node.js | 18+ | only for `make diagrams` |

- **macOS:** install [Docker Desktop](https://www.docker.com/products/docker-desktop/); `make`
  comes with the Xcode command line tools (`xcode-select --install`). Python:
  `brew install python@3.12`. Node: `brew install node`.
- **Linux:** install Docker Engine and the Compose plugin
  ([docs](https://docs.docker.com/engine/install/)), then `sudo apt install make` (or your
  distribution's equivalent). Add yourself to the `docker` group or use `sudo`.
- **Windows:** use WSL2 with Ubuntu, install Docker Desktop with the WSL2 backend, and run
  every command inside the WSL2 shell (`sudo apt install make`).

Tests, lint, type checks and migrations run inside a container, so a host Python is not
required for any of them.

## Quick start (Docker)

```bash
# 1. Get the code
git clone https://github.com/aakash-full-stack-developer/loyalty-points-transfer-engine.git
cd loyalty-points-transfer-engine

# 2. Configuration (defaults work as they are)
cp .env.example .env

# 3. Build the images (the first build downloads dependencies and takes a few minutes)
make build

# 4. Start PostgreSQL, Redis, the partner simulator, the API and the worker
make up
#  ✔ Container loyalty-engine-api-1  Healthy

# 5. Create the schema
make migrate
# INFO  [alembic.runtime.migration] Running upgrade  -> 0001, Create the full schema: ...

# 6. Load the programs, rates and two demo users
make seed
# seed complete, created: programs=5, system_accounts=10, rates=6, bonuses=1, users=2, user_accounts=10
```

7. Open the interactive API docs at <http://localhost:8000/docs>.

8. Run the end-to-end walkthrough:

```bash
make demo
# [ 1] Alice's balances
# ...
# [ 8] Partner times out AFTER applying the credit -> the reconciler settles it
#      OK  HTTP 202 PENDING_VERIFICATION after 8.0s: the outcome is unknown, so nothing is guessed
#      OK  COMPLETED after 22s: the reconciler found the credit (reconciled_partner_has_credit)
# ...
# Demo finished: every step behaved as expected.
```

`make reset` does steps 4 to 6 in one go, starting from an empty database. The demo can be
run repeatedly; it checks balance changes rather than absolute balances.

Service URLs:

| Service | URL |
| --- | --- |
| API | <http://localhost:8000> (docs at `/docs`, metrics at `/metrics`) |
| Partner simulator | <http://localhost:8001> (docs at `/docs`) |
| Worker metrics | <http://localhost:9100/metrics> |
| PostgreSQL | `localhost:5432`, user/password/database `loyalty` (`make psql`) |
| Redis | `localhost:6379` |

## Running the API without Docker

The dependencies (PostgreSQL, Redis, simulator) still run in Docker; the API runs on the
host with auto-reload. Needs Python 3.12.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env                 # host values: localhost URLs
docker compose up -d --wait postgres redis partner-simulator
alembic upgrade head
python -m scripts.seed
make run-local                       # http://127.0.0.1:8000, reloads on code changes
```

Stop the Docker `api` container first if it is running (`docker compose stop api`), since
both use port 8000. With the virtualenv active, any make target can run on the host instead
of in the tools container: `make test TOOLS=`, `make lint TOOLS_NODEPS=`.

## Make commands

Run `make` to print this list.

| Command | What it does | When to use it |
| --- | --- | --- |
| `make help` | Lists every target | Default target |
| `make build` | Builds the runtime and dev (tools) images | First setup; after dependency changes |
| `make up` | Builds if needed and starts every service, waiting until healthy | Start working |
| `make down` | Stops and removes containers; keeps the database volume | Stop working |
| `make logs` | Follows logs of all services; `make logs s=worker` for one | Watching behaviour |
| `make ps` | Shows service status and health | Checking the stack |
| `make shell` | Bash in the tools container (source mounted, dev tools installed) | Ad-hoc commands |
| `make migrate` | Applies all migrations (`alembic upgrade head`) | After pulling new migrations |
| `make makemigration name=add_x` | Autogenerates a migration from model changes | After changing `app/db/models.py` |
| `make seed` | Loads seed data; idempotent | After `make migrate` on a new database |
| `make check-invariants` | Verifies the ledger invariants; exit code 1 on any violation | Any time; after incidents |
| `make cleanup-idempotency` | Deletes expired idempotency keys | Housekeeping (would be a cron job) |
| `make psql` | psql on the PostgreSQL container | Inspecting data |
| `make reset` | Drops all data, starts the stack, migrates and seeds | Clean slate |
| `make test` | All tests (starts required services) | Before every commit |
| `make test-unit` | Unit tests only, no services (about 1 s) | Fast feedback |
| `make test-integration` | Integration tests (PostgreSQL, Redis, simulator) | |
| `make coverage` | All tests with coverage (terminal + `htmlcov/`); fails below 90% | Before a PR |
| `make lint` | `ruff check` and `ruff format --check` | Before every commit |
| `make format` | Auto-fixes lint issues and formats | Before `make lint` |
| `make typecheck` | `mypy` in strict mode | Before every commit |
| `make diagrams` | Renders `docs/diagrams/*.mmd` to SVG and PNG (needs Node.js) | After editing a diagram |
| `make demo` | End-to-end walkthrough of every scenario (needs `make up`) | Showing the system; smoke test |
| `make run-local` | Runs the API on the host with auto-reload (needs a venv) | Local development |

## Configuration

Settings are read from the environment and from `.env` (copy `.env.example`, where each
value is documented). Inside Docker Compose, `DATABASE_URL`, `REDIS_URL`,
`PARTNER_BASE_URL` and `LOG_JSON` are overridden with container hostnames. Invalid values
stop the service at startup.

| Variable | Default | Description |
| --- | --- | --- |
| `POSTGRES_USER` | `loyalty` | PostgreSQL user (Compose) |
| `POSTGRES_PASSWORD` | `loyalty` | PostgreSQL password (Compose) |
| `POSTGRES_DB` | `loyalty` | PostgreSQL database (Compose) |
| `POSTGRES_HOST_PORT` | `5432` | Host port for PostgreSQL (Compose) |
| `REDIS_HOST_PORT` | `6379` | Host port for Redis (Compose) |
| `API_HOST_PORT` | `8000` | Host port for the API (Compose) |
| `SIMULATOR_HOST_PORT` | `8001` | Host port for the partner simulator (Compose) |
| `WORKER_METRICS_HOST_PORT` | `9100` | Host port for the worker's metrics (Compose) |
| `APP_NAME` | `loyalty-transfer-engine` | Service name, used as the PostgreSQL `application_name` |
| `ENVIRONMENT` | `local` | `local`, `docker`, `test` or `production` |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR` |
| `LOG_JSON` | `false` | JSON log lines (`true` in Docker) or human-readable output |
| `DATABASE_URL` | `postgresql+asyncpg://loyalty:loyalty@localhost:5432/loyalty` | PostgreSQL URL (asyncpg driver) |
| `DB_POOL_SIZE` | `10` | Pooled connections per process |
| `DB_MAX_OVERFLOW` | `5` | Extra connections allowed under burst load |
| `DB_POOL_TIMEOUT_SECONDS` | `5` | Wait for a free pooled connection |
| `DB_STATEMENT_TIMEOUT_MS` | `5000` | Server-side limit for any SQL statement |
| `DB_LOCK_TIMEOUT_MS` | `3000` | Server-side limit for waiting on a lock |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis URL |
| `REDIS_SOCKET_TIMEOUT_SECONDS` | `1.0` | Redis connect/read timeout |
| `PARTNER_BASE_URL` | `http://localhost:8001` | Partner API base URL (the simulator in development) |
| `PARTNER_CONNECT_TIMEOUT_SECONDS` | `1.0` | TCP connect timeout to a partner |
| `PARTNER_READ_TIMEOUT_SECONDS` | `3.0` | Wait for a partner response after sending |
| `PARTNER_RETRY_MAX_ATTEMPTS` | `3` | Attempts per partner call (retryable outcomes only) |
| `PARTNER_RETRY_BASE_DELAY_MS` | `100` | Backoff base delay (full jitter) |
| `PARTNER_RETRY_MAX_DELAY_MS` | `1000` | Backoff cap per retry |
| `PARTNER_TOTAL_DEADLINE_SECONDS` | `8.0` | Budget for one partner call including retries |
| `CIRCUIT_BREAKER_FAILURE_THRESHOLD` | `5` | Consecutive failures that open a partner's circuit |
| `CIRCUIT_BREAKER_COOLDOWN_SECONDS` | `30` | Wait before a half-open trial request |
| `RATE_LIMIT_TRANSFERS_PER_WINDOW` | `30` | `POST /v1/transfers` per user per window |
| `RATE_LIMIT_WINDOW_SECONDS` | `60` | Rate-limit window length |
| `IDEMPOTENCY_TTL_SECONDS` | `86400` | How long an idempotency key is remembered |
| `IDEMPOTENCY_LOCK_TTL_MS` | `30000` | Redis lock TTL while a request is in progress |
| `RATE_CACHE_TTL_SECONDS` | `60` | Maximum age of a cached route configuration |
| `QUOTE_TTL_SECONDS` | `60` | Validity shown in a quote's `expires_at` |
| `MAX_TRANSFER_POINTS` | `1000000` | Platform-wide cap per transfer (routes have their own maximum too) |
| `ADMIN_API_KEY` | `change-me-admin-key` | `X-Admin-Key` for admin endpoints; must be changed in production |
| `CORS_ALLOW_ORIGINS` | `[]` | JSON list of browser origins; empty disables CORS |
| `RECONCILIATION_INITIAL_DELAY_SECONDS` | `10` | Delay before the first check of an unknown outcome |
| `RECONCILER_INTERVAL_SECONDS` | `5` | Worker polling interval |
| `RECONCILER_BATCH_SIZE` | `50` | Transfers claimed per round |
| `RECONCILIATION_LEASE_SECONDS` | `60` | How long a claimed transfer is reserved for one worker |
| `RECONCILIATION_STUCK_AFTER_SECONDS` | `60` | Age after which an interrupted transfer is recovered |
| `RECONCILIATION_NOT_FOUND_GRACE_SECONDS` | `60` | Age after which "partner has no credit" is trusted |
| `RECONCILIATION_MAX_ATTEMPTS` | `5` | Failed checks (partner unreachable) before manual review |
| `RECONCILIATION_BACKOFF_BASE_SECONDS` | `10` | Backoff between checks: base × 2^(n−1) |
| `RECONCILIATION_BACKOFF_MAX_SECONDS` | `600` | Backoff cap |
| `RECONCILER_METRICS_PORT` | `9100` | Worker `/metrics` port; `0` disables it |
| `HEALTH_CHECK_TIMEOUT_SECONDS` | `2.0` | Time budget for each readiness check |

Startup validation also enforces that the stuck, not-found and lease windows are longer
than `PARTNER_TOTAL_DEADLINE_SECONDS` (otherwise the reconciler could touch a transfer whose
partner call is still in flight) and that `ADMIN_API_KEY` is not the default when
`ENVIRONMENT=production`. The demo script also reads `DEMO_API_URL`,
`DEMO_SIMULATOR_URL` and `DEMO_WORKER_METRICS_URL` (defaults point at the Compose services).

## API reference

All endpoints speak JSON. Errors are RFC 7807 `application/problem+json` with a stable
`code` (see [Error codes](#error-codes)). Every response carries `X-Request-ID` (sent back if
you provide one). The full, generated reference is at <http://localhost:8000/docs>.

**Identity.** Authentication is out of scope: `X-User-Id` stands in for an authenticated
identity and must name an existing user (`user_alice`, `user_bob` after seeding). Admin
endpoints require `X-Admin-Key`.

| Method | Path | Headers | Success | Errors |
| --- | --- | --- | --- | --- |
| GET | `/health/live` | | 200 | |
| GET | `/health/ready` | | 200 | 503 when PostgreSQL or Redis is unavailable |
| GET | `/metrics` | | 200 (Prometheus text) | |
| GET | `/v1/programs` | | 200 | |
| GET | `/v1/accounts` | `X-User-Id` | 200 | 401 |
| POST | `/v1/quotes` | | 200 | 422 |
| POST | `/v1/transfers` | `X-User-Id`, `Idempotency-Key` | 201 final, 202 pending | 400, 401, 409, 422, 429 |
| GET | `/v1/transfers/{transfer_id}` | `X-User-Id` | 200 | 401, 404 |
| GET | `/v1/transfers?limit=&cursor=` | `X-User-Id` | 200 | 400, 401 |
| GET | `/v1/admin/rates?source_program=&destination_program=` | `X-Admin-Key` | 200 | 401 |
| POST | `/v1/admin/rates` | `X-Admin-Key` | 201 | 401, 422 |
| POST | `/v1/admin/bonuses` | `X-Admin-Key` | 201 | 401, 409, 422 |
| POST | `/v1/admin/transfers/{transfer_id}/reconcile` | `X-Admin-Key` | 200 | 401, 404 |

### Programs and balances

```bash
curl -s localhost:8000/v1/programs
# {"data":[{"code":"NOVA_REWARDS","name":"Nova Rewards","type":"CARD","active":true}, ...]}

curl -s localhost:8000/v1/accounts -H "X-User-Id: user_alice"
# {"user_id":"user_alice","data":[{"program":"NOVA_REWARDS","program_name":"Nova Rewards",
#   "program_type":"CARD","balance":250000,"external_member_id":"****0001", ...}, ...]}
```

Member ids are masked to their last four characters.

### Quotes

`POST /v1/quotes` prices a transfer without side effects.

```bash
curl -s -X POST localhost:8000/v1/quotes -H "Content-Type: application/json" \
  -d '{"source_program":"NOVA_REWARDS","destination_program":"SKYWARD_MILES","source_points":10000}'
```

```json
{
  "source_program": "NOVA_REWARDS",
  "destination_program": "SKYWARD_MILES",
  "source_points": 10000,
  "base_points": 10000,
  "bonus_points": 2500,
  "destination_points": 12500,
  "rate": {"rate_id": 1, "version": 1, "numerator": 1, "denominator": 1, "bonus_id": 1, "bonus_bps": 2500},
  "quoted_at": "2026-09-23T14:40:12.786039Z",
  "expires_at": "2026-09-23T14:41:12.786039Z"
}
```

### Transfers

`POST /v1/transfers` body: `{"source_program", "destination_program", "source_points"}`.
Points are strict positive integers (`1000.5` and `"1000"` are rejected); unknown fields are
rejected.

| Status | Meaning |
| --- | --- |
| `201` | Final state reached: `status` is `COMPLETED` or `REVERSED` (then `failure` explains why) |
| `202` | Created, outcome still being verified: `status` is `PENDING_VERIFICATION`; poll `GET /v1/transfers/{id}` |
| `4xx` | Nothing was created |

The transfer resource:

```json
{
  "id": "tr_01M37DXDX944G56J154K1KDC53",
  "status": "COMPLETED",
  "source": {"program": "NOVA_REWARDS", "points": 10000},
  "destination": {"program": "SKYWARD_MILES", "points": 12500, "base_points": 10000, "bonus_points": 2500},
  "rate": {"rate_id": 1, "version": 1, "numerator": 1, "denominator": 1, "bonus_id": 1, "bonus_bps": 2500},
  "failure": null,
  "partner_confirmation_id": "SKYWARD-7C880C70660A",
  "created_at": "2026-09-23T15:24:57.637256Z",
  "updated_at": "2026-09-23T15:24:57.659961Z",
  "completed_at": "2026-09-23T15:24:57.659961Z",
  "events": [
    {"from_status": null, "to_status": "PENDING", "reason": "transfer_requested", "metadata": {}, "created_at": "..."},
    {"from_status": "PENDING", "to_status": "SOURCE_DEBITED", "reason": "source_points_debited", "metadata": {"journal_id": 11}, "created_at": "..."},
    {"from_status": "SOURCE_DEBITED", "to_status": "PARTNER_SUBMITTED", "reason": "partner_credit_requested", "metadata": {"partner": "SKYWARD"}, "created_at": "..."},
    {"from_status": "PARTNER_SUBMITTED", "to_status": "COMPLETED", "reason": "partner_confirmed_credit", "metadata": {"attempts": 1, "partner_outcome": "SUCCESS", "confirmation_id": "SKYWARD-7C880C70660A"}, "created_at": "..."}
  ]
}
```

`GET /v1/transfers` returns `{"data": [...], "next_cursor": ...}` newest first, without
events; pass `next_cursor` back as `?cursor=` for the next page (`limit` 1 to 100, default
20). Another user's transfer returns 404, not 403, so ids cannot be probed.

### Full walkthrough

```bash
# 1. Balances
curl -s localhost:8000/v1/accounts -H "X-User-Id: user_alice"
# NOVA_REWARDS 250000, SKYWARD_MILES 45000, ...

# 2. Quote
curl -s -X POST localhost:8000/v1/quotes -H "Content-Type: application/json" \
  -d '{"source_program":"NOVA_REWARDS","destination_program":"SKYWARD_MILES","source_points":10000}'
# destination_points 12500

# 3. Transfer
curl -s -X POST localhost:8000/v1/transfers \
  -H "Content-Type: application/json" -H "X-User-Id: user_alice" \
  -H "Idempotency-Key: walkthrough-001" \
  -d '{"source_program":"NOVA_REWARDS","destination_program":"SKYWARD_MILES","source_points":10000}'
# HTTP 201, "status": "COMPLETED"

# 4. Idempotent replay: same key, same body -> the stored response, no second debit
curl -s -i -X POST localhost:8000/v1/transfers \
  -H "Content-Type: application/json" -H "X-User-Id: user_alice" \
  -H "Idempotency-Key: walkthrough-001" \
  -d '{"source_program":"NOVA_REWARDS","destination_program":"SKYWARD_MILES","source_points":10000}'
# HTTP/1.1 201 Created
# idempotent-replayed: true

# 5. Key reuse with a different body
curl -s -X POST localhost:8000/v1/transfers \
  -H "Content-Type: application/json" -H "X-User-Id: user_alice" \
  -H "Idempotency-Key: walkthrough-001" \
  -d '{"source_program":"NOVA_REWARDS","destination_program":"SKYWARD_MILES","source_points":20000}'
# HTTP 422, "code": "IDEMPOTENCY_KEY_REUSED"

# 6. One transfer, with its timeline (use the id from step 3)
curl -s localhost:8000/v1/transfers/<transfer_id> -H "X-User-Id: user_alice"

# 7. List
curl -s "localhost:8000/v1/transfers?limit=5" -H "X-User-Id: user_alice"
```

More captured requests and responses: [docs/api-examples.md](docs/api-examples.md).

### Admin

```bash
ADMIN='X-Admin-Key: change-me-admin-key'

# Rate history for a route
curl -s "localhost:8000/v1/admin/rates?source_program=NOVA_REWARDS&destination_program=STAYWELL_POINTS" -H "$ADMIN"

# New rate version (closes the current one at the same instant)
curl -s -X POST localhost:8000/v1/admin/rates -H "$ADMIN" -H "Content-Type: application/json" \
  -d '{"source_program":"NOVA_REWARDS","destination_program":"STAYWELL_POINTS","numerator":3,"denominator":1,
       "min_source_points":1000,"source_increment":1000,"max_source_points":500000}'

# Time-bound bonus (+10%)
curl -s -X POST localhost:8000/v1/admin/bonuses -H "$ADMIN" -H "Content-Type: application/json" \
  -d '{"source_program":"NOVA_REWARDS","destination_program":"STAYWELL_POINTS","bonus_bps":1000,
       "starts_at":"2026-10-01T00:00:00Z","ends_at":"2026-10-15T00:00:00Z"}'

# Reconcile one transfer now (asks the partner if its outcome is unknown)
curl -s -X POST localhost:8000/v1/admin/transfers/<transfer_id>/reconcile -H "$ADMIN"
# {"result": "COMPLETED", "transfer": {...}}   result: COMPLETED, REVERSED, RETRY_SCHEDULED,
#                                              MANUAL_REVIEW, NOT_DUE or ALREADY_RESOLVED
```

## Error codes

Every error body looks like this (extra fields, such as `min_source_points`, are added
where useful):

```json
{
  "type": "urn:loyalty-engine:problem:below-minimum",
  "title": "Amount is below the route minimum",
  "status": 422,
  "detail": "Minimum transfer for this route is 1000 points.",
  "code": "BELOW_MINIMUM",
  "instance": "/v1/quotes",
  "request_id": "54ea66e501074315938f60ed4d360afc",
  "min_source_points": 1000
}
```

| Code | HTTP | Meaning | Client action |
| --- | ---: | --- | --- |
| `VALIDATION_ERROR` | 422 | Malformed body or parameters (see `errors`) | Fix the request |
| `NOT_FOUND` | 404 | Unknown path | Check the URL |
| `METHOD_NOT_ALLOWED` | 405 | Wrong HTTP method (see `Allow`) | Use the allowed method |
| `HTTP_ERROR` | varies | Other protocol-level error | Check the request |
| `AUTHENTICATION_REQUIRED` | 401 | Missing, malformed or unknown `X-User-Id` | Send a valid identity |
| `ADMIN_AUTH_REQUIRED` | 401 | Missing or wrong `X-Admin-Key` | Send the admin key |
| `RATE_LIMITED` | 429 | Too many transfers in the window | Retry after `Retry-After` seconds |
| `INVALID_CURSOR` | 400 | Pagination cursor is not valid | Restart listing without a cursor |
| `IDEMPOTENCY_KEY_REQUIRED` | 400 | `Idempotency-Key` header missing | Send a unique key (e.g. a UUID) |
| `IDEMPOTENCY_KEY_INVALID` | 400 | Key longer than 255 or not printable ASCII | Use a valid key |
| `IDEMPOTENCY_REQUEST_IN_PROGRESS` | 409 | The same key is still being processed | Retry shortly with the same key |
| `IDEMPOTENCY_KEY_REUSED` | 422 | The key was used with a different body | Use a new key for a new transfer |
| `PROGRAM_NOT_FOUND` | 422 | Unknown program code | Check `GET /v1/programs` |
| `PROGRAM_INACTIVE` | 422 | Program not accepting transfers | Choose another program |
| `SAME_PROGRAM` | 422 | Source equals destination | Choose different programs |
| `ROUTE_NOT_SUPPORTED` | 422 | No rate for this direction | Choose a supported route |
| `BELOW_MINIMUM` | 422 | Under the route minimum (`min_source_points`) | Increase the amount |
| `ABOVE_MAXIMUM` | 422 | Over the route or platform maximum (`max_source_points`) | Reduce the amount |
| `INVALID_INCREMENT` | 422 | Not a multiple of `source_increment` | Round to the increment |
| `ZERO_DESTINATION_POINTS` | 422 | Converts to less than one point | Increase the amount |
| `ACCOUNT_NOT_FOUND` | 422 | The user has no account in that program | Link the program first |
| `INSUFFICIENT_BALANCE` | 422 | Not enough points | Transfer less |
| `TRANSFER_NOT_FOUND` | 404 | Unknown transfer, or not yours | Check the id |
| `INVALID_RATE` | 422 | Admin: inconsistent rate definition | Fix the rate |
| `INVALID_BONUS` | 422 | Admin: invalid bonus window | Fix the dates |
| `BONUS_OVERLAP` | 409 | Admin: overlaps another bonus on the route | Change the window |
| `INTERNAL_ERROR` | 500 | Unexpected error; no internals exposed | Retry with the same `Idempotency-Key`; quote `request_id` to support |

The catalogue lives in [app/domain/errors.py](app/domain/errors.py).

## Conversion rate engine

**Formula** (integer math only, [rate_engine.py](app/services/rate_engine.py)):

```text
base        = floor(source_points × numerator / denominator)
bonus       = floor(base × bonus_bps / 10000)        if a bonus is active
destination = base + bonus
```

`numerator / denominator` is destination points per source point: `1:2` (one card point
to two hotel points) is numerator 2, denominator 1; `3:1 reverse` (three miles to one card
point) is numerator 1, denominator 3. Both steps round **down**, so rounding never creates
points; the platform keeps less than one point per transfer. A property-based test proves
the result is never negative and never above the exact value.

**Rules per route:** `min_source_points`, `source_increment` (the amount must be a multiple),
optional `max_source_points`, plus a platform-wide `MAX_TRANSFER_POINTS`. An amount that
converts to zero points is rejected.

**Bonuses** are time-bound percentages in basis points (2500 = +25%) applied to the base
amount; overlapping bonuses on a route are refused by the database.

**Versioning and snapshots.** A rate is never updated. `POST /v1/admin/rates` closes the
current version and opens version + 1 at the same instant, in one transaction; every
transfer stores the `rate_id` and a JSON snapshot (ratio, version, bonus), so later changes
never alter history. Transfers read the rate from PostgreSQL; quotes use a Redis cache that
expires within `RATE_CACHE_TTL_SECONDS`, never outlives a scheduled rate or bonus change,
is cleared on admin changes, and falls back to PostgreSQL if Redis is down.

**Seeded routes** (fictional programs):

| Route | Ratio | Numerator / denominator | Min | Increment | Max | Example |
| --- | --- | --- | ---: | ---: | ---: | --- |
| NOVA_REWARDS → SKYWARD_MILES | 1:1, +25% bonus for 30 days | 1 / 1 | 1,000 | 1,000 | 500,000 | 10,000 → 12,500 |
| NOVA_REWARDS → STAYWELL_POINTS | 1:2 | 2 / 1 | 1,000 | 1,000 | 500,000 | 1,000 → 2,000 |
| ZENITH_POINTS → SKYWARD_MILES | 1:1 | 1 / 1 | 1,000 | 1,000 | 500,000 | 1,000 → 1,000 |
| ZENITH_POINTS → HARBOR_CRUISE_POINTS | 3:2 (rounding visible) | 2 / 3 | 5,000 | 1,000 | 300,000 | 5,000 → 3,333 |
| SKYWARD_MILES → NOVA_REWARDS | 3:1 reverse | 1 / 3 | 3,000 | 3,000 | 300,000 | 9,000 → 3,000 |
| STAYWELL_POINTS → NOVA_REWARDS | 5:1 reverse | 1 / 5 | 5,000 | 5,000 | 500,000 | 5,000 → 1,000 |
| SKYWARD_MILES → ZENITH_POINTS | not supported | | | | | `ROUTE_NOT_SUPPORTED` |

Seeded users: `user_alice` (NOVA 250,000 · ZENITH 120,000 · SKYWARD 45,000 · STAYWELL
80,000 · HARBOR 20,000) and `user_bob` (NOVA 50,000 · ZENITH 300,000 · SKYWARD 150,000 ·
STAYWELL 10,000 · HARBOR 60,000).

**Changing a rate:** see the [admin examples](#admin). The next quote and transfer use the
new version immediately; existing transfers keep their snapshot.

## Transfer lifecycle

![Transfer state machine](docs/images/transfer-states.svg)

| Status | Meaning | Final? |
| --- | --- | --- |
| `PENDING` | Created; exists only inside the debit transaction | |
| `SOURCE_DEBITED` | Source points moved into clearing | |
| `PARTNER_SUBMITTED` | The partner is about to be (or was) called | |
| `PENDING_VERIFICATION` | The partner's answer is unknown; the reconciler will ask it | |
| `COMPLETED` | Partner confirmed; destination credited | yes |
| `REVERSED` | Partner refused or never got the request; source points returned | yes |
| `MANUAL_REVIEW` | Partner unreachable after all verification attempts; an operator decides | for automation |

Transitions outside this diagram are refused in code, and every transition writes an
audit event, returned as the transfer's `events` timeline.

## Atomicity and rollback

A database transaction cannot include a partner's API, so a transfer is a **saga** of short
local transactions ([ADR 0001](docs/adr/0001-saga-with-compensation.md)):

1. **Debit** (one transaction): price the transfer, lock the source account, check the
   balance, create the transfer and move the points into the source program's clearing
   account. Any failure here leaves nothing behind.
2. **Submit**: record that the partner is about to be called.
3. **Call the partner** with **no transaction or lock held**, using the transfer id as an
   idempotent reference.
4. **Outcome** (one transaction): settle on success; on a **definitive failure** (rejected,
   or provably never sent) post a **compensating reversal** that returns the points.

"Automatic rollback" is compensation: the debit is never deleted; a reversal journal
undoes it, and both stay in the ledger. The subtle case is an **unknown outcome**: a
timeout or 5xx after the request was sent. The partner may have applied the credit, so
reversing could pay twice. Instead the points stay in clearing, the API returns `202
PENDING_VERIFICATION`, and the **reconciler** asks the partner by reference: credit found →
settle; not found (after a grace period longer than any in-flight request) → reverse;
unreachable after several attempts → `MANUAL_REVIEW`. It also recovers transfers
interrupted by a crash at any step. See the
[failure sequence diagram](docs/architecture.md#3-failure-paths-and-reconciliation) and
[ADR 0005](docs/adr/0005-synchronous-api-with-async-reconciliation.md).

## Idempotency

Send a unique `Idempotency-Key` (for example a UUID) with every `POST /v1/transfers` and
reuse it when retrying the same request.

| Situation | Response |
| --- | --- |
| First request | Processed normally |
| Same key, same body, finished | The stored status and body, with `Idempotent-Replayed: true` |
| Same key, same body, still running | `409 IDEMPOTENCY_REQUEST_IN_PROGRESS`: retry shortly |
| Same key, different body | `422 IDEMPOTENCY_KEY_REUSED` |
| Missing or invalid key | `400 IDEMPOTENCY_KEY_REQUIRED` / `IDEMPOTENCY_KEY_INVALID` |

- Keys are scoped per user and remembered for `IDEMPOTENCY_TTL_SECONDS` (24 h).
- 2xx responses and 4xx business errors are stored and replayed; 5xx responses are not,
  so the request can be retried with the same key.
- The body is fingerprinted after validation (SHA-256), so JSON formatting and key order do
  not matter.
- A Redis lock stops concurrent duplicates cheaply; the PostgreSQL record is the guarantee
  and keeps working when Redis is down. Transfers are also unique per (user, key).
- Partner calls are idempotent separately: the transfer id is the partner reference.

Details: [ADR 0003](docs/adr/0003-idempotency-redis-lock-and-postgres-record.md).

## Partner integration

Business logic depends only on the adapter interface in [base.py](app/partners/base.py).
Every partner response is classified into one of four outcomes:

| Outcome | Examples | What the saga does |
| --- | --- | --- |
| `SUCCESS` | 200/201 with a confirmation id | settle |
| `REJECTED` | other 4xx (422 unknown member, 409) | reverse |
| `NOT_SENT` | connection refused, connect timeout, 429, circuit open | reverse (nothing was applied) |
| `UNKNOWN` | read timeout, 5xx, dropped connection | verify later; never guessed |

Resilience ([resilience.py](app/partners/resilience.py)):

- **Timeouts:** separate connect (`PARTNER_CONNECT_TIMEOUT_SECONDS`) and read
  (`PARTNER_READ_TIMEOUT_SECONDS`) timeouts, and an overall deadline including retries
  (`PARTNER_TOTAL_DEADLINE_SECONDS`).
- **Retries:** only retryable outcomes, with exponential backoff and full jitter. Safe
  because the reference is idempotent. Once any attempt is `UNKNOWN`, the final outcome is
  never downgraded to `NOT_SENT`.
- **Circuit breaker** per partner: opens after `CIRCUIT_BREAKER_FAILURE_THRESHOLD`
  consecutive failures, fails fast while open, allows one trial request after
  `CIRCUIT_BREAKER_COOLDOWN_SECONDS`. Business rejections do not count as failures.

### Simulating partner failures

The simulator (port 8001) supports eight modes per partner. Partner codes: `NOVA`,
`ZENITH`, `SKYWARD`, `STAYWELL`, `HARBOR`.

```bash
curl -s -X POST localhost:8001/simulator/config -H "Content-Type: application/json" \
  -d '{"partner_code":"SKYWARD","mode":"success"}'
curl -s -X POST localhost:8001/simulator/config -H "Content-Type: application/json" \
  -d '{"partner_code":"SKYWARD","mode":"reject"}'
curl -s -X POST localhost:8001/simulator/config -H "Content-Type: application/json" \
  -d '{"partner_code":"SKYWARD","mode":"error"}'
curl -s -X POST localhost:8001/simulator/config -H "Content-Type: application/json" \
  -d '{"partner_code":"SKYWARD","mode":"unavailable"}'
curl -s -X POST localhost:8001/simulator/config -H "Content-Type: application/json" \
  -d '{"partner_code":"SKYWARD","mode":"timeout","delay_ms":10000}'
curl -s -X POST localhost:8001/simulator/config -H "Content-Type: application/json" \
  -d '{"partner_code":"SKYWARD","mode":"timeout_after_commit","delay_ms":10000}'
curl -s -X POST localhost:8001/simulator/config -H "Content-Type: application/json" \
  -d '{"partner_code":"SKYWARD","mode":"slow","delay_ms":1000}'
curl -s -X POST localhost:8001/simulator/config -H "Content-Type: application/json" \
  -d '{"partner_code":"SKYWARD","mode":"flaky","failure_rate":0.3}'

curl -s localhost:8001/simulator/config        # current modes
curl -s localhost:8001/simulator/credits       # every credit applied at the partner
curl -s -X POST localhost:8001/simulator/reset # clear credits and modes
```

| Mode | Partner behaviour | Result of a transfer |
| --- | --- | --- |
| `success` | applies the credit | `COMPLETED` |
| `reject` | 422, nothing applied | `REVERSED` (`PARTNER_REJECTED`) |
| `error` / `unavailable` | 500 / 503, nothing applied | retried; then `PENDING_VERIFICATION` → reconciler |
| `timeout` | too slow, nothing applied | `PENDING_VERIFICATION` → `REVERSED` after the grace period |
| `timeout_after_commit` | applies, then answers too late (replays too) | `PENDING_VERIFICATION` → `COMPLETED` by the reconciler |
| `slow` | delayed but within the timeout | `COMPLETED` |
| `flaky` | random 500s at `failure_rate` | mixed; all resolve |

Full description: [docs/partner-simulator.md](docs/partner-simulator.md).

## Concurrency and consistency guarantees

- **No double spending.** The source account is locked (`SELECT ... FOR NO KEY UPDATE`)
  before the balance check, and `CHECK (balance >= 0)` on user accounts is the final guard
  even if code skipped the check. 20 concurrent transfers against a balance that covers 10:
  exactly 10 succeed and the balance ends at exactly 0.
- **No deadlocks.** Every transaction locks the transfer row first, then accounts in
  ascending id order. Concurrent transfers in opposite directions all complete (tested).
- **No double settlement.** At most one journal of each type per transfer (unique
  constraint); every status change re-checks the status under a row lock; reconcilers
  claim work with `FOR UPDATE SKIP LOCKED` and a lease.
- **Ledger invariants,** enforced by the database and verified on demand:
  - every program's balances sum to zero (points are never created or destroyed);
  - every journal balances within each program;
  - every account balance equals the sum of its entries;
  - no user balance is negative.

```bash
make check-invariants
# OK: all 4 ledger invariants hold (20 accounts, 10 journals)
```

Details: [docs/ledger-invariants.md](docs/ledger-invariants.md),
[ADR 0002](docs/adr/0002-double-entry-ledger.md).

## Observability

**Logs** are structured (JSON in Docker, readable locally). Every line carries
`request_id`; lines inside a request also carry `user_id`, and saga lines carry
`transfer_id`. Member ids and secrets are never logged.

```json
{"transfer_id": "tr_01M37HJ854F5E3STWRJEEGBS4Z", "from_status": "PARTNER_SUBMITTED", "to_status": "PENDING_VERIFICATION", "reason": "partner_outcome_unknown", "event": "transfer_status_changed", "request_id": "726385a4a3aa43eeb5197489664bb310", "user_id": "user_alice", "level": "info", "logger": "app.domain.transfer_state", "timestamp": "2026-09-23T16:28:53.622575Z"}
```

**Metrics** (Prometheus): the API at `/metrics`, the worker at `:9100/metrics`.

| Metric | Type | Labels | Served by |
| --- | --- | --- | --- |
| `transfers_total` | counter | `status`, `route` | API, worker |
| `transfer_duration_seconds` | histogram | `status` | API |
| `partner_requests_total` | counter | `partner`, `operation`, `outcome` | API, worker |
| `partner_request_duration_seconds` | histogram | `partner`, `operation` | API, worker |
| `circuit_breaker_state` | gauge (0 closed, 1 half-open, 2 open) | `partner` | API, worker |
| `idempotent_replays_total` | counter | | API |
| `idempotency_conflicts_total` | counter | `reason` | API |
| `rate_limit_rejections_total` | counter | | API |
| `reconciliation_resolved_total` | counter | `result` | worker |
| `transfers_pending_verification` | gauge | | worker |
| `transfers_manual_review` | gauge | | worker |

**Suggested alerts:** `transfers_manual_review > 0` (an operator is needed);
`transfers_pending_verification` rising for 15 minutes (a partner is not settling);
`circuit_breaker_state == 2` for 5 minutes (a partner is down); a spike in
`rate(partner_requests_total{outcome="UNKNOWN"}[5m])` (timeouts or 5xx); a spike in
`transfers_total{status="REVERSED"}` (rejections).

**Health:** `/health/live` (process up, no dependency checks) and `/health/ready`
(PostgreSQL and Redis reachable; 503 otherwise).

## Testing

```bash
make test               # all 363 tests (about 40 s)
make test-unit          # 229 unit tests, no services (about 1 s)
make test-integration   # integration tests against PostgreSQL, Redis and the simulator
make coverage           # coverage report; fails below 90% (currently 96%)
```

Integration tests use their own database (`loyalty_test`, recreated and migrated at the
start of each run) and Redis database 1, so development data is never touched. After every
test that moves points, the ledger invariants are checked.

What the hardest tests prove:

- **Concurrency:** 20 concurrent transfers never overdraw; 10 concurrent requests with one
  key create exactly one transfer; opposite-direction transfers never deadlock; two
  reconcilers never settle twice; the API and the reconciler racing on one transfer settle
  it once.
- **Chaos:** 50 concurrent transfers against four misbehaving partners (flaky, late,
  rejecting) with a live reconciler. Every transfer ends `COMPLETED` or `REVERSED`, the
  partner holds a credit exactly for the completed ones, every balance equals its start
  plus the completed transfers, and all four recovery paths are exercised.
- **Crash recovery** at each saga step, **Redis outages**, **property-based** rate math, and
  the **database** refusing invalid ledger writes.

Layout: `tests/unit/` (no services) and `tests/integration/`; markers `unit` and
`integration` are applied by directory. More: [docs/testing.md](docs/testing.md). CI
([ci.yml](.github/workflows/ci.yml)) runs lint, type checks, unit tests, integration tests
with coverage (PostgreSQL and Redis service containers) and a Docker build on every push.

## Database

Schema: [docs/architecture.md#5-database](docs/architecture.md#5-database) and
[app/db/models.py](app/db/models.py). Tables: `programs`, `conversion_rates`,
`transfer_bonuses`, `users`, `accounts`, `ledger_journals`, `ledger_entries`, `transfers`,
`transfer_events`, `idempotency_keys`.

**Migrations** (Alembic, in `migrations/versions/`):

```bash
# 1. change app/db/models.py, then generate a migration
make makemigration name=add_program_region
# 2. review the generated file (autogenerate misses triggers and some constraints)
# 3. apply it
make migrate
# roll back the latest migration
docker compose run --rm tools alembic downgrade -1
# show the current revision
docker compose run --rm tools alembic current
```

**Resetting data:** `make reset` drops the database volume, then migrates and seeds.
`make seed` alone is idempotent and only adds what is missing.

**Useful queries** (`make psql`):

```sql
-- Balances per user and program
SELECT a.user_id, p.code, a.balance
FROM accounts a JOIN programs p ON p.id = a.program_id
WHERE a.owner_type = 'USER' ORDER BY a.user_id, p.code;

-- Transfers by status
SELECT status, count(*) FROM transfers GROUP BY status ORDER BY status;

-- Transfers waiting for verification or an operator
SELECT id, status, verification_attempts, next_verification_at, failure_code
FROM transfers WHERE status IN ('PENDING_VERIFICATION', 'MANUAL_REVIEW');

-- The timeline of one transfer
SELECT created_at, from_status, to_status, reason FROM transfer_events
WHERE transfer_id = 'tr_...' ORDER BY id;

-- Invariant: every program sums to zero (expect no rows)
SELECT p.code, SUM(a.balance) FROM accounts a JOIN programs p ON p.id = a.program_id
GROUP BY p.code HAVING SUM(a.balance) <> 0;
```

## Developer guide

**Adding a loyalty or card program.** Add it to `PROGRAMS` (and its routes to `RATES`) in
[scripts/seed.py](scripts/seed.py) and run `make seed`; seeding is idempotent and creates the
program, its `TRANSFER_CLEARING` and `PARTNER_SETTLEMENT` accounts and version 1 of each
route. Rates can then be changed through the admin API. If the partner speaks the standard
credit API, nothing else is needed: its `partner_code` is routed to the default adapter.

**Adding a partner with a different API.** Implement `PartnerAdapter`
([base.py](app/partners/base.py)): `credit_points` (idempotent on `reference`) and
`get_credit_status`, returning the normalized results, never raw HTTP. Map its responses to
`SUCCESS`, `REJECTED`, `NOT_SENT` or `UNKNOWN`; when in doubt, `UNKNOWN`. Wrap it in
`ResilientPartnerAdapter` and register it in `build_partner_registry`
([registry.py](app/partners/registry.py)) under its `partner_code`. Add unit tests for the
classification with respx. The saga and the reconciler need no change.

**Adding or changing a rate.** Use `POST /v1/admin/rates`; never edit rates in the database.

**Adding a migration.** Change `app/db/models.py`, run `make makemigration name=...`,
review the file (name constraints explicitly, add triggers by hand), run `make migrate` and
`make test` (integration tests rebuild their database from all migrations).

**Code style.** `make format`, `make lint` and `make typecheck` must pass: ruff (line length
100) and mypy in strict mode. Points are always `int`; never use floats for points or
rates. Services never commit on behalf of their callers; transactions are explicit
(`unit_of_work`). Never hold a transaction open across a network call.

**Branches and commits.** Branch from `master` (`feat/...`, `fix/...`, `docs/...`). Commit
messages follow Conventional Commits: `feat: add ...`, `fix: ...`, `test: ...`, `docs: ...`,
one logical change per commit.

**Pull request checklist:**

- [ ] `make lint`, `make typecheck`, `make test` pass; `make coverage` stays above 90%
- [ ] New behaviour has tests, including its failure paths
- [ ] Money paths use integers and the ledger; no balance is written directly
- [ ] No transaction or lock is held during a partner call
- [ ] New settings are in `app/config.py`, `.env.example` and this README
- [ ] New error codes are in `app/domain/errors.py` and this README
- [ ] Docs updated (architecture, failure scenarios) if behaviour changed; `make diagrams`

See also [CONTRIBUTING.md](CONTRIBUTING.md).

## Assumptions

From the brief ([docs/PROJECT_BRIEF.md](docs/PROJECT_BRIEF.md)):

- **A1** The service keeps its own double-entry ledger of user balances per program. The
  source debit happens in the ledger; the destination credit happens through a partner
  API. Every program has ledger accounts and a partner adapter, so both directions use the
  same code path.
- **A2** Partner APIs are mocked by a separate simulator service reached over real HTTP,
  with configurable failure modes.
- **A3** Transfers execute synchronously with a bounded timeout; an unknown partner outcome
  returns 202 and is resolved by a background reconciler.
- **A4** Authentication is out of scope: `X-User-Id` stands in for an authenticated identity,
  and admin endpoints use `X-Admin-Key`.
- **A5** Points are integers (`BIGINT`); floats are never used for points or rates.

Made during implementation:

- An `X-User-Id` that does not name an existing user is treated as unauthenticated (401),
  as a real token check would be.
- The destination member id comes from the user's linked account in that program.
- A reversed transfer returns `201` (a final state was reached); the `status` field carries
  the business outcome ([ADR 0007](docs/adr/0007-http-status-for-transfer-outcomes.md)).
- A platform-wide cap (`MAX_TRANSFER_POINTS`) applies on top of each route's maximum.
- The seeded programs, companies and member ids are fictional.

## Trade-offs and limitations

| Limitation | Why it is acceptable here |
| --- | --- |
| No real authentication | Out of scope per the brief; identity is isolated in one dependency, so a JWT/OAuth check replaces it |
| Mocked partners | Real partner APIs were not available; the simulator reproduces their failure modes over real HTTP, and adapters isolate the integration |
| In-process circuit breaker state | Each API instance protects itself; shared state (Redis) is a small change if needed |
| Synchronous API with a bounded wait | Most transfers finish in one request; the rare unknown case returns 202 honestly |
| Per-program clearing and settlement rows are hot rows | They serialise transfers within a program; fine at this scale, could be sharded or derived from entries |
| Fixed-window rate limiting | Simple and cheap; allows short bursts across window edges |
| Rate limiter fails open when Redis is down | It protects capacity, not money; idempotency and the ledger do not depend on Redis |
| Single region, single database | Appropriate for a take-home; see future improvements |
| A partner applying a request long after our grace period | Cannot be ruled out without partner cooperation; documented in [failure-scenarios.md](docs/failure-scenarios.md) |

## Production readiness and future improvements

- **Authentication and authorization:** OAuth2/JWT from an identity provider replacing
  `X-User-Id`; scoped admin roles with an audit log of admin actions.
- **Outbox pattern and events:** publish `transfer.completed` / `transfer.reversed` events
  through a transactional outbox (for notifications, analytics, partner settlement files).
- **Partner webhooks and void-by-reference:** accept partner callbacks to resolve unknown
  outcomes faster, and call a void API before reversing, closing the residual risk.
- **Generalized saga for card issuers:** a partner-side debit step for card programs where
  the issuer, not this service, holds the balance.
- **Shared circuit-breaker state** in Redis across API instances.
- **Secrets management** (Vault, AWS Secrets Manager) instead of environment variables;
  key rotation for the admin key.
- **Load and soak testing** (k6 or Locust) to size the pools and measure hot-row contention;
  contract tests against real partner sandboxes.
- **Multi-region:** a single-writer PostgreSQL primary with read replicas for queries; the
  reconciler runs in any region thanks to `SKIP LOCKED`.
- **Operations UI** for `MANUAL_REVIEW`: inspect the timeline and the partner's answer, then
  complete or reverse with a recorded reason.
- **Settlement reconciliation** with partners (daily files versus `PARTNER_SETTLEMENT`).

## Troubleshooting

| Problem | Fix |
| --- | --- |
| `port is already allocated` on `make up` | Another service uses 5432, 6379, 8000, 8001 or 9100. Change `POSTGRES_HOST_PORT`, `REDIS_HOST_PORT`, `API_HOST_PORT`, `SIMULATOR_HOST_PORT` or `WORKER_METRICS_HOST_PORT` in `.env`, or stop the other service. |
| `Cannot connect to the Docker daemon` | Start Docker Desktop (or `sudo systemctl start docker`). |
| `make up` fails with a container unhealthy | `make ps`, then `make logs s=<service>`. Run `make up` again; a first start can race while images are rebuilt. |
| `relation "..." does not exist` | The schema is missing: `make migrate`, then `make seed`. |
| Migration errors after switching branches | Start clean: `make reset` (drops all data). |
| `401 AUTHENTICATION_REQUIRED` | Send `X-User-Id: user_alice` (or `user_bob`); run `make seed` if the users are missing. |
| Transfers stay `PENDING_VERIFICATION` | Check the worker: `make ps`, `make logs s=worker`. It first checks after `RECONCILIATION_INITIAL_DELAY_SECONDS` and reverses "not found" credits only after `RECONCILIATION_NOT_FOUND_GRACE_SECONDS`. |
| Every transfer is `REVERSED` with `PARTNER_REJECTED` | A simulator mode is still set: `curl -s localhost:8001/simulator/config`, then `curl -s -X POST localhost:8001/simulator/reset`. |
| Redis connection errors in logs | `make ps` to check Redis. Quotes, idempotency and rate limiting keep working without it (degraded); restart with `make up`. |
| The demo fails at step 8 | The worker is not running or not yet healthy: `make up`, `make logs s=worker`. |
| Full reset of everything | `make down`, `docker compose down --volumes --remove-orphans`, then `make build && make reset`. |

Useful log views: `make logs s=worker` (reconciliation rounds), `make logs s=api`
(requests and saga steps), `make logs s=partner-simulator` (what each partner received).

## Author

Aakash Limbani · akashlimbani22@gmail.com

Built as the Track 2 take-home for Optivoy: a bi-directional loyalty points transfer
engine.
