# Optivoy Track 2 — Loyalty Points Transfer Engine

## Step-by-Step Prompt Playbook

This playbook turns the Track 2 assignment into a sequence of prompts you can give to an AI coding assistant (Claude Code, Cursor, or similar), one step at a time. Each prompt builds on the previous one. By the end you will have a production-style microservice, an architecture diagram, a full test suite, and a complete README with every command and developer guideline.

---

## 0. Read This First

### 0.1 What Optivoy actually wants to see

Meher's task lists four features and two deliverables, but the real evaluation is about **engineering judgment in a money-like domain**. Loyalty points behave like currency: losing, duplicating, or miscalculating them is a real financial and trust problem. The reviewer will read your code and ask one question over and over: *"What happens when something goes wrong?"*

| Client requirement | What they are really checking | Where your submission proves it |
| --- | --- | --- |
| Bi-directional conversion | You understand the domain: card → loyalty and loyalty → card are different routes with different rules | Rate table keyed by direction, disabled routes, asymmetric reverse rates |
| Flexible conversion rate engine | Correct, auditable math; no floats; rules can change without code changes | Integer ratios, minimums, increments, time-bound bonuses, rate versioning, rate snapshot on every transfer |
| Atomic transactions with automatic rollback | You know a DB transaction cannot cover an external API call | Local ACID transaction for the debit, saga with compensation for the partner step, explicit "unknown outcome" state, reconciliation worker |
| Idempotency keys | Retries never move points twice | `Idempotency-Key` header, request fingerprinting, Redis lock + PostgreSQL durable record, idempotent partner references |
| Partner API integration | You isolate external dependencies and handle their failures | Adapter interface, mock partner service with failure modes, timeouts, retries, circuit breaker |
| PostgreSQL | Schema design, constraints, locking, transactions | Double-entry ledger, `CHECK` constraints, `SELECT … FOR UPDATE`, `SKIP LOCKED`, migrations |
| Redis | You use it for the right jobs, not as the source of truth | Idempotency locks, rate cache, rate limiting |
| REST | Clean, predictable API design | Versioned endpoints, proper status codes, RFC 7807 errors, OpenAPI docs |
| Architecture diagram | You can communicate a design | Component, sequence, state machine, and ER diagrams |
| Complete source code | Production readiness | Tests, Docker, one-command setup, logging, metrics, README |

### 0.2 Language decision

The task allows **Python or Go only** (not Node.js). This playbook uses **Python 3.12 + FastAPI** because it is the fastest path to a clean, well-documented result within one week, and its syntax is close to TypeScript's async style. If you choose Go instead, change the stack line in Prompt 1 (suggested Go stack: `chi` router, `pgx` for PostgreSQL, `go-redis`, `golang-migrate`, `testify`) and keep every other requirement the same.

### 0.3 How to use these prompts

1. Run the prompts **in order**, one per session or step. Do not paste them all at once.
2. After each step, **run the verification commands** listed in the prompt yourself. Do not move on until they pass.
3. **Read and understand every file generated.** In the next interview round, Omar or the team will ask you to explain your design decisions. You must be able to defend every line. Each step ends with "Be ready to explain" points for this reason.
4. **Commit after every step** with a clear message (e.g., `feat: add conversion rate engine with versioning and bonuses`). Reviewers often read git history; a clean, incremental history signals discipline.
5. If the AI assistant deviates from the brief (for example, uses floats for points or puts the partner call inside a DB transaction), stop it and point it back to `docs/PROJECT_BRIEF.md`.

### 0.4 Recommended 7-day schedule

| Day | Prompts | Outcome |
| --- | --- | --- |
| Day 1 | 1, 2 | Project skeleton running in Docker, schema migrated, seed data loaded |
| Day 2 | 3, 4 | Rate engine and quotes working, ledger service with invariant tests |
| Day 3 | 5, 6 | Partner simulator + resilient adapter, idempotency layer |
| Day 4 | 7 | End-to-end transfer saga working in both directions |
| Day 5 | 8, 9 | Reconciliation worker, observability, error handling, rate limiting |
| Day 6 | 10, 11 | Full test suite, architecture diagrams and design docs |
| Day 7 | 12, 13 | README complete, final self-review, submission |

Aim to submit on Day 6 or early Day 7. Submitting slightly early, with polish, reads better than submitting at the deadline.

---

## 1. Optional: Clarification Email to Meher

Meher invited questions. If you have not already received answers, send this on Day 1 and continue building with the stated assumptions (they are designed so that any answer only needs small changes).

```text
Subject: Track 2 – a few quick clarifications

Hi Meher,

I've started on Track 2 (Bi-Directional Loyalty Points Transfer Engine). To make sure
I'm solving the right problem, I have a few quick questions. I'll proceed with the
assumptions below unless you'd prefer otherwise:

1. Balances: I'm assuming the service maintains its own ledger of user balances per
   program, and credits the destination program through a partner API. Would you
   prefer both sides to be treated as external partner APIs?
2. Partner APIs: Since real partner APIs aren't available, I plan to build a mock
   partner service that can simulate failures, timeouts, and slow responses. Is that okay?
3. Execution model: I plan to process transfers synchronously with a bounded timeout,
   returning 202 with a pending status if the partner outcome is unknown, and resolving
   it through a background reconciliation process.
4. Authentication: I plan to keep authentication out of scope and use a user-identity
   header as a stand-in, documented as such.
5. Language: I'm planning to use Python (FastAPI). Is there a preference between
   Python and Go?

Thanks, and happy to adjust if any of these don't match your expectations.

Best regards,
Aakash Limbani
```

---

## 2. The Project Brief (used by every prompt)

Prompt 1 asks the assistant to save the brief below as `docs/PROJECT_BRIEF.md`. Every later prompt tells the assistant to re-read it, which keeps the design consistent across sessions. You do not need to paste this section separately; it is embedded in Prompt 1.

---

## PROMPT 1 — Project Brief, Scaffold, and Local Environment

```text
You are a senior backend engineer helping me build a take-home assignment for Optivoy,
an early-stage AI travel and loyalty platform. The reviewers will judge production
readiness, correctness under failure, and engineering judgment — not just whether it works.

ASSIGNMENT (from the client, verbatim):
"Track 2: Backend — Bi-Directional Loyalty Points Transfer Engine.
Build a microservice that handles bi-directional point conversions between credit cards
and travel loyalty programs.
Key features: flexible conversion rate engine; atomic transactions with automatic
rollback on failure; idempotency keys; partner API integration.
Tech stack: Python/Go, PostgreSQL, Redis, REST.
Deliverables: architecture diagram, complete source code."

STEP 1 GOAL: create the project brief, the repository skeleton, and a working local
environment. Do NOT implement business logic yet.

1) Create docs/PROJECT_BRIEF.md with exactly the following content (this is the
single source of truth for all later steps):

--- BEGIN PROJECT_BRIEF.md ---
# Project Brief — Loyalty Points Transfer Engine

## Stack
Python 3.12, FastAPI, Pydantic v2, pydantic-settings, SQLAlchemy 2.0 (async) with asyncpg,
Alembic, redis-py (asyncio), httpx, structlog, prometheus-client, pytest + pytest-asyncio,
ruff, mypy. Docker + Docker Compose. Makefile for all commands.

## Core assumptions (documented, can be changed)
A1. The service keeps an internal double-entry ledger of user balances per loyalty program.
    The source debit happens in our ledger; the destination credit happens through a
    partner API. Every program has both local ledger accounts and a partner adapter, so
    both directions (card -> loyalty, loyalty -> card) use the same code path.
A2. Partner APIs are mocked by a separate "partner simulator" service reached over real
    HTTP, with configurable failure modes.
A3. Transfers execute synchronously with a bounded timeout. If the partner outcome is
    unknown, the API returns 202 and a background reconciler resolves the transfer.
A4. Authentication is out of scope. The X-User-Id header stands in for an authenticated
    identity. Admin endpoints require X-Admin-Key.
A5. Points are always integers (BIGINT). Floats are never used for points or rates.

## Domain rules
- Programs: code, name, type (CARD or LOYALTY), partner_code, active flag.
- Conversion rates are keyed by (source_program, destination_program). Direction matters:
  A->B and B->A are separate rows with separate rules. A missing or inactive row means
  the route is not supported.
- Rate fields: numerator, denominator (integers), min_source_points, source_increment,
  max_source_points, effective_from, effective_to (nullable), version, active.
- Calculation: base = floor(source_points * numerator / denominator);
  bonus = floor(base * bonus_bps / 10000) if an active bonus exists; destination = base + bonus.
  Reject if source_points < min, > max, not a multiple of increment, or destination == 0.
- Rate versioning: creating a new rate for a route closes the previous version
  (sets effective_to) instead of updating it. Every transfer stores rate_id and a JSON
  rate_snapshot (ratio, bonus, version) so history never changes.
- Transfer bonuses: (route, bonus_bps, starts_at, ends_at), time-bound.

## Ledger
- accounts: user balance accounts (one per user per program, with external_member_id),
  plus SYSTEM accounts per program: TRANSFER_CLEARING and PARTNER_SETTLEMENT.
- User account balances have CHECK (balance >= 0). System accounts may be negative.
- ledger_entries are immutable (insert only). Each journal is balanced per program:
  total debits == total credits within the same program (points of different programs
  are different units and are never mixed in one balance equation).
- Invariant: for each program, the sum of all account balances equals the seeded total.

## Transfer saga (state machine)
PENDING -> SOURCE_DEBITED -> PARTNER_SUBMITTED -> COMPLETED
PARTNER_SUBMITTED -> REVERSED            (definitive partner failure; points returned)
PARTNER_SUBMITTED -> PENDING_VERIFICATION (outcome unknown: timeout / 5xx)
PENDING_VERIFICATION -> COMPLETED | REVERSED | MANUAL_REVIEW
SOURCE_DEBITED -> REVERSED               (request never reached partner, e.g. circuit open)
Every transition is validated against an allowed-transitions map and recorded in
transfer_events (audit trail). Invalid transitions raise an error.

Flow:
1. Validate request, compute quote with the active rate.
2. DB transaction: lock source account (SELECT ... FOR UPDATE), check balance,
   create transfer, journal: debit user source account, credit source TRANSFER_CLEARING.
   Commit. Status SOURCE_DEBITED.
3. Call partner credit OUTSIDE any DB transaction, with partner_reference = transfer id
   (partner side is idempotent on reference). Timeout, retries with exponential backoff
   and jitter for retryable errors, circuit breaker.
4a. Success: DB transaction: source clearing -> source PARTNER_SETTLEMENT; destination
    PARTNER_SETTLEMENT -> user destination account; store confirmation id; COMPLETED.
4b. Definitive failure (partner 4xx business rejection, or request provably never sent):
    DB transaction: reversal journal clearing -> user source account; REVERSED with
    failure_code.
4c. Unknown outcome (timeout, 5xx after retries): PENDING_VERIFICATION with
    next_verification_at. The reconciler queries partner status by reference and completes
    or reverses. After max attempts: MANUAL_REVIEW (logged + metric).
Never hold a DB transaction or row lock while calling a partner.

## Idempotency
- POST /v1/transfers requires the Idempotency-Key header (400 if missing).
- Scope: (user_id, key). Fingerprint: SHA-256 of method + path + canonical JSON body.
- Redis SET NX lock with TTL guards concurrent requests with the same key -> 409
  IDEMPOTENCY_REQUEST_IN_PROGRESS.
- PostgreSQL idempotency_keys table is the durable record (unique on scope + key).
- Same key + same fingerprint + completed -> replay stored status code and body,
  with header Idempotent-Replayed: true.
- Same key + different fingerprint -> 422 IDEMPOTENCY_KEY_REUSED.
- Store final responses (2xx and 4xx business errors). Do not store 5xx; release the key
  so the client can retry. Keys expire after 24 hours.
- Extra safety net: UNIQUE (user_id, idempotency_key) on transfers.

## Redis usage
Idempotency locks, active-rate cache (invalidated when a rate/bonus changes),
per-user rate limiting on POST /v1/transfers (fixed window, 429 when exceeded).
PostgreSQL is always the source of truth.

## API (all JSON, errors as RFC 7807 application/problem+json with a stable "code")
GET  /health/live, GET /health/ready (checks PostgreSQL and Redis)
GET  /metrics (Prometheus)
GET  /v1/programs
GET  /v1/accounts                      (current user's balances)
POST /v1/quotes                        (no side effects)
POST /v1/transfers                     (Idempotency-Key required)
GET  /v1/transfers/{transfer_id}       (includes events timeline)
GET  /v1/transfers                     (cursor pagination, newest first)
GET  /v1/admin/rates, POST /v1/admin/rates, POST /v1/admin/bonuses
POST /v1/admin/transfers/{transfer_id}/reconcile
HTTP status for POST /v1/transfers: 201 when a transfer reaches a final state
(COMPLETED or REVERSED — the "status" field is the source of truth), 202 when
PENDING_VERIFICATION, 4xx for validation/business errors (no transfer created).

## Partner simulator (separate FastAPI app, separate container)
POST /partners/{partner_code}/credits  {reference, member_id, points}
  -> 201 {confirmation_id, status}; idempotent on reference.
GET  /partners/{partner_code}/credits/{reference} -> 200 status or 404.
POST /simulator/config {partner_code, mode, delay_ms} and GET /simulator/config.
Modes: success, reject (422), error (500), unavailable (503), timeout (sleep longer than
client timeout, credit NOT applied), timeout_after_commit (credit applied, response
delayed beyond client timeout), slow (delay but within timeout), flaky (random 500s).

## Quality bar
Type hints everywhere, small focused modules, dependency injection via FastAPI deps,
structured JSON logs with request_id and transfer_id, meaningful tests for every failure
path, one-command setup, and a README a new developer can follow without asking questions.
--- END PROJECT_BRIEF.md ---

2) Create this repository structure (empty modules with docstrings are fine for now):
app/ (main.py, config.py, api/, domain/, services/, partners/, db/, cache/,
observability/, workers/), partner_simulator/, migrations/, scripts/, tests/unit/,
tests/integration/, docs/, docs/adr/.

3) Tooling:
- pyproject.toml with dependencies and dev dependencies, ruff and mypy config,
  pytest config (asyncio mode auto).
- Dockerfile (multi-stage, non-root user, slim image) used by api, worker, and simulator.
- docker-compose.yml with services: postgres:16, redis:7, api, worker (placeholder
  command for now), partner-simulator. Healthchecks on postgres and redis;
  api depends on healthy services. Named volume for postgres.
- .env.example with every setting documented by a comment. .gitignore, .dockerignore.
- app/config.py using pydantic-settings (database URL, redis URL, partner base URL,
  partner timeout, retry settings, circuit breaker settings, rate limit settings,
  idempotency TTL, admin key, log level).
- Makefile targets: help (default, lists all targets with descriptions), up, down,
  logs, ps, build, shell, migrate, makemigration name=..., seed, reset (drop volumes,
  up, migrate, seed), test, test-unit, test-integration, coverage, lint, format,
  typecheck, demo (placeholder), run-local (uvicorn without Docker).
- Structured logging with structlog (JSON in Docker, pretty locally) and a middleware
  that assigns/propagates X-Request-ID.
- /health/live and /health/ready implemented for real (ready checks DB and Redis).
- Partner simulator app skeleton with a /health endpoint.

4) Create a minimal README.md placeholder with a "Quick start" section only;
the full README comes in a later step.

CONSTRAINTS: Do not implement business logic. Do not add libraries outside the brief
without explaining why. Keep everything runnable.

VERIFY (tell me the exact commands and expected output):
cp .env.example .env && make build && make up && make ps
curl localhost:8000/health/ready  -> {"status":"ok", checks for db and redis}
curl localhost:8001/health        -> simulator ok
make lint && make typecheck
```

**Be ready to explain:** why PostgreSQL is the source of truth and Redis is not; why the partner simulator is a separate service reached over HTTP instead of an in-process mock; why points are integers.

---

## PROMPT 2 — Database Schema, Migrations, and Seed Data

```text
Read docs/PROJECT_BRIEF.md first and follow it strictly.

STEP 2 GOAL: design and migrate the full PostgreSQL schema, and create seed data.

1) SQLAlchemy 2.0 models (app/db/models.py) and an Alembic migration for:
- users (id, created_at)
- programs (id, code UNIQUE, name, type CHECK IN ('CARD','LOYALTY'), partner_code, active)
- conversion_rates (id, source_program_id, destination_program_id, numerator > 0,
  denominator > 0, min_source_points >= 1, source_increment >= 1, max_source_points,
  version, effective_from, effective_to NULL, active, created_at)
  CHECK source != destination. Index for finding the active rate for a route.
  Prevent two open versions for the same route (partial unique index where
  effective_to IS NULL AND active).
- transfer_bonuses (id, source_program_id, destination_program_id, bonus_bps > 0,
  starts_at, ends_at, CHECK ends_at > starts_at)
- accounts (id, owner_type CHECK IN ('USER','SYSTEM'), user_id NULL, program_id,
  account_type CHECK IN ('USER_BALANCE','TRANSFER_CLEARING','PARTNER_SETTLEMENT'),
  external_member_id NULL, balance BIGINT, created_at, updated_at)
  CHECK: user accounts balance >= 0. UNIQUE (user_id, program_id) for user accounts;
  UNIQUE (program_id, account_type) for system accounts.
- ledger_journals (id, transfer_id NULL, journal_type e.g. TRANSFER_DEBIT,
  TRANSFER_SETTLE, TRANSFER_REVERSAL, SEED, created_at)
- ledger_entries (id, journal_id, account_id, program_id, direction CHECK IN
  ('DEBIT','CREDIT'), amount BIGINT CHECK > 0, created_at). Insert-only.
- transfers (id text PK with 'tr_' prefix + ULID, user_id, idempotency_key,
  source_program_id, destination_program_id, source_account_id, destination_account_id,
  source_points, base_points, bonus_points, destination_points, rate_id,
  rate_snapshot JSONB, status CHECK IN (all brief states), failure_code,
  failure_message, partner_reference, partner_confirmation_id,
  verification_attempts default 0, next_verification_at NULL,
  created_at, updated_at, completed_at NULL)
  UNIQUE (user_id, idempotency_key). Index (user_id, created_at DESC).
  Partial index on (status, next_verification_at) for reconcilable statuses.
- transfer_events (id, transfer_id, from_status NULL, to_status, reason, metadata JSONB,
  created_at)
- idempotency_keys (id, user_id, key, request_fingerprint, status CHECK IN
  ('IN_PROGRESS','COMPLETED'), response_status_code NULL, response_body JSONB NULL,
  transfer_id NULL, created_at, expires_at). UNIQUE (user_id, key).
Use timestamptz everywhere, server defaults for timestamps, and explicit constraint names.

2) app/db/session.py: async engine with sensible pool settings from config, session
factory, and a FastAPI dependency. A small unit-of-work helper for transactions.

3) scripts/seed.py (idempotent — safe to run twice) with fictional programs:
Cards: NOVA_REWARDS (card), ZENITH_POINTS (card).
Loyalty: SKYWARD_MILES (airline), STAYWELL_POINTS (hotel), HARBOR_CRUISE_POINTS (cruise).
Rates (examples — include both directions and asymmetry):
- NOVA_REWARDS -> SKYWARD_MILES 1:1, min 1000, increment 1000
- NOVA_REWARDS -> STAYWELL_POINTS 1:2, min 1000, increment 1000
- ZENITH_POINTS -> SKYWARD_MILES 1:1, min 1000, increment 1000
- ZENITH_POINTS -> HARBOR_CRUISE_POINTS 4:5 (i.e. numerator 4, denominator 5... choose
  values so rounding is visible), min 5000, increment 1000
- SKYWARD_MILES -> NOVA_REWARDS 3:1 reverse (worse rate), min 3000, increment 3000
- STAYWELL_POINTS -> NOVA_REWARDS 5:1 reverse, min 5000, increment 5000
- No route SKYWARD_MILES -> ZENITH_POINTS (to demonstrate an unsupported route)
Bonus: NOVA_REWARDS -> SKYWARD_MILES +25% (2500 bps), active for the next 30 days.
System accounts (TRANSFER_CLEARING, PARTNER_SETTLEMENT) for every program.
Users: user_alice, user_bob with user accounts in every program, realistic balances,
and external_member_id values. Initial balances must be created through balanced SEED
journals (credit user account, debit PARTNER_SETTLEMENT) — never by writing balances
directly — so the ledger invariant holds from the start.

4) Wire make migrate, make makemigration, make seed, make reset.

VERIFY:
make reset
docker compose exec postgres psql -U <user> -d <db> -c "\dt"
make seed (run twice — no duplicates, no errors)
A SQL query proving that for each program SUM(balance) over all accounts == 0 after seeding
(user balances positive, settlement accounts negative by the same amount).
Add the invariant query to docs/ for later use.
```

**Be ready to explain:** why balances are seeded via journals; why points of different programs are never in one balance equation; why `ledger_entries` are insert-only; why the partial unique index prevents two active rate versions.

---

## PROMPT 3 — Conversion Rate Engine, Quotes, and Rate Administration

```text
Read docs/PROJECT_BRIEF.md first and follow it strictly.

STEP 3 GOAL: implement the flexible conversion rate engine.

1) app/services/rate_engine.py
- A pure calculation function (no I/O) that takes a rate, an optional bonus, and
  source_points, and returns a Quote value object: source_points, base_points,
  bonus_points, destination_points, rate snapshot (numerator, denominator, version,
  rate_id, bonus_bps). Integer math only (use // after multiplying; never floats or
  Decimal-to-float conversion).
- Validation raising domain errors with stable codes:
  ROUTE_NOT_SUPPORTED, PROGRAM_NOT_FOUND, PROGRAM_INACTIVE, SAME_PROGRAM,
  BELOW_MINIMUM, ABOVE_MAXIMUM, INVALID_INCREMENT, ZERO_DESTINATION_POINTS.
- A RateRepository that loads the active rate (effective_from <= now and
  (effective_to IS NULL OR effective_to > now)) and active bonus for a route.
- A cached layer using Redis (key includes the route; short TTL; explicit invalidation
  on rate/bonus changes). The cache must be safe to lose: on Redis failure, log a warning
  and fall back to PostgreSQL rather than failing the request.

2) Endpoints:
- GET /v1/programs
- POST /v1/quotes {source_program, destination_program, source_points} -> quote,
  with expires_at (e.g. 60s) for UX clarity. No side effects.
- GET /v1/admin/rates (current and historical versions)
- POST /v1/admin/rates: create a new version for a route in one DB transaction —
  close the current version (effective_to = now) and insert the new one (version + 1).
  Never update rate values in place. Invalidate cache after commit.
- POST /v1/admin/bonuses: create a time-bound bonus; invalidate cache.
- Admin endpoints require X-Admin-Key (constant-time comparison).

3) Error handling foundation: app/api/errors.py with an exception handler that converts
domain errors to RFC 7807 problem+json {type, title, status, detail, code, instance,
request_id}. Unknown exceptions -> 500 with a generic message (no stack traces leaked),
full details logged.

4) Unit tests (tests/unit/test_rate_engine.py), table-driven:
1:1, 1:2, 3:1 reverse, rounding down with an uneven ratio, bonus applied on base,
bonus expired (not applied), below minimum, above maximum, invalid increment,
zero destination, unsupported route, same program. Plus a property-style test
(hypothesis is fine) proving destination_points is never negative and never exceeds
source_points * numerator / denominator * (1 + bonus).
Integration test: creating a new rate version closes the old one and the next quote
uses the new version; old transfers keep their snapshot (assert snapshot concept via
repository — full transfer test comes later).

VERIFY:
make test-unit
curl -X POST localhost:8000/v1/quotes -H "X-User-Id: user_alice" -H "Content-Type: application/json" \
  -d '{"source_program":"NOVA_REWARDS","destination_program":"SKYWARD_MILES","source_points":10000}'
-> base 10000, bonus 2500, destination 12500
Same request for SKYWARD_MILES -> ZENITH_POINTS -> 422 ROUTE_NOT_SUPPORTED
Show me example requests/responses so I can reuse them in the README.
```

**Be ready to explain:** why the calculation is a pure function; why rates are versioned instead of updated; why the cache must fail open to PostgreSQL; how rounding is handled and why rounding down protects the platform.

---

## PROMPT 4 — Ledger Service and Account Balances

```text
Read docs/PROJECT_BRIEF.md first and follow it strictly.

STEP 4 GOAL: implement the double-entry ledger that all balance changes go through.

1) app/services/ledger.py
- post_journal(session, journal_type, transfer_id, entries) where entries is a list of
  (account, direction, amount). It must:
  a) validate the journal is balanced per program (debits == credits per program);
  b) insert the journal and entries;
  c) update account balances (DEBIT decreases a user balance, CREDIT increases — define
     and document the sign convention clearly in the module docstring);
  d) rely on the DB CHECK constraint as the final guard against negative user balances,
     translating the IntegrityError into INSUFFICIENT_BALANCE.
- The ledger never commits by itself; the caller owns the transaction boundary.
- lock_account_for_update(session, account_id) using SELECT ... FOR UPDATE.
- Accounts must be locked in a consistent order (by id) whenever more than one user
  account is locked in the same transaction, to avoid deadlocks. Document this.

2) Endpoint: GET /v1/accounts (for X-User-Id) -> balances per program with
external_member_id masked (show last 4 characters only).

3) scripts/check_invariants.py and a `make check-invariants` target: verifies that for
every program the sum of balances is zero, that every journal is balanced, and that
every user account balance equals the sum of its ledger entries. Exit code non-zero
on any violation. This will be used by tests and the demo.

4) Tests:
- Unit: unbalanced journal rejected; mixed-program journal validated per program.
- Integration: posting a journal updates balances; insufficient balance raises
  INSUFFICIENT_BALANCE and leaves no partial writes (transaction rolled back);
  invariant script passes after a series of journals.

VERIFY:
make test
make check-invariants
curl localhost:8000/v1/accounts -H "X-User-Id: user_alice"
```

**Be ready to explain:** double-entry bookkeeping; why the ledger doesn't commit itself; how row locks plus the `CHECK` constraint prevent double-spending under concurrency; lock ordering and deadlocks.

---

## PROMPT 5 — Partner Simulator, Partner Adapter, and Resilience

```text
Read docs/PROJECT_BRIEF.md first and follow it strictly.

STEP 5 GOAL: implement partner integration with realistic failure handling.

1) partner_simulator/ (separate FastAPI app, port 8001):
- In-memory store of credits keyed by (partner_code, reference). Idempotent: repeating a
  credit with the same reference returns the original result and never double-credits.
- All modes from the brief: success, reject, error, unavailable, timeout,
  timeout_after_commit, slow, flaky (configurable failure rate).
- POST /simulator/config and GET /simulator/config to switch modes per partner at
  runtime; POST /simulator/reset to clear state.
- Log every request with reference and mode.

2) app/partners/base.py: an abstract PartnerAdapter interface:
  credit_points(partner_code, reference, member_id, points) -> PartnerCreditResult
  get_credit_status(partner_code, reference) -> PartnerStatusResult
  Results use normalized domain types, never raw HTTP responses. Outcome classification:
  SUCCESS, REJECTED (definitive — e.g. invalid member), NOT_SENT (provably never reached
  the partner: connection refused, circuit open), UNKNOWN (timeout, 5xx, broken
  connection after sending). Include the reason and whether it is retryable.

3) app/partners/http_partner.py: an httpx-based adapter implementing the interface, with
separate connect and read timeouts from config, and error classification exactly as above.

4) app/partners/resilience.py (implement ourselves, small and readable, fully tested):
- Retry with exponential backoff + full jitter, max attempts from config, retrying only
  retryable outcomes. Safe because the partner call is idempotent by reference.
- Circuit breaker per partner (CLOSED -> OPEN after N consecutive failures -> HALF_OPEN
  after cooldown -> CLOSED on success). When OPEN, fail fast with NOT_SENT.
  In-process state is acceptable; document in the code and README that with multiple
  instances the state could move to Redis.
- An overall deadline so a transfer request never waits longer than a configured budget.

5) app/partners/registry.py: map partner_code -> adapter, so adding a new partner is a
config/registration change, not a change to business logic.

6) Tests (use respx or httpx MockTransport for unit tests):
- classification for 201, 422, 500, 503, connect error, read timeout;
- retries happen only for retryable outcomes and stop at max attempts;
- circuit opens after threshold, fails fast while open, half-opens after cooldown;
- simulator idempotency: same reference twice -> single credit.

VERIFY:
make test
curl -X POST localhost:8001/simulator/config -H "Content-Type: application/json" \
  -d '{"partner_code":"SKYWARD","mode":"reject"}'
Show me commands for switching every mode, for the README.
```

**Be ready to explain:** why a timeout is "unknown" and not "failed"; why retries are safe only because of the partner reference; how the circuit breaker protects both us and the partner; why business logic depends on an interface instead of httpx.

---

## PROMPT 6 — Idempotency Layer

```text
Read docs/PROJECT_BRIEF.md first and follow it strictly.

STEP 6 GOAL: implement idempotency for POST /v1/transfers, exactly as specified in the brief.

1) app/services/idempotency.py
- compute_fingerprint(method, path, body) using canonical JSON (sorted keys,
  no whitespace) and SHA-256.
- An IdempotencyService with a clear lifecycle:
  begin(user_id, key, fingerprint) -> one of: NEW (proceed), REPLAY (return stored
  response), CONFLICT_IN_PROGRESS (409), CONFLICT_MISMATCH (422).
  complete(record, status_code, body, transfer_id) -> stores the final response.
  release(record) -> for 5xx outcomes: delete the record and release the lock so the
  client can retry.
- Redis lock: SET key NX PX ttl with a unique token; release only if the token matches
  (Lua script compare-and-delete). If Redis is unavailable, fall back to the PostgreSQL
  unique constraint (INSERT ... ON CONFLICT DO NOTHING) and log a warning — correctness
  must never depend on Redis alone.
- Validation of the key: required, 1–255 chars, printable characters only.
- Expired keys (older than TTL) are treated as new; add a `make cleanup-idempotency`
  script or document that a periodic job would delete expired rows.

2) Integrate it as a reusable FastAPI dependency or decorator for the transfers route
(route itself comes in the next step — create a small test route or unit-test the
service directly for now).

3) Tests:
- same key + same body -> second call replays identical status + body with
  Idempotent-Replayed: true;
- same key + different body -> 422 IDEMPOTENCY_KEY_REUSED;
- two concurrent requests with the same key (asyncio.gather) -> exactly one proceeds,
  the other gets 409;
- 5xx path releases the key, and a retry proceeds;
- Redis down -> still correct through the PostgreSQL constraint;
- missing header -> 400 IDEMPOTENCY_KEY_REQUIRED.

VERIFY: make test
```

**Be ready to explain:** why both Redis and PostgreSQL are used; why the fingerprint check exists; why 5xx responses are not stored; the difference between client-to-us idempotency and us-to-partner idempotency.

---

## PROMPT 7 — Transfer Saga and Transfer Endpoints

```text
Read docs/PROJECT_BRIEF.md first and follow it strictly. This is the most important step.

STEP 7 GOAL: implement the bi-directional transfer flow as a saga with automatic rollback.

1) app/domain/transfer_state.py
- TransferStatus enum and an ALLOWED_TRANSITIONS map exactly matching the brief.
- transition(transfer, to_status, reason, metadata) validates the move, updates status
  and timestamps, and appends a transfer_events row. Invalid transitions raise
  InvalidStateTransition.

2) app/services/transfer_service.py — TransferService.create_transfer(user_id, request,
idempotency_key) implementing the brief's flow step by step, with each step in its own
small, well-named method:
  a) quote via the rate engine;
  b) DB TRANSACTION 1: lock the source user account, check balance, create the transfer
     (PENDING), post TRANSFER_DEBIT journal (user source -> source TRANSFER_CLEARING),
     transition to SOURCE_DEBITED. Commit. Insufficient balance -> 422, no transfer row.
  c) transition to PARTNER_SUBMITTED (own short transaction), then call the partner
     adapter OUTSIDE any transaction with reference = transfer id and the destination
     account's external_member_id.
  d) SUCCESS -> DB TRANSACTION: TRANSFER_SETTLE journal (source clearing -> source
     PARTNER_SETTLEMENT; destination PARTNER_SETTLEMENT -> user destination account),
     store confirmation id, COMPLETED.
     REJECTED or NOT_SENT -> DB TRANSACTION: TRANSFER_REVERSAL journal
     (source clearing -> user source account), REVERSED with failure_code
     (PARTNER_REJECTED or PARTNER_UNAVAILABLE).
     UNKNOWN -> PENDING_VERIFICATION with next_verification_at.
  e) If the process crashes between steps, the reconciler (next step) must be able to
     pick the transfer up. Make sure every intermediate state is persisted before the
     next external action.
  Never hold a DB transaction or lock while calling the partner. Add a comment at the
  partner call explaining why.

3) Endpoints (app/api/routes/transfers.py):
- POST /v1/transfers {source_program, destination_program, source_points}
  Headers: X-User-Id, Idempotency-Key. Wrapped with the idempotency layer and a Redis
  rate limiter. Response: transfer resource. 201 for COMPLETED/REVERSED, 202 for
  PENDING_VERIFICATION.
- GET /v1/transfers/{id} (only the owner can read it -> 404 otherwise, not 403, to avoid
  leaking existence), including the events timeline and rate snapshot.
- GET /v1/transfers?limit=&cursor= (opaque cursor over created_at + id, newest first).
Transfer resource fields: id, status, source {program, points}, destination {program,
points, base_points, bonus_points}, rate snapshot, failure {code, message} or null,
partner_confirmation_id, created_at, completed_at, events[].

4) Integration tests (real PostgreSQL + Redis + simulator via docker compose):
- card -> loyalty happy path: balances updated correctly on both sides; COMPLETED;
- loyalty -> card (reverse direction) happy path with the reverse rate;
- partner reject -> REVERSED and the source balance is fully restored;
- partner unavailable / circuit open -> REVERSED (PARTNER_UNAVAILABLE);
- partner timeout -> 202 PENDING_VERIFICATION and source points held in clearing;
- insufficient balance -> 422, no transfer row, no ledger rows;
- unsupported route -> 422;
- rate changed after a transfer -> old transfer keeps its snapshot;
- after every test: the ledger invariant check passes.

VERIFY:
make test
Give me a curl sequence (for the README) that: shows balances, gets a quote, performs a
transfer, replays it with the same Idempotency-Key, and shows balances again.
```

**Be ready to explain:** the whole saga on a whiteboard; why the debit is committed before the partner call; what "automatic rollback" means when an external system is involved; why reading another user's transfer returns 404.

---

## PROMPT 8 — Reconciliation Worker (Resolving Unknown Outcomes)

```text
Read docs/PROJECT_BRIEF.md first and follow it strictly.

STEP 8 GOAL: build the background reconciler that makes the system self-healing.

1) app/services/reconciliation.py + app/workers/reconciler.py (run as
`python -m app.workers.reconciler`, the `worker` service in docker compose):
- Loop every N seconds (config). Select a batch of transfers that are either
  PENDING_VERIFICATION with next_verification_at <= now, or stuck in SOURCE_DEBITED /
  PARTNER_SUBMITTED for longer than a grace period (process crashed mid-saga).
  Use SELECT ... FOR UPDATE SKIP LOCKED so multiple workers never process the same
  transfer.
- For each: query partner get_credit_status(reference).
  Found and completed -> settle and COMPLETED.
  Not found (after the grace period) -> REVERSED (PARTNER_NOT_RECEIVED). Document why
  this is safe: the partner is idempotent by reference and the grace period exceeds the
  maximum in-flight time.
  SOURCE_DEBITED stuck (partner never called) -> REVERSED.
  Partner still unreachable -> increment verification_attempts, exponential backoff on
  next_verification_at; after max attempts -> MANUAL_REVIEW with a clear reason,
  an ERROR log, and a metric.
- Graceful shutdown on SIGTERM/SIGINT (finish the current transfer, then exit).
- POST /v1/admin/transfers/{id}/reconcile triggers reconciliation of one transfer on demand.

2) Integration tests:
- timeout_after_commit mode: transfer goes PENDING_VERIFICATION, reconciler finds the
  credit at the partner -> COMPLETED, destination balance credited exactly once;
- timeout mode (credit not applied): reconciler -> REVERSED, source restored;
- partner down during reconciliation: attempts increase, eventually MANUAL_REVIEW;
- two reconciler instances running concurrently never double-settle (SKIP LOCKED);
- simulated crash: a transfer left in SOURCE_DEBITED is reversed after the grace period;
- ledger invariant holds after every scenario.

VERIFY:
make up (worker running) && make logs (show worker resolving transfers)
make test
```

**Be ready to explain:** why the reconciler is what makes "automatic rollback" trustworthy; `SKIP LOCKED`; why `MANUAL_REVIEW` exists instead of guessing; how the system recovers from a crash at any point in the saga.

---

## PROMPT 9 — Observability, Error Handling, Rate Limiting, and Hardening

```text
Read docs/PROJECT_BRIEF.md first and follow it strictly.

STEP 9 GOAL: make the service production-grade.

1) Observability:
- Structured logs with request_id, user_id, transfer_id, status transitions, partner
  call duration and outcome. Never log secrets or full member ids (mask them).
- Prometheus metrics at /metrics: transfers_total{status, route}, transfer_duration_seconds,
  partner_requests_total{partner, outcome}, partner_request_duration_seconds{partner},
  circuit_breaker_state{partner}, idempotent_replays_total, idempotency_conflicts_total,
  reconciliation_resolved_total{result}, transfers_pending_verification (gauge),
  rate_limit_rejections_total.
- X-Request-ID returned on every response.

2) Hardening:
- Consistent problem+json for every error, including 404/405/validation errors.
  Document all error codes in one place (app/domain/errors.py), since they become part
  of the README.
- Request validation: positive integers, max transfer size, program codes format.
- Redis fixed-window rate limit per user on POST /v1/transfers -> 429 with Retry-After.
  Fail open (allow + warn) if Redis is down; document the trade-off.
- Graceful shutdown for the API (close DB pool, Redis, httpx client).
- Database: statement timeout and lock timeout settings from config.
- Security: admin key compared in constant time, no stack traces in responses, CORS
  disabled by default, dependency versions pinned.

3) Complete the `make demo` target: scripts/demo.sh that runs an end-to-end scenario
walkthrough against the running stack with readable output:
  1. show balances; 2. quote; 3. successful card -> airline transfer (with bonus);
  4. replay with the same Idempotency-Key (no second debit);
  5. reuse key with a different body (422);
  6. reverse direction airline -> card;
  7. partner reject -> automatic reversal, balance restored;
  8. partner timeout_after_commit -> 202 pending -> reconciler completes it;
  9. unsupported route; 10. insufficient balance;
  11. final balances + invariant check.
Reset simulator modes at the end.

VERIFY: make reset && make demo && make check-invariants && curl localhost:8000/metrics
```

**Be ready to explain:** which metrics you would alert on (e.g., `MANUAL_REVIEW` count, pending-verification age, circuit open); why the rate limiter fails open while idempotency does not.

---

## PROMPT 10 — Complete Test Suite and Concurrency Tests

```text
Read docs/PROJECT_BRIEF.md first. Review all existing tests and fill the gaps.

STEP 10 GOAL: a test suite that proves correctness under failure and concurrency.

1) Test infrastructure: fixtures that reset the DB and simulator between tests, factories
for users/accounts/rates, a helper that asserts the ledger invariant, and clear markers
(unit, integration). Integration tests run against the docker compose services
(document how). Coverage report with `make coverage`; target >= 85% on app/services,
app/domain, app/partners.

2) Add the concurrency tests reviewers care most about:
- 20 concurrent transfers with DIFFERENT keys that together exceed the balance ->
  balance never negative, number of successes matches available balance, invariant holds;
- 10 concurrent requests with the SAME key -> exactly one transfer, one debit;
- concurrent transfers in opposite directions for the same user -> no deadlock
  (lock ordering), all complete.

3) Add an end-to-end "chaos" test: flaky partner mode + 50 transfers + reconciler running
-> every transfer ends in COMPLETED or REVERSED (or MANUAL_REVIEW only if explicitly
forced), no points created or destroyed, invariant holds.

4) Make sure test names read like specifications, e.g.
test_partner_rejection_reverses_debit_and_restores_balance.

5) Add a GitHub Actions workflow (.github/workflows/ci.yml): lint, typecheck, unit
tests, integration tests with postgres and redis services, coverage summary.

VERIFY: make lint && make typecheck && make test && make coverage
Report the coverage numbers and any flaky tests.
```

**Be ready to explain:** how you tested concurrency; what the invariant guarantees; what you would add with more time (load testing, contract tests with real partners).

---

## PROMPT 11 — Architecture Diagrams and Design Documentation

```text
Read docs/PROJECT_BRIEF.md and the actual implementation. Documentation must match the
code exactly — verify every claim against the source.

STEP 11 GOAL: produce the architecture deliverable.

1) docs/architecture.md containing Mermaid diagrams (renders on GitHub):
  a) Component diagram: client, FastAPI API (routes, idempotency layer, rate limiter,
     transfer service, rate engine, ledger, partner registry/adapters with retry +
     circuit breaker), reconciler worker, PostgreSQL, Redis, partner simulator,
     /metrics. Label what each arrow carries.
  b) Sequence diagram: successful transfer (idempotency check -> quote -> TX1 debit ->
     partner call -> TX2 settle -> response).
  c) Sequence diagram: failure paths (partner reject -> reversal; timeout ->
     pending verification -> reconciler -> complete or reverse).
  d) State machine diagram of transfer statuses.
  e) ER diagram of the database.
  f) Ledger example: a table showing journal entries for one successful transfer and one
     reversed transfer, with balances before/after.
Each diagram followed by 3–6 sentences explaining it.

2) Export the component and sequence diagrams as PNG/SVG into docs/images/ using
mermaid-cli (npx @mermaid-js/mermaid-cli) and add a `make diagrams` target, so reviewers
who don't render Mermaid can still see them.

3) docs/adr/ — short Architecture Decision Records (context, decision, consequences,
alternatives considered):
  0001 Saga with compensation instead of distributed transactions (2PC)
  0002 Double-entry ledger for balances
  0003 Idempotency with Redis lock + PostgreSQL durable record
  0004 Integer ratio conversion with rate versioning and snapshots
  0005 Synchronous API with async reconciliation for unknown outcomes
  0006 PostgreSQL as source of truth; Redis only for ephemeral concerns
  0007 HTTP status semantics for transfer outcomes (201/202)

4) docs/failure-scenarios.md: a table of every failure scenario (partner reject, timeout,
timeout after commit, 5xx, circuit open, Redis down, DB down, process crash at each saga
step, duplicate request, concurrent requests) -> system behaviour -> final state ->
how it's tested (test name).

VERIFY: make diagrams, and open docs/architecture.md to confirm the Mermaid renders.
```

**Be ready to explain:** why not 2PC; every row of the failure-scenarios table. This document is your strongest interview asset — know it well.

---

## PROMPT 12 — The Complete README

```text
Read the entire repository, docs/PROJECT_BRIEF.md, docs/architecture.md, docs/adr/,
docs/failure-scenarios.md, Makefile, docker-compose.yml, and .env.example.
Rewrite README.md completely. Every command must be copy-paste runnable and verified
against the actual Makefile and code. Do not document anything that doesn't exist.

Audience: (1) the Optivoy reviewer who has 15 minutes, and (2) a new developer joining
the project. Put the reviewer's needs first.

REQUIRED SECTIONS (in this order):
1. Title + one-paragraph summary of what the service does and why it matters for a
   loyalty platform.
2. "For reviewers — start here": a 5-line path: quick start command, where the
   architecture is, the most interesting files to read (transfer_service.py,
   reconciliation.py, idempotency.py, rate_engine.py, ledger.py), and `make demo`.
3. Table of contents.
4. Features — mapping each client requirement (conversion engine, atomic transactions
   with rollback, idempotency, partner integration, bi-directional) to how it's
   implemented, with links to the code.
5. Architecture overview — embedded component diagram image + short explanation +
   link to docs/architecture.md.
6. Tech stack with one line on why each was chosen.
7. Project structure — annotated tree.
8. Prerequisites with versions (Docker, Docker Compose v2, make, Python 3.12 for local
   dev, optional Node for mermaid-cli) and install hints for macOS, Linux, Windows (WSL2).
9. Quick start (Docker) — numbered steps with expected output:
   clone, cp .env.example .env, make build, make up, make migrate, make seed,
   open http://localhost:8000/docs, make demo.
10. Running locally without Docker for the API (venv, pip install -e ".[dev]",
    services in Docker, make run-local).
11. Make commands reference — a table of EVERY Makefile target with description and when
    to use it.
12. Configuration — a table of EVERY environment variable: name, default, description.
13. API reference — for each endpoint: method, path, headers, request, response, status
    codes, and a curl example. Include the full transfer walkthrough: balances -> quote
    -> transfer -> idempotent replay -> key reuse error -> get transfer -> list transfers.
14. Error codes — table of every problem+json code, HTTP status, meaning, client action.
15. Conversion rate engine — formula, rounding rule, minimums/increments, bonuses,
    versioning, snapshots, seeded routes table (including the unsupported one), and how
    to add or change a rate via the admin API.
16. Transfer lifecycle — state machine diagram, what each status means, and which states
    are final.
17. Atomicity and rollback — the saga explained simply, why a DB transaction can't cover a
    partner call, compensation, unknown outcomes, reconciliation.
18. Idempotency — how to use the header, replay behaviour, conflicts, TTL.
19. Partner integration — adapter interface, retry/backoff, circuit breaker, timeouts,
    and "Simulating partner failures" with the exact curl commands for every simulator mode.
20. Concurrency and consistency guarantees — row locks, lock ordering, CHECK constraints,
    ledger invariant, `make check-invariants`.
21. Observability — log format and fields, metrics list, suggested alerts.
22. Testing — how to run unit, integration, coverage; what the concurrency and chaos
    tests prove; test layout.
23. Database — schema overview, migrations workflow (create, apply, roll back),
    resetting data, useful SQL queries (balances, transfers by status, invariant).
24. Developer guide — adding a new loyalty program, adding a new partner adapter,
    adding a new rate, adding a migration, code style (ruff, mypy), branch/commit
    conventions, PR checklist.
25. Assumptions — A1–A5 from the brief, plus any made during implementation.
26. Trade-offs and limitations — honest list (e.g. in-process circuit breaker, mocked
    partners, no auth, single-region, sync API) and why each is acceptable here.
27. Production readiness / future improvements — real auth (OAuth/JWT), outbox pattern
    and event publishing, shared circuit-breaker state, secrets management, partner
    webhooks, load testing, multi-region, admin audit UI, partner-side debit for card
    issuers (generalized saga).
28. Troubleshooting — port conflicts, containers unhealthy, migration errors, how to fully
    reset, how to read worker logs, Redis connection issues.
29. Author and contact.

STYLE: clear headings, short paragraphs, tables for references, fenced code blocks for
every command, expected output where useful, no marketing language, no emojis.

Also create CONTRIBUTING.md (short, points to the developer guide) and make sure
.env.example matches the configuration table exactly.

VERIFY: Follow the README quick start from a clean clone in a new directory and report
any step that fails or any command that doesn't match the Makefile.
```

**Be ready to explain:** you should be able to walk a reviewer through the README live, in about 10 minutes, as a presentation of your design.

---

## PROMPT 13 — Final Review as the Optivoy Reviewer

```text
Act as a senior staff engineer at Optivoy reviewing this take-home submission for a
founding-level engineering role. Be critical and specific.

Review the whole repository against:
1. The original assignment: bi-directional conversion, flexible rate engine, atomic
   transactions with automatic rollback, idempotency keys, partner API integration,
   Python/Go + PostgreSQL + Redis + REST, architecture diagram, complete source code.
2. Correctness: can points ever be lost, duplicated, or go negative? Walk through a
   crash at every line between the debit commit and the final state.
3. Is any DB transaction or lock held during a network call?
4. Float usage anywhere in point or rate math?
5. Idempotency edge cases, concurrency, deadlocks.
6. Code quality: naming, module boundaries, typing, duplication, dead code, TODOs.
7. Tests: missing scenarios, flaky patterns, meaningful names.
8. Documentation: does every README command work? Do docs match the code?
9. Security: secrets in repo, leaked stack traces, member ids in logs.
10. Anything that looks AI-generated and unreviewed (boilerplate comments, unused code,
    inconsistent style).

Output: a prioritized list (Critical / Important / Nice-to-have) with file and line
references and a concrete fix for each. Then fix all Critical and Important items, run
make lint, make typecheck, make test, make demo, make check-invariants, and report results.
```

After this prompt, do your **own** final pass: clone the repo fresh into a new folder, follow the README exactly, and run `make demo`. If anything fails for you, it will fail for the reviewer.

---

## 3. Final Submission Checklist

**Requirements coverage**

- [ ] Transfers work in both directions (card → loyalty and loyalty → card) with different rates
- [ ] Rate engine: integer ratios, minimum, increment, maximum, bonuses, versioning, snapshots
- [ ] Atomic debit, saga, automatic reversal, unknown-outcome handling, reconciler
- [ ] Idempotency: replay, key reuse rejection, concurrent same-key protection
- [ ] Partner adapter with timeouts, retries, circuit breaker, and a simulator with failure modes
- [ ] PostgreSQL, Redis, and REST all used meaningfully

**Deliverables**

- [ ] Architecture diagram(s) in `docs/architecture.md` and exported images in `docs/images/`
- [ ] Complete source code in a clean GitHub repository (public, or private with access granted to Meher/Omar)

**Quality**

- [ ] `make reset && make demo` works from a fresh clone
- [ ] `make lint`, `make typecheck`, `make test`, `make check-invariants` all pass
- [ ] CI badge green on GitHub
- [ ] README complete; every command verified
- [ ] ADRs and failure-scenarios document present
- [ ] No secrets committed; `.env.example` complete
- [ ] Clean, incremental commit history

**Personal readiness**

- [ ] You can draw the saga and state machine from memory
- [ ] You can explain every ADR and every row of the failure-scenarios table
- [ ] You can explain what you would change for real production scale

Optional but valuable: record a 3–5 minute screen video walking through the architecture and `make demo`. It was not required for Track 2, but it makes the reviewer's job easier and shows communication skills.

---

## 4. Submission Email to Meher

```text
Subject: Track 2 Submission – Loyalty Points Transfer Engine – Aakash Limbani

Hi Meher,

Please find my submission for Track 2 (Bi-Directional Loyalty Points Transfer Engine):

Repository: <GitHub link>
Architecture: <link to docs/architecture.md>
(Optional) Walkthrough video: <link>

Quick start: clone the repository, run `make reset`, then `make demo` to see an
end-to-end walkthrough of successful transfers in both directions, idempotent retries,
partner failures with automatic rollback, and reconciliation of unknown outcomes.

Highlights:
- Flexible conversion engine with integer ratios, minimums, increments, time-bound
  bonuses, and versioned rates (each transfer keeps a snapshot of the rate it used).
- Atomicity through a double-entry ledger and a saga with compensation, including
  safe handling of partner timeouts where the outcome is unknown.
- Idempotency keys backed by Redis locks and a durable PostgreSQL record.
- Partner integration behind an adapter interface, with timeouts, retries with backoff,
  a circuit breaker, and a partner simulator for failure scenarios.
- Concurrency and failure-path tests, including a ledger invariant check proving points
  are never created or lost.

The README documents all assumptions, trade-offs, and what I would change for full
production. I'd be glad to walk the team through the design and discuss any decisions.

Thank you for the opportunity.

Best regards,
Aakash Limbani
```