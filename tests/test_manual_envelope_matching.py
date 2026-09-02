"""Tests for manual envelopes reaching the CSV rows they describe.

A manual entry exists to be matched. Its whole purpose is to say what a
transaction really was when the bank narration cannot - "team dinner on a work
trip" is not derivable from "Finance Engelberg CHE".

That was impossible for anything spending on a credit card. Three layers
independently blocked it, and the failure mode was the worst kind: the manual
entry did not error, it just never found its counterpart and was written out
ALONGSIDE the imported row, double-counting the spend.
"""

from datetime import date
from decimal import Decimal

from cassoulet.stages.envelope import Envelope
from cassoulet.utils.envelope_utilities import (
    build_envelope_date_index,
    can_reconcile,
    is_transfer_eligible,
)

CARD = "Liabilities:UK:CreditCard:HSBC"
SPEND = "Expenses:UK:StaffEntertainment"
BANK = "Assets:Bank:HSBC:Checking"


def _card_purchase_csv():
    """As the importer builds it: outbound from the card, one leg only."""
    return Envelope(
        date=date(2025, 2, 10), payee=None, narration="Finance Engelberg CHE",
        outbound_units=Decimal("1695.90"), outbound_type="GBP", outbound_account=CARD,
        envelope_id="csv_charge", source_type="csv",
    )


def _card_purchase_manual():
    """As a human writes it: the same spend, with the category it really was."""
    return Envelope(
        date=date(2025, 2, 10), payee="Finance Engelberg CHE",
        narration="Work trip hospitality",
        outbound_units=Decimal("1695.90"), outbound_type="GBP", outbound_account=CARD,
        inbound_units=Decimal("1695.90"), inbound_type="GBP", inbound_account=SPEND,
        envelope_id="manual_charge", source_type="beancount",
    )


class TestTheBlockers:
    """Each layer that stopped a manual entry finding its row."""

    def test_card_spending_is_not_transfer_eligible(self):
        # Correct, and not the thing under test: spending is not a transfer
        # between accounts. The bug was letting this answer a DIFFERENT
        # question - whether two views of one transaction may be reconciled.
        assert not is_transfer_eligible(_card_purchase_csv())
        assert not is_transfer_eligible(_card_purchase_manual())

    def test_the_pair_is_reconcilable_despite_being_ineligible(self):
        # This is the point. Eligibility says no; reconciliation says yes.
        assert can_reconcile(_card_purchase_manual(), _card_purchase_csv())

    def test_eligible_only_index_hides_the_row_to_be_matched(self):
        # The decisive layer. Restricting the index settles the question before
        # the pair exists - the row is not there to be found, so no downstream
        # gate can help.
        envelopes = [_card_purchase_manual(), _card_purchase_csv()]
        eligible_only = build_envelope_date_index(envelopes, transfer_eligible_only=True)
        assert sum(len(v) for v in eligible_only.values()) == 0

        full = build_envelope_date_index(envelopes, transfer_eligible_only=False)
        assert sum(len(v) for v in full.values()) == 2


class TestIndexSafety:
    """The full index must not contain envelopes that blow up on inspection."""

    def test_remediation_stubs_have_no_source_type(self):
        # is_manual_envelope() RAISES on an unset source_type, and can_reconcile
        # reaches it. Including remediation stubs in the full index killed the
        # entire scorer - "made 0 matches" out of 15,296 envelopes, 193 balance
        # errors - which read like a matching regression rather than a crash.
        stub = Envelope(
            date=date(2025, 2, 10), payee=None, narration="gap remediation",
            inbound_units=Decimal("10.00"), inbound_type="GBP", inbound_account=BANK,
            envelope_id="gap_remediation_x", source_type=None,
        )
        assert not stub.source_type

        # The scorer filters on exactly this before building the full index.
        kept = [e for e in [stub, _card_purchase_csv()] if e.source_type]
        assert [e.envelope_id for e in kept] == ["csv_charge"]


class TestScopeIsNotWidened:
    """CSV-to-CSV behaviour must be untouched by any of this."""

    def test_two_csv_card_purchases_stay_ineligible(self):
        # Widening the index for every subject took balance errors from 12 to
        # 193: thousands of card purchases became transfer candidates for
        # coincidental same-amount bank debits. Only a manual subject may look
        # at the full index.
        other = _card_purchase_csv()
        other.envelope_id = "csv_charge_2"
        assert not is_transfer_eligible(other)
        assert not can_reconcile(_card_purchase_csv(), other), (
            "two CSV rows are not two views of one transaction"
        )

    def test_reconciliation_still_requires_one_manual_and_one_csv(self):
        both_manual = _card_purchase_manual()
        other = _card_purchase_manual()
        other.envelope_id = "manual_2"
        assert not can_reconcile(both_manual, other)

    def test_reconciliation_still_requires_a_shared_account(self):
        elsewhere = Envelope(
            date=date(2025, 2, 10), payee="Finance Engelberg CHE", narration="x",
            outbound_units=Decimal("1695.90"), outbound_type="GBP", outbound_account=BANK,
            envelope_id="csv_elsewhere", source_type="csv",
        )
        assert not can_reconcile(_card_purchase_manual(), elsewhere)
