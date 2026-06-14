"""
Transfer Scoring Patterns Configuration

This file defines patterns for scoring potential transfer matches between envelopes.
Patterns are evaluated to generate scores for envelope pairs during transfer detection.

IMPORTANT DESIGN PHILOSOPHY:
1. Patterns are designed with non-overlapping ranges to avoid unintended compounding
2. We use explicit ranges (e.g., '2-3', '4-5') not overlapping conditions ('<=3', '<=5')
3. If patterns overlap, scores WILL aggregate - this is intentional
4. Higher scores indicate stronger match confidence
5. Negative scores indicate anti-patterns or suspicious activity

Pattern Design Guidelines:
- Date patterns: Use non-overlapping ranges for clean scoring
- Amount patterns: Use explicit ranges to control aggregation
- Type patterns: Can compound when meaningful (e.g., SIPP + same institution)
- Anti-patterns: Always compound (multiple red flags = lower total score)

If you want scores to aggregate, create overlapping patterns.
If you want exclusive matching, use non-overlapping ranges.
"""

TRANSFER_SCORING_PATTERNS = {
    # ========================================================================
    # Manual Overrides (Highest Priority)
    # ========================================================================

    'manual_skip_transfer_detection': {
        'name': 'Skip Transfer Detection',
        'reason': 'Manual envelope has skip-transfer-detection flag',
        'patterns': [
            {
                'either_has_skip_transfer_detection': True,
            }
        ],
        'metadata': {
            'score': -1000,  # Absolute block - opt out of transfer matching
        }
    },

    'override_directive': {
        'name': 'Override Directive',
        'reason': 'Manual envelope has override-target directive',
        'patterns': [
            {
                'has_override_directive': True,
            }
        ],
        'metadata': {
            'score': 300,  # Highest priority - explicit override
        }
    },

    'match_directive': {
        'name': 'Match Directive',
        'reason': 'Manual envelope has match-target directive',
        'patterns': [
            {
                'has_explicit_match_directive': True,
            }
        ],
        'metadata': {
            'score': 200,  # High priority - explicit match request
        }
    },

    # ========================================================================
    # Anti-Patterns (Kill Bad Matches)
    # ========================================================================

    'amounts_incompatible': {
        'name': 'Amounts Incompatible',
        'reason': 'Amount difference fails thresholds for account type',
        'patterns': [
            {
                'amount_match_quality': '0-39',
            }
        ],
        'metadata': {
            'score': -500,  # Kill obviously bad matches
        }
    },

    # Anti-Patterns: Date Gap Penalties
    # These use date_gap() which returns absolute difference (always positive)
    # For signed/directional gaps, use transfer_delay()
    #
    # NOTE: Different penalties for cash vs commodity (in-specie) transfers:
    # - Cash transfers are fast (0-4 days typical, >10 days is suspicious)
    # - Commodity transfers are slow (5-30 days normal for broker-to-broker)

    # CASH TRANSFER DATE PENALTIES (strict)
    'excessive_date_gap_cash': {
        'name': 'Excessive Date Gap (Cash)',
        'reason': 'Cash transfer >10 days apart (too slow)',
        'patterns': [
            {
                'date_gap': '>10',
                'is_potential_in_specie_transfer': False,  # Only apply to cash
            }
        ],
        'metadata': {
            'score': -1000,  # Kill - too slow for cash transfers
        }
    },

    'large_date_gap_cash': {
        'name': 'Large Date Gap (Cash)',
        'reason': 'Cash transfer 5-10 days apart (suspicious)',
        'patterns': [
            {
                'date_gap': '>4',
                'is_potential_in_specie_transfer': False,  # Only apply to cash
            }
        ],
        'metadata': {
            'score': -200,  # Heavy penalty - suspicious for modern banking
        }
    },

    'moderate_date_gap_cash': {
        'name': 'Moderate Date Gap (Cash)',
        'reason': 'Cash transfer 4 days apart',
        'patterns': [
            {
                'date_gap': 4,
                'is_potential_in_specie_transfer': False,  # Only apply to cash
            }
        ],
        'metadata': {
            'score': -80,  # Moderate penalty - unusual but possible
        }
    },

    # IN-SPECIE TRANSFER DATE PENALTIES (relaxed)
    'excessive_date_gap_commodity': {
        'name': 'Excessive Date Gap (Commodity)',
        'reason': 'In-specie transfer >30 days apart (too slow)',
        'patterns': [
            {
                'date_gap': '>30',
                'is_potential_in_specie_transfer': True,  # Only apply to commodities
            }
        ],
        'metadata': {
            'score': -800,  # Kill - even in-specie shouldn't take this long
        }
    },

    'large_date_gap_commodity': {
        'name': 'Large Date Gap (Commodity)',
        'reason': 'In-specie transfer 20-30 days apart',
        'patterns': [
            {
                'date_gap': '>19',
                'is_potential_in_specie_transfer': True,
            }
        ],
        'metadata': {
            'score': -100,  # Penalty but not fatal - slow but realistic
        }
    },

    'moderate_date_gap_commodity': {
        'name': 'Moderate Date Gap (Commodity)',
        'reason': 'In-specie transfer 10-20 days apart',
        'patterns': [
            {
                'date_gap': '>9',
                'is_potential_in_specie_transfer': True,
            }
        ],
        'metadata': {
            'score': -30,  # Light penalty - normal for in-specie
        }
    },

    'typical_date_gap_commodity': {
        'name': 'Typical Date Gap (Commodity)',
        'reason': 'In-specie transfer 5-10 days apart',
        'patterns': [
            {
                'date_gap': '>4',
                'is_potential_in_specie_transfer': True,
            }
        ],
        'metadata': {
            'score': -10,  # Minimal penalty - very common for in-specie
        }
    },

    # ========================================================================
    # Date Proximity Patterns (Mutually Exclusive)
    # ========================================================================

    'same_day_transfer': {
        'name': 'Same Day Transfer',
        'reason': 'Transactions on the same day',
        'patterns': [
            {
                'transfer_delay': 0,
            }
        ],
        'metadata': {
            'score': 60,
        }
    },

    'next_day_transfer': {
        'name': 'Next Day Transfer',
        'reason': 'Next day settlement',
        'patterns': [
            {
                'transfer_delay': 1,
            }
        ],
        'metadata': {
            'score': 50,
        }
    },

    'few_days_transfer': {
        'name': 'Few Days Transfer',
        'reason': '2-3 day settlement',
        'patterns': [
            {
                'transfer_delay': '2-3',
            }
        ],
        'metadata': {
            'score': 40,
        }
    },

    'business_week_transfer': {
        'name': 'Business Week Transfer',
        'reason': '4 day settlement',
        'patterns': [
            {
                'transfer_delay': 4,
            }
        ],
        'metadata': {
            'score': 30,
        }
    },

    # ========================================================================
    # Amount Quality Patterns (Graduated Scoring)
    # ========================================================================
    # Uses amount_match_quality function which is account-type aware:
    # - Bank accounts: Strict matching (< £0.50)
    # - Broker accounts: Tolerant matching (< £10 AND < 5%)
    # ========================================================================

    'amounts_excellent': {
        'name': 'Amounts Excellent',
        'reason': 'Amount quality 80-100 (near-perfect match)',
        'patterns': [
            {
                'amount_match_quality': '80-100',
            }
        ],
        'metadata': {
            'score': 100,
        }
    },

    'amounts_good': {
        'name': 'Amounts Good',
        'reason': 'Amount quality 60-79 (acceptable variance)',
        'patterns': [
            {
                'amount_match_quality': '60-79',
            }
        ],
        'metadata': {
            'score': 60,
        }
    },

    'amounts_acceptable': {
        'name': 'Amounts Acceptable',
        'reason': 'Amount quality 40-59 (loose match)',
        'patterns': [
            {
                'amount_match_quality': '40-59',
            }
        ],
        'metadata': {
            'score': 40,
        }
    },

    # ========================================================================
    # Legacy Amount Matching Patterns (For Transfer Delta)
    # ========================================================================
    # These patterns use transfer_delta and transfer_difference for
    # additional context when amount_match_quality doesn't apply
    # ========================================================================

    'exact_amount_match': {
        'name': 'Exact Amount Match',
        'reason': 'Amounts match exactly',
        'patterns': [
            {
                'transfer_delta': 0,
            }
        ],
        'metadata': {
            'score': 80,
        }
    },

    'within_penny': {
        'name': 'Within Penny',
        'reason': 'Amounts within a penny',
        'patterns': [
            {
                'transfer_difference': '0.001-0.01',
            }
        ],
        'metadata': {
            'score': 75,
        }
    },

    'small_fee': {
        'name': 'Small Fee',
        'reason': 'Small transfer fee detected',
        'patterns': [
            {
                'transfer_loss': '0.01-5',
                'transfer_loss_percent': '0-0.5',
            }
        ],
        'metadata': {
            'score': 70,
        }
    },

    'moderate_percentage_fee': {
        'name': 'Moderate Percentage Fee',
        'reason': 'Moderate fee as percentage',
        'patterns': [
            {
                'transfer_loss_percent': '0.5-2',
            }
        ],
        'metadata': {
            'score': 50,
        }
    },

    'higher_percentage_fee': {
        'name': 'Higher Percentage Fee',
        'reason': 'Higher but reasonable fee',
        'patterns': [
            {
                'transfer_loss_percent': '2-5',
            }
        ],
        'metadata': {
            'score': 30,
        }
    },

    'high_percentage_fee': {
        'name': 'High Percentage Fee',
        'reason': 'High fee but still possible',
        'patterns': [
            {
                'transfer_loss_percent': '5-10',
            }
        ],
        'metadata': {
            'score': 10,
        }
    },

    # ========================================================================
    # Reconciliation vs Transfer Patterns
    # These patterns distinguish between reconciliation (same source, different views)
    # and transfers (actual movement between accounts)
    # ========================================================================

    # Note: These patterns are CONTEXT-DEPENDENT and are scored differently
    # based on source types (CSV-CSV, manual-CSV, etc.) - see source type patterns below

    # ========================================================================
    # Transfer Type Patterns
    # ========================================================================

    'bank_to_broker_transfer': {
        'name': 'Bank to Broker Transfer',
        'reason': 'Common funding pattern',
        'patterns': [
            {
                'one_account_is_bank_one_is_broker': True,
                'is_compatible_pair': True,
                'both_are_currency_only': True,
            }
        ],
        'metadata': {
            'score': 40,
        }
    },


    'commodity_transfer': {
        'name': 'Commodity Transfer',
        'reason': 'In-specie transfer pattern',
        'patterns': [
            {
                'is_compatible_pair': True,
                'both_are_commodity_only': True,
            }
        ],
        'metadata': {
            'score': 60,  # High score for commodity transfers
        }
    },

    # ========================================================================
    # Source Type Patterns (Context-Dependent Scoring)
    # ========================================================================

    # CSV-CSV Patterns (always transfers, never reconciliation)
    'csv_to_csv_transfer': {
        'name': 'CSV to CSV Transfer',
        'reason': 'Both from automated sources, compatible for transfer',
        'patterns': [
            {
                'both_sources_csv': True,
                'is_compatible_pair': True,
                'have_different_accounts': True,  # Transfers need different accounts
            }
        ],
        'metadata': {
            'score': 40,  # Good transfer pattern for CSV-CSV
        }
    },

    'csv_to_csv_same_account_incompatible': {
        'name': 'CSV Same Account Incompatible',
        'reason': 'CSV sources, same account, not compatible (suspicious)',
        'patterns': [
            {
                'both_sources_csv': True,
                'is_incompatible_pair': True,
                'have_same_account': True,  # Same account = suspicious
            }
        ],
        'metadata': {
            'score': -50,  # Penalty - doesn't make sense
        }
    },


    # ========================================================================
    # Trade Subset Patterns (Complete trade vs partial leg)
    # ========================================================================

    'trade_subset_pattern': {
        'name': 'Trade Subset Match',
        'reason': 'Complete trade matched with partial leg',
        'patterns': [
            {
                'is_compatible_pair': True,
                'is_subset_relationship': True,  # One complete, one partial
                # CRITICAL: Only match if at least one is manual
                # This allows manual reconciliation but blocks spurious CSV-CSV matches
                'one_source_csv_one_manual': True,  # At least one must be manual
            }
        ],
        'metadata': {
            'score': 25,  # Score for manual reconciliation attempts
        }
    },

    # ========================================================================
    # Reconciliation Patterns (Exact Matching Required)
    # ========================================================================
    # Reconciliation = two views of SAME transaction (manual + CSV)
    # Requires EXACT matches: same date, exact amounts, exact units, shared account
    # ========================================================================

    'valid_reconciliation_match': {
        'name': 'Valid Reconciliation Match',
        'reason': 'Manual transaction reconciling with CSV import (exact match required)',
        'patterns': [
            {
                'can_reconcile': True,       # Manual + CSV, shared account, type overlap, valid structure
                'units_match': True,         # EXACT numeric match
            }
        ],
        'metadata': {
            'score': 100,  # Strong positive for valid reconciliation
        }
    },

    # ========================================================================
    # Anti-Patterns (Negative Scores)
    # ========================================================================

    'invalid_aggregation_manual_with_csv_no_shared_account': {
        'name': 'Invalid Aggregation',
        'reason': 'Manual + CSV from different accounts (cannot aggregate)',
        'patterns': [
            {
                'opposite_envelope_source_types': True,  # One manual, one CSV
                'shared_flow_account': False,            # Different accounts
            }
        ],
        'metadata': {
            'score': -500  # Massive penalty - structural mismatch
        }
    },

    'same_account_same_type': {
        'name': 'Same Account Same Type',
        'reason': 'Same account and type (impossible self-transfer)',
        'patterns': [
            {
                'have_same_account': True,       # Same account
                'have_same_primary_type': True,  # Same type
            }
        ],
        'metadata': {
            'score': -500,  # Massive penalty - impossible transfer
        }
    },

    'credit_card_broker_invalid': {
        'name': 'Credit Card to Broker Invalid',
        'reason': 'Credit card and broker accounts cannot transfer directly',
        'patterns': [
            {
                'one_account_is_credit_card_one_is_broker': True,
            }
        ],
        'metadata': {
            # Kill this match - there's no valid scenario where a credit card
            # pays into a SIPP/ISA or vice versa. These are always false positives
            # from coincidental amounts (e.g. £12 Meta ad vs £12.04 custody fee).
            # If a real transfer exists, it must be explicitly specified via
            # manual entry with override directive.
            'score': -1000,
        }
    },

    'same_account_aggregation_invalid': {
        'name': 'Same Account Aggregation Invalid',
        'reason': 'Same account, can aggregate but cannot reconcile (impossible transfer)',
        'patterns': [
            {
                'have_same_account': True,    # Same account
                'can_aggregate': True,        # Aggregating (CSV-CSV or similar)
                'can_reconcile': False,       # NOT reconciling (not manual-CSV)
            }
        ],
        'metadata': {
            'score': -1000,  # Massive penalty - structurally impossible
        }
    },

    'suspicious_gain': {
        'name': 'Suspicious Gain',
        'reason': 'Received more than sent',
        'patterns': [
            {
                'has_transfer_gain': True,
            }
        ],
        'metadata': {
            'score': -100,  # Strong penalty
        }
    },


    'excessive_fee': {
        'name': 'Excessive Fee',
        'reason': 'Fee exceeds 10%',
        'patterns': [
            {
                'transfer_loss_percent': '>10',
            }
        ],
        'metadata': {
            'score': -50,  # Penalty for unrealistic fees
        }
    },


    'too_far_apart': {
        'name': 'Too Far Apart',
        'reason': 'More than 30 days apart',
        'patterns': [
            {
                'transfer_delay': '>30',
            }
        ],
        'metadata': {
            'score': -20,  # Unlikely to be related
        }
    },

}