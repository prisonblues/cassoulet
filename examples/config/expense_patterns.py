"""
Sample expense categorization patterns.

These map transaction narrations to expense accounts.
Copy and modify for your own payees.
"""

EXPENSE_PATTERNS = {
    'groceries': {
        'name': 'Grocery stores',
        'priority': 10,
        'patterns': [
            {
                'text_contains': ['TESCO', 'SAINSBURY', 'ASDA', 'MORRISONS',
                                  'WAITROSE', 'LIDL', 'ALDI', 'CO-OP'],
                'metadata': {
                    'expense_account': 'Expenses:Groceries',
                    'category': 'groceries',
                },
            },
        ],
    },
    'transport': {
        'name': 'Transport',
        'priority': 10,
        'patterns': [
            {
                'text_contains': ['TFL', 'TRANSPORT FOR LONDON', 'TRAINLINE',
                                  'NATIONAL RAIL'],
                'metadata': {
                    'expense_account': 'Expenses:Transport',
                    'category': 'transport',
                },
            },
        ],
    },
    'subscriptions': {
        'name': 'Subscriptions',
        'priority': 10,
        'patterns': [
            {
                'text_contains': ['SPOTIFY', 'NETFLIX', 'AMAZON PRIME',
                                  'DISNEY PLUS', 'YOUTUBE'],
                'metadata': {
                    'expense_account': 'Expenses:Subscriptions',
                    'category': 'subscriptions',
                },
            },
        ],
    },
    'shopping': {
        'name': 'Online shopping',
        'priority': 5,
        'patterns': [
            {
                'text_contains': ['AMAZON', 'AMZN', 'EBAY'],
                'metadata': {
                    'expense_account': 'Expenses:Shopping',
                    'category': 'shopping',
                },
            },
        ],
    },
    'council_tax': {
        'name': 'Council Tax',
        'priority': 10,
        'patterns': [
            {
                'text_contains': ['COUNCIL TAX', 'BOROUGH COUNCIL',
                                  'CITY COUNCIL', 'DISTRICT COUNCIL'],
                'metadata': {
                    'expense_account': 'Expenses:CouncilTax',
                    'category': 'council_tax',
                },
            },
        ],
    },
}
