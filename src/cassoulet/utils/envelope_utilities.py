"""
Envelope utility functions for reading and analyzing envelope data.

These utilities provide a clean interface for accessing envelope data
without cluttering the core Envelope data structure.

Also includes EnvelopeBuilder for creating envelopes from various sources.
"""

import logging
import re
import sys
from typing import Optional, Dict, Any, Set, List, Union, Tuple
from decimal import Decimal
from datetime import date
from dataclasses import replace

from cassoulet.utils.currencies import is_currency, is_commodity
from cassoulet.stages import Envelope
from cassoulet.utils.accounts import (
    account_is_sipp,
    account_is_isa,
    account_is_bank,
    account_is_broker,
    account_is_liability,
    account_institution as get_account_institution,
)

logger = logging.getLogger(__name__)


class NotApplicable:
    """Sentinel for when a calculation doesn't apply to the envelope pair.

    This is used when an operation doesn't make semantic sense for the given
    envelopes (e.g., calculating transfer delta for parallel flows).
    """
    def __repr__(self):
        return "NOT_APPLICABLE"

    def __bool__(self):
        return False  # Falsy for conditionals

    def __eq__(self, other):
        return isinstance(other, NotApplicable)


# Singleton instance
NOT_APPLICABLE = NotApplicable()


def get_amount(envelope: Envelope) -> Optional[Decimal]:
    """
    Get the primary amount for this envelope.
    
    For transfers and expenses: returns outbound_units (negative conceptually)
    For income and dividends: returns inbound_units (positive conceptually)
    Returns None if no amount is present.
    """
    # If we have both, prefer outbound (it's the "active" side of the transaction)
    if envelope.outbound_units is not None:
        return -abs(envelope.outbound_units)  # Negative for outflows
    elif envelope.inbound_units is not None:
        return abs(envelope.inbound_units)  # Positive for inflows
    return None


def get_absolute_amount(envelope: Envelope) -> Optional[Decimal]:
    """Get the absolute value of the primary amount."""
    amount = get_amount(envelope)

    if amount is None:
        return None
    else:
        return abs(amount)

def get_currency(envelope: Envelope) -> Optional[str]:
    """
    Get the currency for this envelope.

    Returns the currency symbol (GBP, USD, etc.) if this is a cash transaction.
    For commodity transactions, returns the cash side's currency if present.
    """
    # Check direct attributes first
    if get_outbound_type(envelope) and is_currency(get_outbound_type(envelope)):
        return get_outbound_type(envelope)
    if get_inbound_type(envelope) and is_currency(get_inbound_type(envelope)):
        return get_inbound_type(envelope)

    return None


def get_commodity(envelope: Envelope) -> Optional[str]:
    """
    Get the commodity for this envelope.

    Returns the commodity symbol (VLS100, AAPL, etc.) if this is an investment transaction.
    """
    # Check direct attributes first
    if get_outbound_type(envelope) and is_commodity(get_outbound_type(envelope)):
        return get_outbound_type(envelope)
    if get_inbound_type(envelope) and is_commodity(get_inbound_type(envelope)):
        return get_inbound_type(envelope)

    return None


def get_outbound_account(envelope: Envelope) -> Optional[str]:
    """
    Get the outbound (source) account from the envelope.

    Checks in order:
    1. Direct outbound_account attribute
    2. transaction_data['outbound_account']
    3. metadata['transfer_from'] for merged transfers

    Returns:
        Account string or None if no outbound account
    """
    # Try direct attribute first
    if hasattr(envelope, 'outbound_account') and envelope.outbound_account:
        return envelope.outbound_account
    # Check transaction_data
    if hasattr(envelope, 'transaction_data') and envelope.transaction_data:
        account = envelope.transaction_data.get('outbound_account')
        if account:
            return account
    # Check metadata for merged transfer info
    if envelope.metadata and 'transfer_from' in envelope.metadata:
        return envelope.metadata['transfer_from']
    return None


def get_inbound_account(envelope: Envelope) -> Optional[str]:
    """
    Get the inbound (destination) account from the envelope.

    Checks in order:
    1. Direct inbound_account attribute
    2. transaction_data['inbound_account']
    3. metadata['transfer_to'] for merged transfers

    Returns:
        Account string or None if no inbound account
    """
    # Try direct attribute first
    if hasattr(envelope, 'inbound_account') and envelope.inbound_account:
        return envelope.inbound_account
    # Check transaction_data
    if hasattr(envelope, 'transaction_data') and envelope.transaction_data:
        account = envelope.transaction_data.get('inbound_account')
        if account:
            return account
    # Check metadata for merged transfer info
    if envelope.metadata and 'transfer_to' in envelope.metadata:
        return envelope.metadata['transfer_to']
    return None


def get_all_accounts(envelope: Envelope) -> Set[str]:
    """
    Get all accounts referenced by this envelope.

    Returns:
        Set of account strings, excluding None
    """
    accounts = set()

    outbound = get_outbound_account(envelope)
    if outbound:
        accounts.add(outbound)

    inbound = get_inbound_account(envelope)
    if inbound:
        accounts.add(inbound)

    return accounts


def get_commodity_units(envelope: Envelope) -> Optional[Decimal]:
    """Get the number of commodity units (shares) in this transaction."""
    # Check direct attributes first
    if get_outbound_type(envelope) and is_commodity(get_outbound_type(envelope)):
        return get_outbound_units(envelope)
    
    envelope.outbound_units
    if get_inbound_type(envelope) and is_commodity(get_inbound_type(envelope)):
        return envelope.inbound_units

    return None


def get_primary_account(envelope: Envelope) -> Optional[str]:
    """
    Get the primary account for this envelope.

    For expenses/transfers out: returns outbound_account
    For income/transfers in: returns inbound_account
    """
    # Prefer the account that matches the primary amount
    if envelope.outbound_units is not None:
        return get_outbound_account(envelope)
    elif envelope.inbound_units is not None:
        return get_inbound_account(envelope)
    # Fallback to any available account
    return get_outbound_account(envelope) or get_inbound_account(envelope)


def is_cash_only(envelope: Envelope) -> bool:
    """Check if this envelope only involves cash (no commodities)."""
    # Get all types from the envelope (checks both direct and transaction_data)
    types = get_envelope_types(envelope)

    # If no types, it's cash only (None is treated as cash)
    if not types:
        return True

    # Check if all types are currencies
    return all(is_currency(t) for t in types)


def has_commodity(envelope: Envelope) -> bool:
    """Check if this envelope involves any commodity."""
    return get_commodity(envelope) is not None


def get_cash_amount(envelope: Envelope) -> Optional[Decimal]:
    """
    Get the cash amount for this envelope.

    For BUY transactions: returns the cash outflow (negative)
    For SELL transactions: returns the cash inflow (positive)
    For cash transfers: returns the amount with appropriate sign
    """
    if is_currency(get_outbound_type(envelope)) and get_outbound_units(envelope):
            return -abs(get_outbound_units(envelope))
    if is_currency(get_inbound_type(envelope)) and get_inbound_units(envelope):
        return abs(get_inbound_units(envelope))

    return None


def get_broker_commission(envelope: Envelope) -> Optional[Tuple[Decimal, str]]:
    """
    Extract broker commission from an envelope if present.

    Checks for commission in:
    1. Manual postings with Commission or Investment expense accounts
    2. Additional postings with commission-related accounts
    3. Metadata fields for commission amounts

    Returns:
        Tuple of (commission_amount, account) if found, None otherwise
    """
    # Check manual postings for explicit commission
    if envelope.metadata and envelope.metadata.get('manual_postings'):
        manual_postings = envelope.metadata['manual_postings']

        # Handle both string and pre-parsed list formats
        if isinstance(manual_postings, str):
            import ast
            try:
                manual_postings = ast.literal_eval(manual_postings)
            except (ValueError, SyntaxError):
                manual_postings = []

        # Look for commission postings
        for posting in manual_postings:
            if isinstance(posting, dict):
                account = posting.get('account', '')
                # Check for commission-related accounts
                if ('Commission' in account or
                    'Expenses:Investment' in account or
                    'Expenses:Fees' in account):
                    # Extract amount from units string
                    units_str = posting.get('units', '')
                    if isinstance(units_str, str) and units_str:
                        parts = units_str.split()
                        if parts:
                            try:
                                amount = Decimal(parts[0])
                                return (amount, account)
                            except (ValueError, TypeError):
                                pass

    # Check additional postings for commission
    if envelope.additional_postings:
        for ap in envelope.additional_postings:
            account = ap.get('account', '')
            if ('Commission' in account or
                'Expenses:Investment' in account or
                'Expenses:Fees' in account):
                units = ap.get('units', 0)
                if units and units > 0:
                    return (Decimal(str(units)), account)

    # Check metadata for direct commission field
    if envelope.metadata:
        if 'commission' in envelope.metadata:
            try:
                amount = Decimal(str(envelope.metadata['commission']))
                return (amount, 'Expenses:Investment:Commission')
            except (ValueError, TypeError):
                pass

    return None


def envelope_has_broker_commission(envelope: Envelope) -> bool:
    """
    Check if an envelope includes broker commission.

    Returns:
        True if commission is found, False otherwise
    """
    return get_broker_commission(envelope) is not None


def get_accounts(envelope: Envelope) -> tuple[Optional[str], Optional[str]]:
    """
    Get both accounts from the envelope.

    Checks both direct fields and metadata for transfer accounts.

    Returns:
        (outbound_account, inbound_account)
    """
    return (get_outbound_account(envelope), get_inbound_account(envelope))


def is_transfer_pattern(envelope: Envelope) -> bool:
    """
    Check if this envelope has the pattern of a transfer.

    Transfers have both inbound and outbound with matching types.
    """
    # Use the helpers that check both direct attributes and transaction_data
    if not (has_outbound_units(envelope) and has_inbound_units(envelope)):
        return False

    # Get all types to check if they match
    types = get_envelope_types(envelope)

    # A transfer has exactly one type (same on both sides)
    # If we have 2 types, it's a BUY/SELL not a transfer
    return len(types) == 1

def is_cross_institution_transfer(envelope: Envelope) -> bool:
    """
    Check if envelope represents a transfer between different institutions.

    Args:
        envelope: Envelope to check

    Returns:
        True if this is a transfer between different institutions
    """
    if not is_transfer_pattern(envelope):
        return False

    outbound_account = get_outbound_account(envelope)
    inbound_account = get_inbound_account(envelope)

    if not outbound_account or not inbound_account:
        return False

    # Import here to avoid circular dependency
    from cassoulet.utils.accounts import account_institution

    source_inst = account_institution(outbound_account)
    dest_inst = account_institution(inbound_account)

    return source_inst and dest_inst and source_inst != dest_inst


def is_investment_transaction(envelope: Envelope) -> bool:
    """
    Check if this envelope represents an investment transaction.
    
    Investment transactions involve at least one commodity.
    """
    return has_commodity(envelope)


def get_transaction_direction(envelope: Envelope) -> str:
    """
    Get the direction of the transaction.

    Returns:
        'outbound' if money/assets are leaving
        'inbound' if money/assets are arriving
        'transfer' if moving between accounts
        'unknown' if unclear
    """
    if has_both_legs(envelope):
        if get_outbound_account(envelope) and get_inbound_account(envelope):
            return 'transfer'
    elif has_outbound_units(envelope):
        return 'outbound'
    elif has_inbound_units(envelope):
        return 'inbound'
    return 'unknown'


def classify_transaction_type(envelope: Envelope) -> str:
    """
    Determine transaction type from envelope pattern.
    
    Classification rules:
    - Both sides same currency -> TRANSFER
    - Both sides same commodity -> COMMODITY_TRANSFER  
    - Currency out, commodity in -> BUY
    - Commodity out, currency in -> SELL
    - Only outbound -> EXPENSE
    - Only inbound -> INCOME
    - Different currencies -> raises ForexNotSupportedError
    
    Returns:
        Transaction type string
        
    Raises:
        ForexNotSupportedError: If forex transaction detected
    
    TODO: Consider expanding transaction types to include:
    - REINVEST: Dividend reinvestment (commodity in, commodity in)
    - SPLIT: Stock split (commodity in/out same account)
    - MERGER: Merger/acquisition 
    - CORPORATE_ACTION: Other non-cash corporate actions
    - CONTRIBUTION: Pension/ISA contribution
    - EMPLOYER_CONTRIBUTION: Employer pension contribution
    - TAX_RELIEF: Tax relief on pension contributions
    - FEE: Management or trading fees (currently just EXPENSE)
    
    Pattern ideas from old TransactionClassifier to consider:
    - Dividend: ['dividend', 'div', 'distribution', 'income distribution']
    - Interest: ['interest', 'gross interest', 'net interest', 'deposit interest']
    - Fees: ['fee', 'charge', 'commission', 'penalty', 'platform fee', 'dealing charge']
    - Tax relief: ['tax relief', 'pension tax relief', 'relief at source']
    - Contributions: ['contribution', 'transfer value', 'pension contribution']
    - Broker patterns: ['hargreaves', 'hl', 'interactive investor', 'ii', 'aj bell']
    
    Could enhance classification with narration pattern matching combined with
    envelope structure validation (e.g., 'dividend' + inbound cash only = DIVIDEND).
    
    For now we keep the simple set that covers 99% of cases.
    """
    from cassoulet.base.exceptions import ForexNotSupportedError

    if has_both_legs(envelope):
        # Two-sided transaction
        # We need the specific types to determine BUY vs SELL
        outbound_type = get_outbound_type(envelope)
        inbound_type = get_inbound_type(envelope)

        if outbound_type == inbound_type:
            # Same type on both sides = transfer
            if is_currency(outbound_type):
                return 'TRANSFER'
            else:
                return 'COMMODITY_TRANSFER'
        else:
            # Different types
            out_is_currency = is_currency(outbound_type)
            in_is_currency = is_currency(inbound_type)

            if out_is_currency and in_is_currency:
                # Two different currencies = forex (not supported)
                raise ForexNotSupportedError(
                    f"Forex detected: {outbound_type} -> {inbound_type}"
                )
            elif out_is_currency and is_commodity(inbound_type):
                return 'BUY'
            elif is_commodity(outbound_type) and in_is_currency:
                return 'SELL'
            else:
                # This shouldn't happen - two different commodities?
                return 'UNKNOWN'
    elif has_outbound_units(envelope):
        return 'EXPENSE'
    elif has_inbound_units(envelope):
        return 'INCOME'
    
    return 'UNKNOWN'


def validate_no_field_loss(
    original: Envelope,
    modified: Envelope
) -> None:
    """
    Ensure critical fields weren't dropped during enhancement.

    Args:
        original: Original envelope
        modified: Modified envelope

    Raises:
        ValueError: If a critical field was lost
    """
    import logging
    logger = logging.getLogger(__name__)

    # Define critical fields that must never be lost
    critical_fields = [
        'consumed_lots',
        'lot_created',
        'transferred_lots',
        'envelope_id',
        'commodity',
        'units',
        'price'
    ]

    for field_name in critical_fields:
        orig_val = getattr(original, field_name, None)
        new_val = getattr(modified, field_name, None)

        # Check if field was present but is now None
        if orig_val is not None and new_val is None:
            error_msg = (
                f"CRITICAL: Field '{field_name}' was lost during enhancement. "
                f"Original value: {orig_val}. "
                f"This is a Steel Thread violation! (Issue #118)"
            )
            logger.critical(error_msg)
            raise ValueError(error_msg)


def enhance_envelope(
    envelope: Envelope,
    reason: str = None,
    **updates
) -> Envelope:
    """
    Safe enhancement that preserves all fields.

    This is the approved way to modify an envelope outside of processors.
    It guarantees that no fields are silently dropped.

    Args:
        envelope: Envelope to enhance
        reason: Why this enhancement is needed (for audit trail)
        **updates: Fields to update (only specify what changes)

    Returns:
        Enhanced envelope with all fields preserved

    Example:
        enhanced = enhance_envelope(
            envelope,
            reason="Mark as cash transfer",
            transaction_type='TRANSFER',
            metadata={**envelope.metadata, 'is_transfer': True}
        )
    """
    # Use dataclasses.replace to preserve ALL fields
    enhanced = replace(envelope, **updates)

    # Validate no critical fields were lost
    validate_no_field_loss(envelope, enhanced)

    # Add to history if reason provided
    if reason and hasattr(enhanced, 'add_history'):
        enhanced.add_history(
            state=enhanced.state,
            component='envelope_utilities',
            action=f"Enhanced: {reason}",
            details={'updates': list(updates.keys())}
        )

    return enhanced


def create_envelope(
    reason: str,
    **fields
) -> Envelope:
    """
    Create a new envelope with explicit tracking and ID generation.

    This is the single source of truth for envelope creation.
    It handles ID generation, metadata tracking, and audit trail.

    Args:
        reason: Why this envelope is being created (required)
        **fields: Fields for the new envelope

    Returns:
        New envelope with ID, creation tracked in metadata

    Example:
        remediation = create_envelope(
            reason="Balance gap remediation",
            date=date.today(),
            narration="Auto-generated balance correction",
            ...
        )
    """
    # Define known Envelope fields (everything else goes to metadata)
    ENVELOPE_FIELDS = {
        'date', 'narration', 'payee', 'flag',
        'outbound_units', 'outbound_type', 'outbound_account',
        'inbound_units', 'inbound_type', 'inbound_account',
        'unit_price', 'transaction_type',
        'balance_after', 'balance_type',
        'sort_order', 'original_order',
        'links', 'beancount_tags',
        'envelope_id', 'source', 'source_type', 'source_file_path', 'source_line_number',
        'state', 'history', 'classification', 'classification_confidence',
        'classification_reasoning', 'reconciliation_set_id',
        'reconciliation_score', 'reconciliation_matched_with',
        'metadata', 'warnings', 'tags',
        'lot_created', 'consumed_lots', 'transferred_lots', 'lot_lineages'
    }

    # Separate envelope fields from extra fields
    envelope_fields = {}
    extra_fields = {}
    for key, value in fields.items():
        if key in ENVELOPE_FIELDS:
            envelope_fields[key] = value
        else:
            extra_fields[key] = value

    # Ensure metadata exists
    if 'metadata' not in envelope_fields:
        envelope_fields['metadata'] = {}

    # Move extra fields to metadata (including transaction_id)
    for key, value in extra_fields.items():
        if value is not None:  # Only add non-None values
            envelope_fields['metadata'][key] = value

    # Extract transaction_id for ID generation (might be in metadata now)
    transaction_id = envelope_fields['metadata'].get('transaction_id') or extra_fields.get('transaction_id')

    # Generate envelope ID if not provided
    if 'envelope_id' not in envelope_fields or not envelope_fields['envelope_id']:
        # Extract values needed for ID generation
        date_val = envelope_fields.get('date')
        narration = envelope_fields.get('narration', '')
        source = envelope_fields.get('source', 'unknown')
        outbound_units = envelope_fields.get('outbound_units')
        inbound_units = envelope_fields.get('inbound_units')
        outbound_account = envelope_fields.get('outbound_account')

        # Generate the ID (include source_file and line_number for uniqueness)
        source_file_path = envelope_fields.get('source_file_path')
        source_line_number = envelope_fields.get('source_line_number')
        envelope_fields['envelope_id'] = Envelope.generate_envelope_id(
            date_val=date_val,
            narration=narration,
            source=source,
            outbound_units=outbound_units,
            inbound_units=inbound_units,
            outbound_account=outbound_account,
            transaction_id=transaction_id,
            source_file=source_file_path,
            line_number=source_line_number
        )

    # Determine transaction_type for investment transactions if not already set
    if 'transaction_type' not in envelope_fields or not envelope_fields['transaction_type']:
        # Check if this is an investment transaction (has commodity data)
        outbound_type = envelope_fields.get('outbound_type')
        inbound_type = envelope_fields.get('inbound_type')
        outbound_units = envelope_fields.get('outbound_units')
        inbound_units = envelope_fields.get('inbound_units')

        # If one side is a commodity and the other is currency, it's an investment transaction
        if outbound_type and inbound_type:
            # Check if one is a commodity (not a currency)
            from cassoulet.utils.currencies import is_currency
            outbound_is_currency = is_currency(outbound_type)
            inbound_is_currency = is_currency(inbound_type)

            if outbound_is_currency and not inbound_is_currency:
                # Cash out, commodity in = BUY
                envelope_fields['transaction_type'] = 'BUY'
            elif not outbound_is_currency and inbound_is_currency:
                # Commodity out, cash in = SELL
                envelope_fields['transaction_type'] = 'SELL'
            elif not outbound_is_currency and not inbound_is_currency:
                # Commodity to commodity = in-specie transfer
                envelope_fields['transaction_type'] = 'TRANSFER'

    # Add creation metadata
    envelope_fields['metadata']['creation_reason'] = reason
    envelope_fields['metadata']['created_by'] = 'envelope_utilities'
    # Only mark as auto_generated if it's not from data ingestion
    if 'Data ingestion' not in reason:
        envelope_fields['metadata']['auto_generated'] = True

    # Create the envelope with only valid fields
    envelope = Envelope(**envelope_fields)

    # Add creation to history
    if hasattr(envelope, 'add_history'):
        envelope.add_history(
            state='created',
            component='envelope_utilities',
            action=f"Created: {reason}"
        )

    return envelope


def build_envelope_lookup(envelopes: List[Envelope]) -> Dict[str, Envelope]:
    """
    Build a lookup dictionary mapping envelope IDs to envelopes.

    Args:
        envelopes: List of envelopes to index

    Returns:
        Dictionary mapping envelope_id to Envelope object

    Note:
        - Envelopes without IDs are skipped
        - If duplicate IDs exist, the last one wins
    """
    lookup = {}
    for envelope in envelopes:
        if envelope.envelope_id:
            lookup[envelope.envelope_id] = envelope
    return lookup


def build_envelope_date_index(
    envelopes: List[Envelope],
    transfer_eligible_only: bool = False
) -> Dict[date, List[Envelope]]:
    """Build a date index for efficient date-range queries.

    Args:
        envelopes: List of envelopes to index
        transfer_eligible_only: If True, only include transfer-eligible envelopes
            (filters out credit card purchases, remediations, etc.)

    Returns:
        Dictionary mapping date to list of envelopes on that date
    """
    from collections import defaultdict
    date_index = defaultdict(list)

    for env in envelopes:
        if env.date:
            if transfer_eligible_only and not is_transfer_eligible(env):
                continue
            date_index[env.date].append(env)

    return dict(date_index)


def find_envelopes_in_date_window(
    envelope: Envelope,
    all_envelopes: List[Envelope],
    date_index: Optional[Dict[date, List[Envelope]]] = None,
    window_days: int = 30
) -> List[Envelope]:
    """Find all envelopes within a date window of the given envelope.

    This is much more efficient than checking all envelopes when dealing
    with large datasets (e.g., 6000+ envelopes).

    Args:
        envelope: Reference envelope
        all_envelopes: All envelopes (used if no date_index provided)
        date_index: Pre-built date index for efficiency (optional)
        window_days: Number of days before/after to include

    Returns:
        List of envelopes within the date window (excluding the reference)
    """
    if not envelope.date:
        return []

    from datetime import timedelta

    start_date = envelope.date - timedelta(days=window_days)
    end_date = envelope.date + timedelta(days=window_days)

    candidates = []

    if date_index:
        # Use the index for efficient lookup
        current_date = start_date
        while current_date <= end_date:
            if current_date in date_index:
                for env in date_index[current_date]:
                    if env.envelope_id != envelope.envelope_id:
                        candidates.append(env)
            current_date += timedelta(days=1)
    else:
        # Fallback to linear search
        for env in all_envelopes:
            if env.envelope_id == envelope.envelope_id:
                continue
            if env.date and start_date <= env.date <= end_date:
                candidates.append(env)

    return candidates


def apply_patterns_to_envelopes(envelopes: List[Envelope], patterns: dict) -> List[Envelope]:
    """
    Apply pattern matching rules to envelopes.

    Args:
        envelopes: List of envelopes to process
        patterns: Pattern dictionary (NO_MATCH_PATTERNS, ENVELOPE_CLASSIFIER_PATTERNS, etc.)

    Returns:
        List of envelopes with pattern metadata applied
    """
    from cassoulet.utils.pattern_matcher import PatternMatcher

    matcher = PatternMatcher(patterns)
    result = []

    for envelope in envelopes:
        # Convert to dict - PatternMatcher expects certain fields
        # Start with all envelope fields
        envelope_dict = envelope.__dict__.copy()

        # Add computed fields that patterns might expect
        # (these are conventions from the pattern files)
        envelope_dict['account'] = get_primary_account(envelope)
        # Keep the original simple logic for amount - patterns may expect the raw value
        envelope_dict['amount'] = envelope.outbound_units or envelope.inbound_units or 0

        # Apply patterns
        match_result = matcher.match_envelope(envelope_dict)

        if match_result:
            # Merge pattern results into metadata
            updated_metadata = {**envelope.metadata, **match_result}
            enhanced = enhance_envelope(envelope, metadata=updated_metadata)
            result.append(enhanced)
        else:
            result.append(envelope)

    return result


def find_sovereign(envelopes: list[Envelope]) -> Optional[Envelope]:
    """
    Find the sovereign (manual) envelope in a group.

    Args:
        envelopes: List of envelopes to search

    Returns:
        The manual envelope if found, None otherwise
    """
    for envelope in envelopes:
        if envelope.source and 'manual' in envelope.source.lower():
            return envelope
    return None


def has_explicit_override(envelope: Envelope, target_id: str) -> bool:
    """
    Check if an envelope explicitly overrides a specific target.

    Args:
        envelope: The envelope to check (typically manual)
        target_id: The ID of the envelope being overridden

    Returns:
        True if this envelope explicitly overrides the target
    """
    if not envelope.metadata:
        return False
    override_id = envelope.metadata.get('override')
    return override_id == target_id


def is_remediation_envelope(envelope: Envelope) -> bool:
    """Check if an envelope is a balance gap remediation.

    Args:
        envelope: Envelope to check

    Returns:
        True if this is a remediation envelope
    """
    # Check multiple indicators
    if envelope.source == 'balance_gap_detector':
        return True

    if envelope.metadata:
        # Check for remediation markers
        if envelope.metadata.get('gap_type'):
            return True
        if envelope.metadata.get('transaction_type_hint') == 'REMEDIATION':
            return True

    # Check tags
    if envelope.tags and ('balance-gap' in envelope.tags or 'missing-data' in envelope.tags):
        return True

    # Check envelope ID patterns
    if envelope.envelope_id:
        if envelope.envelope_id.startswith(('gap_remediation_', 'opening_balance_')):
            return True

    return False


def is_transfer_eligible(envelope: Envelope) -> bool:
    """Check if an envelope is eligible for transfer matching.

    This filters out envelopes that should never be considered for transfers:
    - Remediation envelopes (gap fixes)
    - Zero-amount transactions
    - Transactions with no cash flow
    - Explicitly marked ineligible envelopes

    Args:
        envelope: The envelope to check

    Returns:
        True if eligible for transfer matching
    """
    # Check explicit metadata flag if set
    if envelope.metadata:
        # Explicit eligibility flag
        eligible = envelope.metadata.get('is_transfer_eligible')
        if eligible is not None:
            return eligible

    # Skip remediation envelopes
    if is_remediation_envelope(envelope):
        return False

    # Must have some cash flow (inbound or outbound)
    if not has_flow(envelope):
        return False

    # Skip liability account spending (credit cards, loans, mortgages)
    # Outbound from a liability = expense/spending, not a transfer
    # Payments TO a liability (inbound) are still transfer-eligible
    #
    # An outbound that merely UNDOES an earlier payment is not spending, and is
    # marked eligible before scoring by mark_undone_liability_payments(). That
    # mark arrives through the explicit metadata flag checked at the top of this
    # function.
    if envelope.outbound_account and account_is_liability(envelope.outbound_account):
        return False

    # Default to eligible
    return True


# A bounced payment posts AFTER the payment it undoes, and the gap absorbs
# weekends and processing delays. Two days catches the three real cases in the
# reference ledger (1 and 2 days apart); one day misses GBP 3,182.21.
UNDONE_PAYMENT_MIN_DAYS = 1
UNDONE_PAYMENT_MAX_DAYS = 2


def mark_undone_liability_payments(envelopes: List[Envelope]) -> int:
    """Mark liability outflows that merely undo an earlier payment.

    When a payment to a credit card bounces, the card records the debt going
    back up. Under the normalised convention that is an OUTBOUND movement, and
    is_transfer_eligible() reads outbound-from-a-liability as spending and
    refuses to score it. The bank side of the same failure is a plain inbound
    credit and IS eligible, so the two halves of one bounced payment could never
    reach each other despite scoring 220 against a threshold of 70. Each left
    phantom income on the bank and a phantom expense on the card.

    THE TEST IS STRUCTURAL. An outflow from a liability, of exactly the amount
    of an earlier inflow on the same account in the same currency, posting one
    or two days later, with no other candidate. Nothing reads the narration: an
    earlier version keyed on the word "REVERSAL", which is one bank's
    vocabulary - UNPAID, RETURNED and CANCELLED mean the same and match none of
    it, and an "UNPAID DIRECT DEBIT PAYMENT" of GBP 3,182.21 in this very ledger
    was missed because of it.

    EVERY CONDITION IS LOAD-BEARING, and each was added because its absence
    produced a wrong answer:

      Strictly LATER, never the same day. A genuine bounce is processed after
      the payment. Same-day pairs are corrections or coincidences, and a date
      carries no posting sequence so their order cannot even be established.
      Without this, a GBP 500 card payment was "undone" by a GBP 500 car
      service bought the same day.

      One-to-one. An inflow is consumed by the outflow that claims it. Without
      this, one GBP 500 payment could authorise every GBP 500 purchase that
      week - the mark says nothing was bought, so applying it to several
      purchases suppresses real spending.

      Unambiguous. More than one candidate inflow means decline, not guess.
      Recurring equal charges are common on a card; a Milk & More subscription
      produces exactly this shape twice in three days.

      Same currency. Comparing bare numbers across commodities would equate
      unlike quantities.

    Extremely narrow by design: 3 marks across 6,316 liability outflows in the
    reference ledger, and 1 declined as ambiguous.

    Returns the number of envelopes marked.
    """
    from collections import defaultdict

    # Index inflows by (account, currency, amount) so the search is a lookup
    # rather than a scan of every inflow for every outflow.
    inflow_index: Dict[tuple, List[Envelope]] = defaultdict(list)
    outflows: List[Envelope] = []
    for env in envelopes:
        if not env.date:
            continue
        if (has_inbound_units(env) and env.inbound_units and env.inbound_account
                and account_is_liability(env.inbound_account)):
            inflow_index[(env.inbound_account, env.inbound_type,
                          env.inbound_units)].append(env)
        elif (has_outbound_units(env) and env.outbound_units and env.outbound_account
                and account_is_liability(env.outbound_account)):
            outflows.append(env)

    consumed: set = set()
    marked = 0
    # Oldest first, so when two outflows could claim one inflow the earlier -
    # and so more plausible - reversal wins rather than whichever came first in
    # the input.
    for env in sorted(outflows, key=lambda e: e.date):
        if env.metadata.get('is_transfer_eligible') is not None:
            continue
        key = (env.outbound_account, env.outbound_type, env.outbound_units)
        candidates = [
            inflow for inflow in inflow_index.get(key, ())
            if id(inflow) not in consumed
            and UNDONE_PAYMENT_MIN_DAYS
            <= (env.date - inflow.date).days
            <= UNDONE_PAYMENT_MAX_DAYS
        ]
        if len(candidates) != 1:
            continue  # none, or ambiguous: decline rather than guess
        inflow = candidates[0]
        consumed.add(id(inflow))
        env.metadata['is_transfer_eligible'] = True
        env.metadata['undoes_payment_of'] = str(inflow.envelope_id)
        marked += 1
    return marked


def is_manual_envelope(envelope: Envelope) -> bool:
    """Check if this is a manual envelope (from beancount file).

    Uses the explicit source_type field.
    """
    if not envelope.source_type:
        raise ValueError(f"Envelope {envelope.envelope_id} missing source_type field")

    return envelope.source_type == 'beancount'


def is_csv_envelope(envelope: Envelope) -> bool:
    """Check if this is a CSV-sourced envelope (from importer).

    Uses the explicit source_type field.
    """
    if not envelope.source_type:
        raise ValueError(f"Envelope {envelope.envelope_id} missing source_type field")

    return envelope.source_type == 'csv'


def get_matched_with(envelope: 'Envelope') -> Optional[str]:
    """
    Get the ID of the envelope this one is matched with.

    Returns:
        Envelope ID if matched, None otherwise
    """
    return envelope.metadata.get('matched_with')


def has_balance_data(envelopes: List[Envelope]) -> bool:
    """Check if any envelope has balance data.

    Args:
        envelopes: List of envelopes to check

    Returns:
        True if any envelope has balance data
    """
    return any(e.balance_after is not None for e in envelopes)


# ========================================================================
# LOGIC WRAPPERS FOR ENVELOPE PAIR OPERATIONS
# ========================================================================

def both(env1: Envelope, env2: Envelope, check_func) -> bool:
    """Both envelopes pass the check function.

    Args:
        env1: First envelope
        env2: Second envelope
        check_func: Function that takes an envelope and returns bool

    Returns:
        True if both envelopes pass the check
    """
    return check_func(env1) and check_func(env2)


def either(env1: Envelope, env2: Envelope, check_func) -> bool:
    """At least one envelope passes the check function.

    Args:
        env1: First envelope
        env2: Second envelope
        check_func: Function that takes an envelope and returns bool

    Returns:
        True if at least one envelope passes the check
    """
    return check_func(env1) or check_func(env2)


def exactly_one(env1: Envelope, env2: Envelope, check_func) -> bool:
    """Exactly one envelope passes the check function (XOR).

    Args:
        env1: First envelope
        env2: Second envelope
        check_func: Function that takes an envelope and returns bool

    Returns:
        True if exactly one envelope passes the check
    """
    return check_func(env1) ^ check_func(env2)


def one_each(env1: Envelope, env2: Envelope, check1, check2) -> bool:
    """One envelope passes check1, the other passes check2.

    Args:
        env1: First envelope
        env2: Second envelope
        check1: First check function
        check2: Second check function

    Returns:
        True if envelopes pass different checks (one each)
    """
    return ((check1(env1) and check2(env2)) or
            (check2(env1) and check1(env2)))


def neither(env1: Envelope, env2: Envelope, check_func) -> bool:
    """Neither envelope passes the check function.

    Args:
        env1: First envelope
        env2: Second envelope
        check_func: Function that takes an envelope and returns bool

    Returns:
        True if neither envelope passes the check
    """
    return not check_func(env1) and not check_func(env2)


def equals(value1, value2) -> bool:
    """Check if two values are equal (both must be non-None).

    Returns False if either value is None.

    Args:
        value1: First value
        value2: Second value

    Returns:
        True if both values are non-None and equal
    """
    if value1 is None or value2 is None:
        return False
    return value1 == value2


def not_equals(value1, value2) -> bool:
    """Check if two values are not equal (both must be non-None).

    Returns False if either value is None.

    Args:
        value1: First value
        value2: Second value

    Returns:
        True if both values are non-None and not equal
    """
    if value1 is None or value2 is None:
        return False
    return value1 != value2


def equals_or_none(value1, value2) -> bool:
    """Check if two values are equal, treating None as a valid value.

    None == None returns True.

    Args:
        value1: First value
        value2: Second value

    Returns:
        True if values are equal (including both being None)
    """
    return value1 == value2


# ========================================================================
# SOURCE PATTERN DETECTION
# ========================================================================

def determine_source_pair_type(env1: Envelope, env2: Envelope) -> str:
    """Determine the source pattern for an envelope pair.

    Returns:
        'csv-csv' - Both from CSV files
        'manual-csv' - One manual, one CSV
        'manual-manual' - Both manual
        'unknown' - Can't determine
    """
    if both(env1, env2, is_manual_envelope):
        return 'manual-manual'
    elif both(env1, env2, is_csv_envelope):
        return 'csv-csv'
    elif one_source_csv_one_manual(env1, env2):
        return 'manual-csv'
    else:
        return 'unknown'

# ========================================================================
# PAIR UTILITY FUNCTIONS USING LOGIC WRAPPERS
# ========================================================================

def have_same_account(env1: Envelope, env2: Envelope) -> bool:
    """Check if envelopes involve the same account.

    Args:
        env1: First envelope
        env2: Second envelope

    Returns:
        True if primary accounts match
    """
    acc1 = get_primary_account(env1)
    acc2 = get_primary_account(env2)
    return equals(acc1, acc2)


def have_different_accounts(env1: Envelope, env2: Envelope) -> bool:
    """Check if envelopes involve different accounts.

    Args:
        env1: First envelope
        env2: Second envelope

    Returns:
        True if primary accounts differ
    """
    acc1 = get_primary_account(env1)
    acc2 = get_primary_account(env2)
    return not_equals(acc1, acc2)


def get_primary_type(envelope: Envelope) -> Optional[str]:
    """Get the primary type for this envelope."""

    # Get the primary type for this envelope
    # For outbound transactions, use outbound_type
    # For inbound transactions, use inbound_type

    transaction_direction = get_transaction_direction(envelope)
    
    if transaction_direction == 'outbound':
        return get_outbound_type(envelope)
    if transaction_direction == 'inbound':
        return get_inbound_type(envelope)

    return None


def have_same_primary_type(env1: Envelope, env2: Envelope) -> bool:
    """Check if envelopes have the same primary type (currency or commodity).

    This is used to detect impossible self-transfers where the same type
    (e.g., GBP or AAPL) appears to flow in and out of the same account.

    Examples:
        - GBP out from HSBC, GBP in to HSBC -> True (same type: GBP)
        - AAPL out from HL, AAPL in to HL -> True (same type: AAPL)
        - GBP out from HSBC, USD in to HSBC -> False (different types)
        - GBP out from HSBC, GBP in to Barclays -> True (same type, different accounts)

    Args:
        env1: First envelope
        env2: Second envelope

    Returns:
        True if both envelopes have the same primary type
    """

    type1 = get_primary_type(env1)
    type2 = get_primary_type(env2)

    # Both must have a type and they must match
    return equals(type1, type2)

# ========================================================================
# SINGLE-PURPOSE TYPE AND AMOUNT CHECKING FUNCTIONS
# ========================================================================


def envelope_is_currency_only(envelope: Envelope) -> bool:
    """Check if an envelope contains only currency types.

    Args:
        envelope: Envelope to check

    Returns:
        True if all types in the envelope are currencies
    """
    from cassoulet.utils.currencies import is_currency
    types = get_envelope_types(envelope)
    return len(types) > 0 and all(is_currency(t) for t in types)


def envelope_is_commodity_only(envelope: Envelope) -> bool:
    """Check if an envelope contains only commodity types.

    Args:
        envelope: Envelope to check

    Returns:
        True if all types in the envelope are commodities
    """
    from cassoulet.utils.currencies import is_commodity
    types = get_envelope_types(envelope)
    return len(types) > 0 and all(is_commodity(t) for t in types)


def envelope_has_mixed_types(envelope: Envelope) -> bool:
    """Check if an envelope contains both currency and commodity types.

    This is common for complete trades (e.g., sell AAPL for GBP).

    Args:
        envelope: Envelope to check

    Returns:
        True if envelope contains both currency and commodity types
    """
    from cassoulet.utils.currencies import is_currency, is_commodity

    # Check outbound and inbound types directly
    outbound_is_currency = is_currency(get_outbound_type(envelope))
    outbound_is_commodity = is_commodity(get_outbound_type(envelope))
    inbound_is_currency = is_currency(get_inbound_type(envelope))
    inbound_is_commodity = is_commodity(get_inbound_type(envelope))

    # Has mixed types if we have at least one currency AND at least one commodity
    has_currency = outbound_is_currency or inbound_is_currency
    has_commodity = outbound_is_commodity or inbound_is_commodity

    return has_currency and has_commodity


def both_are_currency_only(env1: Envelope, env2: Envelope) -> bool:
    """Check if both envelopes contain only currency types.

    Note: This checks each envelope independently, not their overlap.
    For overlap checking, use envelope_compatibility_checks().

    Args:
        env1: First envelope
        env2: Second envelope

    Returns:
        True if both envelopes contain only currencies
    """
    return envelope_is_currency_only(env1) and envelope_is_currency_only(env2)


def both_are_commodity_only(env1: Envelope, env2: Envelope) -> bool:
    """Check if both envelopes contain only commodity types.

    Note: This checks each envelope independently, not their overlap.
    For overlap checking, use envelope_compatibility_checks().

    Args:
        env1: First envelope
        env2: Second envelope

    Returns:
        True if both envelopes contain only commodities
    """
    return envelope_is_commodity_only(env1) and envelope_is_commodity_only(env2)


def has_same_units(env1: Envelope, env2: Envelope) -> bool:
    """Check if amounts match appropriately based on envelope relationship.

    Uses envelopes_are_compatible to determine the relationship and
    compare the correct amounts based on context.

    Args:
        env1: First envelope
        env2: Second envelope

    Returns:
        True if amounts are equal in the appropriate context
    """
    # Get full compatibility analysis
    compat = envelope_compatibility_checks(env1, env2)

    if not compat['compatible']:
        return False

    # Check we have overlapping types
    if not compat['type_overlap']:
        return False

    # Compare based on the comparison mode
    if compat['comparison_mode'] == 'reconciliation':
        # Same flow - compare matching legs directly
        if compat['flow_pattern'] == 'both_send':
            return equals(env1.outbound_units, env2.outbound_units)
        elif compat['flow_pattern'] == 'both_receive':
            return equals(env1.inbound_units, env2.inbound_units)

    elif compat['comparison_mode'] in ['transfer', 'manual-override-transfer']:
        # Opposite flows - cross-compare
        if compat['env1_role'] == 'sender' and compat['env2_role'] == 'receiver':
            return equals(env1.outbound_units, env2.inbound_units)
        elif compat['env1_role'] == 'receiver' and compat['env2_role'] == 'sender':
            return equals(env1.inbound_units, env2.outbound_units)

    elif compat['comparison_mode'] in ['partial-match', 'subset']:
        # Complete/partial - compare the matching leg
        if compat['env1_role'] == 'complete_trade':
            # env2 is partial - find which leg matches
            if env2.outbound_units and env1.inbound_units:
                return equals(env2.outbound_units, env1.inbound_units)
            elif env2.inbound_units and env1.outbound_units:
                return equals(env2.inbound_units, env1.outbound_units)
        elif compat['env2_role'] == 'complete_trade':
            # env1 is partial - find which leg matches
            if env1.outbound_units and env2.inbound_units:
                return equals(env1.outbound_units, env2.inbound_units)
            elif env1.inbound_units and env2.outbound_units:
                return equals(env1.inbound_units, env2.outbound_units)

    return False


def has_different_units(env1: Envelope, env2: Envelope) -> bool:
    """Check if amounts differ based on flow pattern.

    This is the logical inverse of has_same_units().

    Args:
        env1: First envelope
        env2: Second envelope

    Returns:
        True if amounts are different in the appropriate context
    """
    # Simply the inverse of has_same_units
    return not has_same_units(env1, env2)


def get_transfer_amounts(env1: Envelope, env2: Envelope) -> Union[Tuple[Decimal, Decimal], NotApplicable]:
    """Extract outbound and inbound amounts from an envelope pair.

    Uses envelopes_are_compatible to determine the relationship and
    extract the correct amounts based on the directional guidance.

    Args:
        env1: First envelope
        env2: Second envelope

    Returns:
        (outbound_amount, inbound_amount) as negative/positive Decimals
        OR NOT_APPLICABLE if not a valid transfer pattern
    """
    # Get full compatibility analysis
    compat = envelope_compatibility_checks(env1, env2)

    # Not compatible or not a transfer-like pattern
    if not compat['compatible']:
        return NOT_APPLICABLE

    if compat['comparison_mode'] not in ['transfer', 'partial-match', 'subset']:
        return NOT_APPLICABLE

    # Use the directional guidance to extract amounts
    guidance = compat.get('directional_guidance', {})
    if not guidance:
        return NOT_APPLICABLE

    # The guidance tells us exactly what to extract
    # For transfers, we need outbound from sender and inbound from receiver
    common_type = list(compat['type_overlap'])[0] if compat['type_overlap'] else None
    if not common_type:
        return NOT_APPLICABLE

    # Use flow pattern to determine which amounts to extract
    # The pattern tells us exactly what the relationship is
    flow = compat['flow_pattern']

    # Map flow patterns to the amounts we need
    if flow == 'env1_sends_env2_receives':
        outbound = env1.outbound_units
        inbound = env2.inbound_units
    elif flow == 'env1_receives_env2_sends':
        outbound = env2.outbound_units
        inbound = env1.inbound_units
    elif flow == 'env2_sends_matching_env1_receives':
        # env2 partial sends, env1 complete receives
        outbound = env2.outbound_units
        inbound = env1.inbound_units
    elif flow == 'env2_receives_matching_env1_sends':
        # env2 partial receives, env1 complete sends
        outbound = env1.outbound_units
        inbound = env2.inbound_units
    elif flow == 'env1_sends_matching_env2_receives':
        # env1 partial sends, env2 complete receives
        outbound = env1.outbound_units
        inbound = env2.inbound_units
    elif flow == 'env1_receives_matching_env2_sends':
        # env1 partial receives, env2 complete sends
        outbound = env2.outbound_units
        inbound = env1.inbound_units
    else:
        return NOT_APPLICABLE

    # Check we have both values
    if outbound is None or inbound is None:
        return NOT_APPLICABLE

    # Return as negative/positive decimals (outbound is negative by convention)
    return -abs(outbound), abs(inbound)

def to_percent(amount: Optional[Decimal], base: Optional[Decimal]) -> Optional[Decimal]:
    """Convert amount to percentage of base.

    Args:
        amount: The amount to convert (must be Decimal)
        base: The base amount (denominator, must be Decimal)

    Returns:
        Percentage or None if base is 0 or either value is None
    """
    if amount is None or base is None or base == 0:
        return None
    # Inputs should already be Decimals
    return (amount / base) * Decimal('100')


# ========================================================================
# ACCOUNT TYPE CHECKING UTILITIES
# ========================================================================
# Account checking functions are imported from accounts.py at the top of the file


# Account pair checking using logic wrappers and accounts.py functions
def both_accounts_are_sipp(env1: Envelope, env2: Envelope) -> bool:
    """Check if both envelopes involve SIPP accounts."""
    def check_sipp(env: Envelope) -> bool:
        acc = get_primary_account(env)
        return acc and account_is_sipp(acc)
    return both(env1, env2, check_sipp)


def either_account_is_sipp(env1: Envelope, env2: Envelope) -> bool:
    """Check if either envelope involves a SIPP account."""
    def check_sipp(env: Envelope) -> bool:
        acc = get_primary_account(env)
        return acc and account_is_sipp(acc)
    return either(env1, env2, check_sipp)


def both_accounts_are_isa(env1: Envelope, env2: Envelope) -> bool:
    """Check if both envelopes involve ISA accounts."""
    def check_isa(env: Envelope) -> bool:
        acc = get_primary_account(env)
        return acc and account_is_isa(acc)
    return both(env1, env2, check_isa)


def either_account_is_isa(env1: Envelope, env2: Envelope) -> bool:
    """Check if either envelope involves an ISA account."""
    def check_isa(env: Envelope) -> bool:
        acc = get_primary_account(env)
        return acc and account_is_isa(acc)
    return either(env1, env2, check_isa)


def both_accounts_are_bank(env1: Envelope, env2: Envelope) -> bool:
    """Check if both envelopes involve bank accounts."""
    def check_bank(env: Envelope) -> bool:
        acc = get_primary_account(env)
        return acc and account_is_bank(acc)
    return both(env1, env2, check_bank)


def either_account_is_bank(env1: Envelope, env2: Envelope) -> bool:
    """Check if either envelope involves a bank account."""
    def check_bank(env: Envelope) -> bool:
        acc = get_primary_account(env)
        return acc and account_is_bank(acc)
    return either(env1, env2, check_bank)


def both_accounts_are_broker(env1: Envelope, env2: Envelope) -> bool:
    """Check if both envelopes involve broker accounts."""
    def check_broker(env: Envelope) -> bool:
        acc = get_primary_account(env)
        return acc and account_is_broker(acc)
    return both(env1, env2, check_broker)


def either_account_is_broker(env1: Envelope, env2: Envelope) -> bool:
    """Check if either envelope involves a broker account."""
    def check_broker(env: Envelope) -> bool:
        acc = get_primary_account(env)
        return acc and account_is_broker(acc)
    return either(env1, env2, check_broker)


def one_account_is_bank_one_is_broker(env1: Envelope, env2: Envelope) -> bool:
    """Check if one envelope is bank and other is broker."""
    def check_bank(env: Envelope) -> bool:
        acc = get_primary_account(env)
        return acc and account_is_bank(acc)
    def check_broker(env: Envelope) -> bool:
        acc = get_primary_account(env)
        return acc and account_is_broker(acc)
    return one_each(env1, env2, check_bank, check_broker)


def one_account_is_credit_card_one_is_broker(env1: Envelope, env2: Envelope) -> bool:
    """Check if one envelope is a credit card and other is a broker.

    This is an INVALID transfer pattern - credit cards don't pay into brokers
    and brokers don't pay credit cards directly. Any match between these
    account types is almost certainly a false positive.
    """
    from cassoulet.utils.accounts import account_is_credit_card
    def check_credit_card(env: Envelope) -> bool:
        acc = get_primary_account(env)
        return acc and account_is_credit_card(acc)
    def check_broker(env: Envelope) -> bool:
        acc = get_primary_account(env)
        return acc and account_is_broker(acc)
    return one_each(env1, env2, check_credit_card, check_broker)


def one_account_is_credit_card_one_is_bank(env1: Envelope, env2: Envelope) -> bool:
    """Check if one envelope is a credit card and the other is a bank.

    This is the credit card BILL PAYMENT shape: money leaves a current account
    and lands against the card, reducing the debt. It is a transfer between two
    of your own accounts, not income and not an expense - the expense was
    already recorded when each individual purchase hit the card.

    Distinct from one_account_is_credit_card_one_is_broker, which is the invalid
    pairing. This one is valid and common: monthly, for the statement balance.

    On its own this only says the account TYPES are right. The scoring patterns
    combine it with amount and date agreement, which is what makes it safe when
    two withdrawals of the same amount fall on the same day and only one of them
    is the card payment.
    """
    from cassoulet.utils.accounts import account_is_credit_card
    def check_credit_card(env: Envelope) -> bool:
        acc = get_primary_account(env)
        return acc and account_is_credit_card(acc)
    def check_bank(env: Envelope) -> bool:
        acc = get_primary_account(env)
        return acc and account_is_bank(acc)
    return one_each(env1, env2, check_credit_card, check_bank)


def is_reversal_of(env1: Envelope, env2: Envelope) -> bool:
    """True if one envelope is a bank reversal of the other. No text is read.

    A reversal is not income and not a refund - the bank undid a payment that
    should never have left. Left unmatched both legs fall out separately, and
    the balance the reversal restores also makes the gap detector fabricate a
    remediation entry for the same amount, so one reversal can produce two
    phantom income postings.

    THIS USED TO PARSE ENGLISH. It matched a payee beginning "REVERSAL OF",
    read a DD-MM date out of it and compared the payee it named. That works
    only for a bank that announces reversals that way and in that language, and
    it found 26 of them.

    The structural signature is stronger: money leaves an account and the
    identical amount returns to the SAME account on the SAME day. Measured
    across the reference ledger it finds every one of those 26 with nothing
    missed, plus 5 more the text rule could not see - four where the bank
    reused the counterparty on both legs and one bounced cheque.

    Same-day is doing real work and is not a tightening for its own sake: every
    true reversal in the reference data posts both legs on one day, while
    widening to even a single day admits ordinary spending that happens to
    reverse an earlier amount.

    What this deliberately does NOT do is decide whether the pair is a reversal
    at all - a pass-through, where money arrives to fund a payment going out the
    same day, has exactly this shape. Excluding those is the caller's job and it
    is done structurally too, by skipping any leg that upstream matching or
    categorisation has already given a real account.
    """
    for back, out in ((env1, env2), (env2, env1)):
        if not has_inbound_units(back) or not has_outbound_units(out):
            continue
        if not back.date or back.date != out.date:
            continue
        account = get_primary_account(back)
        if not account or account != get_primary_account(out):
            continue
        back_amt = get_absolute_amount(back)
        out_amt = get_absolute_amount(out)
        if back_amt is None or out_amt is None or back_amt != out_amt:
            continue
        return True
    return False


def _counterparty(envelope: Envelope) -> str:
    """Who the other side of this transaction was, however the bank spelled it.

    Card statements often carry the merchant in the narration and leave the
    payee empty, while current accounts populate the payee. Matching on payee
    alone silently skipped every credit card refund.
    """
    return ((envelope.payee or '').strip() or (envelope.narration or '').strip()).upper()


def is_refund_of(env1: Envelope, env2: Envelope) -> bool:
    """True if one envelope is a merchant refund of the other.

    Weaker than a reversal: the payee agrees and the direction is opposite, but
    a refund can arrive months later and can be partial, so this asserts only
    the shape. The scoring pattern supplies the date window, and a refund
    settles against whatever account the original purchase was booked to.
    """
    for refund, original in ((env1, env2), (env2, env1)):
        # Roles are NOT interchangeable. The refund is the leg where money comes
        # back and the original the leg where it went out; trying both ways
        # round without this makes an over-refund look like a valid partial one,
        # because 50 <= 100 reads fine once the roles are swapped.
        if not has_inbound_units(refund) or not has_outbound_units(original):
            continue
        counterparty = _counterparty(refund)
        if not counterparty or counterparty != _counterparty(original):
            continue
        if get_primary_account(refund) != get_primary_account(original):
            continue
        # A refund never exceeds what was paid. Equal is the common case: a
        # single order returned in full.
        refunded = get_absolute_amount(refund)
        paid = get_absolute_amount(original)
        if refunded is None or paid is None or refunded > paid:
            continue
        return True
    return False


def _opposite_direction(env1: Envelope, env2: Envelope) -> bool:
    """True if one envelope receives where the other sends."""
    return ((has_inbound_units(env1) and has_outbound_units(env2))
            or (has_outbound_units(env1) and has_inbound_units(env2)))


def accounts_same_institution(env1: Envelope, env2: Envelope) -> bool:
    """Check if both accounts are from the same institution."""
    acc1 = get_primary_account(env1)
    acc2 = get_primary_account(env2)
    if not acc1 or not acc2:
        return False
    inst1 = get_account_institution(acc1)
    inst2 = get_account_institution(acc2)
    return equals(inst1, inst2)

# ========================================================================
# TRANSFER CALCULATION UTILITIES
# ========================================================================

def transfer_delta(env1: Envelope, env2: Envelope) -> Union[Decimal, NotApplicable]:
    """Calculate signed transfer difference: positive = loss, negative = gain, 0 = exact match.

    Args:
        env1: First envelope
        env2: Second envelope

    Returns:
        Decimal amount or NOT_APPLICABLE if envelopes incompatible for transfer calculation
    """
    # Use get_transfer_amounts which now does all the comparability checking
    result = get_transfer_amounts(env1, env2)
    if isinstance(result, NotApplicable):
        return NOT_APPLICABLE

    outbound, inbound = result

    # For transfers: outbound is negative, inbound is positive
    # Perfect transfer: outbound + inbound = 0
    # Loss: outbound + inbound < 0 (less came in than went out)
    # Gain: outbound + inbound > 0 (more came in than went out)
    return outbound + inbound


def transfer_loss(env1: Envelope, env2: Envelope) -> Optional[Decimal]:
    """Amount lost in transfer (positive value, 0 if no loss).

    Args:
        env1: First envelope
        env2: Second envelope

    Returns:
        Positive decimal amount or None if not applicable
    """
    delta = transfer_delta(env1, env2)
    if isinstance(delta, NotApplicable) or delta is None:
        return None
    # If delta is negative, we lost money
    return max(Decimal('0'), -delta)


def transfer_gain(env1: Envelope, env2: Envelope) -> Union[Decimal, NotApplicable]:
    """Amount gained in transfer (positive value, 0 if no gain).

    Args:
        env1: First envelope
        env2: Second envelope

    Returns:
        Positive decimal amount or NOT_APPLICABLE
    """
    delta = transfer_delta(env1, env2)
    if isinstance(delta, NotApplicable):
        return NOT_APPLICABLE
    # If delta is positive, we gained money (suspicious!)
    return max(Decimal('0'), delta)


def has_transfer_gain(env1: Envelope, env2: Envelope) -> bool:
    """Check if transfer shows a gain (received more than sent).

    Args:
        env1: First envelope
        env2: Second envelope

    Returns:
        True if there's a gain
    """
    gain = transfer_gain(env1, env2)
    if isinstance(gain, NotApplicable):
        return False
    return gain > 0


def transfer_difference(env1: Envelope, env2: Envelope) -> Union[Decimal, NotApplicable]:
    """Absolute difference between amounts (always positive).

    Args:
        env1: First envelope
        env2: Second envelope

    Returns:
        Positive decimal amount or None
    """
    result = get_transfer_amounts(env1, env2)
    if isinstance(result, NotApplicable):
        return NOT_APPLICABLE
    outbound, inbound = result
    return abs(abs(outbound) - abs(inbound))


def date_gap(env1: Envelope, env2: Envelope) -> int:
    """Unsigned date difference between two envelopes.

    This ALWAYS returns a positive value (absolute difference), regardless
    of envelope order or compatibility. Used for anti-patterns that need to
    detect large date gaps even when envelopes wouldn't normally be matched.

    Order doesn't matter: date_gap(A, B) == date_gap(B, A)

    Args:
        env1: First envelope
        env2: Second envelope

    Returns:
        Absolute days between dates (always positive)
        999 if either envelope is missing a date (assume no match)
    """
    if not env1.date or not env2.date:
        # Missing date = assume they don't match (return large gap)
        # This is far more statistically likely than assuming they match
        return 999

    return abs((env2.date - env1.date).days)


def transfer_delay(env1: Envelope, env2: Envelope) -> Optional[int]:
    """Days from outbound to inbound (positive if inbound is later).

    This is directional - it looks at which envelope is the outbound
    and which is the inbound, then calculates the delay.

    For reconciliation (manual+CSV with complete+partial envelopes),
    returns None since transfer delay doesn't apply - we're matching
    the same transaction recorded by different sources, not tracking
    money flow between accounts.

    Args:
        env1: First envelope
        env2: Second envelope

    Returns:
        Number of days (can be negative) or None if not applicable
    """
    # For pure reconciliation (not aggregation), we need to be smart about dates
    # When reconciling complete+partial, we calculate directional delay
    if can_reconcile(env1, env2) and not can_aggregate(env1, env2):
        # Identify which is complete and which is partial
        env1_complete = has_both_legs(env1)
        env2_complete = has_both_legs(env2)

        if env1_complete and not env2_complete:
            # env1 is complete, env2 is partial
            if has_outbound_units(env2):
                # Partial is sending - it represents the outbound leg
                send_date = env2.date
                receive_date = env1.date  # Complete date for comparison
            else:
                # Partial is receiving - it represents the inbound leg
                send_date = env1.date  # Complete date for comparison
                receive_date = env2.date
        elif env2_complete and not env1_complete:
            # env2 is complete, env1 is partial
            if has_outbound_units(env1):
                # Partial is sending - it represents the outbound leg
                send_date = env1.date
                receive_date = env2.date  # Complete date for comparison
            else:
                # Partial is receiving - it represents the inbound leg
                send_date = env2.date  # Complete date for comparison
                receive_date = env1.date
        else:
            # Both complete or both partial - use chronological order
            if env1.date and env2.date and env1.date <= env2.date:
                send_date = env1.date
                receive_date = env2.date
            else:
                send_date = env2.date if env2.date else None
                receive_date = env1.date if env1.date else None

        if send_date and receive_date:
            return (receive_date - send_date).days
        return None

    # For transfers/aggregation, use flow analysis
    compat = envelope_compatibility_checks(env1, env2)
    if not compat.get('compatible', False):
        return None

    # Use the flow pattern to determine dates
    flow = compat.get('flow_pattern')

    # analyze_flow_pattern() names the direction from env1's point of view, so
    # the SAME pair yields 'env1_sends_env2_receives' or
    # 'env1_receives_env2_sends' purely according to the argument order the
    # scorer happened to use. Only the first was handled, so this returned None
    # for half of all pairs and silently dropped the date signal - one same-day
    # transfer scored 220 in one order and 350 in the other.
    #
    # The 'env2_*' spellings below are defensive only: analyze_flow_pattern does
    # not currently emit them. They are cheap to accept and cost nothing if a
    # future producer names a flow from env2's side.
    if flow in ('env1_sends_env2_receives', 'env2_receives_env1_sends'):
        # env1 sends, env2 receives
        outbound_date = env1.date
        inbound_date = env2.date
    elif flow in ('env2_sends_env1_receives', 'env1_receives_env2_sends'):
        # env2 sends, env1 receives
        outbound_date = env2.date
        inbound_date = env1.date
    elif flow in ['both_send', 'both_receive']:
        # For reconciliation (same direction), use chronological order
        # Earlier date is "outbound", later is "inbound" for timing purposes
        if env1.date <= env2.date:
            outbound_date = env1.date
            inbound_date = env2.date
        else:
            outbound_date = env2.date
            inbound_date = env1.date
    elif 'matching' in flow:
        # For complete-partial reconciliation (e.g., "env2_receives_matching_env1_sends")
        # Just use the dates chronologically for delay calculation
        if env1.date <= env2.date:
            outbound_date = env1.date
            inbound_date = env2.date
        else:
            outbound_date = env2.date
            inbound_date = env1.date
    else:
        return None

    if not outbound_date or not inbound_date:
        return None

    # Positive if money arrives later than it was sent
    return (inbound_date - outbound_date).days


def to_percent(amount: Optional[Decimal], base: Optional[Decimal]) -> Optional[Decimal]:
    """Convert amount to percentage of base.

    Returns None if base is 0 or either value is None.
    """
    if amount is None or base is None or base == 0:
        return None
    return (abs(amount) / abs(base)) * Decimal('100')


def transfer_loss_percent(env1: Envelope, env2: Envelope) -> Union[Decimal, NotApplicable]:
    """Transfer loss as percentage of outbound amount.

    Returns NOT_APPLICABLE if not a valid transfer.
    """
    loss = transfer_loss(env1, env2)
    if isinstance(loss, NotApplicable):
        return NOT_APPLICABLE

    if loss == 0:
        return Decimal('0')

    # Get the outbound amount
    result = get_transfer_amounts(env1, env2)
    if isinstance(result, NotApplicable):
        return NOT_APPLICABLE
    outbound, _ = result

    percent = to_percent(loss, abs(outbound))
    return percent if percent is not None else NOT_APPLICABLE


def transfer_gain_percent(env1: Envelope, env2: Envelope) -> Union[Decimal, NotApplicable]:
    """Transfer gain as percentage of outbound amount.

    Returns NOT_APPLICABLE if not a valid transfer.
    """
    gain = transfer_gain(env1, env2)
    if isinstance(gain, NotApplicable):
        return NOT_APPLICABLE

    if gain == 0:
        return Decimal('0')

    # Get the outbound amount
    result = get_transfer_amounts(env1, env2)
    if isinstance(result, NotApplicable):
        return NOT_APPLICABLE
    outbound, _ = result

    percent = to_percent(gain, abs(outbound))
    return percent if percent is not None else NOT_APPLICABLE


def transfer_delta_percent(env1: Envelope, env2: Envelope) -> Union[Decimal, NotApplicable]:
    """Signed transfer difference as percentage of outbound amount.

    Returns NOT_APPLICABLE if not a valid transfer.
    """
    delta = transfer_delta(env1, env2)
    if isinstance(delta, NotApplicable):
        return NOT_APPLICABLE

    # Get the outbound amount
    result = get_transfer_amounts(env1, env2)
    if isinstance(result, NotApplicable):
        return NOT_APPLICABLE
    outbound, _ = result

    percent = to_percent(delta, abs(outbound))
    return percent if percent is not None else NOT_APPLICABLE


def transfer_difference_percent(env1: Envelope, env2: Envelope) -> Union[Decimal, NotApplicable]:
    """Absolute transfer difference as percentage of average of both amounts.

    For reconciliation (manual+CSV matching), compares amounts of the common type
    to validate that the transactions actually represent the same thing.

    Returns NOT_APPLICABLE if not a valid transfer or reconciliation.
    """
    # Handle reconciliation mode (similar to how transfer_delay handles it)
    if can_reconcile(env1, env2) and not can_aggregate(env1, env2):
        # For reconciliation, we're matching the SAME transaction from different sources
        # We need to compare amounts of the common type
        common_types = get_common_types(env1, env2)
        if not common_types:
            return NOT_APPLICABLE

        # Use the first common type (usually there's only one, e.g., GBP)
        common_type = list(common_types)[0]

        # Extract amounts for this type from both envelopes
        amount1 = get_units_for_type(env1, common_type)
        amount2 = get_units_for_type(env2, common_type)

        if amount1 is None or amount2 is None:
            return NOT_APPLICABLE

        # Calculate absolute difference
        difference = abs(abs(amount1) - abs(amount2))

        # Use average of both amounts as base
        avg = (abs(amount1) + abs(amount2)) / 2
        if avg == 0:
            return NOT_APPLICABLE

        percent = to_percent(difference, avg)
        return percent if percent is not None else NOT_APPLICABLE

    # For transfers (money moving between accounts), use the existing logic
    difference = transfer_difference(env1, env2)
    if isinstance(difference, NotApplicable):
        return NOT_APPLICABLE

    # Get both amounts
    result = get_transfer_amounts(env1, env2)
    if isinstance(result, NotApplicable):
        return NOT_APPLICABLE
    outbound, inbound = result

    # Use average of both amounts as base
    avg = (abs(outbound) + abs(inbound)) / 2
    if avg == 0:
        return NOT_APPLICABLE

    percent = to_percent(difference, avg)
    return percent if percent is not None else NOT_APPLICABLE


def amount_match_quality(env1: Envelope, env2: Envelope) -> Union[int, NotApplicable]:
    """Calculate match quality score (0-100) based on account type and amount variance.

    Different rules for different account types:
    - Bank accounts: Strict matching (must be nearly exact)
    - Broker accounts: Tolerant matching (allow fees/FX/rounding)

    Automatically handles reconciliation vs transfer modes.

    Returns:
        Integer quality score (0-100) or NOT_APPLICABLE if can't compare
    """
    # First, extract amounts based on comparison mode
    abs_diff = None
    pct_diff = None

    # Reconciliation mode: compare amounts of common type
    if can_reconcile(env1, env2) and not can_aggregate(env1, env2):
        common_types = get_common_types(env1, env2)
        if not common_types:
            return NOT_APPLICABLE

        common_type = list(common_types)[0]
        amount1 = get_units_for_type(env1, common_type)
        amount2 = get_units_for_type(env2, common_type)

        if amount1 is None or amount2 is None:
            return NOT_APPLICABLE

        abs_diff = abs(abs(amount1) - abs(amount2))
        baseline = abs(amount1)
        if baseline == 0:
            return NOT_APPLICABLE
        pct_diff = to_percent(abs_diff, baseline)

    else:
        # Transfer mode: use existing logic
        result = get_transfer_amounts(env1, env2)
        if isinstance(result, NotApplicable):
            return NOT_APPLICABLE

        outbound, inbound = result
        abs_diff = abs(abs(outbound) - abs(inbound))
        baseline = abs(outbound)
        if baseline == 0:
            return NOT_APPLICABLE
        pct_diff = to_percent(abs_diff, baseline)

    if abs_diff is None or pct_diff is None:
        return NOT_APPLICABLE

    # Determine account type (bank vs broker)
    env1_account = get_primary_account(env1)
    env2_account = get_primary_account(env2)

    is_bank_transfer = (
        account_is_bank(env1_account) or
        account_is_bank(env2_account)
    )

    # Apply account-type-specific thresholds
    if is_bank_transfer:
        # STRICT: Bank accounts require exact matches
        if abs_diff < Decimal('0.01'):  # Within 1 penny
            return 100  # Exact match
        else:
            return 0  # Unacceptable variance for bank accounts

    else:
        # TOLERANT: Broker accounts allow slippage
        # Must pass BOTH absolute AND percentage thresholds
        if abs_diff < Decimal('1'):
            return 100  # Perfect match
        elif abs_diff < Decimal('5') and pct_diff < Decimal('2'):
            return 80  # Excellent (small variance)
        elif abs_diff < Decimal('10') and pct_diff < Decimal('5'):
            return 60  # Acceptable (fees/rounding)
        else:
            return 0  # Unacceptable (exceeds £10 OR 5%)


def has_transfer_gain(env1: Envelope, env2: Envelope) -> bool:
    """Check if transfer resulted in a gain (received more than sent).

    Returns False if not a valid transfer.
    """
    gain = transfer_gain(env1, env2)
    return not isinstance(gain, NotApplicable) and gain > 0


def has_explicit_match_directive(env1: Envelope, env2: Envelope) -> bool:
    """Check if manual envelope has explicit match directive in metadata.

    Manual Beancount transactions can include metadata:
      match-target: "envelope_id"     ; Standard match directive
      override-target: "envelope_id"  ; Force match (higher priority)
    """
    env1_meta = env1.metadata or {}
    env2_meta = env2.metadata or {}

    # Check if either envelope targets the other
    # Note: Beancount uses hyphens in metadata keys
    if env1_meta.get('match-target') == env2.envelope_id:
        return True
    if env2_meta.get('match-target') == env1.envelope_id:
        return True

    # Also check override-target (even higher priority)
    if env1_meta.get('override-target') == env2.envelope_id:
        return True
    if env2_meta.get('override-target') == env1.envelope_id:
        return True

    return False


def flows_opposite(env1: Envelope, env2: Envelope) -> bool:
    """Check if envelopes have opposite flows (one in, one out).

    Args:
        env1: First envelope
        env2: Second envelope

    Returns:
        True if flows are opposite (one sends, one receives)
    """
    # Get net flow for each envelope
    # Positive = inbound, Negative = outbound
    def get_net_flow(env):
        inbound = env.inbound_units if env.inbound_units else 0
        outbound = env.outbound_units if env.outbound_units else 0
        return inbound - outbound

    flow1 = get_net_flow(env1)
    flow2 = get_net_flow(env2)

    # Opposite flows means one positive, one negative
    return (flow1 > 0 and flow2 < 0) or (flow1 < 0 and flow2 > 0)


def has_override_directive(env1: Envelope, env2: Envelope) -> bool:
    """Check if envelope has override directive (stronger than match).

    Override directives force a match even when other criteria don't match.
    Checks if env1 has override-target pointing to env2 or vice versa.
    """
    env1_meta = env1.metadata or {}
    env2_meta = env2.metadata or {}

    # Override is stronger than match - check both directions
    if env1_meta.get('override-target') == env2.envelope_id:
        return True
    if env2_meta.get('override-target') == env1.envelope_id:
        return True

    return False

def both_sources_csv(env1: Envelope, env2: Envelope) -> bool:
    """Check if both envelopes are from CSV sources."""
    return both(env1, env2, is_csv_envelope)


# ============================================================================
# Reconciliation vs Aggregation Utilities
# ============================================================================

def either_has_skip_transfer_detection(env1: Envelope, env2: Envelope) -> bool:
    """Check if either envelope has skip-transfer-detection metadata flag.

    This allows manual transactions to opt out of transfer matching entirely.

    Args:
        env1: First envelope
        env2: Second envelope

    Returns:
        True if either envelope has skip-transfer-detection: true
    """
    env1_meta = env1.metadata or {}
    env2_meta = env2.metadata or {}

    return (env1_meta.get('skip-transfer-detection') is True or
            env2_meta.get('skip-transfer-detection') is True)


def shared_flow_account(env1: Envelope, env2: Envelope) -> bool:
    """Check if envelopes share at least one flow account.

    This is used to detect reconciliation (same account, different views)
    vs aggregation (different accounts, combining legs).

    Args:
        env1: First envelope
        env2: Second envelope

    Returns:
        True if any account appears in both envelopes' flows

    Example:
        # Reconciliation: both touch Assets:Bank:Starling
        env1: Assets:Bank:Starling -> [destination]
        env2: Assets:Bank:Starling -> [destination]

        # Aggregation: different accounts
        env1: Assets:Bank:Starling -> [destination]
        env2: [source] -> Assets:Broker:HL:SIPP
    """
    env1_accounts = get_all_accounts(env1)
    env2_accounts = get_all_accounts(env2)

    return bool(env1_accounts & env2_accounts)

# ========================================================================
# Reconciliation vs Aggregation Utilities
# ========================================================================
# These utilities support distinguishing between:
# - Reconciliation: Two views of the SAME transaction (manual + CSV from same account)
# - Aggregation: Two partial legs forming complete transaction (different accounts)
# ========================================================================


def either_has_skip_transfer_detection(env1: Envelope, env2: Envelope) -> bool:
    """Check if either envelope has skip-transfer-detection metadata flag.

    This allows manual transactions to opt out of transfer detection entirely.

    Args:
        env1: First envelope
        env2: Second envelope

    Returns:
        True if either envelope has skip-transfer-detection: true
    """
    env1_meta = env1.metadata or {}
    env2_meta = env2.metadata or {}

    # Check various forms of the flag (with/without hyphens, underscores)
    skip_keys = ['skip-transfer-detection', 'skip_transfer_detection']

    for key in skip_keys:
        if env1_meta.get(key) in [True, 'true', 'True', '1']:
            return True
        if env2_meta.get(key) in [True, 'true', 'True', '1']:
            return True

    return False




def units_match(env1: Envelope, env2: Envelope) -> bool:
    """Check if unit amounts match (considering inbound vs outbound).

    This checks the NUMERIC VALUES only, not the types.
    Type compatibility is checked separately via envelope_compatibility_checks.

    For envelopes with matching flow (both send OR both receive):
    - Compares outbound to outbound, or inbound to inbound
    - Used for reconciliation (manual + CSV of same transaction)

    Args:
        env1: First envelope
        env2: Second envelope

    Returns:
        True if numeric values match
    """
    # Check flow pattern
    has_out1 = env1.outbound_units is not None
    has_in1 = env1.inbound_units is not None
    has_out2 = env2.outbound_units is not None
    has_in2 = env2.inbound_units is not None

    # Both have outbound - compare outbound values
    if has_out1 and has_out2:
        return abs(env1.outbound_units) == abs(env2.outbound_units)

    # Both have inbound - compare inbound values
    if has_in1 and has_in2:
        return abs(env1.inbound_units) == abs(env2.inbound_units)

    # Different flows - not a match
    return False


def opposite_envelope_source_types(env1: Envelope, env2: Envelope) -> bool:
    """Check if envelopes have opposite source types (one manual, one CSV).

    Uses the one_each combinator to ensure exactly one is manual and one is CSV.
    This is the key check for reconciliation.

    Args:
        env1: First envelope
        env2: Second envelope

    Returns:
        True if one is manual and one is CSV
    """
    return one_each(env1, env2, is_manual_envelope, is_csv_envelope)


def get_manual_and_csv_envelopes(env1: Envelope, env2: Envelope) -> Optional[Tuple[Envelope, Envelope]]:
    """Get manual and CSV envelopes if exactly one of each type.

    Returns them in a consistent order: (manual_env, csv_env).
    Used by merger to detect reconciliation.

    Args:
        env1: First envelope
        env2: Second envelope

    Returns:
        (manual_env, csv_env) if exactly one manual and one CSV
        None otherwise

    Example:
        Manual transfer + Bank CSV → (manual_env, bank_csv_env)
        Both manual → None
        Both CSV → None
    """
    if is_manual_envelope(env1) and is_csv_envelope(env2):
        return (env1, env2)
    elif is_manual_envelope(env2) and is_csv_envelope(env1):
        return (env2, env1)
    else:
        return None



def both_sources_manual(env1: Envelope, env2: Envelope) -> bool:
    """Check if both envelopes are from manual sources."""
    return both(env1, env2, is_manual_envelope)


def one_source_csv_one_manual(env1: Envelope, env2: Envelope) -> bool:
    """Check if one envelope is CSV and the other is manual."""
    return one_each(env1, env2, is_csv_envelope, is_manual_envelope)


# ========================================================================
# ENVELOPE STRUCTURE AND COMPATIBILITY CHECKING
# ========================================================================


def get_outbound_units(envelope: Envelope) -> Optional[Decimal]:
    """
    Get the outbound units for this envelope.
    """
    return envelope.outbound_units

def get_inbound_units(envelope: Envelope) -> Optional[Decimal]:
    """
    Get the inbound units for this envelope.
    """
    return envelope.inbound_units

def has_outbound_units(envelope: Envelope) -> bool:
    """Check if envelope has non-zero outbound units.

    Args:
        envelope: The envelope to check

    Returns:
        True if envelope has outbound units > 0
    """

    units = get_outbound_units(envelope)
    if units is None or units == 0:
        return False
    return True

def has_inbound_units(envelope: Envelope) -> bool:
    """Check if envelope has non-zero inbound units.

    Args:
        envelope: The envelope to check

    Returns:
        True if envelope has inbound units > 0
    """

    units = get_inbound_units(envelope)
    if units is None or units == 0:
        return False
    return True

def has_flow(envelope: Envelope) -> bool:
    """Check if envelope has any cash flow (inbound or outbound).

    Args:
        envelope: The envelope to check

    Returns:
        True if envelope has either inbound or outbound units
    """
    return has_inbound_units(envelope) or has_outbound_units(envelope)


def has_both_legs(envelope: Envelope) -> bool:
    """Check if envelope has both inbound and outbound (complete trade).

    This indicates a complete transaction like a BUY or SELL that shows
    both what went out and what came in.
    """
    return has_inbound_units(envelope) and has_outbound_units(envelope)


def is_single_leg(envelope: Envelope) -> bool:
    """Check if envelope has only one direction (partial transaction).

    This is typical of CSV imports that show only one side of a transaction.
    """
    return has_inbound_units(envelope) ^ has_outbound_units(envelope)  # XOR - exactly one


# ========================================================================
# ENVELOPE DIRECTION CHECKS (Expense/Income/Transfer Classification)
# Used by expense categorization engine and other processors
# ========================================================================

def is_expense_envelope(envelope: Envelope) -> bool:
    """Check if envelope is an expense.

    For asset accounts (banks, brokers): outbound = expense
    For liability accounts (credit cards): outbound (debt increasing) = expense

    EnvelopeBuilder normalises liability statements to the Beancount convention,
    so debt increasing is an OUTBOUND posting. This branch tested inbound until
    that landed, which stopped credit card purchases appearing in the
    uncategorised-expense report.
    """
    # Check for liability accounts (credit cards, loans)
    primary_account = envelope.inbound_account or envelope.outbound_account or ''
    if account_is_liability(primary_account):
        # For liabilities: outbound (debt increasing) = expense
        return has_outbound_units(envelope) and not has_inbound_units(envelope)
    else:
        # For assets: outbound = expense
        return has_outbound_units(envelope) and not has_inbound_units(envelope)


def is_income_envelope(envelope: Envelope) -> bool:
    """Check if envelope is income.

    For asset accounts (banks, brokers): inbound = income
    For liability accounts (credit cards): inbound (debt decreasing) = refund/payment

    Mirror of is_expense_envelope: under the normalised convention debt
    DECREASING is an inbound posting. Note this is "not an expense" rather than
    "income" - a bill payment and a refund both land here and neither is income.
    """
    # Check for liability accounts (credit cards, loans)
    primary_account = envelope.inbound_account or envelope.outbound_account or ''
    if account_is_liability(primary_account):
        # For liabilities: inbound (debt decreasing) = refund/payment
        return has_inbound_units(envelope) and not has_outbound_units(envelope)
    else:
        # For assets: inbound = income
        return has_inbound_units(envelope) and not has_outbound_units(envelope)


def is_transfer_envelope(envelope: Envelope) -> bool:
    """Check if envelope is a transfer (both inbound and outbound)."""
    return has_outbound_units(envelope) and has_inbound_units(envelope)


def check_envelope_type(envelope: Envelope, required_type: str) -> bool:
    """Check if envelope matches required structure type.

    Args:
        envelope: Envelope to check
        required_type: One of 'outbound_only', 'inbound_only', 'both'

    Returns:
        True if envelope structure matches requirement
    """
    # STRUCTURAL test, deliberately not an economic one. 'envelope_type' is the
    # shape key used by every pattern config, and it must mean literally "has
    # outbound units and no inbound units".
    #
    # This used to delegate to is_expense_envelope/is_income_envelope. On an
    # asset account the two readings coincide, so the conflation was invisible -
    # but those helpers invert for liabilities (debt increasing is spending),
    # which made 'outbound_only' silently mean "is spending" and flipped the
    # meaning of every pattern applied to a card or loan.
    if required_type == 'outbound_only':
        return has_outbound_units(envelope) and not has_inbound_units(envelope)
    elif required_type == 'inbound_only':
        return has_inbound_units(envelope) and not has_outbound_units(envelope)
    elif required_type == 'both':
        return is_transfer_envelope(envelope)
    return True  # Unknown type = no restriction


def is_credit_card_envelope(envelope: Envelope) -> bool:
    """Check if envelope's primary account is a credit card.

    Uses account_is_credit_card() from accounts.py.
    """
    from cassoulet.utils.accounts import account_is_credit_card
    account = get_outbound_account(envelope) or get_inbound_account(envelope)
    if not account:
        return False
    return account_is_credit_card(account)


def get_outbound_type(envelope: Envelope) -> Optional[str]:
    """Get the outbound type for this envelope."""
    return envelope.outbound_type or None

def get_inbound_type(envelope: Envelope) -> Optional[str]:
    """Get the inbound type for this envelope."""
    return envelope.inbound_type or None

def get_envelope_types(envelope: Envelope) -> Set[str]:
    """Get all types present in an envelope.

    Returns:
        Set of type strings (currencies/commodities), excluding None
    """
    return {t for t in (envelope.outbound_type, envelope.inbound_type) if t}

def types_compatible(env1: Envelope, env2: Envelope) -> bool:
    """Check if envelope types overlap (including subset relationships).

    Examples:
        - GBP envelope + GBP envelope -> True
        - GBP+AAPL envelope + GBP envelope -> True (subset)
        - GBP envelope + USD envelope -> False (no overlap)
    """
    types1 = get_envelope_types(env1)
    types2 = get_envelope_types(env2)
    return bool(types1 & types2)  # Any overlap


def get_common_types(env1: Envelope, env2: Envelope) -> Set[str]:
    """Get types that appear in both envelopes.

    Returns:
        Set of common type strings
    """
    types1 = get_envelope_types(env1)
    types2 = get_envelope_types(env2)
    return types1 & types2


def get_units_for_type(envelope: Envelope, type_: str) -> Optional[Decimal]:
    """Get units for a specific type in an envelope.

    Args:
        envelope: Envelope to check
        type_: Type string to look for (e.g., 'GBP', 'AAPL')

    Returns:
        Units if type is found, None otherwise
    """
    if get_outbound_type(envelope) == type_:
        return envelope.outbound_units
    if get_inbound_type(envelope) == type_:
        return envelope.inbound_units
    return None


def units_compatible_for_type(env1: Envelope, env2: Envelope, type_: str) -> bool:
    """Check if units match for a specific type in both envelopes.

    Args:
        env1: First envelope
        env2: Second envelope
        type_: Type to check units for

    Returns:
        True if units match for this type
    """
    units1 = get_units_for_type(env1, type_)
    units2 = get_units_for_type(env2, type_)
    return equals(units1, units2)


def units_compatible(env1: Envelope, env2: Envelope) -> bool:
    """Check if units match for all overlapping types.

    For each type that appears in both envelopes, the units must match.
    """
    common_types = get_common_types(env1, env2)
    if not common_types:
        return False  # No types in common

    # All common types must have matching units
    return all(units_compatible_for_type(env1, env2, type_)
              for type_ in common_types)


# ========================================================================
# SUBSET RELATIONSHIP FUNCTIONS
#
# These two functions check for subset relationships but serve different purposes:
# - is_subset_of: Core function, directional check, takes Envelope objects
# - is_subset_relationship: Pattern wrapper, bidirectional, takes two envelopes data
# ========================================================================

def is_subset_of(partial: Envelope, complete: Envelope) -> bool:
    """CORE FUNCTION: Check if partial envelope data is contained in complete envelope.

    This is the low-level function that does the actual subset checking.
    It is DIRECTIONAL - the order of arguments matters.

    Use this when:
    - You have two Envelope objects directly
    - You need to check a specific direction (is A a subset of B?)
    - You're implementing internal logic

    Example:
        CSV import has: "100 AAPL" (partial)
        Manual entry has: "Sell 100 AAPL for £8000" (complete)
        is_subset_of(csv_envelope, manual_envelope) -> True

    Args:
        partial: Potentially partial envelope (must be first)
        complete: Potentially complete envelope (must be second)

    Returns:
        True if partial's data matches one side of complete
    """
    # Complete must actually be complete
    if not has_both_legs(complete):
        return False

    # Partial should be single leg (but could be complete too)
    if get_outbound_type(partial) and get_outbound_units(partial):
        return (equals(get_outbound_type(partial), get_outbound_type(complete)) and
                equals(get_outbound_units(partial), get_outbound_units(complete)))
    elif get_inbound_type(partial) and get_inbound_units(partial):
        return (equals(get_inbound_type(partial), get_inbound_type(complete)) and
                equals(get_inbound_units(partial), get_inbound_units(complete)))

    return False


def is_subset_relationship(env1: Envelope, env2: Envelope) -> bool:
    """Check if either envelope is a subset of the other.

    This is BIDIRECTIONAL - checks both directions automatically.

    Use this when:
    - You're defining patterns in transfer_scoring_patterns.py
    - You want to check if there's ANY subset relationship (either direction)

    Example pattern usage:
        'trade_subset_pattern': {
            'patterns': [{
                'is_subset_relationship': True,
            }]
        }

    Args:
        env1: First envelope
        env2: Second envelope

    Returns:
        True if either envelope's data is a subset of the other
    """
    # Check BOTH directions - this is the key difference from is_subset_of
    return is_subset_of(env1, env2) or is_subset_of(env2, env1)


# ========================================================================
# ENVELOPE RELATIONSHIP ANALYSIS - Modular Components
# ========================================================================
# These functions analyze specific aspects of envelope relationships.
# They can be used independently or composed together.
# ========================================================================

def analyze_structure(env1: Envelope, env2: Envelope) -> Optional[str]:
    """Analyze the structural relationship between two envelopes.

    Returns:
        'single-single': Both have one leg only
        'complete-complete': Both have both legs
        'complete-partial': One has both legs, one has one leg
        None: Invalid structure
    """

    complete1 = has_both_legs(env1)
    complete2 = has_both_legs(env2)
    single1 = is_single_leg(env1)
    single2 = is_single_leg(env2)

    if complete1 and complete2:
        return 'complete-complete'
    elif (complete1 and single2) or (complete2 and single1):
        return 'complete-partial'
    elif single1 and single2:
        return 'single-single'
    else:
        return None

def analyze_type_overlap(env1: Envelope, env2: Envelope) -> set:
    """Analyze type overlap between two envelopes.

    Returns:
        Set of common types (e.g., {'GBP', 'VLS100'})
    """
    # Use the centralized helper that checks both direct attributes and transaction_data
    types1 = get_envelope_types(env1)
    types2 = get_envelope_types(env2)

    return types1 & types2


def analyze_flow_pattern(
    env1: Envelope,
    env2: Envelope,
    structure: str
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Analyze flow pattern and roles for envelope pair.

    Args:
        env1: First envelope
        env2: Second envelope
        structure: Structure type from analyze_structure()

    Returns:
        (flow_pattern, env1_role, env2_role) or (None, None, None)
    """
    has_out1 = has_outbound_units(env1)
    has_in1 = has_inbound_units(env1)
    has_out2 = has_outbound_units(env2)
    has_in2 = has_inbound_units(env2)

    if structure == 'single-single':

        if has_out1 and not has_in1 and has_in2 and not has_out2:
            return ('env1_sends_env2_receives', 'sender', 'receiver')
        elif has_in1 and not has_out1 and has_out2 and not has_in2:
            return ('env1_receives_env2_sends', 'receiver', 'sender')
        elif (has_out1 and has_out2 and not has_in1 and not has_in2):
            return ('both_send', 'sender', 'sender')
        elif (has_in1 and has_in2 and not has_out1 and not has_out2):
            return ('both_receive', 'receiver', 'receiver')
        else:
            return ('unknown', 'unknown', 'unknown')

    elif structure == 'complete-partial':
        complete1 = has_both_legs(env1)

        if complete1:
            role1, role2 = 'complete_trade', 'partial_leg'
            # Check how env2 (partial) relates to env1 (complete)
            if has_out2 and has_in1:
                pattern = 'env2_sends_matching_env1_receives'
            elif has_in2 and has_out1:
                pattern = 'env2_receives_matching_env1_sends'
            elif has_out2 and has_out1:
                pattern = 'env2_sends_matching_env1_sends'
            elif has_in2 and has_in1:
                pattern = 'env2_receives_matching_env1_receives'
            else:
                pattern = 'unknown'
        else:
            role1, role2 = 'partial_leg', 'complete_trade'
            # Check how env1 (partial) relates to env2 (complete)
            if has_out1 and has_in2:
                pattern = 'env1_sends_matching_env2_receives'
            elif has_in1 and has_out2:
                pattern = 'env1_receives_matching_env2_sends'
            elif has_out1 and has_out2:
                pattern = 'env1_sends_matching_env2_sends'
            elif has_in1 and has_in2:
                pattern = 'env1_receives_matching_env2_receives'
            else:
                pattern = 'unknown'

        return (pattern, role1, role2)

    elif structure == 'complete-complete':
        return ('both_complete_trades', 'complete_trade', 'complete_trade')

    else:
        return (None, None, None)


def can_reconcile(env1: Envelope, env2: Envelope) -> bool:
    """Check if two envelopes can be reconciled.

    Reconciliation = two views of the SAME transaction.

    Requirements:
    - One manual, one CSV
    - Type overlap
    - Compatible structure (any structure is potentially valid)
    - Shared flow account (they must touch the same account)

    Returns:
        True if reconciliation is possible
    """
    # Must be manual + CSV
    if not opposite_envelope_source_types(env1, env2):
        return False

    # Must have type overlap
    type_overlap = analyze_type_overlap(env1, env2)
    if not type_overlap:
        return False

    # Must have valid structure
    structure = analyze_structure(env1, env2)
    if structure is None:
        return False

    # Must share a flow account (critical for reconciliation)
    if not shared_flow_account(env1, env2):
        return False

    return True


def can_aggregate(env1: Envelope, env2: Envelope) -> bool:
    """Check if two envelopes can be aggregated.

    Aggregation = merging two partial legs into one complete transaction.

    Requirements:
    - Both CSV (not manual)
    - Type overlap
    - Opposite flows (one sends, one receives)
    - Compatible transaction types
    - No complete broker transaction with bank transfer

    Returns:
        True if aggregation is possible
    """
    # Must be CSV + CSV
    if not both(env1, env2, is_csv_envelope):
        return False

    # Must have type overlap
    if not analyze_type_overlap(env1, env2):
        return False

    # Must have valid structure
    structure = analyze_structure(env1, env2)
    if structure is None:
        return False

    # CRITICAL: A complete broker transaction should NEVER aggregate with a bank transfer
    # This is the main source of bad matches

    # Check if we have any complete investment transactions
    has_complete_investment = False
    for env in [env1, env2]:
        if has_both_legs(env):
            try:
                actual_type = classify_transaction_type(env)
                if actual_type in {'BUY', 'SELL'}:
                    has_complete_investment = True
                    break
            except:
                # Fall back to transaction_type field
                if env.transaction_type in {'BUY', 'SELL'}:
                    has_complete_investment = True
                    break

    # If we have a complete investment transaction, don't aggregate with bank transfers
    if has_complete_investment and either_account_is_bank(env1, env2):
        # Exception: Allow bank-to-broker funding transfers
        if one_account_is_bank_one_is_broker(env1, env2):
            # This is likely a funding transfer - let scoring decide
            pass
        else:
            return False

    # The complete broker transaction + bank transfer check above handles the main issue.
    # We don't want to be too restrictive here because:
    # - Partial imports might have mismatched types
    # - Different systems classify transactions differently
    # - We should rely on scoring patterns to determine compatibility

    # Check flow pattern for opposite flows
    flow_pattern, _, _ = analyze_flow_pattern(env1, env2, structure)

    # Must have opposite flows for aggregation
    if flow_pattern in ['env1_sends_env2_receives', 'env1_receives_env2_sends']:
        return True

    return False


def _determine_comparison_mode(
    structure: str,
    source_pattern: str,
    flow_pattern: str,
    env1: Envelope,
    env2: Envelope
) -> Optional[str]:
    """Determine the comparison mode for an envelope pair.

    This is a modular helper that encapsulates the comparison mode logic.

    Args:
        structure: Structural relationship (single-single, complete-complete, etc.)
        source_pattern: Source relationship (csv-csv, manual-csv, etc.)
        flow_pattern: Flow relationship (env1_sends_env2_receives, etc.)
        env1: First envelope (for subset checking)
        env2: Second envelope (for subset checking)

    Returns:
        Comparison mode string or None if no valid mode
    """
    # Handle complete-partial structures
    if structure == 'complete-partial':
        if is_subset_of(env1, env2) or is_subset_of(env2, env1):
            return 'subset'
        else:
            return 'partial-match'

    # Handle manual-csv patterns (reconciliation candidates)
    if source_pattern == 'manual-csv':
        # Single-single with same flow = reconciliation
        if flow_pattern in ['both_send', 'both_receive']:
            return 'reconciliation'

        # Complete-partial with matching sides = reconciliation
        # (manual complete + CSV partial of same transaction)
        if flow_pattern in [
            'env1_sends_matching_env2_sends',
            'env1_receives_matching_env2_receives',
            'env2_sends_matching_env1_sends',
            'env2_receives_matching_env1_receives'
        ]:
            return 'reconciliation'

        # Complete-complete = reconciliation
        # (manual complete + CSV complete of same transaction)
        if flow_pattern == 'both_complete_trades':
            return 'reconciliation'

        # Opposite flows = manual override transfer
        if flow_pattern in ['env1_sends_env2_receives', 'env1_receives_env2_sends']:
            return 'manual-override-transfer'

    # Handle csv-csv patterns (transfers and aggregation)
    if source_pattern == 'csv-csv':
        # Opposite flows = transfer/aggregation
        if flow_pattern in ['env1_sends_env2_receives', 'env1_receives_env2_sends']:
            return 'transfer'

        # Same flow = suspicious duplicate
        if flow_pattern in ['both_send', 'both_receive']:
            return 'suspicious-duplicate'

    # No valid comparison mode
    return None


# Scoped cache for envelope_compatibility_checks.
# Set to {} to enable caching (e.g. during TransferScorer), None to disable.
_compat_cache: Optional[Dict] = None


def envelope_compatibility_checks(env1: Envelope, env2: Envelope) -> Dict[str, Any]:
    """Comprehensive compatibility check - orchestrates modular analyzers.

    This aggregator function provides detailed diagnostic information by composing
    the modular analyzer functions. For simple checks, use can_reconcile() or
    can_aggregate() instead.

    The relationship is DIRECTIONAL - (env1, env2) may differ from (env2, env1).

    Results are cached per (env1_id, env2_id) pair when _compat_cache is enabled.

    Returns a dictionary with:
        'compatible': bool - Whether envelopes can be meaningfully compared
        'structure': str - Structural relationship (single-single, complete-complete, etc.)
        'source_pattern': str - Source relationship (csv-csv, manual-csv, etc.)
        'type_overlap': set - Common types between envelopes
        'flow_pattern': str - Flow relationship FROM env1's perspective
        'env1_role': str - What role env1 plays (sender, receiver, complete, partial, etc.)
        'env2_role': str - What role env2 plays
        'comparison_mode': str - How to compare (transfer, reconciliation, subset, etc.)
        'directional_guidance': dict - Specific guidance for THIS direction
    """
    if _compat_cache is not None:
        key = (env1.envelope_id, env2.envelope_id)
        cached = _compat_cache.get(key)
        if cached is not None:
            return cached

    result = {
        'compatible': False,
        'structure': None,
        'source_pattern': None,
        'type_overlap': set(),
        'flow_pattern': None,
        'env1_role': None,
        'env2_role': None,
        'comparison_mode': None,
        'directional_guidance': {}
    }

    # Use modular analyzers
    result['structure'] = analyze_structure(env1, env2)
    if result['structure'] is None:
        return result

    result['source_pattern'] = determine_source_pair_type(env1, env2)
    result['type_overlap'] = analyze_type_overlap(env1, env2)

    if not result['type_overlap']:
        return result

    # Analyze flow pattern
    flow_pattern, env1_role, env2_role = analyze_flow_pattern(env1, env2, result['structure'])
    result['flow_pattern'] = flow_pattern
    result['env1_role'] = env1_role
    result['env2_role'] = env2_role

    # Determine comparison mode
    result['comparison_mode'] = _determine_comparison_mode(
        structure=result['structure'],
        source_pattern=result['source_pattern'],
        flow_pattern=result['flow_pattern'],
        env1=env1,
        env2=env2
    )

    # Step 6: Set compatibility and comparison guidance
    if result['comparison_mode'] in ['transfer', 'reconciliation', 'subset', 'partial-match', 'manual-override-transfer']:
        result['compatible'] = True

        # Provide directional guidance based on roles
        common_type = list(result['type_overlap'])[0] if result['type_overlap'] else None

        if result['comparison_mode'] == 'transfer':
            if result['env1_role'] == 'sender':
                result['directional_guidance'] = {
                    'extract_from_env1': f'outbound_{common_type}',
                    'extract_from_env2': f'inbound_{common_type}',
                    'comparison': 'env1 sent amount vs env2 received amount',
                    'expected': 'env1.outbound + env2.inbound ≈ 0 (allowing fees)'
                }
            else:
                result['directional_guidance'] = {
                    'extract_from_env1': f'inbound_{common_type}',
                    'extract_from_env2': f'outbound_{common_type}',
                    'comparison': 'env1 received amount vs env2 sent amount',
                    'expected': 'env2.outbound + env1.inbound ≈ 0 (allowing fees)'
                }
        elif result['comparison_mode'] == 'reconciliation':
            result['directional_guidance'] = {
                'extract_from_env1': f'{"outbound" if env1.outbound_units else "inbound"}_{common_type}',
                'extract_from_env2': f'{"outbound" if env2.outbound_units else "inbound"}_{common_type}',
                'comparison': 'Both show same transaction from different sources',
                'expected': 'amounts should be identical'
            }
        elif result['comparison_mode'] in ['subset', 'partial-match']:
            if result['env1_role'] == 'partial_leg':
                leg_type = 'outbound' if env1.outbound_units else 'inbound'
                result['directional_guidance'] = {
                    'extract_from_env1': f'{leg_type}_{common_type}',
                    'extract_from_env2': f'{leg_type}_{common_type} from complete trade',
                    'comparison': f'env1 partial {leg_type} vs env2 complete\'s {leg_type}',
                    'expected': 'amounts should match for the specific leg'
                }
            else:
                leg_type = 'outbound' if env2.outbound_units else 'inbound'
                result['directional_guidance'] = {
                    'extract_from_env1': f'{leg_type}_{common_type} from complete trade',
                    'extract_from_env2': f'{leg_type}_{common_type}',
                    'comparison': f'env1 complete\'s {leg_type} vs env2 partial {leg_type}',
                    'expected': 'amounts should match for the specific leg'
                }

    if _compat_cache is not None:
        _compat_cache[(env1.envelope_id, env2.envelope_id)] = result
    return result


# ========================================================================
# Pattern Matcher Wrapper Functions
# These functions wrap envelope_utilities functions for use in patterns
# ========================================================================

def is_compatible_pair(env1: Envelope, env2: Envelope) -> bool:
    """Check if envelope pair is compatible.

    Args:
        env1: First envelope
        env2: Second envelope

    Returns:
        True if envelopes are compatible
    """
    compat = envelope_compatibility_checks(env1, env2)
    return compat.get('compatible', False)


def is_potential_in_specie_transfer(env1: Envelope, env2: Envelope) -> bool:
    """Check if envelope pair represents a potential in-specie (commodity) transfer.

    In-specie transfers move securities/commodities between broker accounts and
    typically take longer to settle than cash transfers (5-30 days is normal).

    Args:
        env1: First envelope
        env2: Second envelope

    Returns:
        True if this looks like an in-specie transfer:
        - Both accounts are broker accounts (Assets:Broker:*)
        - Transferring a commodity (not currency)
        - Quantities match (within tolerance)
    """
    # Check both have broker accounts
    accounts = []
    if env1.inbound_account:
        accounts.append(env1.inbound_account)
    if env1.outbound_account:
        accounts.append(env1.outbound_account)
    if env2.inbound_account:
        accounts.append(env2.inbound_account)
    if env2.outbound_account:
        accounts.append(env2.outbound_account)

    # Must have at least 2 accounts and all must be broker accounts
    if len(accounts) < 2:
        return False
    if not all('Broker' in acc for acc in accounts):
        return False

    # Check if transferring a commodity (not currency)
    types = set()
    if env1.inbound_type:
        types.add(env1.inbound_type)
    if env1.outbound_type:
        types.add(env1.outbound_type)
    if env2.inbound_type:
        types.add(env2.inbound_type)
    if env2.outbound_type:
        types.add(env2.outbound_type)

    # Must have exactly one type and it must not be a currency
    if len(types) != 1:
        return False

    transfer_type = list(types)[0]
    if is_currency(transfer_type):
        return False

    # Check quantities match (within tolerance)
    amounts = get_transfer_amounts(env1, env2)
    if amounts == NOT_APPLICABLE:
        return False

    outbound_amt, inbound_amt = amounts
    if outbound_amt is None or inbound_amt is None:
        return False

    # For commodities, quantities should match exactly
    return abs(abs(outbound_amt) - abs(inbound_amt)) < Decimal('0.001')


# NOTE: EnvelopeSingleUtilities and EnvelopePairUtilities classes were removed.
# Pattern matching now uses direct module lookup - all utility functions in this
# module are automatically available for pattern matching without registration.
# This eliminates the maintenance burden and prevents silent failures when new
# functions are added but not registered.


def match_pattern_single(envelope: Envelope, pattern: Dict[str, Any], utility_provider: Any = None) -> bool:
    """
    Match a pattern against a single envelope.

    Args:
        envelope: The envelope to match against
        pattern: Pattern dict with field conditions
        utility_provider: DEPRECATED - kept for API compatibility, ignored.

    Returns:
        True if all pattern conditions match

    Example:
        pattern = {'has_both_legs': True, 'transaction_type': 'TRANSFER'}
    """
    # Suppress unused parameter warning - kept for backward compatibility
    _ = utility_provider

    # Get reference to this module for function lookup
    current_module = sys.modules[__name__]

    for field, expected_value in pattern.items():
        # First check if this is a utility function in the current module
        if hasattr(current_module, field):
            utility = getattr(current_module, field)
            # Verify it's callable (is a function)
            if callable(utility):
                try:
                    result = utility(envelope)
                    if not _check_value_match(result, expected_value, None):
                        return False
                    continue  # Pattern matched via utility function
                except TypeError as e:
                    # Wrong number of arguments - not a single-envelope utility
                    logger.debug(f"Pattern utility '{field}' has wrong signature for single envelope: {e}")
                    # Fall through to try as direct field access
                except Exception as e:
                    logger.debug(f"Pattern utility '{field}' failed: {e}")
                    return False

        # Not a utility function - try as direct field check on envelope
        actual_value = getattr(envelope, field, None)
        if not _check_value_match(actual_value, expected_value, None):
            return False

    return True


def match_pattern_pair(env1: Envelope, env2: Envelope, pattern: Dict[str, Any], utility_provider: Any = None) -> bool:
    """
    Match a pattern against an envelope pair.

    Args:
        env1: First envelope
        env2: Second envelope
        pattern: Pattern dict with pair conditions
        utility_provider: DEPRECATED - kept for API compatibility, ignored.

    Returns:
        True if all pattern conditions match

    Example:
        pattern = {'transfer_difference': '0-5', 'flows_opposite': True}
    """
    # Suppress unused parameter warning - kept for backward compatibility
    _ = utility_provider

    # Get reference to this module for function lookup
    current_module = sys.modules[__name__]

    for field, expected_value in pattern.items():
        # Check if this is a utility function in the current module
        if hasattr(current_module, field):
            utility = getattr(current_module, field)
            # Verify it's callable (is a function)
            if not callable(utility):
                logger.debug(f"Pattern field '{field}' exists but is not callable")
                return False

            try:
                result = utility(env1, env2)
                if not _check_value_match(result, expected_value, None):
                    return False
            except TypeError as e:
                # Wrong number of arguments - not a pair utility
                logger.debug(f"Pattern utility '{field}' has wrong signature for pair matching: {e}")
                return False
            except Exception as e:
                logger.debug(f"Pattern utility '{field}' failed: {e}")
                return False
        else:
            # Not a recognized pair utility
            logger.debug(f"Unknown pair pattern field: '{field}' - function not found in module")
            return False

    return True


def _check_value_match(actual_value: Any, expected_value: Any, utility_provider: Any = None) -> bool:
    """
    Check if actual value matches expected pattern value.

    Args:
        actual_value: The actual value to check
        expected_value: The expected value or pattern
        utility_provider: DEPRECATED - kept for API compatibility, ignored.

    Handles various matching types:
    - Boolean equality
    - String wildcards (*text*)
    - Numeric ranges (e.g., '0-5', '>=10')
    - Direct equality
    """
    # Suppress unused parameter warning - kept for backward compatibility
    _ = utility_provider

    if isinstance(expected_value, bool):
        return actual_value == expected_value

    elif isinstance(expected_value, str):
        # Handle wildcards for strings
        if isinstance(actual_value, str):
            if expected_value.startswith('*') and expected_value.endswith('*'):
                # Contains check
                return expected_value[1:-1].lower() in actual_value.lower()
            return actual_value == expected_value

        # Handle numeric ranges
        elif isinstance(actual_value, (int, float, Decimal)):
            return _check_arithmetic(actual_value, expected_value)

        return str(actual_value) == expected_value

    else:
        # Direct comparison
        return actual_value == expected_value


def _check_arithmetic(value: Union[int, float, Decimal], condition: str) -> bool:
    """Check if a numeric value matches an arithmetic condition."""
    if '-' in condition and not condition.startswith('-'):
        # Range check (e.g., '0-5')
        parts = condition.split('-')
        if len(parts) == 2:
            try:
                min_val = float(parts[0])
                max_val = float(parts[1])
                return min_val <= float(value) <= max_val
            except ValueError:
                pass

    # Comparison operators
    if condition.startswith('>='):
        return float(value) >= float(condition[2:])
    elif condition.startswith('<='):
        return float(value) <= float(condition[2:])
    elif condition.startswith('>'):
        return float(value) > float(condition[1:])
    elif condition.startswith('<'):
        return float(value) < float(condition[1:])
    elif condition.startswith('=='):
        return float(value) == float(condition[2:])
    elif condition.startswith('!='):
        return float(value) != float(condition[2:])

    # Exact match
    try:
        return float(value) == float(condition)
    except ValueError:
        return False


# Keep the old function for backwards compatibility but deprecate it
def match_pattern_on_envelope(pattern_data: Any, pattern: Dict[str, Any], utility_provider: Any) -> bool:
    """
    DEPRECATED: Use match_pattern_single or match_pattern_pair instead.

    Central pattern matching logic that supports utility functions.

    This function is used by TransferScorer, InvestmentClassifier and others
    to evaluate patterns that include utility function conditions.

    Args:
        pattern_data: Either an Envelope or dict with 'env1'/'env2' for pairs
        pattern: Pattern dict with field conditions
        utility_provider: Object providing utility functions (e.g., PatternMatcher)

    Returns:
        True if all pattern conditions match

    Examples:
        Single envelope pattern:
            pattern = {'has_both_legs': True, 'outbound_type__is_currency': True}

        Envelope pair pattern:
            pattern = {'transfer_difference': '0-5', 'flows_opposite': True}
    """
    # Delegate to the new functions
    if isinstance(pattern_data, dict) and 'env1' in pattern_data and 'env2' in pattern_data:
        # Pair pattern
        return match_pattern_pair(pattern_data['env1'], pattern_data['env2'], pattern, utility_provider)
    elif isinstance(pattern_data, Envelope):
        # Single envelope pattern
        return match_pattern_single(pattern_data, pattern, utility_provider)
    else:
        # Unsupported pattern data type
        logger.warning(f"Unsupported pattern_data type: {type(pattern_data)}")
        return False