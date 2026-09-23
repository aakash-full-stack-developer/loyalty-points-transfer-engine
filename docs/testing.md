# Testing

```bash
make test               # everything (starts PostgreSQL, Redis and the simulator if needed)
make test-unit          # unit tests only: no services, about 1 second
make test-integration   # integration tests only
make coverage           # all tests + coverage report (terminal and htmlcov/); fails below 90%
```

All of these run inside the `tools` container (Python 3.12, source mounted). To use a local
virtualenv instead: `make test TOOLS=`.

## Layout

| Directory | Marker | Needs | What it covers |
| --- | --- | --- | --- |
| `tests/unit/` | `unit` | nothing (runs with networking disabled) | pure logic: rate math (including a property-based test), ledger validation, state machine, retry/backoff, circuit breaker, partner response classification, idempotency fingerprints, the simulator, error format, settings |
| `tests/integration/` | `integration` | PostgreSQL, Redis, partner simulator | everything end to end over real HTTP and a real database |

Markers are applied from the directory in `tests/conftest.py`, so they can never drift
from the layout.

## How integration tests run

- **Own database.** At the start of a session `loyalty_test` is dropped, created and migrated
  with Alembic from scratch (which also proves the migrations apply cleanly). Every test
  starts from empty tables (TRUNCATE) and seeds what it needs. The development database is
  never touched. Override with `TEST_DATABASE_URL`.
- **Own Redis database.** Logical database 1, flushed before every test (dev uses 0).
  Override with `TEST_REDIS_URL`.
- **Real partner simulator** over HTTP (`PARTNER_BASE_URL`), reset before and after each test
  that uses it. This also clears any simulator modes set by hand in the dev stack.
- **Fast timeouts.** Partner read timeout 1 s and deadline 3 s, so timeout scenarios take about
  a second each.
- **Ledger invariants after every test** that moves points (`ledger_stays_consistent`
  fixture): no points created or destroyed, every journal balanced, balances equal their
  entries.
- **Controllable clock** (`tests/integration/helpers.py::Clock`) for the reconciler, so "one
  hour later" takes no time.
- **Factories** (`tests/integration/factories.py`) create users with exact balances,
  funded through balanced journals like real seed data.

## What the hardest tests prove

| Test | Proves |
| --- | --- |
| `test_twenty_concurrent_transfers_exceeding_the_balance_never_overdraw` | 20 concurrent transfers against a balance that covers 10: exactly 10 succeed, the balance ends at exactly 0, never below |
| `test_ten_concurrent_requests_with_one_key_create_exactly_one_transfer` | a client retrying the same request in parallel is debited once |
| `test_concurrent_opposite_direction_transfers_never_deadlock` | lock ordering: 20 simultaneous transfers in both directions for one user all complete |
| `test_chaos_flaky_partners_and_a_live_reconciler_never_create_or_lose_points` | 50 concurrent transfers, 4 misbehaving partners (flaky, late, rejecting) and a live reconciler: every transfer ends COMPLETED or REVERSED; the partner holds a credit exactly for the COMPLETED ones; every balance equals start + completed transfers; all four recovery paths are exercised |
| `test_late_partner_answer_is_ignored_once_the_reconciler_resolved_the_transfer` | the API and the reconciler racing on one transfer settle it once |
| `test_two_reconcilers_running_together_settle_each_transfer_once` | `SKIP LOCKED` + leases: parallel workers never double-settle |
| `test_unknown_is_never_downgraded_to_not_sent` | a possibly-applied credit is never treated as "not sent" (which would pay twice) |
| `test_schema_constraints.py` (e.g. `test_user_balance_cannot_go_negative`, `test_ledger_is_append_only`, `test_transfer_can_be_settled_at_most_once`) | the database itself refuses negative balances, unbalanced journals, edits to the ledger, double settlement and overlapping bonuses, even if application code had a bug |

## Coverage

95% overall (branch coverage). `app/services`, `app/domain` and `app/partners` are each
above 85%. Coverage is configured with `concurrency = ["greenlet", "thread"]`: SQLAlchemy's
asyncio layer runs code in greenlets, and without it coverage silently under-reports every
line after an awaited database call.

Not covered by automated tests, deliberately: the process entry points (`main()` of the
worker and scripts) and signal handling, which `make demo` and `docker compose stop worker`
exercise instead.

## Flakiness

The concurrency, reconciliation and chaos suites were each run repeatedly (5 times or more)
with no failures. Timing-sensitive tests use generous bounds (seconds, not milliseconds),
and randomness in the chaos test is bounded so that every recovery path is exercised with
near certainty (the test asserts it).

## CI

`.github/workflows/ci.yml` runs on every push and pull request:

1. **Lint and typecheck:** `ruff check`, `ruff format --check`, `mypy` (strict).
2. **Unit tests:** no services.
3. **Integration tests and coverage:** PostgreSQL 16 and Redis 7 as service containers, the
   partner simulator started with uvicorn, all tests with coverage (90% gate), a markdown
   coverage summary on the run page, and the HTML report as an artifact.
4. **Docker:** the runtime image builds.
