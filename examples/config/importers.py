"""
Sample importer configuration for Jack Spriggins' accounts.

This maps institution CSV formats to Beancount accounts.
Copy and modify for your own accounts.

Demonstrates two patterns:
- Bank importers (HSBC, Starling): single CSV, debit/credit columns
- Single-file broker (HL): one CSV with buys, sells, dividends, and cash
- Multi-file broker (AJ Bell): separate transaction and cash CSVs matched by reference
"""

IMPORTER_CONFIG = [
    # === Bank Importers (single CSV, debit/credit columns) ===
    {
        'importer_class': 'HSBCImporter',
        'account': 'Assets:Bank:HSBC:Checking',
        'file_identifier': 'jack_hsbc_checking.csv',
        'prefix': 'hsbc_checking',
        'sign_convention': 'debit_credit',
        'id_config': {
            'hash_fields': ['Transaction ID', 'Date', 'Payee', 'Debit', 'Credit']
        }
    },
    {
        'importer_class': 'StarlingImporter',
        'account': 'Assets:Bank:Starling:Personal',
        'file_identifier': 'jack_starling.csv',
        'prefix': 'starling_personal',
    },

    # === Single-File Broker (HL) ===
    # One CSV contains all transaction types: buys, sells, dividends, interest,
    # loyalty bonuses, and cash movements. The importer infers the type from
    # the Reference and Description columns.
    {
        'importer_class': 'HLImporter',
        'account': 'Assets:Broker:HL:ISA',
        'file_identifier': 'jack_hl_isa.csv',
        'id_config': {
            'hash_fields': ['Trade date', 'Reference', 'Description', 'Value (£)'],
        }
    },

    # === Multi-File Broker (AJ Bell) ===
    # Two separate CSVs that the pipeline merges by reference number:
    # - *_txn.csv: security transactions (buys, sells) with quantity and price
    # - *_cash.csv: cash movements (contributions, dividends, fees) with receipt/payment
    # Transactions that appear in both files (e.g., a buy shows as a txn entry AND
    # a cash payment) are matched by their shared reference number and merged into
    # a single envelope.
    {
        'importer_class': 'AJBellImporter',
        'account': 'Assets:Broker:AJBELL:SIPP',
        'file_identifier': {
            'transaction': 'jack_ajb_sipp_txn.csv',
            'cash': 'jack_ajb_sipp_cash.csv',
        },
        'prefix': 'ajbell_sipp',
        'investment_config': {
            'sign_convention': 'all_positive',
        },
        'id_config': {
            'hash_fields': ['Date', 'Reference', 'Description', 'Amount (GBP)'],
        }
    },
]
