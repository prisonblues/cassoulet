"""Data cleaner for CSV input and Beancount output.

This module provides a hierarchical set of cleaning functions:
- clean_string(): Base string cleaning with various options
- clean_value(): Clean single primitive values
- clean_object(): Recursively clean complex data structures
- clean_for_writing(): Special cleaning for Beancount file output
- ensure_envelope_decimals(): Ensure all numeric fields in an envelope are Decimals
"""

from decimal import Decimal
from typing import Any, Dict, Union, List, Optional, TYPE_CHECKING
import logging
import unicodedata
import re

# Import currency symbols from central configuration
from cassoulet.utils.currencies import CURRENCY_SYMBOLS, SYMBOL_TO_CURRENCIES

if TYPE_CHECKING:
    from cassoulet.stages.envelope import Envelope

logger = logging.getLogger(__name__)

# BOM characters that can appear in CSV files
BOM_CHARS = ['\ufeff', '\ufffe', '\u0000']

# Common empty/null indicators
EMPTY_VALUES = {'N/A', 'n/a', '-', '–', '—', 'null', 'NULL', 'None', ''}


def normalize_string(value: str, preserve_case: bool = False) -> str:
    """
    Normalize a string for comparison and matching.
    
    This removes quotes, collapses whitespace, normalizes Unicode,
    and optionally converts to lowercase.
    
    Args:
        value: String to normalize
        preserve_case: If True, preserves original case (for names/merchants)
        
    Returns:
        Normalized string suitable for comparison
    """
    if not value:
        return ''
    
    # Strip and remove quotes
    normalized = value.strip().strip('"').strip("'")
    
    # Normalize Unicode to NFC form for consistency
    normalized = unicodedata.normalize('NFC', normalized)
    
    # Collapse multiple spaces
    normalized = ' '.join(normalized.split())
    
    # Lowercase unless preserving case
    if not preserve_case:
        normalized = normalized.lower()
    
    return normalized


def clean_string(value: str, 
                remove_quotes: bool = True,
                remove_bom: bool = True,
                remove_currency: bool = False,
                ensure_printable: bool = False,
                max_length: Optional[int] = None) -> str:
    """
    Base string cleaner - the foundation for all string cleaning.
    
    Args:
        value: Raw string value
        remove_quotes: Remove surrounding quotes (CSV input)
        remove_bom: Remove BOM characters (CSV input)
        remove_currency: Remove currency symbols (amount parsing)
        ensure_printable: Remove non-printable chars (Beancount output)
        max_length: Truncate if longer than this
        
    Returns:
        Cleaned string value
    """
    if not value:
        return ''
    
    # Initial whitespace trim
    cleaned = value.strip()
    
    # Remove quotes
    if remove_quotes:
        # Remove outer quotes (both single and double)
        if (cleaned.startswith('"') and cleaned.endswith('"')) or \
           (cleaned.startswith("'") and cleaned.endswith("'")):
            cleaned = cleaned[1:-1].strip()
    
    # Remove BOM characters
    if remove_bom:
        for bom in BOM_CHARS:
            cleaned = cleaned.replace(bom, '')
    
    # Remove currency symbols
    if remove_currency:
        # Use the reverse mapping to efficiently remove all currency symbols
        for symbol in SYMBOL_TO_CURRENCIES.keys():
            if symbol and not symbol.isalpha():  # Don't remove text codes like 'CHF' or 'kr'
                cleaned = cleaned.replace(symbol, '')
        # Trim again after removing currency
        cleaned = cleaned.strip()
    
    # Ensure printable (for Beancount output)
    if ensure_printable:
        # Remove null bytes and non-printable chars
        cleaned = ''.join(c for c in cleaned if c.isprintable() or c in '\n\r\t')
    
    # Truncate if needed
    if max_length and len(cleaned) > max_length:
        cleaned = cleaned[:max_length] + "... [truncated]"
    
    return cleaned


def clean_value(value: Any, 
               remove_quotes: bool = False,
               remove_currency: bool = False,
               ensure_printable: bool = False,
               max_length: Optional[int] = None) -> Any:
    """
    Clean a single primitive value.
    
    Handles primitive types (string, number, bool, None).
    For strings, delegates to clean_string().
    For complex objects, use clean_object() instead.
    
    Args:
        value: The primitive value to clean
        remove_quotes: Remove quotes from strings
        remove_currency: Remove currency symbols from strings
        ensure_printable: Ensure only printable characters
        max_length: Maximum length for strings
        
    Returns:
        Cleaned value
    """
    # Handle None
    if value is None:
        return None
    
    # Handle booleans
    if isinstance(value, bool):
        return value
    
    # Handle strings - delegate to clean_string
    if isinstance(value, str):
        return clean_string(value, 
                          remove_quotes=remove_quotes,
                          remove_currency=remove_currency,
                          ensure_printable=ensure_printable,
                          max_length=max_length)
    
    # Handle numbers (int, float, Decimal) - usually pass through
    if isinstance(value, (int, float, Decimal)):
        # For floats with excessive precision, format them
        if isinstance(value, float):
            # Check if it's effectively an integer
            if value == int(value):
                return int(value)
            # Otherwise keep reasonable precision
            return round(value, 10)  # 10 decimal places should be enough
        return value
    
    # For anything else that's not a complex object, convert to string and clean
    try:
        str_val = str(value)
        return clean_string(str_val, 
                          ensure_printable=ensure_printable,
                          max_length=max_length)
    except Exception:
        return None


def clean_object(obj: Any,
                max_depth: int = 10,
                remove_internal: bool = True,
                simplify_complex: bool = False,
                **clean_kwargs) -> Any:
    """
    Recursively clean ANY data structure.
    
    Handles complex types (dict, list, tuple, set) recursively,
    delegating to clean_value() for primitives.
    
    PURPOSE AND USAGE:
    - Used by clean_for_writing() to prepare metadata for Beancount output
    - The remove_internal flag strips internal processing metadata like:
      match_candidates, debug_*, processing_*, envelope_*, etc.
    - Currently removes these fields during cleaning, but this filtering
      might be better done in the posting writer itself
    
    TODO: When refactoring the posting writer to work with envelopes,
    consider moving the field filtering logic there. The writer should
    decide what metadata to include in output files, not the generic cleaner.
    
    Args:
        obj: Any object to clean
        max_depth: Maximum recursion depth
        remove_internal: Remove internal metadata keys
        simplify_complex: Convert complex structures to simpler forms
        **clean_kwargs: Arguments passed to clean_value()
        
    Returns:
        Cleaned object
    """
    # Prevent infinite recursion
    if max_depth <= 0:
        logger.warning("Maximum recursion depth reached in clean_object")
        return None if simplify_complex else str(obj)[:100] + "..."
    
    # Handle primitives with clean_value
    if obj is None or isinstance(obj, (bool, int, float, Decimal, str)):
        return clean_value(obj, **clean_kwargs)
    
    # Handle dictionaries
    if isinstance(obj, dict):
        cleaned = {}
        for key, value in obj.items():
            # Skip internal keys if requested
            if remove_internal and is_internal_key(str(key)):
                continue
            # Recursively clean the value
            cleaned_val = clean_object(value, max_depth - 1, 
                                      remove_internal, simplify_complex,
                                      **clean_kwargs)
            if cleaned_val is not None or not simplify_complex:
                cleaned[key] = cleaned_val
        return cleaned
    
    # Handle lists
    if isinstance(obj, list):
        # For large lists, truncate if simplifying
        if simplify_complex and len(obj) > 10:
            obj = obj[:10]
        
        cleaned = []
        for item in obj:
            cleaned_item = clean_object(item, max_depth - 1,
                                       remove_internal, simplify_complex,
                                       **clean_kwargs)
            if cleaned_item is not None or not simplify_complex:
                cleaned.append(cleaned_item)
        
        # If simplifying and all items are simple, join to string
        if simplify_complex and all(isinstance(x, (str, int, float)) for x in cleaned):
            return ', '.join(str(x) for x in cleaned)
        
        return cleaned
    
    # Handle tuples
    if isinstance(obj, tuple):
        cleaned = []
        for item in obj:
            cleaned_item = clean_object(item, max_depth - 1,
                                       remove_internal, simplify_complex,
                                       **clean_kwargs)
            if cleaned_item is not None or not simplify_complex:
                cleaned.append(cleaned_item)
        return tuple(cleaned)
    
    # Handle sets
    if isinstance(obj, set):
        cleaned = set()
        for item in obj:
            cleaned_item = clean_object(item, max_depth - 1,
                                       remove_internal, simplify_complex,
                                       **clean_kwargs)
            if cleaned_item is not None:
                cleaned.add(cleaned_item)
        return cleaned
    
    # For any other type (including custom objects), try to convert to string
    try:
        str_val = str(obj)
        return clean_value(str_val, **clean_kwargs)
    except Exception:
        return None if simplify_complex else f"<{type(obj).__name__}>"


def clean_for_writing(obj: Any) -> Any:
    """
    Clean data specifically for Beancount file writing.
    
    This ensures the data is safe to write to .beancount files:
    - Removes non-printable characters
    - Removes internal metadata
    - Simplifies complex structures
    - Truncates excessive lengths
    
    PURPOSE AND USAGE:
    - Called by posting_generator.py when creating transaction metadata
    - Called by transaction_writer.py before writing transactions to files
    - Uses is_internal_key() to filter out processing metadata
    
    TODO: When refactoring the posting writer to work with envelopes,
    consider moving this filtering logic to the writer itself. The writer
    should decide what metadata belongs in output files, not the cleaner.
    
    Args:
        obj: Any object to clean for writing
        
    Returns:
        Cleaned object safe for Beancount files
    """
    return clean_object(obj,
                       remove_internal=True,
                       simplify_complex=True,
                       ensure_printable=True,
                       max_length=10000)


def is_internal_key(key: str) -> bool:
    """
    Check if a metadata key is internal and should be removed.
    
    PURPOSE:
    Identifies internal processing metadata that should NOT appear
    in final Beancount output files. This includes:
    - Transfer matching metadata (match_candidates, transfer_candidates)
    - Debug and processing info (debug_*, processing_*, envelope_*)
    - Implementation details (consumed_lots, posting_metadata)
    
    USAGE:
    Called by clean_object() when remove_internal=True, which happens
    via: clean_for_writing() -> posting_generator -> transaction_writer
    
    TODO: This filtering logic might belong in the posting writer rather
    than in a generic cleaner. The writer should decide what metadata
    is appropriate for output files, not have it pre-filtered here.
    
    Args:
        key: The metadata key to check
        
    Returns:
        True if the key should be removed, False otherwise
    """
    # Keys that are clearly internal processing metadata
    internal_patterns = [
        'match_candidates', 'transfer_candidates', 'potential_matches',
        'consumed_lots', 'posting_metadata', '__tolerances__',
        'extracted_narration', 'extracted_postings', 'score_components',
        'candidates', 'envelope_', 'processing_', 'debug_', 'internal_',
        'linked_transactions', 'transaction_ids', 'links'
    ]
    
    key_lower = key.lower()
    for pattern in internal_patterns:
        if pattern in key_lower:
            return True
    
    return False


def normalize_amount(amount: Union[str, Decimal, float, int], precision: int = 2) -> str:
    """
    Normalize amount to consistent decimal format.
    
    Args:
        amount: Amount value to normalize
        precision: Decimal places (2 for cash, 8 for crypto, etc.)
        
    Returns:
        Normalized amount string with consistent precision
    """
    if not amount and amount != 0:
        return f"0.{'0' * precision}"
    
    try:
        # Clean string amounts
        if isinstance(amount, str):
            # Remove currency symbols and formatting
            cleaned = amount.strip()
            # Use SYMBOL_TO_CURRENCIES to remove all currency symbols
            for symbol in SYMBOL_TO_CURRENCIES.keys():
                if symbol and not symbol.isalpha():
                    cleaned = cleaned.replace(symbol, '')
            # Remove commas and spaces
            cleaned = cleaned.replace(',', '').replace(' ', '')
            # Handle parentheses for negative values
            if cleaned.startswith('(') and cleaned.endswith(')'):
                cleaned = '-' + cleaned[1:-1]
        else:
            cleaned = str(amount)
        
        # Convert to Decimal for precise calculation
        value = Decimal(cleaned)
        
        # Handle negative zero
        if value == 0:
            return f"0.{'0' * precision}"
        
        # Format with specified precision
        format_str = f"{{:.{precision}f}}"
        return format_str.format(value)
        
    except (ValueError, TypeError, Exception):
        # If conversion fails, return cleaned original
        return str(amount).strip()


def parse_amount(amount: Union[str, Decimal, float, int],
                       is_debit: bool = False,
                       preserve_sign: bool = False) -> Decimal:
    """
    Parse an amount string to Decimal with sign handling.
    
    Handles CR/DR suffixes, parentheses for negatives, and sign conventions.
    
    Args:
        amount: Amount value to parse
        is_debit: If True, return as negative (ignore original sign)
        preserve_sign: If True, preserve original sign from the data
        
    Returns:
        Decimal amount with appropriate sign
    """
    if not amount and amount != 0:
        return Decimal('0')
    
    is_negative = False
    
    # Clean string amounts
    if isinstance(amount, str):
        # Remove currency symbols and formatting
        cleaned = amount.strip()
        # Use SYMBOL_TO_CURRENCIES to remove all currency symbols
        for symbol in SYMBOL_TO_CURRENCIES.keys():
            if symbol and not symbol.isalpha():
                cleaned = cleaned.replace(symbol, '')
        # Remove commas and spaces
        cleaned = cleaned.replace(',', '').replace(' ', '')
        
        # Handle CR/DR suffixes (accounting notation)
        if cleaned.upper().endswith('CR'):
            cleaned = cleaned[:-2].strip()
            is_negative = False  # Credit is positive
        elif cleaned.upper().endswith('DR'):
            cleaned = cleaned[:-2].strip()
            is_negative = True  # Debit is negative
        # Handle parentheses for negative values
        elif cleaned.startswith('(') and cleaned.endswith(')'):
            cleaned = cleaned[1:-1]
            is_negative = True
        elif cleaned.startswith('-'):
            is_negative = True
            cleaned = cleaned[1:]
    else:
        cleaned = str(amount)
        if isinstance(amount, (int, float, Decimal)) and amount < 0:
            is_negative = True
            cleaned = str(abs(amount))
    
    try:
        value = Decimal(cleaned)
    except (ValueError, TypeError, Exception):
        logger.warning(f"Failed to parse amount: '{amount}' -> '{cleaned}'")
        return Decimal('0')
    
    # Apply sign logic
    if preserve_sign:
        return -value if is_negative else value
    elif is_debit:
        return -abs(value)
    else:
        return abs(value)


def normalize_identifier(value: str) -> str:
    """
    Normalize string to valid identifier format.
    
    Converts to lowercase, replaces spaces and special chars with underscores.
    Useful for file names, configuration keys, etc.
    
    Args:
        value: String to convert to identifier
        
    Returns:
        Normalized identifier string
        
    Examples:
        >>> normalize_identifier("AJ Bell")
        'aj_bell'
        >>> normalize_identifier("Interactive Investor")
        'interactive_investor'
    """
    if not value:
        return 'unknown'
    
    # Lowercase and replace spaces/hyphens with underscores
    normalized = value.lower()
    normalized = re.sub(r'[\s\-]+', '_', normalized)
    
    # Remove other problematic characters
    normalized = re.sub(r'[^a-z0-9_]', '', normalized)
    
    # Remove leading/trailing underscores
    normalized = normalized.strip('_')
    
    return normalized or 'unknown'


def ensure_envelope_decimals(envelope: 'Envelope') -> 'Envelope':
    """
    Ensure all numeric fields in an envelope are Decimals.

    This function checks and converts numeric fields to Decimals in-place using
    parse_amount() which handles all edge cases (currency symbols, CR/DR notation,
    parentheses for negatives, etc).

    Args:
        envelope: The envelope to validate and fix

    Returns:
        The same envelope with numeric fields converted to Decimals

    Raises:
        TypeError: If a numeric field contains a value that can't be converted to Decimal
    """
    # Fields that should be Decimals when present
    # For each field, specify whether to preserve the sign
    NUMERIC_FIELDS = {
        'outbound_units': True,  # Preserve sign (might already be negative)
        'inbound_units': True,   # Preserve sign (should be positive)
        'unit_price': False,     # Always positive (price per unit)
        'balance_after': True,   # Preserve sign (can be negative for overdrafts)
    }

    for field, preserve_sign in NUMERIC_FIELDS.items():
        value = getattr(envelope, field, None)

        if value is None:
            continue

        if isinstance(value, Decimal):
            continue  # Already correct type

        # Use parse_amount to handle all conversion cases
        try:
            parsed_value = parse_amount(value, preserve_sign=preserve_sign)
            setattr(envelope, field, parsed_value)

            if not isinstance(value, Decimal):
                logger.warning(
                    f"Had to convert {field} from {type(value).__name__} to Decimal for envelope {envelope.envelope_id}. "
                    f"This indicates a bug - field should already be Decimal at creation. Value was: {value!r}"
                )

        except Exception as e:
            logger.error(f"Failed to convert {field}={value} to Decimal in envelope {envelope.envelope_id}: {e}")
            raise TypeError(f"Envelope {field} must be convertible to Decimal, got {type(value).__name__}: {value}")

    # Also check metadata fields that might contain Decimals
    if envelope.metadata:
        # transfer_leakage should be a Decimal if present (always positive - it's the loss amount)
        if 'transfer_leakage' in envelope.metadata:
            leakage = envelope.metadata['transfer_leakage']
            if leakage is not None and not isinstance(leakage, Decimal):
                try:
                    # Transfer leakage is always stored as positive (the amount lost)
                    envelope.metadata['transfer_leakage'] = parse_amount(leakage, preserve_sign=False)
                    logger.warning(
                        f"Had to convert transfer_leakage from {type(leakage).__name__} to Decimal for envelope {envelope.envelope_id}. "
                        f"This indicates a bug - should already be Decimal from transfer_merger. Value was: {leakage!r}"
                    )
                except Exception as e:
                    logger.error(f"Failed to convert transfer_leakage={leakage} to Decimal: {e}")
                    # Don't raise, just log - metadata is less critical

    return envelope
