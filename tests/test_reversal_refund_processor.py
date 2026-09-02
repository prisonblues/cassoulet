"""Tests for reversal and refund resolution.

Money coming back on the SAME account it left from is neither a transfer nor
income. Left unresolved it inflates income twice over: once as its own leg, and
again when the gap detector fabricates a remediation entry for the balance the
reversal restored.
"""

from datetime import date, timedelta
from decimal import Decimal

from cassoulet.stages.envelope import Envelope
from cassoulet.stages.reversal_refund_processor import ReversalRefundProcessor
from cassoulet.utils.envelope_utilities import is_refund_of, is_reversal_of

CHK = "Assets:Bank:HSBC:Checking"
CARD = "Liabilities:UK:CreditCard:HSBC"
OTHER = "Assets:Bank:Starling:Joint"


def _out(amount, account=CHK, payee=None, narration="", when=date(2025, 11, 24), eid="o"):
    return Envelope(date=when, payee=payee, narration=narration,
                    outbound_units=Decimal(amount), outbound_type="GBP",
                    outbound_account=account, envelope_id=eid, source_type="csv")


def _in(amount, account=CHK, payee=None, narration="", when=date(2025, 11, 24), eid="i"):
    return Envelope(date=when, payee=payee, narration=narration,
                    inbound_units=Decimal(amount), inbound_type="GBP",
                    inbound_account=account, envelope_id=eid, source_type="csv")


class TestReversalDetection:
    """A reversal names the payee it reverses and quotes the original's date."""

    def test_matching_reversal_is_recognised(self):
        original = _out("324.22", payee="VIRGIN MONEY", eid="orig")
        reversal = _in("324.22", payee="REVERSAL OF 24-11", narration="VIRGIN MONEY", eid="rev")
        assert is_reversal_of(reversal, original)
        assert is_reversal_of(original, reversal), "argument order must not matter"

    def test_reversal_naming_a_different_payee_is_rejected(self):
        original = _out("324.22", payee="VIRGIN MONEY", eid="orig")
        reversal = _in("324.22", payee="REVERSAL OF 24-11", narration="SOMEONE ELSE", eid="rev")
        assert not is_reversal_of(reversal, original)

    def test_reversal_on_a_different_account_is_rejected(self):
        original = _out("324.22", payee="VIRGIN MONEY", eid="orig")
        reversal = _in("324.22", account=OTHER, payee="REVERSAL OF 24-11",
                       narration="VIRGIN MONEY", eid="rev")
        assert not is_reversal_of(reversal, original)

    def test_reversal_of_a_different_amount_is_rejected(self):
        original = _out("324.22", payee="VIRGIN MONEY", eid="orig")
        reversal = _in("99.00", payee="REVERSAL OF 24-11", narration="VIRGIN MONEY", eid="rev")
        assert not is_reversal_of(reversal, original)

    def test_two_reversals_are_not_a_pair(self):
        a = _in("324.22", payee="REVERSAL OF 24-11", narration="VIRGIN MONEY", eid="a")
        b = _out("324.22", payee="REVERSAL OF 24-11", narration="VIRGIN MONEY", eid="b")
        assert not is_reversal_of(a, b)


class TestRefundDetection:
    """A refund agrees on counterparty and direction, and never exceeds the original."""

    def test_full_refund_is_recognised(self):
        purchase = _out("73.98", payee="Lipoelastic", eid="buy")
        refund = _in("73.98", payee="Lipoelastic", when=date(2025, 11, 30), eid="ref")
        assert is_refund_of(refund, purchase)

    def test_partial_refund_is_recognised(self):
        # A returned order often comes back in pieces.
        purchase = _out("73.98", payee="Lipoelastic", eid="buy")
        refund = _in("69.99", payee="Lipoelastic", when=date(2025, 11, 30), eid="ref")
        assert is_refund_of(refund, purchase)

    def test_refund_larger_than_the_purchase_is_rejected(self):
        purchase = _out("50.00", payee="Lipoelastic", eid="buy")
        refund = _in("100.00", payee="Lipoelastic", when=date(2025, 11, 30), eid="ref")
        assert not is_refund_of(refund, purchase)

    def test_counterparty_falls_back_to_narration(self):
        # Card statements leave the payee blank and name the merchant in the
        # narration; matching on payee alone skipped every card refund.
        purchase = _out("302.95", account=CARD, narration="LOVESPACE LIMITED LONDON ENG", eid="buy")
        refund = _in("302.95", account=CARD, narration="LOVESPACE LIMITED LONDON ENG",
                     when=date(2025, 11, 27), eid="ref")
        assert is_refund_of(refund, purchase)

    def test_different_counterparty_is_rejected(self):
        purchase = _out("50.00", payee="Lipoelastic", eid="buy")
        refund = _in("50.00", payee="Vinted", when=date(2025, 11, 30), eid="ref")
        assert not is_refund_of(refund, purchase)


class TestResolution:
    """The returning leg inherits the account the original was booked to."""

    def _run(self, envelopes):
        processor = ReversalRefundProcessor()
        out, _ = processor.process_envelopes(envelopes)
        return out, processor.get_stats()

    def test_reversal_inherits_the_originals_account(self):
        original = _out("324.22", payee="VIRGIN MONEY", eid="orig")
        original.metadata["expense_account"] = "Expenses:Utilities:Mobile"
        reversal = _in("324.22", payee="REVERSAL OF 24-11", narration="VIRGIN MONEY", eid="rev")

        _, stats = self._run([original, reversal])

        assert reversal.metadata["expense_account"] == "Expenses:Utilities:Mobile"
        assert reversal.metadata["reverses_envelope_id"] == "orig"
        assert stats["reversals_resolved"] == 1

    def test_catch_all_accounts_are_inherited_too(self):
        # Netting is the point: two legs in the catch-all cancel, which beats
        # leaving the original there and the money coming back as income.
        original = _out("282.03", payee="MBNA LIMITED", eid="orig")
        original.metadata["expense_account"] = "Expenses:UK:Unknown"
        reversal = _in("282.03", payee="REVERSAL OF 24-11", narration="MBNA LIMITED", eid="rev")

        self._run([original, reversal])

        assert reversal.metadata["expense_account"] == "Expenses:UK:Unknown"

    def test_nearest_prior_purchase_wins(self):
        old = _out("50.00", payee="Vinted", when=date(2025, 8, 1), eid="old")
        old.metadata["expense_account"] = "Expenses:Shopping:Old"
        recent = _out("50.00", payee="Vinted", when=date(2025, 11, 1), eid="recent")
        recent.metadata["expense_account"] = "Expenses:Shopping:Recent"
        refund = _in("50.00", payee="Vinted", when=date(2025, 11, 20), eid="ref")

        self._run([old, recent, refund])

        assert refund.metadata["expense_account"] == "Expenses:Shopping:Recent"

    def test_original_must_precede_the_reversal(self):
        later = _out("324.22", payee="VIRGIN MONEY", when=date(2025, 12, 1), eid="later")
        later.metadata["expense_account"] = "Expenses:Utilities:Mobile"
        reversal = _in("324.22", payee="REVERSAL OF 24-11", narration="VIRGIN MONEY",
                       when=date(2025, 11, 24), eid="rev")

        _, stats = self._run([later, reversal])

        assert "expense_account" not in reversal.metadata
        assert stats["unresolved"] == 1

    def test_refund_outside_the_window_is_left_alone(self):
        purchase = _out("50.00", payee="Vinted", when=date(2025, 1, 1), eid="buy")
        purchase.metadata["expense_account"] = "Expenses:Shopping"
        refund = _in("50.00", payee="Vinted", when=date(2025, 11, 24), eid="ref")

        self._run([purchase, refund])

        assert "expense_account" not in refund.metadata

    def test_ordinary_spending_is_untouched(self):
        purchase = _out("12.00", payee="TESCO", eid="buy")
        _, stats = self._run([purchase])
        assert stats["reversals_resolved"] == 0
        assert stats["refunds_resolved"] == 0
        assert stats["unresolved"] == 0
        assert "expense_account" not in purchase.metadata

    def test_envelope_count_is_preserved(self):
        # Steel Thread: this stage annotates, it never adds or drops envelopes.
        original = _out("324.22", payee="VIRGIN MONEY", eid="orig")
        reversal = _in("324.22", payee="REVERSAL OF 24-11", narration="VIRGIN MONEY", eid="rev")
        out, _ = self._run([original, reversal])
        assert len(out) == 2


class TestOriginalConsumption:
    """An original can be refunded in pieces, but never for more than it was worth."""

    def _run(self, envelopes):
        processor = ReversalRefundProcessor()
        processor.process_envelopes(envelopes)
        return processor.get_stats()

    def test_split_refund_against_one_purchase_is_allowed(self):
        # Lipoelastic: GBP 73.98 came back as 3.99 + 69.99.
        purchase = _out("73.98", payee="Lipoelastic", when=date(2025, 4, 9), eid="buy")
        purchase.metadata["expense_account"] = "Expenses:Healthcare"
        first = _in("3.99", payee="Lipoelastic", when=date(2025, 4, 12), eid="r1")
        second = _in("69.99", payee="Lipoelastic", when=date(2025, 4, 26), eid="r2")

        stats = self._run([purchase, first, second])

        assert first.metadata["expense_account"] == "Expenses:Healthcare"
        assert second.metadata["expense_account"] == "Expenses:Healthcare"
        assert stats["refunds_resolved"] == 2

    def test_one_purchase_cannot_be_claimed_by_several_full_refunds(self):
        # Three identical credits against a single charge were crediting the
        # expense account three times over against one debit.
        purchase = _out("10.00", payee="Milk and More", when=date(2025, 6, 3), eid="buy")
        purchase.metadata["expense_account"] = "Expenses:Food:Groceries"
        credits = [_in("10.00", payee="Milk and More", when=date(2025, 6, 7), eid=f"r{i}")
                   for i in range(3)]

        stats = self._run([purchase] + credits)

        claimed = [c for c in credits if "expense_account" in c.metadata]
        assert len(claimed) == 1, "only one credit may claim a single purchase"
        assert stats["refunds_resolved"] == 1

    def test_a_second_purchase_gives_a_second_refund_somewhere_to_land(self):
        first_buy = _out("10.00", payee="Milk and More", when=date(2025, 6, 1), eid="b1")
        first_buy.metadata["expense_account"] = "Expenses:Food:Groceries"
        second_buy = _out("10.00", payee="Milk and More", when=date(2025, 6, 2), eid="b2")
        second_buy.metadata["expense_account"] = "Expenses:Food:Groceries"
        credits = [_in("10.00", payee="Milk and More", when=date(2025, 6, 7), eid=f"r{i}")
                   for i in range(2)]

        stats = self._run([first_buy, second_buy] + credits)

        assert stats["refunds_resolved"] == 2
