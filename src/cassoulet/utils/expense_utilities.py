"""
Expense-specific utilities for expense categorization.

This module contains utilities specific to expense categorization patterns.
General pattern matching utilities are in pattern_matcher.py.

For pattern authors, use these in expense patterns:
- check_envelope_type: 'outbound_only', 'inbound_only', 'both'
- check_account_type: 'bank', 'broker', 'sipp', 'isa'
- check_amount: '<30', '>500', '20-100', 'positive'
- text_contains: ['NETFLIX', 'SPOTIFY']
- payee/narration: '*TESCO*' wildcards
- date: '2024-01-23' (exact date match - for specific transactions)
- date_from: '2023-04-14' (only match on or after this date)
- date_to: '2024-12-31' (only match on or before this date)
"""

from datetime import date
from typing import List

from cassoulet.stages import Envelope
from cassoulet.utils.envelope_utilities import (
    get_outbound_account,
    get_inbound_account,
    get_outbound_units,
    get_inbound_units,
    # Envelope direction checks
    check_envelope_type,
    is_credit_card_envelope,
)
from cassoulet.utils.pattern_matcher import (
    match_wildcard,
    text_contains_any,
    check_amount as check_amount_value,
)
from cassoulet.utils.accounts import (
    account_is_bank,
    account_is_broker,
    account_is_sipp,
    account_is_isa,
)


# ========================================================================
# TEXT EXTRACTION (expense-specific helpers)
# ========================================================================

def get_searchable_text(envelope: Envelope) -> str:
    """Get combined payee + narration, normalized for matching."""
    parts = []
    if envelope.payee:
        parts.append(envelope.payee.strip().upper())
    if envelope.narration:
        parts.append(envelope.narration.strip().upper())
    return ' '.join(parts)


def envelope_text_contains(envelope: Envelope, keywords: List[str]) -> bool:
    """Check if envelope's payee OR narration contains ANY keyword."""
    text = get_searchable_text(envelope)
    return text_contains_any(text, keywords)


def payee_matches(envelope: Envelope, pattern: str) -> bool:
    """Check if payee matches a wildcard pattern."""
    payee = envelope.payee.strip().upper() if envelope.payee else ''
    return match_wildcard(payee, pattern)


def narration_matches(envelope: Envelope, pattern: str) -> bool:
    """Check if narration matches a wildcard pattern."""
    narration = envelope.narration.strip().upper() if envelope.narration else ''
    return match_wildcard(narration, pattern)


# ========================================================================
# ACCOUNT TYPE CHECKS (expense-specific)
# ========================================================================

_ACCOUNT_TYPE_CHECKS = {
    'bank': account_is_bank,
    'broker': account_is_broker,
    'sipp': account_is_sipp,
    'isa': account_is_isa,
}


def check_account_type(envelope: Envelope, account_type: str) -> bool:
    """Check if outbound account matches specified type."""
    account = get_outbound_account(envelope)
    if not account:
        return False
    check_fn = _ACCOUNT_TYPE_CHECKS.get(account_type.lower())
    return check_fn(account) if check_fn else False


def check_outbound_account_pattern(envelope: Envelope, pattern: str) -> bool:
    """Check if outbound account matches wildcard pattern."""
    account = get_outbound_account(envelope)
    return match_wildcard(account, pattern) if account else False


def check_inbound_account_pattern(envelope: Envelope, pattern: str) -> bool:
    """Check if inbound account matches wildcard pattern."""
    account = get_inbound_account(envelope)
    return match_wildcard(account, pattern) if account else False


# ========================================================================
# AMOUNT CHECK (envelope-aware wrapper)
# ========================================================================

def check_amount(envelope: Envelope, condition: str) -> bool:
    """Check if envelope amount matches condition string.

    Gets the amount from envelope (outbound or inbound) and checks
    against the condition using pattern_matcher.check_amount.
    """
    amount = get_outbound_units(envelope) or get_inbound_units(envelope)
    if amount is None:
        return False
    return check_amount_value(abs(amount), condition)


# ========================================================================
# DATE CHECKS (for time-bounded patterns)
# ========================================================================

def _parse_date(date_str: str) -> date:
    """Parse a date string in YYYY-MM-DD format."""
    return date.fromisoformat(date_str)


def check_date_exact(envelope: Envelope, date_str: str) -> bool:
    """Check if envelope date matches exactly."""
    if envelope.date is None:
        return False
    target_date = _parse_date(date_str)
    return envelope.date == target_date


def check_date_from(envelope: Envelope, date_from_str: str) -> bool:
    """Check if envelope date is on or after the specified date."""
    if envelope.date is None:
        return False
    date_from = _parse_date(date_from_str)
    return envelope.date >= date_from


def check_date_to(envelope: Envelope, date_to_str: str) -> bool:
    """Check if envelope date is on or before the specified date."""
    if envelope.date is None:
        return False
    date_to = _parse_date(date_to_str)
    return envelope.date <= date_to


# ========================================================================
# PATTERN MATCHING (orchestrates all checks for expense patterns)
# ========================================================================

def match_expense_pattern(envelope: Envelope, pattern: dict) -> bool:
    """Check if envelope matches an expense categorization pattern.

    Evaluates conditions in order of computational cost:
    1. envelope_type (cheapest - simple boolean)
    2. date_from / date_to (cheap - simple comparison)
    3. account_type / is_credit_card
    4. amount
    5. outbound_account / inbound_account patterns
    6. text_contains / payee / narration (most expensive)

    Short-circuits on first failure.
    """
    # 1. Envelope type (direction check) - cheapest
    if 'envelope_type' in pattern:
        if not check_envelope_type(envelope, pattern['envelope_type']):
            return False

    # 2. Date constraints (cheap - simple comparison)
    if 'date' in pattern:
        if not check_date_exact(envelope, pattern['date']):
            return False

    if 'date_from' in pattern:
        if not check_date_from(envelope, pattern['date_from']):
            return False

    if 'date_to' in pattern:
        if not check_date_to(envelope, pattern['date_to']):
            return False

    # 3. Account type checks
    if 'account_type' in pattern:
        if not check_account_type(envelope, pattern['account_type']):
            return False

    if 'is_credit_card' in pattern:
        if pattern['is_credit_card'] != is_credit_card_envelope(envelope):
            return False

    # 4. Amount check
    if 'amount' in pattern:
        if not check_amount(envelope, pattern['amount']):
            return False

    # 5. Account pattern matching
    if 'outbound_account' in pattern:
        if not check_outbound_account_pattern(envelope, pattern['outbound_account']):
            return False

    if 'inbound_account' in pattern:
        if not check_inbound_account_pattern(envelope, pattern['inbound_account']):
            return False

    # 6. Text matching (most expensive - do last)
    if 'text_contains' in pattern:
        keywords = pattern['text_contains']
        if isinstance(keywords, str):
            keywords = [keywords]
        if not envelope_text_contains(envelope, keywords):
            return False

    if 'payee' in pattern:
        if not payee_matches(envelope, pattern['payee']):
            return False

    if 'narration' in pattern:
        if not narration_matches(envelope, pattern['narration']):
            return False

    # All conditions passed
    return True
