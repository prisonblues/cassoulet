"""
V7 Account Validator

Validates and corrects account references in transactions to ensure they match
the defined chart of accounts.
"""

import logging
from pathlib import Path
from typing import List, Dict, Set, Optional, Tuple
from copy import deepcopy

from beancount.core.data import Transaction, Posting, Open
from beancount import loader
from beancount.core import amount

logger = logging.getLogger(__name__)


class AccountValidator:
    """Validates and corrects account references in transactions."""
    
    # Known account mappings for common mistakes (user can extend via subclass)
    ACCOUNT_CORRECTIONS = {
        'Income:Investment:Dividends': 'Income:Dividends',
    }
    
    # Bank accounts can only hold currencies
    BANK_CURRENCIES = {'GBP', 'USD', 'EUR', 'CHF', 'JPY', 'CAD', 'AUD', 'NZD'}
    
    # Patterns to identify account types
    BANK_PATTERNS = ['Bank:', 'HSBC', 'Starling', 'Monzo', 'Santander', 'Checking', 'Savings', 'Current']
    BROKER_PATTERNS = ['Broker:', 'ISA', 'SIPP', 'Investment', 'AJBELL', 'HL:', 'II:', 'VANGUARD']
    
    def __init__(self, accounts_file: str = None):
        """Initialize the account validator.

        Args:
            accounts_file: Path to accounts.beancount file. Falls back to default if None.
        """
        accounts_file = accounts_file or "accounts/accounts.beancount"
        self.accounts_file = accounts_file
        self.account_metadata = {}  # Store account metadata including type - MUST be before _load_valid_accounts
        self.valid_accounts = self._load_valid_accounts()
        self.corrections_applied = []
        
        logger.info(f"AccountValidator initialized with {len(self.valid_accounts)} valid accounts")
    
    def _load_valid_accounts(self) -> Set[str]:
        """Load the set of valid account names from accounts.beancount.
        
        Returns:
            Set of valid account names
        """
        valid_accounts = set()
        
        if not Path(self.accounts_file).exists():
            logger.warning(f"Accounts file not found: {self.accounts_file}")
            return valid_accounts
        
        try:
            entries, errors, options = loader.load_file(self.accounts_file)
            
            for entry in entries:
                if isinstance(entry, Open):
                    valid_accounts.add(entry.account)
                    # Store account metadata for validation
                    self.account_metadata[entry.account] = entry.meta if entry.meta else {}
                    
            logger.info(f"Loaded {len(valid_accounts)} valid accounts from {self.accounts_file}")
            
        except Exception as e:
            logger.error(f"Error loading accounts: {e}")
        
        return valid_accounts
    
    def is_bank_account(self, account: str) -> bool:
        """Check if an account is a bank account."""
        return any(pattern in account for pattern in self.BANK_PATTERNS)
    
    def is_broker_account(self, account: str) -> bool:
        """Check if an account is a broker account."""
        return any(pattern in account for pattern in self.BROKER_PATTERNS)
    
    def validate_commodity_for_account(self, account: str, commodity: str) -> Tuple[bool, Optional[str]]:
        """
        Validate if a commodity is allowed in an account.
        
        Returns:
            (is_valid, error_message)
        """
        # Rule 1: Bank accounts can only hold currencies
        if self.is_bank_account(account):
            if commodity not in self.BANK_CURRENCIES:
                return False, f"Bank account {account} cannot hold commodity {commodity}. Only currencies allowed: {self.BANK_CURRENCIES}"
        
        # Rule 2: Cash/Current/Checking/Savings accounts strictly currency only
        cash_keywords = ['Cash', 'Current', 'Checking', 'Savings']
        if any(keyword in account for keyword in cash_keywords):
            if commodity not in self.BANK_CURRENCIES:
                return False, f"Cash account {account} cannot hold non-currency commodity {commodity}"
        
        # Rule 3: Pension/ISA/SIPP/Broker can hold anything
        if self.is_broker_account(account):
            return True, None
        
        # Rule 4: Expense/Income accounts - currencies only
        if account.startswith('Expenses:') or account.startswith('Income:'):
            if commodity not in self.BANK_CURRENCIES:
                logger.warning(f"Non-currency {commodity} in {account} - may need review")
        
        return True, None
    
    def validate_and_correct_transaction(self, txn: Transaction) -> Tuple[Transaction, List[str]]:
        """Validate and correct account references in a transaction.
        
        Args:
            txn: Transaction to validate
            
        Returns:
            Tuple of (corrected transaction, list of warnings)
        """
        warnings = []
        needs_correction = False
        new_postings = []
        has_critical_error = False
        
        # Handle entries without postings (deferred posting architecture)
        if not txn.postings:
            return txn, []
        
        for posting in txn.postings:
            account = posting.account
            corrected_account = account
            
            # CRITICAL FIX: First check if account is already valid
            # DO NOT MODIFY VALID ACCOUNTS!
            if account in self.valid_accounts:
                # Account is valid - check commodity restrictions
                if posting.units:
                    commodity = posting.units.currency
                    is_valid, error_msg = self.validate_commodity_for_account(account, commodity)
                    if not is_valid:
                        warnings.append(f"CRITICAL: {error_msg}")
                        has_critical_error = True
                        # Don't create the transaction if there's a critical error
                        logger.critical(f"BLOCKED TRANSACTION: {error_msg}")
                        return (txn, [f"BLOCKED: {error_msg}"])
                # Use the account as-is
                new_postings.append(posting)
                continue
            
            # Only apply corrections if account is NOT valid
            # Check if account needs correction
            if account in self.ACCOUNT_CORRECTIONS:
                corrected_account = self.ACCOUNT_CORRECTIONS[account]
                warnings.append(f"Corrected account '{account}' to '{corrected_account}'")
                needs_correction = True
            else:
                # Account is invalid and no correction available
                # DO NOT GUESS - leave it as is and let validation catch it
                warnings.append(f"Unknown account '{account}' - keeping as-is for validation")
                corrected_account = account  # Keep the original invalid account
            
            # Create new posting with corrected account if needed
            if corrected_account != account:
                new_posting = Posting(
                    account=corrected_account,
                    units=posting.units,
                    cost=posting.cost,
                    price=posting.price,
                    flag=posting.flag,
                    meta=posting.meta
                )
                new_postings.append(new_posting)
            else:
                new_postings.append(posting)
        
        # Create new transaction if corrections were made
        if needs_correction:
            new_txn = Transaction(
                meta=txn.meta,
                date=txn.date,
                flag=txn.flag,
                payee=txn.payee,
                narration=txn.narration,
                tags=txn.tags,
                links=txn.links,
                postings=new_postings
            )
            return new_txn, warnings
        
        return txn, warnings
    
    def _suggest_account(self, invalid_account: str) -> Optional[str]:
        """Suggest a valid account name for an invalid one.
        
        Uses heuristics to find the most likely valid account.
        
        Args:
            invalid_account: The invalid account name
            
        Returns:
            Suggested valid account name or None
        """
        parts = invalid_account.split(':')
        
        # Special cases for Junior ISAs — find first matching JISA account
        if 'JISA' in invalid_account or 'Junior' in invalid_account:
            jisa_matches = [acc for acc in self.valid_accounts if ':JISA:' in acc]
            if jisa_matches:
                return sorted(jisa_matches)[0]
        
        # Try progressively shorter account paths
        for i in range(len(parts), 0, -1):
            test_parts = parts[:i]
            
            # Find accounts that start with this prefix
            prefix = ':'.join(test_parts)
            matches = [acc for acc in self.valid_accounts if acc.startswith(prefix)]
            
            if matches:
                # Prefer exact matches
                if prefix in matches:
                    return prefix
                # Otherwise return the first match
                return matches[0]
        
        # Check for common account type patterns
        if invalid_account.startswith('Income:') and 'Dividend' in invalid_account:
            return 'Income:Dividends'
        
        if invalid_account.startswith('Income:') and 'Interest' in invalid_account:
            return 'Income:Interest'
        
        if invalid_account.startswith('Expenses:'):
            return 'Expenses:Unknown'
        
        if invalid_account.startswith('Assets:'):
            return 'Assets:Suspense'
        
        return None
    
    def validate_transactions(self, transactions: List[Transaction]) -> Tuple[List[Transaction], List[str]]:
        """Validate and correct a list of transactions.
        
        Args:
            transactions: List of transactions to validate
            
        Returns:
            Tuple of (corrected transactions, list of all warnings)
        """
        corrected_transactions = []
        all_warnings = []
        
        for txn in transactions:
            corrected_txn, warnings = self.validate_and_correct_transaction(txn)
            corrected_transactions.append(corrected_txn)
            
            if warnings:
                # Add transaction context to warnings
                for warning in warnings:
                    contextualized = f"{txn.date} {txn.narration[:30]}: {warning}"
                    all_warnings.append(contextualized)
        
        if all_warnings:
            logger.warning(f"Account validation found {len(all_warnings)} issues")
            for warning in all_warnings[:10]:  # Show first 10 warnings
                logger.warning(f"  {warning}")
            if len(all_warnings) > 10:
                logger.warning(f"  ... and {len(all_warnings) - 10} more")
        
        return corrected_transactions, all_warnings
    
    def get_valid_accounts_by_type(self, account_type: str) -> List[str]:
        """Get all valid accounts of a specific type.
        
        Args:
            account_type: Type prefix like 'Assets:Bank' or 'Income:'
            
        Returns:
            List of valid account names matching the type
        """
        return [acc for acc in self.valid_accounts if acc.startswith(account_type)]