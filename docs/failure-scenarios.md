# Failure scenarios

Every way a transfer can go wrong that the system is designed for: what happens, where it
ends, and the test that proves it. Test names are checked against the test suite by
`tests/unit/test_docs.py`, so this table cannot silently go stale.

Terms: **clearing** is the source program's `TRANSFER_CLEARING` account, where debited
points wait while a transfer is in flight. **Reconciler** is the worker described in
[architecture.md](architecture.md#3-failure-paths-and-reconciliation).

## Partner behaviour

| # | Scenario | System behaviour | Final state | Proven by |
| --- | --- | --- | --- | --- |
| 1 | Partner rejects the credit (4xx, e.g. unknown member) | Definitive: the reversal journal returns the points in the same request | `REVERSED` / `PARTNER_REJECTED`, balance restored | `test_partner_rejection_reverses_the_debit_and_restores_the_balance`, `test_rejection_is_definitive`, `test_rejection_is_final_and_never_retried` |
| 2 | Partner times out and did **not** apply the credit | Unknown outcome: 202, points held in clearing. The reconciler finds no credit and, after the grace period, reverses | `REVERSED` / `PARTNER_NOT_RECEIVED` | `test_partner_timeout_leaves_points_in_clearing_pending_verification`, `test_credit_never_received_is_reversed_after_the_grace_period` |
| 3 | Partner applies the credit, then times out (`timeout_after_commit`) | Unknown outcome: 202. The reconciler finds the credit and settles; the destination is credited exactly once | `COMPLETED` | `test_timeout_after_commit_without_retries_is_pending_although_partner_credited`, `test_credit_applied_before_the_timeout_is_completed_and_credited_once`, `test_timeout_after_commit_is_unknown_but_partner_has_the_credit` |
| 4 | Every retry of an applied credit also times out | Retries reuse the reference, so the partner never credits twice; the transfer waits for verification | `PENDING_VERIFICATION`, then `COMPLETED` | `test_retries_against_a_slow_partner_stay_pending_and_never_double_credit`, `test_retrying_the_same_reference_never_credits_twice`, `test_retrying_an_applied_reference_returns_the_original_confirmation` |
| 5 | Partner returns 5xx | Retried with exponential backoff and jitter (safe: idempotent reference). Success if a retry succeeds; otherwise unknown outcome, as in row 2 | `COMPLETED`, or `PENDING_VERIFICATION` then resolved | `test_retryable_failures_are_retried_until_success`, `test_resilient_adapter_retries_an_outage_then_opens_the_circuit` |
| 6 | Partner unreachable (connection refused) | Provably never sent: reversed immediately | `REVERSED` / `PARTNER_UNAVAILABLE` | `test_unreachable_partner_reverses_with_partner_unavailable`, `test_unreachable_partner_is_not_sent` |
| 7 | Circuit breaker open (partner failing repeatedly) | Fails fast without calling the partner; reversed immediately. Half-opens after the cooldown | `REVERSED` / `PARTNER_UNAVAILABLE` | `test_open_circuit_fails_fast_and_reverses_without_calling_the_partner`, `test_circuit_opens_after_threshold_and_fails_fast`, `test_circuit_half_opens_after_cooldown_and_closes_on_success` |
| 8 | First attempt times out, then the circuit opens or the deadline passes | Stays **unknown**, never downgraded to "not sent": the first attempt may have been applied | `PENDING_VERIFICATION`, then resolved | `test_unknown_is_never_downgraded_to_not_sent`, `test_unknown_followed_by_open_circuit_stays_unknown`, `test_attempt_cut_off_by_the_deadline_is_unknown` |
| 9 | Partner answers "not found" while a request could still be in flight | Checked again, at the latest when the grace period ends; does not count as a failed attempt | resolved once the answer is decisive | `test_missing_credit_inside_the_grace_period_is_rechecked_not_reversed`, `test_repeated_not_found_answers_never_escalate_to_manual_review` |
| 10 | Partner unreachable during reconciliation | Exponential backoff between checks; after `RECONCILIATION_MAX_ATTEMPTS` an operator decides, with an ERROR log and the `transfers_manual_review` gauge. Points stay in clearing | `MANUAL_REVIEW` / `VERIFICATION_EXHAUSTED` | `test_unreachable_partner_escalates_to_manual_review_after_max_attempts` |
| 11 | Many partners misbehaving at once (flaky, late, rejecting), with a live reconciler | Every transfer resolves; a credit exists at the partner exactly for the completed transfers | `COMPLETED` or `REVERSED` for all 50 | `test_chaos_flaky_partners_and_a_live_reconciler_never_create_or_lose_points` |

## Infrastructure

| # | Scenario | System behaviour | Final state | Proven by |
| --- | --- | --- | --- | --- |
| 12 | Redis down: quotes | The rate cache fails open to PostgreSQL | quotes keep working | `test_quotes_still_work_when_redis_is_down`, `test_redis_outage_falls_back_to_database_without_error` |
| 13 | Redis down: idempotency | The PostgreSQL unique constraint alone admits exactly one of several concurrent requests | exactly-once preserved | `test_redis_outage_still_admits_exactly_one_request` |
| 14 | Redis down: rate limiting | Fails open (allowed, warning logged): the limiter protects capacity, not money | transfers keep working | `test_redis_outage_fails_open` |
| 15 | Redis or PostgreSQL down: health | `/health/ready` returns 503 so a load balancer stops routing traffic; `/health/live` stays 200 | instance marked not ready | `test_readiness_is_503_when_redis_is_unreachable`, `test_liveness_returns_ok_without_touching_dependencies` |
| 16 | PostgreSQL unavailable or a query fails mid-request | Generic 500 with a `request_id`, no internals leaked, the idempotency key released for a retry. Whatever was committed before the failure is a saga state the reconciler resumes (rows 18 to 21). An actual database outage is not simulated in the tests; the states it can leave behind are | nothing, or a resumable state | `test_unhandled_exception_returns_generic_problem_without_internals`, `test_unexpected_error_releases_the_key_so_a_retry_can_proceed` |
| 17 | Slow query or lock wait | Server-side `statement_timeout` and `lock_timeout` on every connection turn a hang into an error (row 16) | as row 16 | configuration (`DB_STATEMENT_TIMEOUT_MS`, `DB_LOCK_TIMEOUT_MS`); not separately tested |

## Process crashes, at each step of the saga

| # | Crash point | System behaviour | Final state | Proven by |
| --- | --- | --- | --- | --- |
| 18 | During the debit transaction | The transaction rolls back: no transfer, no ledger rows | nothing happened; the client retries | `test_insufficient_balance_is_reported_and_nothing_is_written`, `test_insufficient_balance_creates_no_transfer_and_no_ledger_rows` |
| 19 | After the debit commit, before `PARTNER_SUBMITTED` | The partner was never called (the call happens only after `PARTNER_SUBMITTED` commits). The reconciler reverses once the transfer is older than the stuck window | `REVERSED` / `TRANSFER_INTERRUPTED` | `test_crash_before_the_partner_call_is_reversed_after_the_stuck_window` |
| 20 | After `PARTNER_SUBMITTED`, before or during the partner call | The reconciler asks the partner: no credit after the grace period, so it reverses | `REVERSED` / `PARTNER_NOT_RECEIVED` | `test_crash_after_submission_is_resolved_by_asking_the_partner` (crash_before_partner_call) |
| 21 | After the partner applied the credit, before the outcome was recorded | The reconciler finds the credit and settles | `COMPLETED` | `test_crash_after_submission_is_resolved_by_asking_the_partner` (crash_after_partner_call) |
| 22 | Reconciler worker dies mid-batch | Unprocessed claims expire with their lease and are claimed again; a resolution in progress rolls back | resolved by the next round | lease behaviour: `test_concurrent_claims_never_overlap`; worker lifecycle: `test_worker_resolves_pending_transfers_and_stops_gracefully` |

## Duplicates and concurrency

| # | Scenario | System behaviour | Final state | Proven by |
| --- | --- | --- | --- | --- |
| 23 | Client retries with the same key | The stored response is replayed with `Idempotent-Replayed: true`; no second debit | one transfer | `test_same_idempotency_key_replays_without_a_second_debit`, `test_same_key_and_body_replays_the_stored_response` |
| 24 | Same key reused for a different request | 422 `IDEMPOTENCY_KEY_REUSED` | no new transfer | `test_reusing_a_key_for_a_different_transfer_is_rejected`, `test_same_key_with_a_different_body_is_rejected` |
| 25 | Concurrent requests with the same key | One proceeds; the others get 409 or, after it finishes, the replay | exactly one transfer and one debit | `test_ten_concurrent_requests_with_one_key_create_exactly_one_transfer`, `test_concurrent_requests_with_the_same_key_run_once` |
| 26 | Concurrent transfers draining one balance | Row lock plus `CHECK (balance >= 0)`: exactly the affordable number succeed | balance exactly 0, never negative | `test_twenty_concurrent_transfers_exceeding_the_balance_never_overdraw`, `test_concurrent_debits_never_overdraw` |
| 27 | Concurrent transfers in opposite directions | Locks are always taken in ascending account id order | all complete, no deadlock | `test_concurrent_opposite_direction_transfers_never_deadlock`, `test_opposite_direction_journals_do_not_deadlock` |
| 28 | Two reconcilers at once | `SKIP LOCKED` claims plus leases; each resolution re-checks the status under a row lock | each transfer settled once | `test_two_reconcilers_running_together_settle_each_transfer_once`, `test_concurrent_claims_never_overlap` |
| 29 | API and reconciler race on one transfer | The later actor sees the status already moved and does nothing | settled or reversed once | `test_late_partner_answer_is_ignored_once_the_reconciler_resolved_the_transfer`, `test_late_failure_answer_cannot_reverse_a_completed_transfer` |
| 30 | The idempotency layer is bypassed (for example an abandoned key taken over) | `UNIQUE (user_id, idempotency_key)` on transfers returns the existing transfer | one transfer | `test_duplicate_key_that_bypasses_the_idempotency_layer_returns_the_same_transfer`, `test_in_progress_record_without_a_lock` |

## Data and code defects

| # | Scenario | System behaviour | Final state | Proven by |
| --- | --- | --- | --- | --- |
| 31 | A rate changes while transfers exist | New version, old one closed; each transfer keeps its snapshot | history unchanged | `test_rate_change_after_a_transfer_does_not_change_its_snapshot`, `test_new_rate_version_closes_previous_and_is_used_by_next_quote` |
| 32 | Two admins change the same rate at once | Serialised by a per-route advisory lock | consecutive versions, one current | `test_concurrent_rate_changes_get_consecutive_versions` |
| 33 | A bug tries to overdraw, unbalance, edit the ledger or settle twice | The database refuses | the write fails, nothing changes | `test_user_balance_cannot_go_negative`, `test_unbalanced_journal_is_rejected`, `test_ledger_is_append_only`, `test_transfer_can_be_settled_at_most_once`, `test_entry_program_must_match_its_account_program` |
| 34 | A balance is edited outside the ledger | `make check-invariants` reports the program total and the drift, exit code 1 | detected | `test_invariant_check_detects_a_tampered_balance` |

## Known residual risk

| Scenario | Why it is not covered | Mitigation |
| --- | --- | --- |
| A partner applies a credit it received long after our client gave up (beyond `RECONCILIATION_NOT_FOUND_GRACE_SECONDS`), after the reconciler already reversed | No partner protocol can rule this out without cooperation from the partner | The grace period is validated to exceed the partner deadline; a production integration would add a request expiry or a "void by reference" call before reversing |
