"""
Account metadata utilities for reading institution and type information from accounts.beancount.

This module provides utilities to read and cache account metadata from the chart of accounts,
making institution names, types, and other metadata easily accessible throughout the codebase.
"""

import logging
from typing import Dict, Optional, Any, Set, List, Tuple
from pathlib import Path
import importlib.util
import sys
from beancount.loader import load_file
from beancount.core.data import Open, Transaction

logger = logging.getLogger(__name__)


class AccountMetadataRegistry:
    """
    Registry for account metadata loaded from accounts.beancount.
    
    This provides a single source of truth for institution information,
    account types, and other metadata defined in the chart of accounts.
    """
    
    def __init__(self, accounts_file: str = None, importers_config_path: str = "importers/config/importers.py"):
        """
        Initialize the registry.

        Args:
            accounts_file: Path to accounts.beancount file. Falls back to default if None.
            importers_config_path: Path to the importers configuration file.
        """
        if accounts_file is None:
            accounts_file = "accounts/accounts.beancount"
        
        self.accounts_file = accounts_file
        self.importers_config_path = importers_config_path
        self.metadata_cache: Dict[str, Dict[str, Any]] = {}
        self.institutions: Set[str] = set()
        self.account_to_institution: Dict[str, str] = {}
        self.account_to_output_file: Dict[str, str] = {}  # Map accounts to their output file prefix
        self._loaded = False
        self._importers_loaded = False
    
    def load(self) -> bool:
        """
        Load account metadata from the accounts file.
        
        Returns:
            True if successfully loaded, False otherwise
        """
        try:
            entries, errors, options = load_file(self.accounts_file)
            
            if errors:
                logger.warning(f"Found {len(errors)} errors loading {self.accounts_file}")
            
            # Extract metadata from Open directives
            for entry in entries:
                if isinstance(entry, Open):
                    account = entry.account
                    metadata = dict(entry.meta)
                    
                    # Store the metadata
                    self.metadata_cache[account] = metadata
                    
                    # Track institutions
                    if 'institution' in metadata:
                        self.institutions.add(metadata['institution'])
                    
                    logger.debug(f"Loaded metadata for {account}: {metadata.get('institution', 'N/A')}")
            
            self._loaded = True
            logger.info(f"Loaded metadata for {len(self.metadata_cache)} accounts from {self.accounts_file}")
            
            # Also load importer configuration if available
            self._load_importer_config()
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to load account metadata: {e}")
            return False
    
    def _load_importer_config(self):
        """Load the importer configuration and build account-to-institution mapping."""
        try:
            # Load the importers.py module dynamically
            spec = importlib.util.spec_from_file_location("importers_config", self.importers_config_path)
            if spec and spec.loader:
                config_module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(config_module)
                
                # Get the IMPORTER_CONFIG list
                if hasattr(config_module, 'IMPORTER_CONFIG'):
                    for config in config_module.IMPORTER_CONFIG:
                        account = config.get('account', '')
                        
                        # Extract institution from account structure
                        # Format: Assets:Type:INSTITUTION:SubAccount
                        parts = account.split(':')
                        if len(parts) >= 3:
                            # The third part is the institution
                            institution = parts[2].lower()
                            
                            # Store the mapping
                            self.account_to_institution[account] = institution
                            self.institutions.add(institution)

                            # CRITICAL: Check if there's a prefix - use that for the output file
                            # The prefix is the canonical identifier for this importer's output
                            # It's used for both:
                            # 1. Output filename: entries/output/YYYY/machine_generated/{prefix}_YYYY.beancount
                            # 2. Source identifier in envelope metadata
                            prefix = config.get('prefix')
                            if prefix:
                                # Use the specific prefix from config
                                # E.g., 'vanguard_jisa_wolf' for Vanguard:JISA:Wolf
                                # This mapping is used by get_output_file_prefix()
                                self.account_to_output_file[account] = prefix
                            elif len(parts) >= 4 and parts[1].lower() == 'broker':
                                # For broker accounts without id_config, include the account type
                                # E.g., Assets:Broker:Vanguard:JISA:Wolf → vanguard_jisa
                                output_prefix = f"{institution}_{parts[3].lower()}"
                                self.account_to_output_file[account] = output_prefix
                            
                            # Also handle sub-accounts (like AJBELL:SIPP, AJBELL:ISA)
                            if len(parts) >= 4:
                                # Create a pattern for all sub-accounts of this institution
                                base_pattern = ':'.join(parts[:3])
                                self.account_to_institution[base_pattern] = institution
                    
                    self._importers_loaded = True
                    logger.info(f"Loaded {len(self.institutions)} unique institutions from importer config")
                    logger.debug(f"Institutions found: {sorted(self.institutions)}")
                else:
                    logger.warning(f"No IMPORTER_CONFIG found in {self.importers_config_path}")
                    
        except Exception as e:
            logger.debug(f"Could not load importer config (this is normal if not using importers): {e}")
    
    def get_metadata(self, account: str) -> Dict[str, Any]:
        """
        Get all metadata for an account.
        
        Args:
            account: Account name (e.g., "Assets:Bank:HSBC:Checking")
            
        Returns:
            Dictionary of metadata, empty dict if account not found
        """
        if not self._loaded:
            self.load()
        
        return self.metadata_cache.get(account, {})
    
    def get_institution(self, account: str) -> Optional[str]:
        """
        Get the institution name for an account.
        
        Args:
            account: Account name
            
        Returns:
            Institution name from metadata, or None if not found
        """
        metadata = self.get_metadata(account)
        return metadata.get('institution')
    
    def get_account_type(self, account: str) -> Optional[str]:
        """
        Get the account type from metadata.
        
        Args:
            account: Account name
            
        Returns:
            Account type (e.g., "Bank", "Brokerage", "ISA", "SIPP")
        """
        metadata = self.get_metadata(account)
        return metadata.get('type')
    
    def get_account_number(self, account: str) -> Optional[str]:
        """
        Get the account number.
        
        Args:
            account: Account name
            
        Returns:
            Account number or None
        """
        metadata = self.get_metadata(account)
        return metadata.get('account_no')
    
    def get_owner(self, account: str) -> Optional[str]:
        """
        Get the owner of an account.
        
        Args:
            account: Account name
            
        Returns:
            Owner (e.g., "personal", "company:Acme Trading Ltd", "joint")
        """
        metadata = self.get_metadata(account)
        return metadata.get('owner')
    
    def get_all_institutions(self) -> Set[str]:
        """
        Get all unique institution names from the accounts.
        
        Returns:
            Set of institution names
        """
        if not self._loaded:
            self.load()
        
        return self.institutions.copy()
    
    def find_accounts_by_institution(self, institution: str) -> Dict[str, Dict[str, Any]]:
        """
        Find all accounts for a given institution.
        
        Args:
            institution: Institution name to search for
            
        Returns:
            Dictionary mapping account names to their metadata
        """
        if not self._loaded:
            self.load()
        
        results = {}
        for account, metadata in self.metadata_cache.items():
            if metadata.get('institution') == institution:
                results[account] = metadata
        
        return results
    
    def find_accounts_by_type(self, account_type: str) -> Dict[str, Dict[str, Any]]:
        """
        Find all accounts of a given type.
        
        Args:
            account_type: Type to search for (e.g., "Bank", "Brokerage", "ISA")
            
        Returns:
            Dictionary mapping account names to their metadata
        """
        if not self._loaded:
            self.load()
        
        results = {}
        for account, metadata in self.metadata_cache.items():
            if metadata.get('type') == account_type:
                results[account] = metadata
        
        return results
    
    def is_investment_account(self, account: str) -> bool:
        """
        Check if an account is an investment account based on metadata.
        
        Args:
            account: Account name
            
        Returns:
            True if investment account
        """
        account_type = self.get_account_type(account)
        if not account_type:
            return False
        
        investment_types = {'Brokerage', 'ISA', 'SIPP', 'JISA', 'Pension'}
        return account_type in investment_types
    
    def is_bank_account(self, account: str) -> bool:
        """
        Check if an account is a bank account based on metadata.
        
        Args:
            account: Account name
            
        Returns:
            True if bank account
        """
        account_type = self.get_account_type(account)
        return account_type == 'Bank'
    
    def extract_institution(self, transaction: Transaction) -> Optional[str]:
        """Extract institution name from a transaction.
        
        Uses the importer configuration to determine the institution based on
        the accounts used in the transaction.
        
        Args:
            transaction: The transaction to analyze
            
        Returns:
            Institution name (lowercase) or None
        """
        if not self._importers_loaded:
            self._load_importer_config()
        
        # Check transaction metadata first (if it was set during import)
        if transaction.meta and 'institution' in transaction.meta:
            inst = transaction.meta['institution'].lower()
            # Normalize known variations
            if inst in self.institutions:
                return inst
        
        # Look at all accounts in the transaction
        institutions_found = set()
        
        # Handle deferred posting architecture
        if not transaction.postings:
            # Use metadata when postings are None
            if transaction.meta:
                account = transaction.meta.get('source_account') or transaction.meta.get('account')
                if account:
                    # Direct match
                    if account in self.account_to_institution:
                        return self.account_to_institution[account]
                    
                    # Try to match by prefix (for sub-accounts)
                    for known_account, institution in self.account_to_institution.items():
                        if account.startswith(known_account):
                            return institution
            return None
        
        for posting in transaction.postings:
            account = posting.account
            
            # Direct match
            if account in self.account_to_institution:
                institutions_found.add(self.account_to_institution[account])
                continue
            
            # Try to match by prefix (for sub-accounts)
            for known_account, institution in self.account_to_institution.items():
                if account.startswith(known_account):
                    institutions_found.add(institution)
                    break
        
        # If we found exactly one institution, use it
        if len(institutions_found) == 1:
            return institutions_found.pop()
        
        # If multiple institutions, check if it's a transfer
        if len(institutions_found) > 1:
            # This is likely a transfer between institutions
            if transaction.meta and transaction.meta.get('classification-type') == 'transfer':
                return 'transfers'
            # Otherwise, use the first one alphabetically for consistency
            return sorted(institutions_found)[0]
        
        # Check for transfer patterns in narration
        if transaction.narration:
            narration_lower = transaction.narration.lower()
            if any(word in narration_lower for word in ['transfer', 'trf from', 'trf to']):
                return 'transfers'
        
        # Default to unknown
        return 'unknown'
    
    def get_output_file_prefix(self, account: str) -> str:
        """Get the output file prefix for an account.

        This determines which file an account's transactions should go to.

        IMPORTANT: This method ALWAYS uses the importer's configured prefix when available.
        The prefix is set during _load_importer_config() and stored in account_to_output_file.
        The fallback logic below only applies to accounts WITHOUT a configured importer.

        For example:
        - Assets:Bank:HSBC:Checking → "hsbc_checking" (from importer config prefix)
        - Assets:Bank:Starling:Personal → "starling_personal" (from importer config prefix)
        - Assets:Broker:Vanguard:JISA:Wolf → "vanguard_jisa_wolf" (from importer config prefix)

        Args:
            account: The account path

        Returns:
            Output file prefix (e.g., "hsbc_checking", "vanguard_jisa_wolf", "starling_personal")
        """
        if not self._importers_loaded:
            self._load_importer_config()

        # First check if we have a direct mapping from importer config
        # This will exist for any account that has a configured importer with a 'prefix'
        if account in self.account_to_output_file:
            return self.account_to_output_file[account]

        # FALLBACK: Only used for accounts without a configured importer
        # Build output name from account structure
        parts = account.split(':')
        if len(parts) >= 3:
            institution = parts[2].lower()

            # For accounts with sub-types, include them in the output name
            if len(parts) >= 4:
                sub_account = '_'.join(parts[3:]).lower()
                return f"{institution}_{sub_account}"

            return institution

        return "unknown"


# Global registry instance
_global_registry = None


def get_account_metadata_registry(accounts_file: str = None) -> AccountMetadataRegistry:
    """
    Get or create the global account metadata registry.
    
    Args:
        accounts_file: Optional path to accounts file
        
    Returns:
        The global registry instance
    """
    global _global_registry
    
    if _global_registry is None:
        _global_registry = AccountMetadataRegistry(accounts_file)
        _global_registry.load()
    
    return _global_registry


# Convenience functions that use the global registry

def get_institution_from_metadata(account: str) -> Optional[str]:
    """
    Get institution name from account metadata.
    
    Args:
        account: Account name
        
    Returns:
        Institution name or None
    """
    registry = get_account_metadata_registry()
    return registry.get_institution(account)


def get_account_type_from_metadata(account: str) -> Optional[str]:
    """
    Get account type from metadata.
    
    Args:
        account: Account name
        
    Returns:
        Account type or None
    """
    registry = get_account_metadata_registry()
    return registry.get_account_type(account)


def get_institution_type_from_metadata(account: str) -> str:
    """
    Get institution type from account metadata.
    
    This maps account types to institution types for compatibility
    with existing code.
    
    Args:
        account: Account name
        
    Returns:
        Institution type (bank, broker, pension, etc.) or "unknown"
    """
    account_type = get_account_type_from_metadata(account)
    
    if not account_type:
        return "unknown"
    
    # Map account types to institution types
    type_mapping = {
        'Bank': 'bank',
        'Brokerage': 'broker',
        'ISA': 'broker',  # ISAs are investment accounts
        'SIPP': 'pension',
        'JISA': 'broker',  # Junior ISAs are investment accounts  
        'Pension': 'pension',
    }
    
    return type_mapping.get(account_type, 'unknown')


# ========================================================================
# Account utility functions
# ========================================================================

def account_is_asset(account: str) -> bool:
    """Check if account is an Asset account (based on Beancount account type).

    Args:
        account: Account name (e.g., "Assets:Bank:HSBC:Checking")

    Returns:
        True if the account starts with "Assets:"
    """
    if not account:
        return False
    return account.startswith('Assets:')


def account_is_liability(account: str) -> bool:
    """Check if account is a Liability account (based on Beancount account type).

    This includes credit cards, loans, mortgages, etc.

    Args:
        account: Account name (e.g., "Liabilities:UK:CreditCard:HSBC")

    Returns:
        True if the account starts with "Liabilities:"
    """
    if not account:
        return False
    return account.startswith('Liabilities:')


def account_is_sipp(account: str) -> bool:
    """Check if account is a SIPP account.

    Checks both the account path and metadata type.
    """
    if not account:
        return False
    
    # Check path for SIPP
    if 'SIPP' in account.upper():
        return True
    
    # Check metadata type
    account_type = get_account_type_from_metadata(account)
    return account_type == 'SIPP'


def account_is_isa(account: str) -> bool:
    """Check if account is an ISA account.
    
    Checks both the account path and metadata type.
    """
    if not account:
        return False
    
    # Check path for ISA (but not JISA)
    account_upper = account.upper()
    if 'ISA' in account_upper and 'JISA' not in account_upper:
        return True
    
    # Check metadata type
    account_type = get_account_type_from_metadata(account)
    return account_type == 'ISA'


def account_is_broker(account: str) -> bool:
    """Check if account is a broker/investment account.
    
    Checks both the account path and metadata.
    """
    if not account:
        return False
    
    # Check path
    if 'Assets:Broker:' in account:
        return True
    
    # Check if it's an investment account via metadata
    registry = get_account_metadata_registry()
    return registry.is_investment_account(account)


def account_is_bank(account: str) -> bool:
    """Check if account is a bank account.

    Checks both the account path and metadata.
    """
    if not account:
        return False

    # Check path
    if 'Assets:Bank:' in account:
        return True

    # Check metadata
    registry = get_account_metadata_registry()
    return registry.is_bank_account(account)


def account_is_credit_card(account: str) -> bool:
    """Check if account is a credit card account.

    Checks both the account path and metadata type.

    Examples:
        Liabilities:UK:CreditCard:HSBC -> True
        Liabilities:UK:CreditCard:Amex -> True
        Assets:Bank:HSBC:Checking -> False
    """
    if not account:
        return False

    # Check path for credit card pattern
    if 'CreditCard' in account or 'Credit-Card' in account:
        return True

    # Check metadata type
    account_type = get_account_type_from_metadata(account)
    return account_type == 'CreditCard'


def account_institution(account: str) -> Optional[str]:
    """Get institution for an account.
    
    First tries metadata, then falls back to path extraction.
    
    Examples:
        Assets:Broker:HL:SIPP -> HL
        Assets:Bank:HSBC:Checking -> HSBC
    """
    if not account:
        return None
    
    # Try metadata first (more reliable)
    institution = get_institution_from_metadata(account)
    if institution:
        return institution
    
    # Fall back to path extraction
    parts = account.split(':')
    if len(parts) >= 3 and parts[0] == 'Assets':
        return parts[2]
    
    return None


def narration_contains_institution(narration: str, institution: str) -> bool:
    """Check if narration contains an institution name or abbreviation."""
    if not narration or not institution:
        return False
    
    narration_lower = narration.lower()
    institution_lower = institution.lower()
    
    # Direct match
    if institution_lower in narration_lower:
        return True
    
    # Common abbreviations and variations
    abbreviations = {
        'hargreaves lansdown': ['hl', 'hargreaves', 'lansdown'],
        'interactive investor': ['ii', 'interactive', 'investor'],
        'aj bell': ['aj bell', 'ajbell', 'aj'],
        'santander': ['santander', 'san'],
        'hsbc': ['hsbc'],
        'starling': ['starling'],
        'monzo': ['monzo'],
        'barclays': ['barclays'],
        'lloyds': ['lloyds'],
        'natwest': ['natwest', 'nw'],
        'halifax': ['halifax'],
        'nationwide': ['nationwide'],
        'vanguard': ['vanguard', 'vg'],
    }
    
    # Check if institution matches any key or is contained in any key
    for full_name, abbrevs in abbreviations.items():
        if (institution_lower in full_name or 
            full_name in institution_lower or
            institution_lower == full_name):
            # Check all abbreviations for this institution
            for abbrev in abbrevs:
                if abbrev in narration_lower:
                    return True
    
    return False


# ========================================================================
# Account to Output File Mapper
# ========================================================================

class AccountFileMapper:
    """
    Maps account names to output file identifiers.
    
    This solves the problem of manual transactions not knowing which
    output file they belong to, especially for in-specie transfers.
    
    Steel Thread Principle: Every transaction MUST be written somewhere.
    If we can't determine the correct file, it goes to orphans with explanation.
    """
    
    def __init__(self, account_config: Dict[str, Dict] = None):
        """
        Initialize with account configuration.
        
        Args:
            account_config: Dict mapping account names to their config
                           including 'file_identifier' field
        """
        self.account_config = account_config or {}
        self._build_mapping()
    
    def _build_mapping(self) -> None:
        """Build the account to file identifier mapping."""
        self.account_to_file = {}
        
        # Build mapping from account config
        for account, config in self.account_config.items():
            if 'file_identifier' in config:
                file_id = config['file_identifier']
                self.account_to_file[account] = file_id
                logger.debug(f"Mapped {account} -> {file_id}")
    
    def get_output_file_for_account(
        self, 
        account: str,
        year: int = None
    ) -> Tuple[Optional[str], str]:
        """
        Determine the output file identifier for an account.
        
        Args:
            account: The Beancount account name (e.g., "Assets:Broker:II:SIPP")
            year: Optional year for the output file
            
        Returns:
            Tuple of (file_identifier, reason)
            - file_identifier: e.g., "ii_sipp" or None if not found
            - reason: Explanation of how it was determined
        """
        # Method 1: Direct account mapping
        if account in self.account_to_file:
            file_id = self.account_to_file[account]
            return file_id, f"Direct mapping from account config"
        
        # Method 2: Try parent accounts (e.g., Assets:Broker:II for Assets:Broker:II:SIPP)
        parts = account.split(':')
        for i in range(len(parts) - 1, 1, -1):
            parent = ':'.join(parts[:i])
            if parent in self.account_to_file:
                file_id = self.account_to_file[parent]
                return file_id, f"Mapped via parent account {parent}"
        
        # Method 3: Try to infer from account structure
        file_id = self._infer_from_account_structure(account)
        if file_id:
            return file_id, f"Inferred from account structure"
        
        # No mapping found
        return None, f"No mapping found for account {account}"
    
    def _infer_from_account_structure(self, account: str) -> Optional[str]:
        """
        Try to infer the output file from account structure.
        
        E.g., Assets:Broker:II:SIPP -> ii_sipp
              Assets:Bank:HSBC:Checking -> hsbc_checking
        """
        parts = account.lower().split(':')
        
        # Look for institution patterns
        if len(parts) >= 3:
            if parts[1] == 'broker':
                # Broker accounts: institution_accounttype
                if len(parts) >= 4:
                    institution = parts[2]
                    account_type = parts[3]
                    return f"{institution}_{account_type}"
                else:
                    return parts[2]
            elif parts[1] == 'bank':
                # Bank accounts: institution_accounttype or just institution
                if len(parts) >= 4:
                    institution = parts[2]
                    account_type = parts[3]
                    # Special handling for common patterns
                    if account_type in ['checking', 'savings', 'personal']:
                        return f"{institution}_{account_type}"
                    return institution
                else:
                    return parts[2] if len(parts) >= 3 else None
        
        return None
    
    def get_output_file_for_transaction(
        self,
        transaction,
        csv_matches: List[str] = None
    ) -> Tuple[Optional[str], str, bool]:
        """
        Determine the output file for a transaction.
        
        Args:
            transaction: The Beancount transaction
            csv_matches: List of CSV file identifiers that matched this transaction
            
        Returns:
            Tuple of (file_identifier, reason, is_orphan)
            - file_identifier: e.g., "ii_sipp" or "txn_orphans" 
            - reason: Explanation of the determination
            - is_orphan: True if this should go to orphans file
        """
        # Try to get accounts from transaction
        accounts = self._extract_accounts_from_transaction(transaction)
        
        if not accounts:
            return "txn_orphans", "No accounts found in transaction", True
        
        # Try each account to find a mapping
        for account in accounts:
            file_id, reason = self.get_output_file_for_account(account)
            if file_id:
                return file_id, reason, False
        
        # Method 2: Check CSV matches
        if csv_matches:
            # Use the first CSV match as the output file
            return csv_matches[0], f"Matched CSV file {csv_matches[0]}", False
        
        # Fallback to orphans
        accounts_str = ', '.join(accounts[:3])  # First 3 accounts
        return "txn_orphans", f"Could not map accounts: {accounts_str}", True
    
    def _extract_accounts_from_transaction(self, transaction) -> List[str]:
        """Extract all accounts from a transaction."""
        accounts = []
        
        # Check metadata for deferred posting architecture
        if transaction.meta:
            # Check for source/destination accounts in metadata
            if 'source_account' in transaction.meta:
                accounts.append(transaction.meta['source_account'])
            if 'destination_account' in transaction.meta:
                accounts.append(transaction.meta['destination_account'])
            if 'importer-account' in transaction.meta:
                accounts.append(transaction.meta['importer-account'])
            
            # Check for manual postings
            if 'manual_postings' in transaction.meta:
                for posting_data in transaction.meta['manual_postings']:
                    if 'account' in posting_data:
                        accounts.append(posting_data['account'])
        
        # Check actual postings if they exist
        if transaction.postings:
            for posting in transaction.postings:
                if posting.account:
                    accounts.append(posting.account)
        
        # Remove duplicates while preserving order
        seen = set()
        unique_accounts = []
        for account in accounts:
            if account not in seen:
                seen.add(account)
                unique_accounts.append(account)
        
        return unique_accounts