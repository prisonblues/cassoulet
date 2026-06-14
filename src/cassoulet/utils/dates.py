"""
Date parsing utilities for importers.

Provides centralized date parsing logic to reduce duplication across importers.
"""

import logging
from datetime import datetime, date
from typing import List, Optional, Tuple, Union

# Common date formats used by financial institutions
DEFAULT_DATE_FORMATS = [
    '%d/%m/%Y',      # UK standard: 31/12/2023
    '%d %b %Y',      # HSBC style: 31 Dec 2023
    '%Y-%m-%d',      # ISO format: 2023-12-31
    '%d-%m-%Y',      # Alternative UK: 31-12-2023
    '%m/%d/%Y',      # US format: 12/31/2023
    '%d %B %Y',      # Full month: 31 December 2023
    '%Y%m%d',        # Compact: 20231231
]

def parse_date(date_str: str, formats: Union[str, List[str]] = None) -> Optional[datetime.date]:
    """
    Parse a single date string, trying formats in priority order (UK first).
    
    Simple brute force: try each format until one works.
    
    Args:
        date_str: The date string to parse
        formats: Single format string or list of formats to try (auto-detects if None)
        
    Returns:
        Parsed date or None if parsing fails
        
    Example:
        >>> parse_date("31/12/2023")
        datetime.date(2023, 12, 31)
        >>> parse_date("31/12/2023", "%d/%m/%Y")  # With specific format
        datetime.date(2023, 12, 31)
        >>> parse_date("01/02/2023")  # Ambiguous - returns Feb 1st (UK format)
        datetime.date(2023, 2, 1)
    """
    if not date_str:
        return None
        
    # Clean the date string
    date_str = date_str.strip().strip('"')
    
    # Handle single format string
    if isinstance(formats, str):
        formats = [formats]
    
    # If no formats provided, get a prioritized list based on pattern
    if formats is None:
        formats = detect_date_format(date_str)
        
    # Try each format in order - first success wins
    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt).date()
        except ValueError:
            continue
    
    # Log failure if no format worked
    logging.debug(f"Could not parse date '{date_str}' with formats: {formats}")
    return None

def parse_date_with_format(date_str: str, formats: List[str] = None) -> Optional[Tuple[datetime.date, str]]:
    """
    Parse a date string and return both the date and the format used.
    
    Args:
        date_str: The date string to parse
        formats: List of date formats to try (auto-detects if None)
        
    Returns:
        Tuple of (parsed date, format used) or None if parsing fails
    """
    if not date_str:
        return None
        
    # Clean the date string
    date_str = date_str.strip().strip('"')
    
    # If no formats provided, auto-detect based on the string pattern
    if formats is None:
        formats = detect_date_format(date_str)
    
    # Try each format
    for fmt in formats:
        try:
            date = datetime.strptime(date_str, fmt).date()
            return (date, fmt)
        except ValueError:
            continue
    
    return None

def parse_date_strict(date_str: str, formats: List[str] = None) -> datetime.date:
    """
    Parse a date string, raising an exception if parsing fails.
    
    Uses auto-detection if no formats provided.
    
    Args:
        date_str: The date string to parse
        formats: Optional specific formats to try (bypasses auto-detection)
        
    Returns:
        Parsed date
        
    Raises:
        ValueError: If date cannot be parsed
    """
    result = parse_date(date_str, formats)
    if result is None:
        raise ValueError(f"Could not parse date: {date_str}")
    return result


def _get_candidate_formats(date_str: str = None) -> List[str]:
    """
    Get candidate date formats based on pattern in string (if provided).
    
    Helper function used by both single string and list detection.
    
    Args:
        date_str: Optional date string to analyze for patterns
        
    Returns:
        List of candidate formats, UK formats first
    """
    # If we have a sample string, narrow down based on separators
    if date_str:
        date_str = date_str.strip()
        
        if '/' in date_str:
            return [
                '%d/%m/%Y', '%d/%m/%y',  # UK
                '%m/%d/%Y', '%m/%d/%y',  # US
                '%Y/%m/%d'               # ISO variant
            ]
        elif '-' in date_str:
            return [
                '%d-%m-%Y', '%d-%m-%y',  # UK
                '%Y-%m-%d',              # ISO
                '%m-%d-%Y', '%m-%d-%y'   # US
            ]
        elif ' ' in date_str:
            # Check for month names
            date_lower = date_str.lower()
            if any(month in date_lower for month in ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 
                                                      'jul', 'aug', 'sep', 'oct', 'nov', 'dec']):
                return [
                    '%d %b %Y',      # UK: 31 Dec 2023
                    '%d %B %Y',      # UK: 31 December 2023
                    '%b %d, %Y',     # US: Dec 31, 2023
                    '%B %d, %Y',     # US: December 31, 2023
                    '%b %d %Y',      # US: Dec 31 2023
                ]
        elif len(date_str) == 8 and date_str.isdigit():
            return ['%Y%m%d', '%d%m%Y']
    
    # Default comprehensive list, UK formats first
    return [
        '%d/%m/%Y', '%d/%m/%y',      # UK standard
        '%d-%m-%Y', '%d-%m-%y',      # UK with dash
        '%d %b %Y', '%d %B %Y',      # UK with month names
        '%Y-%m-%d',                  # ISO
        '%m/%d/%Y', '%m/%d/%y',      # US standard
        '%m-%d-%Y', '%m-%d-%y',      # US with dash
        '%b %d, %Y', '%B %d, %Y',    # US with month names
        '%Y%m%d',                    # Compact
    ]


def detect_date_format(date_input: Union[str, List[str]], max_samples: int = 100) -> Optional[Union[str, List[str]]]:
    """
    Detect date format from either a single string or list of strings.
    
    For a single string: Returns list of candidate formats to try.
    For a list: Returns the single best format that works for all/most samples.
    
    Args:
        date_input: Either a single date string or list of date strings
        max_samples: For lists, maximum samples to test (default 100)
        
    Returns:
        - For single string: List of formats to try (for compatibility)
        - For list: Single best format string, or None
        
    Examples:
        >>> detect_date_format("31/12/2023")
        ['%d/%m/%Y', '%d/%m/%y', '%m/%d/%Y', '%m/%d/%y', '%Y/%m/%d']
        
        >>> detect_date_format(["31/12/2023", "15/02/2023"])
        '%d/%m/%Y'
    """
    # Single string - return candidate list (backward compatibility)
    if isinstance(date_input, str):
        if not date_input:
            return DEFAULT_DATE_FORMATS
        return _get_candidate_formats(date_input)
    
    # List of strings - find best format
    if isinstance(date_input, list):
        if not date_input:
            return None
            
        # Clean and limit the sample
        sample = [s.strip().strip('"') for s in date_input[:max_samples] if s and s.strip()]
        if not sample:
            return None
        
        # Get candidate formats based on first sample
        candidate_formats = _get_candidate_formats(sample[0] if sample else None)
        
        best_format = None
        best_score = 0
        
        for fmt in candidate_formats:
            successes = 0
            for date_str in sample:
                try:
                    datetime.strptime(date_str, fmt)
                    successes += 1
                except ValueError:
                    pass
            
            # If this format parses more dates than previous best, use it
            if successes > best_score:
                best_score = successes
                best_format = fmt
                
                # If we get 100% success rate, we can stop early
                if successes == len(sample):
                    logging.debug(f"Perfect match found: {fmt} parses all {successes} dates")
                    return best_format
        
        if best_format and best_score > 0:
            success_rate = (best_score / len(sample)) * 100
            logging.debug(f"Best format: {best_format} with {success_rate:.1f}% success rate")
            return best_format
        
        return None
    
    # Unknown type
    return None

def ensure_date(date_like: Union[str, date, datetime, None], 
                 formats: List[str] = None,
                 strict: bool = False) -> Optional[date]:
    """
    Universal date converter that handles any date-like input.
    
    This is the SINGLE entry point for all date conversions in the system.
    It handles:
    - date objects (returned as-is)
    - datetime objects (converted to date)
    - strings (parsed with auto-detection or provided formats)
    - None (returned as None)
    
    Args:
        date_like: Any date-like object (string, date, datetime, or None)
        formats: Optional specific formats to try for strings
        strict: If True, raise ValueError on parse failure
        
    Returns:
        A date object or None
        
    Raises:
        ValueError: If strict=True and parsing fails
        
    Examples:
        >>> ensure_date(date(2023, 12, 31))
        datetime.date(2023, 12, 31)
        
        >>> ensure_date("31/12/2023")
        datetime.date(2023, 12, 31)
        
        >>> ensure_date(datetime(2023, 12, 31, 12, 0))
        datetime.date(2023, 12, 31)
        
        >>> ensure_date(None)
        None
        
        >>> ensure_date("invalid", strict=True)
        ValueError: Could not parse date: invalid
    """
    # Handle None
    if date_like is None:
        return None
    
    # Already a date object - return as-is
    if isinstance(date_like, date) and not isinstance(date_like, datetime):
        return date_like
    
    # datetime object - extract date
    if isinstance(date_like, datetime):
        return date_like.date()
    
    # String - parse it
    if isinstance(date_like, str):
        result = parse_date(date_like, formats)
        if result is None and strict:
            raise ValueError(f"Could not parse date: {date_like}")
        return result
    
    # Unknown type - this is actually reachable for non-date/datetime/string/None types
    if strict:
        raise ValueError(f"Cannot convert {type(date_like).__name__} to date: {date_like}")
    
    logging.warning(f"Cannot convert {type(date_like).__name__} to date: {date_like}")
    return None