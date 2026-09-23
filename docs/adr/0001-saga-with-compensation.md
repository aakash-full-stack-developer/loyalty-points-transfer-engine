# ADR 0001: Saga with compensation instead of distributed transactions

Status: accepted

## Context

A transfer changes two systems: our ledger (debit the source program) and a partner's
system (credit the destination program). A database transaction can only span our own
database. Partner APIs are plain HTTP endpoints: they do not take part in a two-phase
commit, they can be slow, and a call can time out after the partner has already applied
it.

## Decision

Run each transfer as a **saga** of short local transactions with a **compensating
action**:

1. Debit the source into a clearing account and commit (`SOURCE_DEBITED`).
2. Record that the partner is about to be called and commit (`PARTNER_SUBMITTED`).
3. Call the partner with no transaction or lock held, using the transfer id as an
   idempotent reference.
4. On success, settle (`COMPLETED`). On a definitive failure, post a reversal journal that
   returns the points (`REVERSED`). On an unknown outcome, wait for verification
   (`PENDING_VERIFICATION`, see ADR 0005).

Every state is committed before the next external action, so a crash at any point leaves a
state the reconciler can resume.

## Consequences

- Money is never "in the air": between steps the points sit in a visible clearing account.
- Rollback is compensation, not deletion; the ledger keeps a full history of both the
  debit and the reversal.
- Partner calls never hold database locks, so a slow partner cannot block other transfers.
- A transfer can be observed in intermediate states, so clients receive a `status` and the
  API returns 202 while an outcome is unknown.
- More code than a single transaction: a state machine, a reconciler, and failure-path
  tests for every step.

## Alternatives considered

- **Two-phase commit (XA).** Requires every partner to be a transaction participant, which
  real loyalty APIs are not, and holds locks across network calls.
- **Call the partner inside the database transaction.** A rollback cannot undo a credit
  the partner already applied, and locks would be held for the whole network call.
- **Credit first, debit afterwards.** A failed debit would leave points created from
  nothing; debiting first means the worst case is points held in clearing.
