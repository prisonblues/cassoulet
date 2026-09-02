"""
No-match patterns for CSV import stage.

These patterns identify transactions that should NEVER be matched as transfers
between accounts. Applied at CSV import to set transfer-eligible: False flags.

Pattern matching rules:
- ALL fields in a pattern must match (AND logic)
- If a field has multiple values, ANY can match (OR logic)
- If ANY pattern in the patterns array matches, metadata is applied
"""

NO_MATCH_PATTERNS = {
    'sipp_tax_relief': {
        'name': 'SIPP Tax Relief',
        'reason': 'Government tax relief on pension contributions, not a transfer between your accounts',
        'patterns': [
            # HMRC tax relief payments
            {'narration': 'SIPP CONTRIBUTION CLAIM', 'account': '*SIPP*'},
            {'narration': 'contribution claim', 'account': '*SIPP*'},
            {'narration': 'tax relief', 'account': '*SIPP*', 'amount': 'positive'},
            {'narration': 'HMRC*', 'account': '*SIPP*', 'amount': 'positive'},
        ],
        'metadata': {
            'transfer-eligible': False,
            'dont-match-reason': 'HMRC tax relief payment',
            'transaction-type-hint': 'TAX_RELIEF'
        }
    },
    
    'government_benefits': {
        'name': 'Government Benefits',
        'reason': 'Government benefit payments, not transfers between your accounts',
        'patterns': [
            {'narration': '*DWP*'},
            {'narration': '*state pension*'},
            {'narration': '*child benefit*'},
            {'narration': '*universal credit*'},
            {'narration': '*tax credit*'},
            {'payee': 'DWP'},
            {'payee': 'HMRC', 'amount': 'positive'},  # Benefits from HMRC
        ],
        'metadata': {
            'transfer-eligible': False,
            'dont-match-reason': 'Government benefit payment',
            'transaction-type-hint': 'INCOME_OTHER'
        }
    },
    
    'employer_contribution': {
        'name': 'Employer Pension Contributions',
        'reason': 'Employer pension contributions, not transfers from your own accounts',
        'patterns': [
            {'narration': '*employer contribution*', 'account': '*SIPP*'},
            {'narration': '*employer contribution*', 'account': '*Pension*'},
            {'narration': '*company match*', 'account': '*SIPP*'},
            {'narration': '*company pension*', 'account': '*Pension*'},
            {'narration': 'ER CONTRBN*', 'account': '*SIPP*'},  # Common abbreviation
        ],
        'metadata': {
            'transfer-eligible': False,
            'dont-match-reason': 'Employer pension contribution',
            'transaction-type-hint': 'EMPLOYER_CONTRIBUTION'
        }
    },
    
    'salary_income': {
        'name': 'Salary and Wages',
        'reason': 'Employment income, not a transfer between your accounts',
        'patterns': [
            {'narration': '*salary*'},
            {'narration': '*wages*'},
            {'narration': '*payroll*'},
            {'narration': 'BACS*', 'amount': 'positive'},  # Many salaries come via BACS
        ],
        'metadata': {
            'transfer-eligible': False,
            'dont-match-reason': 'Salary/wage payment',
            'transaction-type-hint': 'INCOME_SALARY'
        }
    },
    
    'dividend_income': {
        'name': 'Dividend Income',
        'reason': 'Investment dividends, not transfers between accounts',
        'patterns': [
            {'narration': '*dividend*'},
            {'narration': '*DIV*', 'account': '*Broker*'},
            {'narration': '*distribution*', 'account': '*Broker*'},
        ],
        'metadata': {
            'transfer-eligible': False,
            'dont-match-reason': 'Dividend payment',
            'transaction-type-hint': 'DIVIDEND'
        }
    },
    
    'interest_income': {
        'name': 'Interest Income',
        'reason': 'Interest earned, not a transfer between accounts',
        'patterns': [
            {'narration': '*interest*', 'amount': 'positive'},
            {'narration': '*credit interest*'},
            {'narration': 'INT PAID*'},
            # Starling puts interest description in Reference field
            {'reference': '*interest*', 'amount': 'positive'},
        ],
        'metadata': {
            'transfer-eligible': False,
            'dont-match-reason': 'Interest payment',
            'transaction-type-hint': 'INTEREST'
        }
    },
    
    'hl_internal_reallocation': {
        'name': 'HL Internal Income Reallocation',
        'reason': 'Internal HL accounting entry to move income into main cash balance, not a real transfer',
        'patterns': [
            {'narration': 'transfer from income account', 'account': '*HL*'},
            {'narration': 'income account transfer', 'account': '*HL*'},
        ],
        'metadata': {
            'transfer-eligible': False,
            'dont-match-reason': 'Internal HL reallocation',
            'internal-entry': True
        }
    },
    
    'ajbell_internal': {
        'name': 'AJ Bell Internal Movements',
        'reason': 'Internal AJ Bell accounting entries, not real transfers',
        'patterns': [
            {'narration': 'Transfer from SIPP Cash Account', 'account': '*AJBELL*'},
            {'narration': 'Transfer to SIPP Cash Account', 'account': '*AJBELL*'},
        ],
        'metadata': {
            'transfer-eligible': False,
            'dont-match-reason': 'Internal AJ Bell movement',
            'internal-entry': True
        }
    },
    
    'fee_payments': {
        'name': 'Fee Payments',
        'reason': 'Service fees and charges, not transfers',
        'patterns': [
            {'narration': '*fee*', 'amount': 'negative'},
            {'narration': '*charge*', 'amount': 'negative'},
            {'narration': '*commission*', 'amount': 'negative'},
            {'narration': 'platform fee*', 'amount': 'negative'},
            {'narration': 'management fee*', 'amount': 'negative'},
        ],
        'metadata': {
            'transfer-eligible': False,
            'dont-match-reason': 'Fee payment',
            'transaction-type-hint': 'FEE'
        }
    },
    
    'card_payments': {
        'name': 'Card Payments',
        'reason': 'Debit/credit card payments to merchants, not transfers',
        'patterns': [
            {'narration': 'CARD PAYMENT*'},
            {'narration': 'POS*'},  # Point of sale
            {'narration': 'CONTACTLESS*'},
            {'narration': 'VISA DEBIT*'},
            {'narration': 'MASTERCARD*'},
        ],
        'metadata': {
            'transfer-eligible': False,
            'dont-match-reason': 'Card payment to merchant',
            'transaction-type-hint': 'EXPENSE'
        }
    },
    
    # KNOWN DEFECT, repo-wide: nothing in this file currently takes effect.
    # Every rule here writes 'transfer-eligible' (hyphen) into envelope
    # metadata, and apply_patterns_to_envelopes merges the key verbatim, but
    # is_transfer_eligible() reads 'is_transfer_eligible'. The keys never meet,
    # so direct debits, merchant card payments, fees, ATM withdrawals, salary
    # and dividends are all still offered to the transfer scorer. Fixing the
    # key will switch roughly ten dormant exclusions on at once - including one
    # that would catch the BANK leg of a card bill collected by direct debit
    # ("DIRECT DEBIT AMEX"), which this rule does not cover. Do that as its own
    # change, with both legs handled together.
    #
    # ORDER MATTERS: first match wins, so this sits ABOVE 'direct_debits'.
    # A card bill is very often collected BY direct debit, so the card leg reads
    # "DIRECT DEBIT PAYMENT - THANK YOU" and the rule below would mark it
    # transfer-ineligible - killing the match before scoring ever sees it. Paying
    # your own card IS a transfer between your own accounts; the expense was
    # recorded when each purchase hit the card. Only inbound legs qualify:
    # outbound from a liability is spending, which is_transfer_eligible already
    # rejects.
    'credit_card_bill_payment': {
        'name': 'Credit Card Bill Payment',
        'reason': 'Paying your own card is a transfer, even when collected by direct debit',
        'patterns': [
            {'envelope_type': 'inbound_only', 'narration': '*PAYMENT - THANK YOU*'},
            {'envelope_type': 'inbound_only', 'narration': '*CARD PYMT*'},
        ],
        'metadata': {
            'transfer-eligible': True,
            'transaction-type-hint': 'TRANSFER'
        }
    },

    'direct_debits': {
        'name': 'Direct Debits',
        'reason': 'Regular bill payments, not transfers between your accounts',
        'patterns': [
            {'narration': 'DD *'},  # Direct Debit prefix
            {'narration': 'DIRECT DEBIT*'},
            {'narration': '*COUNCIL TAX*'},
            {'narration': '*INSURANCE*'},
            {'narration': '*UTILITIES*'},
            {'narration': '*MORTGAGE*'},
        ],
        'metadata': {
            'transfer-eligible': False,
            'dont-match-reason': 'Direct debit payment',
            'transaction-type-hint': 'EXPENSE'
        }
    },
    
    'atm_withdrawals': {
        'name': 'ATM Withdrawals',
        'reason': 'Cash withdrawals, not transfers to another account',
        'patterns': [
            {'narration': 'ATM *'},
            {'narration': 'CASH WITHDRAWAL*'},
            {'narration': 'CASH MACHINE*'},
            {'narration': '*ATM*', 'amount': 'negative'},
        ],
        'metadata': {
            'transfer-eligible': False,
            'dont-match-reason': 'ATM cash withdrawal',
            'transaction-type-hint': 'CASH_WITHDRAWAL'
        }
    }
}