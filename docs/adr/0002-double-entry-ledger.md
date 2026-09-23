# ADR 0002: Double-entry ledger for balances

Status: accepted

## Context

Points behave like currency: users, auditors and partners need to know where every point
came from and went. A single mutable `balance` column cannot explain itself, cannot detect
drift, and makes "points created by a bug" invisible.

## Decision

Every balance change is a **journal** of two or more **entries**, posted through one
function (`app/services/ledger.py::post_journal`):

- One sign convention for every account: `balance = credits - debits`.
- A journal must balance **per program**: points of different programs are different
  units and are never netted against each other.
- Each program has system accounts: `TRANSFER_CLEARING` (points in flight) and
  `PARTNER_SETTLEMENT` (the platform's position with that partner). Opening balances are
  posted as SEED journals against settlement, never written directly.
- `accounts.balance` is a running total kept for fast reads and row locking; the entries
  are the record.
- The database enforces the rules as well: user balances `CHECK (balance >= 0)`,
  append-only triggers on journals and entries, a deferred trigger rejecting unbalanced
  journals at commit, a composite foreign key tying each entry to its account's program,
  and one journal of each type per transfer.
- Accounts are locked (`SELECT ... FOR NO KEY UPDATE`) and updated in ascending id order,
  so concurrent transfers cannot deadlock.

## Consequences

- Invariant: every program's balances sum to zero. `make check-invariants` verifies it,
  plus balanced journals and balance = sum of entries, and the test suite checks it after
  every scenario.
- Corrections are new journals (reversals), never edits, so history is complete.
- The clearing and settlement rows of a program are updated by every transfer in that
  program, which serialises writes per program. Acceptable at this scale; at high volume
  they could be sharded or derived from entries.

## Alternatives considered

- **A single balance column with an audit log.** The log can drift from the balance and
  does not prove conservation.
- **Event sourcing without a balance column.** Correct, but every balance check would sum
  history, and locking a balance for a debit becomes harder.
