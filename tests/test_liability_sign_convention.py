"""Tests for liability sign-convention normalisation.

A card or loan statement is written from the creditor's side: a charge INCREASES
what you owe and prints positive, a payment reduces it and prints negative.
Beancount is the mirror - you owe money, so the balance is negative.

EnvelopeBuilder normalises this from the ACCOUNT type, so every liability
importer gets it right without individual configuration. These tests pin that
behaviour down, because getting it wrong inverts a whole account silently:
balances, the sign of every expense it feeds, and whether a bill payment is
eligible for transfer matching at all.
"""

from datetime import date
from decimal import Decimal

import pytest

from cassoulet.utils.csv_row_data import CSVRowData
from cassoulet.utils.envelope_builder import EnvelopeBuilder
from cassoulet.utils.envelope_utilities import (
    is_expense_envelope,
    is_income_envelope,
    is_transfer_eligible,
)

CARD = "Liabilities:UK:CreditCard:HSBC"
LOAN = "Liabilities:UK:Loan:Santander"
BANK = "Assets:Bank:HSBC:Checking"


def _build(amount, account, narration="TXN", sign_convention="standard"):
    row = CSVRowData()
    row.date = date(2025, 4, 26)
    row.narrative = narration
    row.amount = Decimal(amount)
    row.row_number = 1
    row.source_file = "test.csv"
    row.raw_row = {"Amount": amount, "Description": narration}
    return EnvelopeBuilder.from_csv_row_data(
        row, account, "TestBank", sign_convention, "test.csv"
    )


def _posting(envelope):
    """Signed amount as it will appear on the account's posting."""
    if envelope.inbound_units:
        return envelope.inbound_units
    if envelope.outbound_units:
        return -envelope.outbound_units
    return Decimal(0)


class TestLiabilityNormalisation:
    """A liability statement is inverted on import; an asset account is not."""

    def test_card_charge_increases_debt_so_posts_negative(self):
        # Statement prints a purchase positive. You now owe more.
        assert _posting(_build("6.50", CARD, "BAYLEY & SAGE")) == Decimal("-6.50")

    def test_card_payment_reduces_debt_so_posts_positive(self):
        # Statement prints a payment negative. You now owe less.
        assert _posting(_build("-195.00", CARD, "PAYMENT - THANK YOU")) == Decimal("195.00")

    def test_loan_gets_the_same_treatment_as_a_card(self):
        # The rule keys off the account type, not the institution, so an
        # importer added later needs no special configuration.
        assert _posting(_build("500.00", LOAN, "DRAWDOWN")) == Decimal("-500.00")
        assert _posting(_build("-500.00", LOAN, "REPAYMENT")) == Decimal("500.00")

    @pytest.mark.parametrize("amount,expected", [("-195.00", "-195.00"), ("195.00", "195.00")])
    def test_asset_accounts_are_untouched(self, amount, expected):
        assert _posting(_build(amount, BANK)) == Decimal(expected)

    def test_reversed_convention_flips_back_to_standard(self):
        # An importer that already declared 'reversed' must not be
        # double-negated: reversed on a liability resolves to standard.
        assert _posting(_build("6.50", CARD, sign_convention="reversed")) == Decimal("6.50")


class TestDirectionHelpersAgree:
    """The direction helpers must follow the same invariant as the builder.

    These read the opposite way round to an asset account, and drifting out of
    step with EnvelopeBuilder is what previously hid credit card purchases from
    the uncategorised-expense report.
    """

    def test_card_charge_is_an_expense(self):
        env = _build("6.50", CARD, "BAYLEY & SAGE")
        assert is_expense_envelope(env)
        assert not is_income_envelope(env)

    def test_card_payment_is_not_an_expense(self):
        # A bill payment is a transfer. It is emphatically not income either,
        # but is_income_envelope means "not an expense" for liabilities.
        env = _build("-195.00", CARD, "PAYMENT - THANK YOU")
        assert not is_expense_envelope(env)

    def test_bank_debit_is_an_expense(self):
        assert is_expense_envelope(_build("-195.00", BANK))


class TestTransferEligibility:
    """Bill payments must survive to the transfer scorer; spending must not."""

    def test_card_payment_is_transfer_eligible(self):
        # Under the old inverted import this was outbound-from-liability, which
        # is_transfer_eligible() rejects as spending - so card payments never
        # reached the scorer and both legs fell out as Income:Other.
        assert is_transfer_eligible(_build("-195.00", CARD, "PAYMENT - THANK YOU"))

    def test_card_spending_is_not_transfer_eligible(self):
        assert not is_transfer_eligible(_build("6.50", CARD, "BAYLEY & SAGE"))
