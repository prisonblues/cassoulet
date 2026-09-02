"""
Envelope classification patterns for post-merge stage.

These patterns classify transactions AFTER envelope merging when we have
more context (both sides of transfers, full transaction structure, etc.).

Applied by PostMergeClassifier to determine transaction types and 
appropriate expense/income accounts.

IMPORTANT - Issue #88: Third-party payments containing institution names
(e.g., "SANTANDER MORTGAGE", "HSBC CREDIT CARD") are NOT transfers but
payments TO those companies. These should be classified as expenses, not transfers.
"""

"""
Envelope types:
- 'inbound_only': Must have inbound_account but NO outbound_account (income)
- 'outbound_only': Must have outbound_account but NO inbound_account (expense)
- 'both': Must have BOTH inbound and outbound accounts (transfer)
- None or missing: Any structure matches
"""

ENVELOPE_CLASSIFIER_PATTERNS = {
    # ========================================================================
    # Income Classifications
    # ========================================================================
    
    'interest_income_sipp': {
        'name': 'SIPP Interest Income',
        'reason': 'Interest earned in SIPP account',
        'patterns': [
            {'envelope_type': 'inbound_only',  # Must be one-sided income
             'narration': '*interest*', 
             'inbound_account': '*SIPP*',
             'amount': 'positive'}
        ],
        'metadata': {
            'transaction_type': 'INTEREST',
            'income_account': 'Income:Interest:SIPP'
        }
    },
    
    'interest_income_isa': {
        'name': 'ISA Interest Income',
        'reason': 'Interest earned in ISA account',
        'patterns': [
            {'envelope_type': 'inbound_only',  # Must be one-sided income
             'narration': '*interest*',
             'inbound_account': '*ISA*',
             'amount': 'positive'}
        ],
        'metadata': {
            'transaction_type': 'INTEREST',
            'income_account': 'Income:Interest:ISA'
        }
    },
    
    'interest_income_general': {
        'name': 'General Interest Income',
        # inbound_account is scoped to Assets: deliberately. Interest can only be
        # EARNED into an asset account; interest arriving on a liability is interest
        # CHARGED. This was '*', which matched Liabilities: too, so every
        # "INTEREST CHARGED" line on the HSBC credit card was classified as interest
        # income and stamped income_account=Income:Interest into envelope metadata -
        # which transaction_writer then honours ahead of any account-type check.
        'reason': 'Interest earned in regular accounts (asset accounts only)',
        'patterns': [
            {'envelope_type': 'inbound_only',  # Must be one-sided income
             'narration': '*interest*',
             'inbound_account': 'Assets:*',
             'amount': 'positive'},
            # Additional patterns from institution analysis:
            {'envelope_type': 'inbound_only',
             'narration': '*gross interest*',  # AJ Bell pattern
             'inbound_account': 'Assets:*',
             'amount': 'positive'},
            {'envelope_type': 'inbound_only',
             'narration': '*cash account interest*',  # Vanguard pattern
             'inbound_account': 'Assets:*',
             'amount': 'positive'},
            {'envelope_type': 'inbound_only',
             'narration': '*interest to *',  # AJ Bell "interest to DD/MM/YY"
             'inbound_account': 'Assets:*',
             'amount': 'positive'}
        ],
        'metadata': {
            'transaction_type': 'INTEREST',
            'income_account': 'Income:Interest'
        }
    },
    
    'dividend_income_sipp': {
        'name': 'SIPP Dividend Income',
        'reason': 'Dividends received in SIPP account',
        'patterns': [
            {'envelope_type': 'inbound_only',  # Must be one-sided income
             'narration': '*dividend*',
             'inbound_account': '*SIPP*'},
            {'envelope_type': 'inbound_only',
             'narration': '*DIV*',
             'inbound_account': '*SIPP*'},
            {'envelope_type': 'inbound_only',
             'narration': '*distribution*',
             'inbound_account': '*SIPP*'}
        ],
        'metadata': {
            'transaction_type': 'DIVIDEND',
            'income_account': 'Income:Dividends:SIPP'
        }
    },
    
    'dividend_income_isa': {
        'name': 'ISA Dividend Income',
        'reason': 'Dividends received in ISA account',
        'patterns': [
            {'envelope_type': 'inbound_only',
             'narration': '*dividend*',
             'inbound_account': '*ISA*'},
            {'envelope_type': 'inbound_only',
             'narration': '*DIV*',
             'inbound_account': '*ISA*'},
            {'envelope_type': 'inbound_only',
             'narration': '*distribution*',
             'inbound_account': '*ISA*'}
        ],
        'metadata': {
            'transaction_type': 'DIVIDEND',
            'income_account': 'Income:Dividends:ISA'
        }
    },
    
    'dividend_income_general': {
        'name': 'General Dividend Income',
        'reason': 'Dividends received in taxable accounts',
        'patterns': [
            {'envelope_type': 'inbound_only',
             'narration': '*dividend*',
             'inbound_account': '*Broker*'},
            {'envelope_type': 'inbound_only',
             'narration': '*DIV*',
             'inbound_account': '*Broker*'},
            {'envelope_type': 'inbound_only',
             'narration': '*distribution*',
             'inbound_account': '*Broker*'},
            # Additional dividend patterns:
            {'envelope_type': 'inbound_only',
             'narration': '*quarterly div*',
             'inbound_account': '*Broker*'},
            {'envelope_type': 'inbound_only',
             'narration': '*final div*',
             'inbound_account': '*Broker*'},
            {'envelope_type': 'inbound_only',
             'narration': '*interim div*',
             'inbound_account': '*Broker*'},
            {'envelope_type': 'inbound_only',
             'narration': '*income dist*',
             'inbound_account': '*Broker*'}
        ],
        'metadata': {
            'transaction_type': 'DIVIDEND',
            'income_account': 'Income:Dividends'
        }
    },
    
    'tax_relief_sipp': {
        'name': 'SIPP Tax Relief',
        'reason': 'Government tax relief on SIPP contributions',
        'patterns': [
            {'narration': '*tax relief*',
             'inbound_account': '*SIPP*'},
            {'narration': '*HMRC*',
             'inbound_account': '*SIPP*'},
            {'narration': '*contribution claim*',
             'inbound_account': '*SIPP*'},
            # HL specific pattern (from Issue #59):
            {'narration': '*SIPP CONTRIBUTION CLAIM*',
             'inbound_account': '*SIPP*'}
        ],
        'metadata': {
            'transaction_type': 'TAX_RELIEF',
            'income_account': 'Income:TaxRelief:SIPP',
            'transfer-candidate': 'false'  # Explicitly not a transfer
        }
    },
    
    'employer_contribution_sipp': {
        'name': 'Employer SIPP Contribution',
        'reason': 'Employer contribution to SIPP',
        'patterns': [
            {'narration': '*employer*',
             'inbound_account': '*SIPP*'},
            {'narration': '*company match*',
             'inbound_account': '*SIPP*'},
            {'narration': 'ER CONTRBN*',
             'inbound_account': '*SIPP*'}
        ],
        'metadata': {
            'transaction_type': 'EMPLOYER_CONTRIBUTION',
            'income_account': 'Income:Contributions:Employer'
        }
    },
    
    'salary_income': {
        'name': 'Salary Income',
        'reason': 'Regular employment income',
        'patterns': [
            {'narration': '*salary*',
             'inbound_account': '*Bank*',
             'amount': 'positive'},
            {'narration': '*wages*',
             'inbound_account': '*Bank*',
             'amount': 'positive'},
            {'narration': '*payroll*',
             'inbound_account': '*Bank*',
             'amount': 'positive'}
        ],
        'metadata': {
            'transaction_type': 'SALARY',
            'income_account': 'Income:Salary'
        }
    },
    
    # ========================================================================
    # Expense Classifications
    # ========================================================================
    
    'platform_fees': {
        'name': 'Investment Platform Fees',
        'reason': 'Broker/platform management fees',
        'patterns': [
            {'envelope_type': 'outbound_only',  # Must be one-sided expense
             'narration': '*platform fee*',
             'outbound_account': '*Broker*',
             'amount': 'negative'},
            {'envelope_type': 'outbound_only',
             'narration': '*management fee*',
             'outbound_account': '*Broker*',
             'amount': 'negative'},
            {'envelope_type': 'outbound_only',
             'narration': '*annual charge*',
             'outbound_account': '*Broker*',
             'amount': 'negative'},
            # Additional patterns from institution analysis:
            {'envelope_type': 'outbound_only',
             'narration': '*custody charge*',  # AJ Bell
             'outbound_account': '*Broker*',
             'amount': 'negative'},
            {'envelope_type': 'outbound_only',
             'narration': '*shares custody*',  # HL
             'outbound_account': '*Broker*',
             'amount': 'negative'},
            {'envelope_type': 'outbound_only',
             'narration': '*account fee*',  # Vanguard
             'outbound_account': '*Broker*',
             'amount': 'negative'},
            {'envelope_type': 'outbound_only',
             'narration': '*trustee fee*',  # SIPP/pension trustee fees
             'outbound_account': '*Broker*',
             'amount': 'negative'},
            {'envelope_type': 'outbound_only',
             'narration': '*admin fee*',  # Administrative fees
             'outbound_account': '*Broker*',
             'amount': 'negative'},
            {'envelope_type': 'outbound_only',
             'narration': '*safekeeping*',  # Custody/safekeeping fees
             'outbound_account': '*Broker*',
             'amount': 'negative'}
        ],
        'metadata': {
            'transaction_type': 'FEE',
            'expense_account': 'Expenses:Financial:PlatformFees'
        }
    },
    
    'bank_fees': {
        'name': 'Bank Account Fees',
        'reason': 'Banking service charges',
        'patterns': [
            {'narration': '*monthly fee*',
             'outbound_account': '*Bank*',
             'amount': 'negative'},
            {'narration': '*account fee*',
             'outbound_account': '*Bank*',
             'amount': 'negative'},
            {'narration': '*service charge*',
             'outbound_account': '*Bank*',
             'amount': 'negative'},
            {'narration': '*overdraft*',
             'outbound_account': '*Bank*',
             'amount': 'negative'}
        ],
        'metadata': {
            'transaction_type': 'FEE',
            'expense_account': 'Expenses:Financial:BankFees'
        }
    },
    
    'trading_fees': {
        'name': 'Trading Fees',
        'reason': 'Stock trading commissions and fees',
        'patterns': [
            {'narration': '*commission*',
             'outbound_account': '*Broker*',
             'amount': 'negative'},
            {'narration': '*dealing fee*',
             'outbound_account': '*Broker*',
             'amount': 'negative'},
            {'narration': '*trading fee*',
             'outbound_account': '*Broker*',
             'amount': 'negative'},
            # Additional patterns from institution analysis:
            {'narration': '*dealing charge*',  # Common UK term
             'outbound_account': '*Broker*',
             'amount': 'negative'},
            {'narration': '*dealing commission*',  # II pattern
             'outbound_account': '*Broker*',
             'amount': 'negative'},
            {'narration': '*fx fee*',  # Foreign exchange fees
             'outbound_account': '*Broker*',
             'amount': 'negative'},
            {'narration': '*stamp duty*',  # UK investment tax
             'outbound_account': '*Broker*',
             'amount': 'negative'}
        ],
        'metadata': {
            'transaction_type': 'FEE',
            'expense_account': 'Expenses:Financial:TradingFees'
        }
    },
    
    'utilities': {
        'name': 'Utility Bills',
        'reason': 'Gas, electric, water, internet bills',
        'patterns': [
            {'narration': '*electricity*',
             'outbound_account': '*Bank*',
             'amount': 'negative'},
            {'narration': '*gas bill*',
             'outbound_account': '*Bank*',
             'amount': 'negative'},
            {'narration': '*water*',
             'outbound_account': '*Bank*',
             'amount': 'negative'},
            {'narration': '*broadband*',
             'outbound_account': '*Bank*',
             'amount': 'negative'},
            {'narration': '*internet*',
             'outbound_account': '*Bank*',
             'amount': 'negative'}
        ],
        'metadata': {
            'transaction_type': 'EXPENSE',
            'expense_account': 'Expenses:Utilities'
        }
    },
    
    'insurance': {
        'name': 'Insurance Payments',
        'reason': 'Various insurance premiums',
        'patterns': [
            {'narration': '*insurance*',
             'outbound_account': '*Bank*',
             'amount': 'negative'},
            {'narration': '*premium*',
             'outbound_account': '*Bank*',
             'amount': 'negative'}
        ],
        'metadata': {
            'transaction_type': 'EXPENSE',
            'expense_account': 'Expenses:Insurance'
        }
    },
    
    'mortgage': {
        'name': 'Mortgage Payment',
        'reason': 'Monthly mortgage payment',
        'patterns': [
            {'narration': '*mortgage*',
             'outbound_account': '*Bank*',
             'amount': 'negative'},
            {'narration': '*home loan*',
             'outbound_account': '*Bank*',
             'amount': 'negative'}
        ],
        'metadata': {
            'transaction_type': 'EXPENSE',
            'expense_account': 'Expenses:Housing:Mortgage'
        }
    },
    
    'credit_card_payment': {
        'name': 'Credit Card Payment',
        'reason': 'Credit card bill payment',
        'patterns': [
            {'narration': '*credit card*',
             'outbound_account': '*Bank*',
             'amount': 'negative'},
            {'narration': '*VISA*',
             'outbound_account': '*Bank*',
             'amount': 'negative'},
            {'narration': '*MASTERCARD*',
             'outbound_account': '*Bank*',
             'amount': 'negative'},
            {'narration': '*AMEX*',
             'outbound_account': '*Bank*',
             'amount': 'negative'}
        ],
        'metadata': {
            'transaction_type': 'EXPENSE',
            'expense_account': 'Liabilities:CreditCard',
            'transfer-candidate': 'false'  # Issue #88 - Not a transfer!
        }
    },
    
    'loan_payment': {
        'name': 'Loan Payment',
        'reason': 'Loan repayment',
        'patterns': [
            {'narration': '*loan*',
             'outbound_account': '*Bank*',
             'amount': 'negative'},
            {'narration': '*finance*',
             'outbound_account': '*Bank*',
             'amount': 'negative'}
        ],
        'metadata': {
            'transaction_type': 'EXPENSE',
            'expense_account': 'Liabilities:Loans',
            'transfer-candidate': 'false'  # Issue #88 - Not a transfer!
        }
    },
    
    'rent': {
        'name': 'Rent Payment',
        'reason': 'Monthly rent payment',
        'patterns': [
            {'narration': '*rent*',
             'outbound_account': '*Bank*',
             'amount': 'negative'}
        ],
        'metadata': {
            'transaction_type': 'EXPENSE',
            'expense_account': 'Expenses:Housing:Rent'
        }
    },
    
    'groceries': {
        'name': 'Grocery Shopping',
        'reason': 'Food and household shopping',
        'patterns': [
            {'narration': '*TESCO*',
             'outbound_account': '*Bank*',
             'amount': 'negative'},
            {'narration': '*SAINSBURY*',
             'outbound_account': '*Bank*',
             'amount': 'negative'},
            {'narration': '*ASDA*',
             'outbound_account': '*Bank*',
             'amount': 'negative'},
            {'narration': '*MORRISONS*',
             'outbound_account': '*Bank*',
             'amount': 'negative'},
            {'narration': '*WAITROSE*',
             'outbound_account': '*Bank*',
             'amount': 'negative'},
            {'narration': '*LIDL*',
             'outbound_account': '*Bank*',
             'amount': 'negative'},
            {'narration': '*ALDI*',
             'outbound_account': '*Bank*',
             'amount': 'negative'}
        ],
        'metadata': {
            'transaction_type': 'EXPENSE',
            'expense_account': 'Expenses:Food:Groceries'
        }
    },
    
    'transport': {
        'name': 'Transportation',
        'reason': 'Public transport and travel expenses',
        'patterns': [
            {'narration': '*TFL*',  # Transport for London
             'outbound_account': '*Bank*',
             'amount': 'negative'},
            {'narration': '*OYSTER*',
             'outbound_account': '*Bank*',
             'amount': 'negative'},
            {'narration': '*UBER*',
             'outbound_account': '*Bank*',
             'amount': 'negative'},
            {'narration': '*TAXI*',
             'outbound_account': '*Bank*',
             'amount': 'negative'},
            {'narration': '*TRAIN*',
             'outbound_account': '*Bank*',
             'amount': 'negative'}
        ],
        'metadata': {
            'transaction_type': 'EXPENSE',
            'expense_account': 'Expenses:Transport'
        }
    },
    
    'cash_withdrawal': {
        'name': 'Cash Withdrawal',
        'reason': 'ATM cash withdrawal',
        'patterns': [
            {'narration': '*ATM*',
             'outbound_account': '*Bank*',
             'amount': 'negative'},
            {'narration': '*CASH*',
             'outbound_account': '*Bank*',
             'amount': 'negative'}
        ],
        'metadata': {
            'transaction_type': 'CASH_WITHDRAWAL',
            'expense_account': 'Assets:Cash'
        }
    },
    
    # ========================================================================
    # Special Case Patterns (must come before Transfer Classifications)
    # ========================================================================
    
    'hl_income_account_transfer': {
        'name': 'HL Income Account Transfer',
        'reason': 'Internal HL accounting transaction (not a real transfer) - Issue #59',
        'patterns': [
            {'envelope_type': 'inbound_only',
             'narration': '*transfer from income account*',
             'inbound_account': '*HL*'}
        ],
        'metadata': {
            'transaction_type': 'CASH',
            'transfer-candidate': 'false',  # Explicitly not a transfer
            'internal-transfer-type': 'income-reallocation'
        }
    },
    
    'ii_incoming_transfer': {
        'name': 'II Incoming Transfer',
        'reason': 'Transfer from external broker to II',
        'patterns': [
            {'envelope_type': 'inbound_only',
             'narration': '*trf from*',  # II specific pattern
             'inbound_account': '*II*'},
            {'envelope_type': 'inbound_only',
             'narration': '*transfer from*',  # II specific pattern when not internal
             'inbound_account': '*II*'}
        ],
        'metadata': {
            'transaction_type': 'TRANSFER',
            'transfer-source': 'external-broker',
            'transfer-direction': 'incoming'
        }
    },
    
    'reinvestment': {
        'name': 'Dividend Reinvestment',
        'reason': 'Automatic dividend reinvestment',
        'patterns': [
            {'narration': '*reinvest*',
             'inbound_account': '*Broker*'},
            {'narration': '*reinvestment*',
             'inbound_account': '*Broker*'},
            {'narration': '*drip*',  # Dividend Reinvestment Plan
             'inbound_account': '*Broker*'}
            # Removed 'accumulation' pattern - it was matching fund names like "LifeStrategy 100% Equity Fund - Accumulation"
        ],
        'metadata': {
            'transaction_type': 'REINVESTMENT',
            'income_account': 'Income:Dividends:Reinvested'
        }
    },
    
    # ========================================================================
    # Transfer Classifications (for matched transfers)
    # ========================================================================
    
    'pension_contribution': {
        'name': 'Pension Contribution',
        'reason': 'Personal contribution to pension',
        'patterns': [
            {'envelope_type': 'both',  # Must be a matched transfer
             'narration': '*pension contribution*',
             'outbound_account': '*Bank*',
             'inbound_account': '*SIPP*'},
            {'envelope_type': 'both',
             'narration': '*sipp contribution*',
             'outbound_account': '*Bank*',
             'inbound_account': '*SIPP*'}
            # Removed incorrect AJ Bell patterns that matched internal account movements
            # "Transfer from SIPP Cash Account" is an internal movement, not a contribution
        ],
        'metadata': {
            'transaction_type': 'PENSION_CONTRIBUTION',
            'transfer_type': 'pension_contribution'
        }
    },
    
    'isa_contribution': {
        'name': 'ISA Contribution',
        'reason': 'Contribution to ISA account',
        'patterns': [
            {'envelope_type': 'both',  # Must be a matched transfer
             'narration': '*isa*',
             'outbound_account': '*Bank*',
             'inbound_account': '*ISA*'},
            # AJ Bell pattern for ISA contributions:
            {'envelope_type': 'inbound_only',  # One-sided in broker
             'narration': '*transfer from isa cash account*',
             'inbound_account': '*ISA*'},
            # Vanguard deposit patterns:
            {'envelope_type': 'inbound_only',
             'narration': '*deposit for investment*',
             'inbound_account': '*ISA*'},
            {'envelope_type': 'inbound_only',
             'narration': '*regular deposit*',
             'inbound_account': '*ISA*'}
        ],
        'metadata': {
            'transaction_type': 'ISA_CONTRIBUTION',
            'transfer_type': 'isa_contribution'
        }
    },
    
    'broker_funding': {
        'name': 'Broker Account Funding',
        'reason': 'Transfer to investment account',
        'patterns': [
            {'envelope_type': 'both',  # Must be a matched transfer
             'outbound_account': '*Bank*',
             'inbound_account': '*Broker*'}
        ],
        'metadata': {
            'transaction_type': 'TRANSFER',
            'transfer_type': 'broker_funding'
        }
    }
}