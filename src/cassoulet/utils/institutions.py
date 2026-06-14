"""
Unified institution extraction and normalization utilities.

This module provides a single source of truth for extracting and normalizing
institution information from accounts. It uses account metadata from accounts.beancount
as the primary source, with path parsing as a fallback.
"""

from typing import Optional, Tuple
import logging
from cassoulet.utils.cleaner import normalize_identifier

logger = logging.getLogger(__name__)


def extract_institution(account: str) -> str:
    """
    Extract institution name from account.
    
    First tries to get institution from account metadata (accounts.beancount),
    then falls back to parsing the account path.
    
    Args:
        account: Account path (e.g., "Assets:Broker:HL:SIPP")
        
    Returns:
        Institution name (e.g., "Hargreaves Lansdown") or "Unknown" if not found
        
    Examples:
        >>> extract_institution("Assets:Bank:HSBC:Checking")
        'HSBC UK'  # From metadata
        >>> extract_institution("Assets:Broker:HL:SIPP")
        'Hargreaves Lansdown'  # From metadata
        >>> extract_institution("Assets:SomeNew:Path:Account")
        'Path'  # Fallback to path parsing
    """
    if not account:
        return "Unknown"
    
    # Try to get from metadata first
    try:
        from cassoulet.utils.accounts import get_institution_from_metadata
        institution = get_institution_from_metadata(account)
        if institution:
            return institution
    except Exception as e:
        logger.debug(f"Could not get institution from metadata for {account}: {e}")
    
    # Fallback to path parsing
    parts = account.split(':')
    if len(parts) >= 3:
        # Format: Category:Type:Institution[:SubAccount]
        return parts[2]
    return "Unknown"


def extract_institution_type(account: str) -> str:
    """
    Extract institution type from account.
    
    First tries to get type from account metadata (accounts.beancount),
    then falls back to parsing the account path.
    
    Args:
        account: Account path (e.g., "Assets:Broker:HL:SIPP")
        
    Returns:
        Institution type (e.g., "broker") or "unknown" if not found
        
    Examples:
        >>> extract_institution_type("Assets:Bank:HSBC:Checking")
        'bank'  # From metadata type="Bank"
        >>> extract_institution_type("Assets:Broker:HL:SIPP")
        'broker'  # From metadata type="Brokerage"
        >>> extract_institution_type("Assets:Broker:Vanguard:ISA:Jack")
        'broker'  # From metadata type="ISA"
    """
    if not account:
        return "unknown"
    
    # Try to get from metadata first
    try:
        from cassoulet.utils.accounts import get_institution_type_from_metadata
        inst_type = get_institution_type_from_metadata(account)
        if inst_type != "unknown":
            return inst_type
    except Exception as e:
        logger.debug(f"Could not get institution type from metadata for {account}: {e}")
    
    # Fallback to path parsing
    parts = account.split(':')
    if len(parts) >= 2:
        # Format: Category:Type:Institution[:SubAccount]
        inst_type = parts[1].lower()
        if inst_type in ['bank', 'broker', 'pension', 'credit', 'cash']:
            return inst_type
    return "unknown"


def extract_institution_and_type(account: str) -> Tuple[str, str]:
    """
    Extract both institution name and type from account path.
    
    Args:
        account: Account path (e.g., "Assets:Broker:HL:SIPP")
        
    Returns:
        Tuple of (institution_name, institution_type)
        
    Examples:
        >>> extract_institution_and_type("Assets:Bank:HSBC:Checking")
        ('HSBC', 'bank')
        >>> extract_institution_and_type("Assets:Broker:HL:SIPP")
        ('HL', 'broker')
    """
    return extract_institution(account), extract_institution_type(account)


def get_institution_category(inst_type: str) -> str:
    """
    Get the broader category for an institution type.
    
    Args:
        inst_type: Institution type (e.g., "broker", "bank")
        
    Returns:
        Category (e.g., "investment", "banking")
        
    Examples:
        >>> get_institution_category("broker")
        'investment'
        >>> get_institution_category("bank")
        'banking'
        >>> get_institution_category("pension")
        'investment'
    """
    categories = {
        'bank': 'banking',
        'broker': 'investment',
        'pension': 'investment',
        'credit': 'banking',
        'cash': 'cash',
    }
    return categories.get(inst_type.lower(), 'other')


def is_investment_account(account: str) -> bool:
    """
    Check if an account is an investment account.
    
    Args:
        account: Account path
        
    Returns:
        True if investment account (broker/pension), False otherwise
        
    Examples:
        >>> is_investment_account("Assets:Broker:HL:SIPP")
        True
        >>> is_investment_account("Assets:Pension:Aviva:Workplace")
        True
        >>> is_investment_account("Assets:Bank:HSBC:Checking")
        False
    """
    inst_type = extract_institution_type(account)
    return inst_type in ['broker', 'pension']


def is_banking_account(account: str) -> bool:
    """
    Check if an account is a banking account.
    
    Args:
        account: Account path
        
    Returns:
        True if banking account (bank/credit), False otherwise
        
    Examples:
        >>> is_banking_account("Assets:Bank:HSBC:Checking")
        True
        >>> is_banking_account("Assets:Credit:Amex:Card")
        True
        >>> is_banking_account("Assets:Broker:HL:SIPP")
        False
    """
    inst_type = extract_institution_type(account)
    return inst_type in ['bank', 'credit']


def get_institution_display_name(institution: str) -> str:
    """
    Get a display-friendly name for an institution.
    
    Args:
        institution: Raw institution name
        
    Returns:
        Display-friendly institution name
        
    Examples:
        >>> get_institution_display_name("HL")
        'Hargreaves Lansdown'
        >>> get_institution_display_name("HSBC")
        'HSBC'
        >>> get_institution_display_name("ajbell")
        'AJ Bell'
    """
    # Common institution name mappings
    display_names = {
        'hl': 'Hargreaves Lansdown',
        'ajbell': 'AJ Bell',
        'aj_bell': 'AJ Bell',
        'ii': 'Interactive Investor',
        'interactive_investor': 'Interactive Investor',
        'hsbc': 'HSBC',
        'santander': 'Santander',
        'starling': 'Starling Bank',
        'monzo': 'Monzo',
        'aviva': 'Aviva',
        'amex': 'American Express',
    }
    
    normalized = normalize_identifier(institution)
    return display_names.get(normalized, institution)


def format_institution_for_filename(institution: str) -> str:
    """
    Format institution name for use in filenames.
    
    Args:
        institution: Raw institution name
        
    Returns:
        Filename-safe institution name
        
    Examples:
        >>> format_institution_for_filename("HL")
        'hl'
        >>> format_institution_for_filename("AJ Bell")
        'ajbell'
        >>> format_institution_for_filename("Interactive Investor")
        'ii'
    """
    # Special cases for common abbreviations
    filename_mappings = {
        'hargreaves_lansdown': 'hl',
        'aj_bell': 'ajbell',
        'interactive_investor': 'ii',
        'american_express': 'amex',
        'starling_bank': 'starling',
    }
    
    normalized = normalize_identifier(institution)
    return filename_mappings.get(normalized, normalized)