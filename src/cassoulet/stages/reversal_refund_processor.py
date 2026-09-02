"""Reversal and refund resolution.

A reversal or a refund is money coming back on the SAME account it left from.
That makes it structurally unlike a transfer, which moves money between two
accounts, and it is why these cannot be resolved by the transfer matcher: the
scoring config kills same-account pairs at -500 and -1000 precisely because a
self-transfer is impossible.

The outcome differs too. A transfer merges two legs into one movement. A
reversal or refund keeps both transactions - the bank really did do two things,
and the audit trail should say so - but the money coming back belongs against
whatever the original was booked to, so the two net to zero inside that account
instead of appearing as income.

Left unresolved they are actively misleading. In the reference ledger a single
UK tax year carried GBP 1,851.59 of reversals and GBP 161.69 of refunds sitting
in Income:Other, and because a reversal restores a balance the importer could
not otherwise explain, the gap detector fabricated remediation entries for the
same amounts on top - so one reversal produced two phantom income postings.

This runs AFTER expense categorisation, so the original already carries the
account the reversal should inherit.
"""

import logging
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from cassoulet.stages.envelope import Envelope
from cassoulet.stages.envelope_processor import EnvelopeProcessor
from cassoulet.base.exceptions import ProcessingWarning
from cassoulet.utils.accounts import account_is_bank, account_is_credit_card
from cassoulet.utils.envelope_utilities import (
    _counterparty,
    get_absolute_amount,
    get_primary_account,
    has_inbound_units,
    has_outbound_units,
    is_refund_of,
    is_reversal_of,
)

logger = logging.getLogger(__name__)

# How far apart the two legs may sit. A bank reversal is near-immediate - the
# reference data has every pair on the same day, one within a day. A merchant
# refund is a different animal: card refunds routinely take weeks, and a returned
# order can be refunded in pieces, so the payee agreement has to carry the weight
# over a much longer window.
REVERSAL_MAX_DAYS = 7
REFUND_MAX_DAYS = 120

# Employer expense-reimbursement suggestions.
#
# A reimbursement is a batch payment from an employer covering expenses paid
# personally weeks earlier. Nothing in the bank data links the two - no claim
# reference, no line items - so this searches for the combination of prior card
# spending that sums to the payment.
#
# THE NUMBERS BELOW ARE IN-SAMPLE. The window and item cap were CHOSEN using
# the same 200 controls the rate is then quoted from, which makes 4.5% a
# selection-biased fit rather than a validated rate. With 9 positives in 200 the
# confidence interval alone spans roughly 2-8%. Treat them as "this ballpark",
# not as a measured error rate.
#
# The controls are also uniform-random amounts, which is the wrong null: real
# amounts cluster on round pounds, .00, .50, .99 and restaurant totals, and
# clustering raises collision rates. Proper validation needs real non-claim
# inbound payments, held out by time and payer. Not done.
#
# Against three known claims and 200 random control amounts:
#
#   window   real found   false positives
#     40d       3/3            3.0%
#     60d       3/3            4.5%
#     70d       3/3            7.0%
#   max 3 items instead of 2, at 90d:            21%
#
# Flat between 40 and 60 days, degrading past 70. 60 is chosen over 40 for
# headroom: the observed gaps are 11, 24 and 40 days, so 40 only just clears
# the longest one and would fail silently if the employer batched a week later.
#
# UNIQUENESS AT MINIMUM CARDINALITY is the actual test. Counting subsets across
# all sizes destroys the signal - one claim has 32 subsets in total but exactly
# one at size 1. Prefer the fewest items, and require that answer to be alone.
#
# UNIQUENESS IS NOT AUTHENTICATION. With ~250 candidates there are ~31,000
# pairs; spread over the plausible range of penny values that is well under one
# expected accidental hit, so "exactly one hit" happens by chance a fair share
# of the time. Uniqueness means no SECOND coincidence existed in a sparse,
# truncated pool - not that this pair caused the payment. Shrinking the window
# can perversely convert ambiguity into apparent confidence.
#
# SENSITIVITY IS UNKNOWN. Three for three sets a 95% lower bound near 29%, from
# one payer in one six-month window. Known blind spots, all silent: claims of
# three or more items, payment more than 60 days after spending, split or
# partial or net-of-advance claims, expenses paid from a bank account or another
# card, and any employer not in the allow-list below.
#
# SUGGESTION ONLY, and it must stay that way. This is an amount coincidence,
# not evidence of business purpose. It must never feed automatic postings, a
# deductible-expense total, or a tax figure.
REIMBURSEMENT_WINDOW_DAYS = 60
REIMBURSEMENT_MAX_ITEMS = 2

# WHICH receivable accounts represent an employer settling an expense claim.
# Named explicitly rather than matched by prefix: "Assets:Receivable" also
# catches Assets:Receivable:Family:Sara, and treating spousal transfers as
# expense claims produced confident nonsense - GBP 4,000 "covered by" two
# GBP 2,000 school-fee payments, GBP 1,000 by a family ski holiday.
#
# A claim must also arrive in a BANK account. An employer pays into a current
# account, not onto a credit card, and money appearing on the card is a refund
# from a merchant rather than a reimbursement from anyone.
EXPENSE_CLAIM_RECEIVABLE_ACCOUNTS = (
    'Assets:Receivables:SimmonsSimmonsLLP',
)


class ReversalRefundProcessor(EnvelopeProcessor):
    """Point reversals and refunds at the account their original was booked to."""

    processor_name = "ReversalRefundProcessor"

    def __init__(self, enabled: bool = True):
        super().__init__()
        self.enabled = enabled
        self.stats = {
            'reversals_resolved': 0,
            'refunds_resolved': 0,
            'unresolved': 0,
            'reimbursements_suggested': 0,
        }

    def _process_internal(
        self, envelopes: List[Envelope]
    ) -> Tuple[List[Envelope], List[ProcessingWarning]]:
        # EnvelopeProcessor.process_envelopes builds the metadata tier itself
        # (input/output counts, merge audit), so this returns the 2-tuple.
        if not self.enabled:
            return envelopes, []

        warnings: List[ProcessingWarning] = []

        # How much of each original has already been claimed. An original can
        # legitimately be refunded in pieces - a returned order often comes back
        # as several credits - but the pieces must not add up to more than was
        # paid. Without this one purchase was being claimed by three separate
        # credits, crediting its expense account three times against a single
        # debit. 13 originals were over-claimed on the reference ledger.
        claimed: Dict[str, Decimal] = {}

        # Index by account so a candidate search never scans the whole ledger.
        by_account: Dict[str, List[Envelope]] = {}
        for env in envelopes:
            account = get_primary_account(env)
            if account:
                by_account.setdefault(account, []).append(env)

        for env in envelopes:
            kind = self._kind(env)
            if kind is None:
                continue
            if env.metadata.get('reverses_envelope_id'):
                continue  # already resolved, e.g. on a re-run

            original = self._find_original(env, by_account, kind, claimed)
            if original is None:
                if kind == 'refund':
                    # Most inbound money is simply not a refund. Silence here is
                    # correct; a reversal failing to pair is the real anomaly.
                    continue
                self.stats['unresolved'] += 1
                warnings.append(ProcessingWarning(
                    processor_name=self.processor_name,
                    severity='WARNING',
                    message=(
                        f"{kind.capitalize()} with no matching original: "
                        f"{env.payee or env.narration}"
                    ),
                    source_transaction=None,
                    details={
                        'envelope_id': env.envelope_id,
                        'date': str(env.date),
                        'account': get_primary_account(env),
                        'kind': kind,
                    },
                ))
                continue

            account = self._original_account(original)
            if account is None:
                if kind == 'refund':
                    continue
                self.stats['unresolved'] += 1
                warnings.append(ProcessingWarning(
                    processor_name=self.processor_name,
                    severity='WARNING',
                    message=(
                        f"Matched the original for this {kind} but it has no account "
                        f"to inherit: {env.payee or env.narration}"
                    ),
                    source_transaction=None,
                    details={
                        'envelope_id': env.envelope_id,
                        'original_envelope_id': original.envelope_id,
                        'kind': kind,
                    },
                ))
                continue

            # The writer takes these ahead of every other rule, which is what
            # makes the pair net to zero in the original's account.
            env.metadata['income_account'] = account
            env.metadata['expense_account'] = account
            env.metadata['reverses_envelope_id'] = original.envelope_id
            env.metadata['reversal_kind'] = kind
            env.add_history(
                state=env.state,
                component=self.processor_name,
                action=f"Resolved {kind}",
                details={
                    'original_envelope_id': original.envelope_id,
                    'original_date': str(original.date),
                    'inherited_account': account,
                },
            )
            amount = get_absolute_amount(env) or Decimal(0)
            claimed[original.envelope_id] = claimed.get(original.envelope_id, Decimal(0)) + amount
            self.stats[f'{kind}s_resolved'] += 1

        logger.info(
            "  Reversals resolved: %d, refunds resolved: %d, unresolved: %d",
            self.stats['reversals_resolved'],
            self.stats['refunds_resolved'],
            self.stats['unresolved'],
        )
        warnings.extend(self._suggest_reimbursement_claims(envelopes))
        return envelopes, warnings

    def _suggest_reimbursement_claims(
        self, envelopes: List[Envelope]
    ) -> List[ProcessingWarning]:
        """Suggest which card spending a reimbursement is likely to cover.

        Emits warnings and changes nothing. See the module constants for why
        this suggests rather than posts.
        """
        from itertools import combinations

        claims = [
            e for e in envelopes
            if has_inbound_units(e) and not has_outbound_units(e)
            and str(e.metadata.get('income_account', '')) in EXPENSE_CLAIM_RECEIVABLE_ACCOUNTS
            and e.inbound_account and account_is_bank(e.inbound_account)
        ]
        if not claims:
            return []

        # Card spending only. Expenses go on the card; bank debits are bills and
        # transfers, and including them enlarges the pool without adding signal.
        spend = []
        for e in envelopes:
            account = e.outbound_account
            if (has_outbound_units(e) and account and account_is_credit_card(account)
                    and e.date and e.outbound_units):
                spend.append(e)

        # An expense can only be reimbursed once. Without this the same dinner
        # is cited by every claim it happens to fit, which is how repeated
        # equal reimbursements end up all pointing at one meal.
        used_expense_ids: set = set()

        # Money you got back is not money you can claim. This stage has just
        # resolved reversals and refunds, so the originals they cancel are known
        # - drop them from the pool rather than offering a refunded dinner as
        # the explanation for a reimbursement.
        cancelled_expense_ids = {
            e.metadata['reverses_envelope_id'] for e in envelopes
            if e.metadata.get('reverses_envelope_id')
        }

        found: List[ProcessingWarning] = []
        for claim in claims:
            target = get_absolute_amount(claim)
            if target is None or not claim.date:
                continue
            # Same currency, unused, and inside the window. Currency is checked
            # because nothing else does: every card posting is GBP today, but a
            # foreign-currency card would otherwise let a GBP claim "exactly"
            # match a USD total.
            claim_ccy = claim.inbound_type
            pool = [s for s in spend
                    if 0 < (claim.date - s.date).days <= REIMBURSEMENT_WINDOW_DAYS
                    and s.outbound_type == claim_ccy
                    and s.envelope_id not in used_expense_ids
                    and s.envelope_id not in cancelled_expense_ids]

            match = None
            for size in range(1, REIMBURSEMENT_MAX_ITEMS + 1):
                hits = []
                for c in combinations(pool, size):
                    if sum(x.outbound_units for x in c) == target:
                        hits.append(c)
                        if len(hits) > 1:
                            break  # ambiguous; no need to enumerate the rest
                if len(hits) == 1:
                    match = hits[0]
                    break
                if hits:
                    break  # ambiguous at this size: decline rather than guess
            if not match:
                continue

            used_expense_ids.update(x.envelope_id for x in match)
            self.stats['reimbursements_suggested'] += 1
            found.append(ProcessingWarning(
                processor_name=self.processor_name,
                severity='INFO',
                message=(
                    f"AMOUNT COINCIDENCE (not evidence) - reimbursement of {target} "
                    f"may cover: "
                    + ", ".join(f"{x.outbound_units} {x.payee or x.narration}" for x in match)
                ),
                source_transaction=None,
                details={
                    'envelope_id': claim.envelope_id,
                    'claim_date': str(claim.date),
                    'amount': str(target),
                    'suggested_expense_ids': [x.envelope_id for x in match],
                    'suggested_expenses': [
                        {'date': str(x.date), 'amount': str(x.outbound_units),
                         'description': x.payee or x.narration}
                        for x in match
                    ],
                    'basis': (
                        f'unique at {len(match)} item(s) within '
                        f'{REIMBURSEMENT_WINDOW_DAYS} days; suggestion only'
                    ),
                },
            ))
        # Logged individually, not just counted. ProcessingWarnings at INFO
        # reach NO output in this pipeline - the forensic report renders only
        # CRITICAL and validation errors, so 22,000 INFO warnings a run are
        # computed and discarded. A suggestion nobody can read is not a
        # suggestion. These go to the main log so they can be grepped:
        #   grep "may cover" logs/<timestamp>/importer_<timestamp>.log
        for w in found:
            logger.info("  %s [%s]", w.message, w.details.get('basis', ''))
        if found:
            logger.info(
                "  Reimbursement claims suggested: %d - SUGGESTIONS ONLY, nothing "
                "reclassified, no tax figure affected. Each is an amount "
                "coincidence needing confirmation against the actual claim. "
                "Review with: grep 'AMOUNT COINCIDENCE' logs/<timestamp>/importer_*.log",
                len(found)
            )
        return found

    def _kind(self, env: Envelope) -> Optional[str]:
        """'reversal', 'refund', or None for the returning leg of a pair.

        A reversal announces itself in the payee, so it is cheap and certain to
        spot. A refund does not - it is only a refund by virtue of an earlier
        purchase existing - so the pairing has to establish it.

        The gate for a refund candidate is that money is coming BACK, which
        under the normalised convention is an inbound posting on either an asset
        (cash arriving) or a liability (debt decreasing). That skips ordinary
        spending, which is the overwhelming majority of any statement, so the
        candidate search stays cheap.

        Envelopes already carrying a specific account are left alone: something
        upstream identified them and this stage should not second-guess it.
        """
        if (env.payee or '').strip().upper().startswith('REVERSAL OF'):
            return 'reversal'
        # Card statements leave the payee empty and put the merchant in the
        # narration, so key off whichever the bank populated.
        if not _counterparty(env):
            return None
        if not has_inbound_units(env) or has_outbound_units(env):
            return None
        if self._has_specific_account(env):
            return None
        return 'refund'

    def _has_specific_account(self, env: Envelope) -> bool:
        for key in ('expense_account', 'income_account'):
            account = env.metadata.get(key)
            if account and account not in ('Expenses:UK:Unknown', 'Income:Other'):
                return True
        return False

    def _find_original(
        self, env: Envelope, by_account: Dict[str, List[Envelope]], kind: str,
        claimed: Dict[str, Decimal],
    ) -> Optional[Envelope]:
        account = get_primary_account(env)
        if not account or not env.date:
            return None
        window = REVERSAL_MAX_DAYS if kind == 'reversal' else REFUND_MAX_DAYS
        predicate = is_reversal_of if kind == 'reversal' else is_refund_of

        best = None
        for candidate in by_account.get(account, []):
            if candidate is env or not candidate.date:
                continue
            if candidate.date > env.date:
                continue  # an original cannot post after the thing undoing it
            if (env.date - candidate.date).days > window:
                continue
            if not predicate(env, candidate):
                continue
            # Do not claim more of an original than it was worth.
            already = claimed.get(candidate.envelope_id, Decimal(0))
            capacity = (get_absolute_amount(candidate) or Decimal(0)) - already
            if (get_absolute_amount(env) or Decimal(0)) > capacity:
                continue
            # Nearest in time wins: a repeat payee is far more likely to be
            # refunded against its most recent charge than an older one.
            if best is None or candidate.date > best.date:
                best = candidate
        return best

    def _original_account(self, original: Envelope) -> Optional[str]:
        """The account the original was booked to.

        Catch-alls are inherited too, deliberately. Netting is the point: two
        legs in Expenses:UK:Unknown cancel each other and the ledger correctly
        shows no net effect, which is strictly better than leaving the original
        in a catch-all and the money coming back as income. Whether the category
        is known is a separate problem, and fixing it later moves both legs
        together.
        """
        account = (original.metadata.get('expense_account')
                   or original.metadata.get('income_account'))
        if account:
            return account
        # Nothing categorised the original at all. The writer would have derived
        # its account from flow direction, and for a plain outbound bank payment
        # that is the expense catch-all - so send the reversal to the same place
        # rather than let it fall through to income.
        return 'Expenses:UK:Unknown'

    def get_stats(self) -> Dict[str, Any]:
        return dict(self.stats)
