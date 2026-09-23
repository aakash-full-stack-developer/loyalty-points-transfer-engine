# ADR 0005: Synchronous API with asynchronous reconciliation for unknown outcomes

Status: accepted

## Context

Most partner calls answer in well under a second, and users expect an immediate result.
Some calls time out or fail with 5xx after the request was sent; the partner may or may
not have applied the credit. The system must never guess, and never leave points in
limbo.

## Decision

- `POST /v1/transfers` runs the saga synchronously, with a bounded partner budget
  (`PARTNER_TOTAL_DEADLINE_SECONDS`, retries with backoff, a circuit breaker).
- Partner outcomes are classified into SUCCESS, REJECTED, NOT_SENT (provably never
  reached the partner) and UNKNOWN. Once any attempt was UNKNOWN, the final outcome is
  never downgraded to NOT_SENT.
- UNKNOWN leaves the points in clearing and returns `202 PENDING_VERIFICATION`.
- A separate **reconciler worker** claims due transfers (`FOR UPDATE SKIP LOCKED` plus a
  lease), asks the partner for the credit by reference, and completes or reverses. It
  also recovers transfers left in `SOURCE_DEBITED` or `PARTNER_SUBMITTED` by a crash.
- "Not found" is trusted only after a grace period longer than the partner deadline
  (validated at startup). An unreachable partner is retried with exponential backoff and,
  after `RECONCILIATION_MAX_ATTEMPTS`, the transfer goes to `MANUAL_REVIEW`.

## Consequences

- The common case is one request with a final answer; the rare case is honest (202) and
  resolves itself.
- Every transfer ends COMPLETED, REVERSED or MANUAL_REVIEW; the chaos test checks that the
  ledger agrees with the partner for every one of them.
- Several workers can run safely; the backlog is visible as metrics.
- Residual risk, documented: a partner that applies a request long after our client gave
  up (beyond the grace period) would conflict with a reversal. Real integrations close
  this with a request expiry or a "void by reference" partner API.

## Alternatives considered

- **Fully asynchronous API (always 202, then poll or webhook).** Simpler failure handling,
  but every client pays the latency and complexity for the rare case.
- **Longer synchronous timeouts.** Only move the problem; an unknown outcome is always
  possible, so a reconciler is needed anyway.
- **Treat timeouts as failures and reverse.** Pays twice whenever the partner did apply
  the credit.
