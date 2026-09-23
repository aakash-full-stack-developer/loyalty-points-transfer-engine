from typing import Any

import pytest

from app.domain.enums import EntryDirection
from app.services.ledger import LedgerError, Posting, net_balance_changes, validate_journal

NOVA, SKYWARD = 1, 3
DEBIT, CREDIT = EntryDirection.DEBIT, EntryDirection.CREDIT


def posting(account_id: int, program_id: int, direction: EntryDirection, amount: Any) -> Posting:
    return Posting(account_id, program_id, direction, amount)


def test_balanced_single_program_journal_is_valid() -> None:
    validate_journal([posting(10, NOVA, DEBIT, 1_000), posting(20, NOVA, CREDIT, 1_000)])


def test_journal_balanced_within_each_program_is_valid() -> None:
    # The shape of a settlement: NOVA clearing -> NOVA settlement, SKYWARD settlement -> user.
    validate_journal(
        [
            posting(11, NOVA, DEBIT, 1_000),
            posting(12, NOVA, CREDIT, 1_000),
            posting(31, SKYWARD, DEBIT, 1_250),
            posting(32, SKYWARD, CREDIT, 1_250),
        ]
    )


def test_unbalanced_journal_is_rejected() -> None:
    with pytest.raises(LedgerError, match="unbalanced"):
        validate_journal([posting(10, NOVA, DEBIT, 1_000), posting(20, NOVA, CREDIT, 999)])


def test_journal_balanced_only_across_programs_is_rejected() -> None:
    # 1,000 NOVA points out and 1,000 SKYWARD miles in: equal numbers, different units.
    with pytest.raises(LedgerError, match="unbalanced"):
        validate_journal([posting(10, NOVA, DEBIT, 1_000), posting(30, SKYWARD, CREDIT, 1_000)])


@pytest.mark.parametrize(
    "postings",
    [
        pytest.param([], id="empty"),
        pytest.param([posting(10, NOVA, DEBIT, 1)], id="single_posting"),
        pytest.param([posting(10, NOVA, DEBIT, 0), posting(20, NOVA, CREDIT, 0)], id="zero_amount"),
        pytest.param(
            [posting(10, NOVA, DEBIT, -5), posting(20, NOVA, CREDIT, -5)], id="negative_amount"
        ),
        pytest.param(
            [posting(10, NOVA, DEBIT, 10.0), posting(20, NOVA, CREDIT, 10.0)], id="float_amount"
        ),
        pytest.param(
            [posting(10, NOVA, DEBIT, True), posting(20, NOVA, CREDIT, True)], id="bool_amount"
        ),
    ],
)
def test_malformed_journals_are_rejected(postings: list[Posting]) -> None:
    with pytest.raises(LedgerError):
        validate_journal(postings)


def test_net_balance_changes_follow_the_sign_convention() -> None:
    changes = net_balance_changes(
        [
            posting(10, NOVA, DEBIT, 1_000),  # user pays
            posting(11, NOVA, CREDIT, 1_000),  # clearing receives
            posting(11, NOVA, DEBIT, 400),  # clearing pays part back
            posting(10, NOVA, CREDIT, 400),
        ]
    )

    assert changes == {10: -600, 11: 600}
