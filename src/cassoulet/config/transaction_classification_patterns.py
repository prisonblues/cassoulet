"""
Transaction classification patterns.

These patterns classify transactions based on envelope structure using
the existing envelope utility functions.
"""

# Pattern definitions for transaction classification
# Utility functions are provided by envelope_utilities module


TRANSACTION_CLASSIFICATION_PATTERNS = {
    # ========================================================================
    # BUY - Cash out, commodity in
    # ========================================================================
    'buy_transaction': {
        'name': 'Stock/Fund Purchase',
        'reason': 'Cash going out, commodity coming in',
        'patterns': [
            {
                # Must have both legs (complete transaction)
                'has_both_legs': True,
                # Has both cash and commodity
                'has_commodity': True,
                'envelope_has_mixed_types': True,
                # Cash is going out (negative units)
                'get_cash_amount': 'negative',
            }
        ],
        'metadata': {
            'transaction_type': 'BUY'
        }
    },

    # ========================================================================
    # SELL - Commodity out, cash in
    # ========================================================================
    'sell_transaction': {
        'name': 'Stock/Fund Sale',
        'reason': 'Commodity going out, cash coming in',
        'patterns': [
            {
                # Must have both legs (complete transaction)
                'has_both_legs': True,
                # Has both cash and commodity
                'has_commodity': True,
                'envelope_has_mixed_types': True,
                # Cash is coming in (positive units)
                'get_cash_amount': 'positive',
            }
        ],
        'metadata': {
            'transaction_type': 'SELL'
        }
    },

    # ========================================================================
    # TRANSFER - Same currency on both sides
    # ========================================================================
    'cash_transfer': {
        'name': 'Cash Transfer',
        'reason': 'Same currency on both sides',
        'patterns': [
            {
                # Must have both legs
                'has_both_legs': True,
                # Only has currency (no commodities)
                'is_cash_only': True,
            }
        ],
        'metadata': {
            'transaction_type': 'TRANSFER'
        }
    },

    # ========================================================================
    # COMMODITY_TRANSFER - Same commodity on both sides
    # ========================================================================
    'commodity_transfer': {
        'name': 'In-Specie Transfer',
        'reason': 'Same commodity on both sides',
        'patterns': [
            {
                # Must have both legs
                'has_both_legs': True,
                # Has a commodity
                'has_commodity': True,
                # Only commodity, no cash
                'envelope_is_commodity_only': True,
            }
        ],
        'metadata': {
            'transaction_type': 'COMMODITY_TRANSFER'
        }
    },

    # ========================================================================
    # DIVIDEND - Cash income with dividend indicators
    # ========================================================================
    'dividend_income': {
        'name': 'Dividend Income',
        'reason': 'Cash inbound with dividend narration',
        'patterns': [
            {
                # Single leg inbound
                'is_single_leg': True,
                'is_cash_only': True,
                # Cash coming in
                'get_cash_amount': 'positive',
                # Note: Narration-based classification would require manual check
            }
        ],
        'metadata': {
            'transaction_type': 'DIVIDEND'
        }
    },

    # ========================================================================
    # INTEREST - Cash income
    # ========================================================================
    'interest_income': {
        'name': 'Interest Income',
        'reason': 'Cash inbound',
        'patterns': [
            {
                # Single leg inbound
                'is_single_leg': True,
                'is_cash_only': True,
                # Cash coming in
                'get_cash_amount': 'positive',
                # Priority after dividend
            }
        ],
        'metadata': {
            'transaction_type': 'INTEREST',
            'priority': 2  # Lower priority than dividend
        }
    },

    # ========================================================================
    # FEE - Cash outbound
    # ========================================================================
    'investment_fee': {
        'name': 'Investment Fee',
        'reason': 'Cash outbound',
        'patterns': [
            {
                # Single leg outbound
                'is_single_leg': True,
                'is_cash_only': True,
                # Cash going out
                'get_cash_amount': 'negative',
                # Could be fee or expense
            }
        ],
        'metadata': {
            'transaction_type': 'FEE',
            'priority': 1  # Higher priority than generic expense
        }
    },

    # ========================================================================
    # INCOME - Generic cash inbound (fallback)
    # ========================================================================
    'generic_income': {
        'name': 'Income',
        'reason': 'Cash inbound without specific classification',
        'patterns': [
            {
                # Single leg inbound
                'is_single_leg': True,
                'is_cash_only': True,
                'get_cash_amount': 'positive',
            }
        ],
        'metadata': {
            'transaction_type': 'INCOME',
            'priority': 99  # Lowest priority fallback
        }
    },

    # ========================================================================
    # EXPENSE - Generic cash outbound (fallback)
    # ========================================================================
    'generic_expense': {
        'name': 'Expense',
        'reason': 'Cash outbound without specific classification',
        'patterns': [
            {
                # Single leg outbound
                'is_single_leg': True,
                'is_cash_only': True,
                'get_cash_amount': 'negative',
            }
        ],
        'metadata': {
            'transaction_type': 'EXPENSE',
            'priority': 99  # Lowest priority fallback
        }
    },
}