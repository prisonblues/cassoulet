"""Header-to-semantic-field mapping, and the 'Name' column fallback.

Monzo calls the counterparty column 'Name' and puts the bank's own reference in
'Description'. Because 'Name' was not recognised, the importer kept the
reference and threw the counterparty away - so payments to a column that said
"Coinbase" were stored as "CBAGBPXQFYSTGW", uncategorisable and unmatchable.
"""

from cassoulet.utils.csv_reader import CSVReader

MONZO = ['Transaction ID', 'Date', 'Time', 'Type', 'Name', 'Emoji', 'Category',
         'Amount', 'Currency', 'Local amount', 'Local currency',
         'Notes and #tags', 'Address', 'Receipt', 'Description',
         'Category split', 'Money Out', 'Money In', 'Balance',
         'Balance currency']

HOLDINGS = ['Symbol', 'Name', 'Qty', 'Price', 'Day Gain/Loss', 'Market Value',
            'Book Cost', 'Gain/Loss', 'Average Price']

HSBC = ['Transaction ID', 'Date', 'Payee', 'Memo', 'Type', 'Debit', 'Credit',
        'Balance', 'Account Type', 'Sort Code', 'Account Number']


def _map(headers):
    r = CSVReader(account='Assets:Bank:Test')
    r._analyze_headers(headers)
    return r.semantic_map


def test_monzo_name_column_becomes_the_payee():
    m = _map(MONZO)
    assert m['payee'].column_name == 'Name'
    # and the description stays the narrative - the fallback adds, never steals
    assert m['narrative'].column_name == 'Description'


def test_holdings_file_name_is_not_a_payee():
    """'Name' there is the security name; quantity/price mark the file as holdings."""
    assert 'payee' not in _map(HOLDINGS)


def test_a_real_payee_column_is_never_overridden():
    m = _map(HSBC)
    assert m['payee'].column_name == 'Payee'


def test_name_is_not_matched_as_a_substring():
    """'Account Name' is not a counterparty - detection is substring-based for
    patterns over three characters, which is why 'name' stays out of
    HEADER_PATTERNS and is only resolved here, against the whole header set."""
    assert 'payee' not in _map(['Date', 'Account Name', 'Amount', 'Balance'])
    assert 'payee' not in _map(['Date', 'Holder Name', 'Amount', 'Balance'])
