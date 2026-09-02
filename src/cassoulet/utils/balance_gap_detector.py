"""
Balance Gap Detector for Envelopes

Envelope-native implementation that detects balance gaps and creates remediation envelopes.
Works with Envelope objects instead of raw CSV data, following DRY principles.

Part of the balance handling refactor.
"""

from dataclasses import dataclass
from decimal import Decimal
from datetime import date, timedelta
from typing import List, Optional
import logging

from cassoulet.stages.envelope import Envelope, EnvelopeState
from cassoulet.utils.envelope_utilities import create_envelope

logger = logging.getLogger(__name__)

# Where remediation for an unexplained balance gap is posted.
#
# A gap means the statement balance and the sum of imported transactions
# disagree: money moved and we do not know why. That is a plug, not a fact, and
# it must never masquerade as one. Previously the two legs went to Income:Other
# and Expenses:UK:Unknown, so invented entries were indistinguishable from real
# income and real spending - GBP 775.66 of fabricated income in a single UK tax
# year in the reference ledger, some of it duplicating reversals the importer
# had already seen.
#
# Equity rather than Assets:Suspense: holding a suspense ASSET implies you own
# something identifiable, whereas this is an admission that the books do not
# balance yet. One account for both directions, so the sign shows which way the
# gap ran and the balance can be watched toward zero as data is filled in.
BALANCE_GAP_PLUG_ACCOUNT = 'Equity:Plug:BalanceGap'


@dataclass
class BalanceGap:
    """Represents a detected balance gap."""
    account: str
    institution: str
    gap_date: date
    gap_amount: Decimal
    previous_date: Optional[date] = None
    previous_balance: Optional[Decimal] = None
    expected_balance: Optional[Decimal] = None
    actual_balance: Optional[Decimal] = None
    gap_type: str = 'missing_transactions'  # 'missing_transactions' or 'balance_error'

    @property
    def description(self) -> str:
        return f"Gap of {self.gap_amount:.2f} GBP on {self.gap_date}"


def detect_gaps(envelopes: List[Envelope],
                account: str,
                institution: str,
                tolerance: Decimal = Decimal('0.01')) -> List[BalanceGap]:
    """
    Detect balance gaps in sorted envelopes.

    This works with already-parsed Envelope data instead of raw CSV,
    eliminating duplicate parsing logic.

    Args:
        envelopes: Sorted list of envelopes (must be sorted by date/order)
        account: Account name for gap tracking
        institution: Institution name for remediation
        tolerance: Maximum acceptable balance difference

    Returns:
        List of detected balance gaps
    """
    gaps = []

    if not envelopes:
        return gaps

    # Check for opening balance from first express balance
    # The logic: if the first balance assertion implies a non-zero starting balance,
    # we need to create an opening balance entry
    first_balance_env = None
    first_balance_idx = -1
    for idx, env in enumerate(envelopes):
        if env.balance_after is not None and env.balance_type == 'express':
            first_balance_env = env
            first_balance_idx = idx
            break

    if first_balance_env:
        # Calculate what the opening balance must have been
        # Sum all transactions up to and including the first balance
        accumulated_before_first = Decimal('0')
        for i in range(first_balance_idx + 1):
            env = envelopes[i]
            if env.outbound_units:
                accumulated_before_first -= env.outbound_units
            if env.inbound_units:
                accumulated_before_first += env.inbound_units

        # Opening balance = First balance - accumulated amounts
        implied_opening = first_balance_env.balance_after - accumulated_before_first

        # If implied opening balance is non-zero, we need an opening balance entry
        if abs(implied_opening) > tolerance:
            logger.info(f"Detected opening balance of {implied_opening:.2f} for {account}")
            gaps.append(BalanceGap(
                account=account,
                institution=institution,
                gap_date=envelopes[0].date,  # Date of first transaction
                gap_amount=implied_opening,
                gap_type='opening_balance',
                actual_balance=first_balance_env.balance_after,
                expected_balance=accumulated_before_first  # What we'd expect if starting from 0
            ))

    # Track previous balance point for gap detection
    previous_date = None
    previous_balance = None
    accumulated_amount = Decimal('0')
    transactions_since_last_balance = []

    # Check EVERY express balance, but skip consecutive identical balances on same day
    # This catches duplicates while avoiding duplicate gap remediations
    for i, env in enumerate(envelopes):
        # Track transaction amount
        transaction_amount = Decimal('0')
        if env.outbound_units:
            transaction_amount -= env.outbound_units
        if env.inbound_units:
            transaction_amount += env.inbound_units

        if env.balance_after is None or env.balance_type != 'express':
            # No express balance - accumulate the amount
            accumulated_amount += transaction_amount
            transactions_since_last_balance.append(env)
            continue

        # We have an express balance - check it as an assertion point
        logger.debug(f"Checking balance assertion on {env.date}: {env.balance_after}")

        # Reset transactions list when moving to a NEW date (before adding current txn)
        # This allows same-day duplicates to accumulate in the list
        if previous_date is not None and previous_date != env.date:
            transactions_since_last_balance = []

        # Add current transaction to the list
        transactions_since_last_balance.append(env)

        # Check for gap if we have a previous balance point
        if previous_balance is not None and previous_date is not None:
            # Calculate expected balance
            expected_balance = previous_balance + accumulated_amount + transaction_amount

            # Check if actual balance matches expected
            gap = env.balance_after - expected_balance

            # Use >= to catch gaps exactly at tolerance boundary (e.g., 0.01)
            if abs(gap) >= tolerance:
                # Determine gap type
                # Count transactions excluding the current one for more accurate classification
                prior_transactions = len(transactions_since_last_balance) - 1
                if prior_transactions == 0:
                    # TRUE GAP: No transactions between balance points
                    gap_type = 'missing_transactions'
                    logger.info(f"TRUE GAP: {gap:.2f} between {previous_date} and {env.date}")
                else:
                    # Balance inconsistency despite having transactions
                    gap_type = 'balance_error'
                    logger.warning(f"BALANCE ERROR: {gap:.2f} on {env.date} despite {prior_transactions} transactions")

                gaps.append(BalanceGap(
                    account=account,
                    institution=institution,
                    gap_date=env.date,
                    gap_amount=gap,
                    previous_date=previous_date,
                    previous_balance=previous_balance,
                    expected_balance=expected_balance,
                    actual_balance=env.balance_after,
                    gap_type=gap_type
                ))

        # Update state for next assertion point
        previous_balance = env.balance_after
        previous_date = env.date
        accumulated_amount = Decimal('0')
        # Note: transactions_since_last_balance is reset at the start of each new date

    logger.info(f"Detected {len(gaps)} balance gaps for {account}")
    for gap in gaps:
        logger.info(f"  {gap.description}")

    return gaps


def create_remediations(gaps: List[BalanceGap]) -> List[Envelope]:
    """
    Create remediation envelopes for detected gaps.

    TODO: When fully implementing gap remediation output:
    - Write remediation transactions to balance_remediations_YYYY.beancount files
    - Update includes.beancount to include these remediation files
    - The pipeline._write_gap_remediations() and _update_includes_with_files()
      methods provide the framework for this

    Args:
        gaps: List of detected balance gaps

    Returns:
        List of remediation envelopes
    """
    remediations = []

    for idx, gap in enumerate(gaps):
        # Determine narration based on gap type
        if gap.gap_type == 'opening_balance':
            narration = (f"OPENING BALANCE: {gap.account} | "
                        f"Initial balance of {gap.gap_amount:.2f} GBP")
        elif gap.gap_type == 'balance_error':
            narration = (f"BALANCE CORRECTION: Discrepancy on {gap.gap_date} | "
                        f"Correction of {gap.gap_amount:.2f} GBP")
        else:
            narration = (f"MISSING TRANSACTIONS: Gap detected on {gap.gap_date} | "
                        f"Balance gap of {gap.gap_amount:.2f} GBP")

        # Create remediation envelope using safe creation function
        # NOTE: Both legs must be populated for a complete transaction

        if gap.gap_type == 'opening_balance':
            # Opening balance: gap_amount is the opening balance VALUE
            # Always ADD to account from Income:Other (regardless of sign)
            inbound_account = gap.account
            inbound_units = abs(gap.gap_amount)
            outbound_account = BALANCE_GAP_PLUG_ACCOUNT
            outbound_units = abs(gap.gap_amount)
        elif gap.gap_type == 'balance_error':
            # Balance errors: Gap = CSV_balance - calculated_from_transactions
            # The calculated value is what gets imported to beancount
            # Negative gap = CSV < calculated = need to SUBTRACT to match CSV
            # Positive gap = CSV > calculated = need to ADD to match CSV
            # This is INVERTED from missing_transactions logic!

            if gap.gap_amount < 0:
                # Negative gap: CSV balance < calculated, need to SUBTRACT
                outbound_account = gap.account
                outbound_units = abs(gap.gap_amount)
                inbound_account = BALANCE_GAP_PLUG_ACCOUNT
                inbound_units = abs(gap.gap_amount)
            else:
                # Positive gap: CSV balance > calculated, need to ADD
                inbound_account = gap.account
                inbound_units = abs(gap.gap_amount)
                outbound_account = BALANCE_GAP_PLUG_ACCOUNT
                outbound_units = abs(gap.gap_amount)
        else:
            # Missing transactions: Gap = actual - expected
            # For missing_transactions type, there are NO known transactions between balance points
            # So gap represents the balance CHANGE that occurred
            # Negative gap = balance decreased → record EXPENSE (money out)
            # Positive gap = balance increased → record INCOME (money in)

            if gap.gap_amount < 0:
                # Negative gap: balance decreased, record as expense
                outbound_account = gap.account
                outbound_units = abs(gap.gap_amount)
                inbound_account = BALANCE_GAP_PLUG_ACCOUNT
                inbound_units = abs(gap.gap_amount)
            else:
                # Positive gap: balance increased, record as income
                inbound_account = gap.account
                inbound_units = abs(gap.gap_amount)
                outbound_account = BALANCE_GAP_PLUG_ACCOUNT
                outbound_units = abs(gap.gap_amount)

        # Date the remediation appropriately:
        # - opening_balance & missing_transactions: day BEFORE gap (no transactions on gap day)
        # - balance_error: SAME day as gap (to cancel duplicate transactions on that day)
        if gap.gap_type == 'balance_error':
            remediation_date = gap.gap_date
        else:
            remediation_date = gap.gap_date - timedelta(days=1)

        remediation = create_envelope(
            reason=f"{gap.gap_type} remediation for {gap.account} on {gap.gap_date}",
            date=remediation_date,
            narration=narration,
            payee=None,
            flag='*',
            # Both legs must be populated
            inbound_units=inbound_units,
            inbound_type='GBP',
            inbound_account=inbound_account,
            outbound_units=outbound_units,
            outbound_type='GBP',
            outbound_account=outbound_account,
            # Balance tracking
            balance_after=None,  # Will be computed later
            balance_type=None,
            # Special identification - include index to ensure uniqueness
            envelope_id=(f"opening_balance_{gap.account.replace(':', '_')}_{gap.gap_date}_{idx}"
                        if gap.gap_type == 'opening_balance'
                        else f"gap_remediation_{gap.account.replace(':', '_')}_{gap.gap_date}_{idx}"),
            source='balance_gap_detector',
            metadata={
                'gap_type': gap.gap_type,
                'gap_amount': str(gap.gap_amount),
                'expected_balance': str(gap.expected_balance) if gap.expected_balance else None,
                'actual_balance': str(gap.actual_balance) if gap.actual_balance else None,
                'missing_data_tag': True,
                # Mark as not eligible for transfer matching
                'transfer_eligible': False,
                'dont_match_reason': 'Auto-generated gap remediation',
                'transaction_type_hint': 'REMEDIATION'
            }
        )

        # Add history
        remediation.add_history(
            state=EnvelopeState.INGESTED,
            component='balance_gap_detector',
            action=f'Created {gap.gap_type} remediation',
            details={
                'gap_amount': str(gap.gap_amount),
                'gap_date': str(gap.gap_date)
            }
        )

        # Set comprehensive tags based on gap type
        remediation.tags.add('gap-remediation')  # Always add this

        if gap.gap_type == 'opening_balance':
            remediation.tags.add('opening-balance')
        elif gap.gap_type == 'balance_error':
            remediation.tags.add('balance-correction')
            remediation.tags.add('missing-data')
        else:  # missing_transactions
            remediation.tags.add('missing-data')
            remediation.tags.add('balance-gap')

        remediations.append(remediation)

    logger.info(f"Created {len(remediations)} remediation envelopes")

    return remediations