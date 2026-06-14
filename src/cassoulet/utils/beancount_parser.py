"""
Safe Beancount parser utility.

This module provides a resilient way to parse Beancount files that:
1. Always returns transaction data, even when booking fails  
2. Uses parser to get raw data, falls back to loader when safe
3. Preserves all metadata and posting information
4. Handles edge cases gracefully

The key insight: Beancount's parser gives us the raw parsed data without
attempting to book transactions. This is perfect for manual transactions
that may reference accounts or lots not yet in context.
"""

import logging
from typing import List, Tuple, Dict, Any, Optional
from pathlib import Path

from beancount import loader
from beancount.parser import parser
from beancount.core import data

logger = logging.getLogger(__name__)


def safe_parse_file(
    filepath: str,
    encoding: str = 'utf-8',
    preserve_raw_postings: bool = True
) -> Tuple[List[data.Directive], List[Any], Dict[str, Any]]:
    """
    Robustly load a Beancount file, handling booking failures gracefully.
    
    This function tries multiple strategies:
    1. First tries loader.load_file() for full booking
    2. If critical data is missing, falls back to parser.parse_file()
    3. Merges results to preserve maximum information
    
    Args:
        filepath: Path to the Beancount file
        encoding: File encoding (default: utf-8)
        preserve_raw_postings: If True, preserve posting data even when booking fails
        
    Returns:
        Tuple of (entries, errors, options)
        - entries: List of directives with preserved posting data
        - errors: Combined list of errors from all attempts
        - options: Beancount options dict
    """
    filepath = Path(filepath)
    if not filepath.exists():
        logger.error(f"File not found: {filepath}")
        return [], [f"File not found: {filepath}"], {}
    
    all_errors = []
    
    # Step 1: Try standard loader (with booking)
    try:
        entries, errors, options = loader.load_file(str(filepath), encoding=encoding)
        all_errors.extend(errors)
        
        # Check if we lost critical transaction data
        transactions_ok = True
        for entry in entries:
            if isinstance(entry, data.Transaction):
                # Check for transactions that lost postings due to booking failures
                if not entry.postings or len(entry.postings) == 0:
                    transactions_ok = False
                    logger.warning(
                        f"Transaction lost postings during booking: {entry.date} {entry.narration}"
                    )
                    break
        
        if transactions_ok:
            # Everything loaded fine, return as-is
            logger.debug(f"Successfully loaded {filepath} with standard loader")
            return entries, all_errors, options
            
    except Exception as e:
        logger.warning(f"Standard loader failed for {filepath}: {e}")
        all_errors.append(f"Loader error: {e}")
    
    # Step 2: Fall back to parser (no booking) to get raw data
    logger.info(f"Falling back to parser for {filepath} to preserve transaction data")
    try:
        parsed_entries, parse_errors, parsed_options = parser.parse_file(
            str(filepath), encoding=encoding
        )
        
        # Build a map of parsed transactions by (date, narration) for merging
        parsed_txn_map = {}
        for entry in parsed_entries:
            if isinstance(entry, data.Transaction):
                key = (entry.date, entry.narration)
                parsed_txn_map[key] = entry
        
        # Step 3: Merge - use loader results but restore missing postings from parser
        if 'entries' in locals():
            # We have loader results, merge them
            merged_entries = []
            for entry in entries:
                if isinstance(entry, data.Transaction):
                    key = (entry.date, entry.narration)
                    if not entry.postings and key in parsed_txn_map:
                        # This transaction lost its postings, restore from parsed version
                        parsed_txn = parsed_txn_map[key]
                        if parsed_txn.postings:
                            logger.info(
                                f"Restoring {len(parsed_txn.postings)} postings for "
                                f"{entry.date} {entry.narration}"
                            )
                            # Preserve metadata from loader, postings from parser
                            restored_entry = entry._replace(postings=parsed_txn.postings)
                            # Mark that we restored postings
                            if restored_entry.meta:
                                restored_entry.meta['postings_restored'] = True
                            merged_entries.append(restored_entry)
                        else:
                            merged_entries.append(entry)
                    else:
                        merged_entries.append(entry)
                else:
                    merged_entries.append(entry)
            
            return merged_entries, all_errors, options
        else:
            # No loader results, use parser results directly
            return parsed_entries, parse_errors, parsed_options
            
    except Exception as e:
        logger.error(f"Parser also failed for {filepath}: {e}")
        all_errors.append(f"Parser error: {e}")
        return [], all_errors, {}


def extract_posting_data(transaction: data.Transaction) -> List[Dict[str, Any]]:
    """
    Extract posting data from a transaction in a standardized format.
    
    This handles both booked and unbooked transactions, preserving
    all available information.
    
    Args:
        transaction: Beancount Transaction object
        
    Returns:
        List of posting data dictionaries
    """
    posting_data = []
    
    if not transaction.postings:
        return posting_data
    
    for posting in transaction.postings:
        pdata = {
            'account': posting.account,
            'units': str(posting.units) if posting.units else None,
            'cost': None,
            'price': str(posting.price) if posting.price else None,
            'flag': posting.flag,
            'meta': posting.meta or {}
        }
        
        # Handle cost in various formats
        if posting.cost:
            from beancount.core.position import Cost, CostSpec
            
            if isinstance(posting.cost, Cost):
                # Fully resolved cost
                pdata['cost'] = {
                    'number': str(posting.cost.number) if posting.cost.number else None,
                    'currency': posting.cost.currency,
                    'date': posting.cost.date.isoformat() if posting.cost.date else None,
                    'label': posting.cost.label
                }
            elif isinstance(posting.cost, CostSpec):
                # Unresolved cost spec (booking required)
                pdata['cost'] = {
                    'type': 'CostSpec',
                    'number_per': str(posting.cost.number_per) if hasattr(posting.cost, 'number_per') and posting.cost.number_per else None,
                    'number_total': str(posting.cost.number_total) if hasattr(posting.cost, 'number_total') and posting.cost.number_total else None,
                    'currency': posting.cost.currency if hasattr(posting.cost, 'currency') else None,
                    'date': posting.cost.date.isoformat() if hasattr(posting.cost, 'date') and posting.cost.date else None,
                    'label': posting.cost.label if hasattr(posting.cost, 'label') else None,
                    'merge': posting.cost.merge if hasattr(posting.cost, 'merge') else None
                }
                # Special handling for MISSING values (indicates FIFO request)
                if hasattr(posting.cost, 'number_per') and posting.cost.number_per and hasattr(posting.cost.number_per, '__name__') and posting.cost.number_per.__name__ == 'MISSING':
                    pdata['cost']['number_per'] = 'FIFO'
                if hasattr(posting.cost, 'currency') and posting.cost.currency and hasattr(posting.cost.currency, '__name__') and posting.cost.currency.__name__ == 'MISSING':
                    pdata['cost']['currency'] = 'FIFO'
        
        posting_data.append(pdata)
    
    return posting_data


