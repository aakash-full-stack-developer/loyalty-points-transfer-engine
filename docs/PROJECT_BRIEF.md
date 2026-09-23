# Project Brief — Loyalty Points Transfer Engine

## Stack
Python 3.12, FastAPI (served by uvicorn), Pydantic v2, pydantic-settings, SQLAlchemy 2.0 (async)
with asyncpg, Alembic, redis-py (asyncio), httpx, structlog, prometheus-client,
pytest + pytest-asyncio + pytest-cov, hypothesis (property tests), respx (httpx mocking),
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
  Seeded partner codes: NOVA_REWARDS -> NOVA, ZENITH_POINTS -> ZENITH,
  SKYWARD_MILES -> SKYWARD, STAYWELL_POINTS -> STAYWELL, HARBOR_CRUISE_POINTS -> HARBOR.
- Conversion rates are keyed by (source_program, destination_program). Direction matters:
  A->B and B->A are separate rows with separate rules. A missing or inactive row means
  the route is not supported.
- Rate fields: numerator, denominator (integers), min_source_points, source_increment,
  max_source_points, effective_from, effective_to (nullable), version, active.
- Ratio convention: numerator / denominator = destination points per source point.
  "1:2" (1 card point -> 2 hotel points) is numerator 2, denominator 1.
  "3:1 reverse" (3 miles -> 1 card point) is numerator 1, denominator 3.
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
- Invariant: for each program, the sum of all account balances (user + system) is always
  zero. Seeding credits user accounts and debits that program's PARTNER_SETTLEMENT
  account, so the settlement account mirrors the seeded user total with a negative sign.

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

## Revisions to the original playbook brief
These clarify ambiguities found while reviewing the playbook. Later steps follow this file.
1. Ratio convention made explicit (see Domain rules), so reverse rates cannot be stored
   the wrong way round.
2. Ledger invariant stated as "sum per program == 0", which is what seeding through
   balanced journals actually produces.
3. Partner codes for the seeded programs defined, so simulator config commands are stable.
4. Seed data (step 2): ZENITH_POINTS -> HARBOR_CRUISE_POINTS uses numerator 2,
   denominator 3 (min 5000, increment 1000) instead of 4:5. With 4:5 every multiple of
   1000 converts exactly, so rounding would never be visible; with 2:3, 5000 -> 3333.
5. uvicorn, pytest-cov, hypothesis and respx added to the stack: uvicorn serves the app;
   the other three are test-only and are required by later steps.
