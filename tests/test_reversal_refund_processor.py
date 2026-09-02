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

    def test_the_payee_is_irrelevant(self):
        """Detection is structural. Banks that never write "REVERSAL OF" - or
        write it in another language - are covered by exactly the same rule."""
        original = _out("324.22", payee="VIRGIN MONEY", eid="orig")
        reversal = _in("324.22", payee="SOMETHING ELSE ENTIRELY", eid="rev")
        assert is_reversal_of(reversal, original)

    def test_reversal_on_a_different_account_is_rejected(self):
        original = _out("324.22", payee="VIRGIN MONEY", eid="orig")
        reversal = _in("324.22", account=OTHER, payee="REVERSAL OF 24-11",
                       narration="VIRGIN MONEY", eid="rev")
        assert not is_reversal_of(reversal, original)

    def test_reversal_of_a_different_amount_is_rejected(self):
        original = _out("324.22", payee="VIRGIN MONEY", eid="orig")
        reversal = _in("99.00", payee="REVERSAL OF 24-11", narration="VIRGIN MONEY", eid="rev")
        assert not is_reversal_of(reversal, original)

    def test_a_different_day_is_not_a_reversal(self):
        """Same-day is load-bearing: widen it and ordinary spending that happens
        to reverse an earlier amount starts pairing."""
        original = _out("324.22", payee="VIRGIN MONEY", when=date(2025, 11, 24), eid="orig")
        reversal = _in("324.22", payee="VIRGIN MONEY", when=date(2025, 11, 25), eid="rev")
        assert not is_reversal_of(reversal, original)

    def test_same_direction_is_not_a_reversal(self):
        a = _in("324.22", payee="VIRGIN MONEY", eid="a")
        b = _in("324.22", payee="VIRGIN MONEY", eid="b")
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

    def test_a_charge_on_another_day_is_not_reversed(self):
        later = _out("324.22", payee="VIRGIN MONEY", when=date(2025, 12, 1), eid="later")
        later.metadata["expense_account"] = "Expenses:Utilities:Mobile"
        reversal = _in("324.22", payee="VIRGIN MONEY", when=date(2025, 11, 24), eid="rev")

        _, stats = self._run([later, reversal])

        assert "expense_account" not in reversal.metadata

    def test_two_equal_charges_on_one_day_are_declined_not_guessed(self):
        """With no counterparty to disambiguate, which charge was undone is
        unprovable - so decline. Guessing would corrupt one of them."""
        a = _out("50.00", payee="SHOP A", eid="a")
        a.metadata["expense_account"] = "Expenses:Shopping"
        b = _out("50.00", payee="SHOP B", eid="b")
        b.metadata["expense_account"] = "Expenses:Groceries"
        back = _in("50.00", payee="EITHER", eid="back")

        self._run([a, b, back])

        assert "reverses_envelope_id" not in back.metadata

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


class TestReimbursementSuggestions:
    """Amount-coincidence search for what an employer reimbursement covers.

    Suggests and logs; never reclassifies. The guards below exist because
    without them the search produced confident nonsense - spousal transfers
    "covered by" school fees, and one dinner cited by every claim it fitted.
    """

    def _card(self, amount, when, eid, payee="MERCHANT", ccy="GBP"):
        return Envelope(date=when, payee=payee, narration="",
                        outbound_units=Decimal(amount), outbound_type=ccy,
                        outbound_account=CARD, envelope_id=eid, source_type="csv")

    def _claim(self, amount, when, eid, account="Assets:Receivables:SimmonsSimmonsLLP",
               into=CHK):
        e = Envelope(date=when, payee="SIMMONS & SIMMONS", narration="",
                     inbound_units=Decimal(amount), inbound_type="GBP",
                     inbound_account=into, envelope_id=eid, source_type="csv")
        e.metadata["income_account"] = account
        return e

    def _suggest(self, envelopes):
        p = ReversalRefundProcessor()
        _, warnings = p.process_envelopes(envelopes)
        return [w for w in warnings if "may cover" in w.message]

    def test_single_expense_is_suggested(self):
        claim = self._claim("100.91", date(2025, 9, 29), "c1")
        exp = self._card("100.91", date(2025, 9, 18), "e1", "THE JUGGED HARE")
        assert len(self._suggest([claim, exp])) == 1

    def test_pair_is_suggested_when_unique(self):
        claim = self._claim("566.53", date(2025, 8, 11), "c1")
        a = self._card("55.78", date(2025, 7, 17), "e1")
        b = self._card("510.75", date(2025, 7, 18), "e2")
        s = self._suggest([claim, a, b])
        assert len(s) == 1
        assert set(s[0].details["suggested_expense_ids"]) == {"e1", "e2"}

    def test_ambiguity_declines(self):
        # Two ways to make the same total: say nothing rather than pick one.
        claim = self._claim("100.00", date(2025, 9, 29), "c1")
        exps = [self._card("100.00", date(2025, 9, 10), "e1"),
                self._card("100.00", date(2025, 9, 11), "e2")]
        assert self._suggest([claim] + exps) == []

    def test_family_receivable_is_not_an_expense_claim(self):
        # Spousal transfers also land in a receivable. Matching them produced
        # GBP 4,000 "covered by" two GBP 2,000 school-fee payments.
        claim = self._claim("4000.00", date(2025, 9, 29), "c1",
                            account="Assets:Receivable:Family:Sara")
        exp = self._card("4000.00", date(2025, 9, 10), "e1", "THE HARRODIAN SCHOOL")
        assert self._suggest([claim, exp]) == []

    def test_claim_must_arrive_in_a_bank_account(self):
        # An employer pays a current account. Money landing on a card is a
        # merchant refund, not a reimbursement.
        claim = self._claim("100.91", date(2025, 9, 29), "c1", into=CARD)
        exp = self._card("100.91", date(2025, 9, 18), "e1")
        assert self._suggest([claim, exp]) == []

    def test_currency_must_match(self):
        claim = self._claim("100.91", date(2025, 9, 29), "c1")
        exp = self._card("100.91", date(2025, 9, 18), "e1", ccy="USD")
        assert self._suggest([claim, exp]) == []

    def test_expense_outside_the_window_is_ignored(self):
        claim = self._claim("100.91", date(2025, 9, 29), "c1")
        exp = self._card("100.91", date(2025, 6, 1), "e1")   # ~120 days earlier
        assert self._suggest([claim, exp]) == []

    def test_one_expense_cannot_serve_two_claims(self):
        # Repeated equal reimbursements would otherwise all cite one dinner.
        c1 = self._claim("100.91", date(2025, 9, 29), "c1")
        c2 = self._claim("100.91", date(2025, 9, 30), "c2")
        exp = self._card("100.91", date(2025, 9, 18), "e1")
        assert len(self._suggest([c1, c2, exp])) == 1

    def test_a_refunded_expense_is_not_claimable(self):
        # Money you got back is not money you can claim.
        claim = self._claim("100.91", date(2025, 9, 29), "c1")
        exp = self._card("100.91", date(2025, 9, 18), "e1", "SOME RESTAURANT")
        refund = Envelope(date=date(2025, 9, 20), payee="SOME RESTAURANT", narration="",
                          inbound_units=Decimal("100.91"), inbound_type="GBP",
                          inbound_account=CARD, envelope_id="r1", source_type="csv")
        refund.metadata["reverses_envelope_id"] = "e1"
        assert self._suggest([claim, exp, refund]) == []

    def test_nothing_is_reclassified(self):
        claim = self._claim("100.91", date(2025, 9, 29), "c1")
        exp = self._card("100.91", date(2025, 9, 18), "e1")
        before = dict(claim.metadata)
        self._suggest([claim, exp])
        assert claim.metadata == before, "suggestions must not mutate the claim"


def test_the_bank_marker_audits_but_never_matches():
    """The marker must not resolve anything on its own.

    Detection is structural. A bank-marked reversal whose charge is on another
    day stays unresolved and is reported - the text is a check on the structural
    rule, never a substitute for it.
    """
    from cassoulet.stages.reversal_refund_processor import ReversalRefundProcessor
    charge = _out("324.22", payee="VIRGIN MONEY", when=date(2025, 11, 20), eid="orig")
    charge.metadata["expense_account"] = "Expenses:Utilities:Mobile"
    marked = _in("324.22", payee="REVERSAL OF 20-11", narration="VIRGIN MONEY",
                 when=date(2025, 11, 24), eid="rev")

    proc = ReversalRefundProcessor()
    envelopes, warnings = proc._process_internal([charge, marked])[:2]

    assert "reverses_envelope_id" not in marked.metadata
    assert any("Bank marked this a reversal" in w.message for w in warnings)


def test_a_same_day_partial_refund_is_still_a_refund():
    """Same-day does not mean reversal when the amounts differ.

    A GBP 9.90 credit against a GBP 29.70 charge at one merchant on one day is a
    partial refund. Treating every same-day pair as the reversal rule's business
    left this one unmatched and put the money back in Income:Other.
    """
    charge = _out("29.70", payee="NORTHCOTE LONDON", when=date(2025, 9, 29), eid="c")
    back = _in("9.90", payee="NORTHCOTE LONDON", when=date(2025, 9, 29), eid="b")
    assert is_refund_of(back, charge)
    assert not is_reversal_of(back, charge), "amounts differ, so not a reversal"
