"""
CSV Utilities - Clean Architecture

This module contains utility functions for CSV processing:
- Type-aware value parsing (amounts, dates, strings)
- Semantic field detection
- Date format detection
"""

import logging
from decimal import Decimal, InvalidOperation
from typing import Optional

# Import cleaning functions from cleaner module
from cassoulet.utils.cleaner import (
    normalize_string,
)

logger = logging.getLogger(__name__)

# Header patterns for semantic detection
# IMPORTANT: More specific patterns MUST come before generic ones
# Otherwise "Settle date" will match generic 'date' instead of 'settlement_date'
HEADER_PATTERNS = {
    'settlement_date': [
        'settlement date', 'settle date', 'settled', 'cleared date'
    ],
    'date': [
        'date', 'transaction date', 'trans date', 'txn date', 'posted date',
        'posting date', 'value date', 'trade date', 'effective date'
    ],
    'narrative': [
        'description', 'desc', 'transaction description', 'trans desc',
        'details', 'narrative', 'narration', 'particular', 'particulars',
        'memo', 'narrative text', 'transaction details'
    ],
    'payee': [
        'payee', 'merchant', 'vendor', 'paid to', 'received from', 'counterparty'
    ],
    'amount': [
        'amount', 'value', 'transaction amount', 'trans amount', 'txn amount',
        'net amount', 'total'
    ],
    'debit': [
        'debit', 'withdrawal', 'paid out', 'paidout', 'out',
        'money out', 'outgoing', 'expense'
    ],
    'credit': [
        'credit', 'deposit', 'paid in', 'paidin', 'in',
        'money in', 'incoming', 'income'
    ],
    'payment': [
        'payment'
    ],
    'receipt': [
        'receipt'
    ],
    'balance': [
        'balance', 'running balance', 'current balance', 'closing balance',
        'available balance', 'ledger balance'
    ],
    'type': [
        'type', 'transaction', 'transaction type', 'trans type', 'txn type', 'transaction_type'
    ],
    'reference': [
        'reference', 'ref', 'reference number', 'transaction id', 'trans id',
        'txn id', 'id', 'transaction_id', 'cheque number'
    ],
    'currency': [
        'currency', 'ccy', 'curr'
    ],
    'commodity': [
        'symbol', 'ticker', 'stock', 'security', 'instrument', 'commodity'
    ],
    'quantity': [
        'quantity', 'qty', 'units', 'shares', 'amount'
    ],
    'price': [
        'price', 'unit cost', 'unit price', 'cost', 'rate'
    ],
    'category': [
        'category', 'spending category', 'expense category', 'class'
    ],
    'notes': [
        'notes', 'memo', 'comment', 'remarks'
    ]
}


def _squash(text: str) -> str:
    """Reduce a header or pattern to comparable form: alphanumerics only.

    Banks write the same field a dozen ways - "Counterparty", "Counter Party",
    "Counter-Party", "COUNTER_PARTY" - and only the spelling differs, never the
    meaning. Comparing on alphanumerics alone makes the separator irrelevant so
    HEADER_PATTERNS does not have to enumerate every punctuation variant. The
    list still carries hand-written squashed entries ('paidout' beside
    'paid out') from before this existed; they are harmless duplicates now.
    """
    return ''.join(c for c in normalize_string(text) if c.isalnum())


def detect_semantic_field(header: str) -> Optional[str]:
    """
    Detect the semantic field type for a header.

    Args:
        header: Column header

    Returns:
        Semantic field name or None
    """
    normalized = normalize_string(header)
    squashed = _squash(header)

    for field, patterns in HEADER_PATTERNS.items():
        for pattern in patterns:
            pattern_normalized = normalize_string(pattern)
            pattern_squashed = _squash(pattern)
            # Exact match, separator-insensitive
            if normalized == pattern_normalized or squashed == pattern_squashed:
                return field
            # Contained match for meaningful patterns
            if len(pattern_normalized) > 3:
                if pattern_normalized in normalized:
                    return field
            if len(pattern_squashed) > 3:
                if pattern_squashed in squashed:
                    return field

    return None


