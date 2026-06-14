"""
Transaction and posting utility functions for analyzing Beancount data.

This module provides utilities for classifying Beancount transactions for
envelope creation, particularly for handling transactions with 3+ postings
through the additional_postings architecture.
"""

import logging
from typing import List, Optional, Tuple, Dict, Any, Set
from decimal import Decimal
from beancount.core.data import Posting, Transaction
from dataclasses import dataclass

from cassoulet.utils.currencies import is_currency, is_commodity

logger = logging.getLogger(__name__)


# ========================================================================
# INTERNAL HELPERS (Not exported but used by classification functions)
# ========================================================================

def _posting_is_currency(posting: Posting) -> bool:
    """Check if a posting uses a currency (not a commodity)."""
    if not posting.units:
        return False
    return is_currency(posting.units.currency)


def _posting_is_commodity(posting: Posting) -> bool:
    """Check if a posting uses a commodity (not a currency)."""
    if not posting.units:
        return False
    return is_commodity(posting.units.currency)


def _posting_type(posting: Posting) -> Optional[str]:
    """Get the currency/commodity type from a posting."""
    if not posting.units:
        return None
    return posting.units.currency


def _posting_amount(posting: Posting) -> Optional[Decimal]:
    """Get the amount from a posting."""
    if not posting.units:
        return None
    return posting.units.number


def _posting_is_positive(posting: Posting) -> bool:
    """Check if posting amount is positive (inflow)."""
    amount = _posting_amount(posting)
    return amount is not None and amount > 0


def _posting_is_negative(posting: Posting) -> bool:
    """Check if posting amount is negative (outflow)."""
    amount = _posting_amount(posting)
    return amount is not None and amount < 0


def _find_largest_cash_posting(
    postings: List[Posting],
    direction: str = 'both'
) -> Optional[Posting]:
    """Find the largest cash posting by absolute value."""
    cash_postings = [p for p in postings if _posting_is_currency(p)]

    if not cash_postings:
        return None

    if direction == 'positive':
        cash_postings = [p for p in cash_postings if _posting_is_positive(p)]
    elif direction == 'negative':
        cash_postings = [p for p in cash_postings if _posting_is_negative(p)]

    if not cash_postings:
        return None

    # Find largest by absolute value
    return max(cash_postings, key=lambda p: abs(_posting_amount(p) or 0))


# ========================================================================
# EXPORTED TRANSACTION ANALYSIS FUNCTIONS
# ========================================================================

def transaction_is_cash_only(transaction: Transaction) -> bool:
    """
    Check if a transaction involves only cash (no commodities).

    Args:
        transaction: Beancount transaction to analyze

    Returns:
        True if all postings are currency-only
    """
    postings_with_units = [p for p in transaction.postings if p.units]
    if not postings_with_units:
        return False
    return all(_posting_is_currency(p) for p in postings_with_units)


def get_transaction_commodities(transaction: Transaction) -> Set[str]:
    """
    Get all commodities involved in a transaction.

    Args:
        transaction: Beancount transaction to analyze

    Returns:
        Set of commodity symbols (excludes currencies)
    """
    commodities = set()
    for posting in transaction.postings:
        if _posting_is_commodity(posting):
            commodities.add(_posting_type(posting))
    return commodities


def separate_commodity_and_cash_postings(
    postings: List[Posting]
) -> Tuple[List[Posting], List[Posting]]:
    """
    Separate postings into commodity and cash groups.

    Args:
        postings: List of Beancount postings

    Returns:
        (commodity_postings, cash_postings)
    """
    commodity_postings = []
    cash_postings = []

    for posting in postings:
        if _posting_is_commodity(posting):
            commodity_postings.append(posting)
        elif _posting_is_currency(posting):
            cash_postings.append(posting)
        # Skip postings with no units (shouldn't happen but be defensive)

    return commodity_postings, cash_postings


def extract_commodity_movements(transaction: Transaction) -> Dict[str, List[Dict[str, Any]]]:
    """
    Extract commodity movements from a transaction's postings.

    Groups postings by commodity and identifies transfers between accounts.

    Args:
        transaction: Beancount transaction to analyze

    Returns:
        Dictionary with commodities as keys, containing lists of movements.
        Each movement contains:
        - account: The account involved
        - quantity: The amount (positive or negative)
        - direction: 'in' if positive, 'out' if negative
        - cost: Cost basis if present
        - price: Price if present
    """
    movements = {}

    for posting in transaction.postings:
        if posting.units:
            commodity = posting.units.currency
            quantity = posting.units.number

            if commodity not in movements:
                movements[commodity] = []

            movements[commodity].append({
                'account': posting.account,
                'quantity': quantity,
                'direction': 'in' if quantity > 0 else 'out',
                'cost': posting.cost,
                'price': posting.price
            })

    return movements


# ========================================================================
# POSTING CLASSIFICATION DATA STRUCTURE
# ========================================================================

@dataclass
class PostingClassification:
    """Result of classifying postings for envelope creation."""
    main_outbound: Optional[Posting]
    main_inbound: Optional[Posting]
    additional: List[Posting]
    classification_type: str  # 'commodity', 'cash', 'in-specie', 'unsupported'
    error_message: Optional[str] = None


# ========================================================================
# CLASSIFICATION HELPER FUNCTIONS
# ========================================================================

def classify_commodity_transaction(
    commodity_postings: List[Posting],
    cash_postings: List[Posting],
    all_postings: List[Posting]
) -> PostingClassification:
    """
    Classify a transaction involving a single commodity.

    Handles BUY, SELL, and in-specie transfers.

    Args:
        commodity_postings: List of commodity postings
        cash_postings: List of cash postings
        all_postings: All postings in the transaction

    Returns:
        PostingClassification with main flow and additional postings
    """
    # Helper to create error response
    def error_response(msg: str) -> PostingClassification:
        return PostingClassification(
            main_outbound=None,
            main_inbound=None,
            additional=[],
            classification_type='unsupported',
            error_message=msg
        )

    # Validation: must have commodity postings
    if not commodity_postings:
        return error_response("No commodity postings found")

    # Validation: all commodity postings must be same type
    commodity_types = {_posting_type(p) for p in commodity_postings}
    if len(commodity_types) > 1:
        return error_response(f"Multiple commodity types found: {commodity_types}")

    # Separate commodity postings by direction
    commodity_in = [p for p in commodity_postings if _posting_is_positive(p)]
    commodity_out = [p for p in commodity_postings if _posting_is_negative(p)]

    # Case 1: In-specie transfer (no cash involved)
    if not cash_postings:
        # Must have exactly one in and one out
        if len(commodity_in) != 1 or len(commodity_out) != 1:
            return error_response(
                f"In-specie transfer must have 1 inbound and 1 outbound, "
                f"found {len(commodity_in)} in and {len(commodity_out)} out"
            )

        return PostingClassification(
            main_outbound=commodity_out[0],
            main_inbound=commodity_in[0],
            additional=[],
            classification_type='in-specie'
        )

    # Case 2: BUY transaction (commodity in, cash out)
    if commodity_in and not commodity_out:
        main_cash_out = _find_largest_cash_posting(cash_postings, direction='negative')
        if not main_cash_out:
            return error_response("BUY transaction missing cash payment")

        # Use the first (should be only) commodity inflow
        main_commodity_in = commodity_in[0]

        # Everything else is additional
        additional = [p for p in all_postings
                     if p != main_commodity_in and p != main_cash_out]

        return PostingClassification(
            main_outbound=main_cash_out,
            main_inbound=main_commodity_in,
            additional=additional,
            classification_type='commodity'
        )

    # Case 3: SELL transaction (commodity out, cash in)
    if commodity_out and not commodity_in:
        main_cash_in = _find_largest_cash_posting(cash_postings, direction='positive')
        if not main_cash_in:
            return error_response("SELL transaction missing cash proceeds")

        # Use the first (should be only) commodity outflow
        main_commodity_out = commodity_out[0]

        # Everything else is additional
        additional = [p for p in all_postings
                     if p != main_commodity_out and p != main_cash_in]

        return PostingClassification(
            main_outbound=main_commodity_out,
            main_inbound=main_cash_in,
            additional=additional,
            classification_type='commodity'
        )

    # Case 4: Mixed commodity directions (shouldn't happen in normal transactions)
    return error_response(
        f"Ambiguous commodity transaction: {len(commodity_in)} inflows and "
        f"{len(commodity_out)} outflows with cash involved"
    )


def classify_cash_only_transaction(postings: List[Posting]) -> PostingClassification:
    """
    Classify a cash-only transaction.

    Uses largest negative as outbound, largest positive as inbound.

    Args:
        postings: List of postings to classify

    Returns:
        PostingClassification with main flow and additional postings
    """
    negative_postings = [p for p in postings if _posting_is_negative(p)]
    positive_postings = [p for p in postings if _posting_is_positive(p)]

    if not negative_postings or not positive_postings:
        return PostingClassification(
            main_outbound=None,
            main_inbound=None,
            additional=[],
            classification_type='unsupported',
            error_message="Cash transaction must have both positive and negative postings"
        )

    # Find largest by absolute value
    main_outbound = max(negative_postings, key=lambda p: abs(_posting_amount(p) or 0))
    main_inbound = max(positive_postings, key=lambda p: abs(_posting_amount(p) or 0))

    # Everything else is additional
    additional = [p for p in postings
                 if p != main_outbound and p != main_inbound]

    return PostingClassification(
        main_outbound=main_outbound,
        main_inbound=main_inbound,
        additional=additional,
        classification_type='cash'
    )
