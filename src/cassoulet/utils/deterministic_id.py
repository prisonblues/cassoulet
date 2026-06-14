"""
Deterministic ID Generation for Transaction Deduplication

This module provides a general-purpose implementation of deterministic ID generation
that doesn't require institution-specific code. It uses semantic field detection
to automatically normalize and hash transaction data.

The Hash Contract:
1. Persistent Identity: IDs are identical across all sessions
2. Universal Coverage: All components use this protocol
3. Architectural Integrity: Protected by automated tests

Key improvements over v1:
- No institution-specific functions
- Automatic field type detection
- Configuration-driven behavior
"""

import hashlib
from typing import Dict, Optional, Any, List, Set

# Import normalization functions from cleaner
from cassoulet.utils.cleaner import (
    normalize_string,
    EMPTY_VALUES
)
from cassoulet.utils.dates import ensure_date

def _generate_hash_and_decorations(id_string: str, prefix: str, hash_length: int = 8, suffix: str = None) -> str:
    """
    Core hashing function that generates a deterministic ID from a canonical string.

    Args:
        id_string: Canonical string representation of data to hash
        prefix: Prefix for the ID (e.g., 'santander', 'M2')
        hash_length: Number of hex characters from hash (default: 8)
        suffix: Optional suffix (e.g., '_L123' for line numbers)

    Returns:
        Deterministic ID like "{prefix}_{hash}" or "{prefix}_{hash}{suffix}"
    """
    hash_obj = hashlib.sha256(id_string.encode('utf-8'))
    hash_value = hash_obj.hexdigest()[:hash_length]

    if suffix:
        return f"{prefix}_{hash_value}{suffix}"
    else:
        return f"{prefix}_{hash_value}"


def generate_id(
    prefix: str,
    data: Dict[str, Any],
    hash_fields: Optional[List[str]] = None,
    exclude_fields: Optional[Set[str]] = None,
    hash_length: int = 8,
    line_number: Optional[int] = None,
    source_file: Optional[str] = None
) -> str:
    """
    Generate a deterministic transaction ID from data.

    This function implements the formal Hash Contract, ensuring that identical
    input data always produces identical IDs across sessions.

    ID Format: {prefix}_{YYYY-MM-DD}_{hash}_L{line} or {prefix}_{hash}_L{line}

    Args:
        prefix: Institution or type prefix (e.g., 'santander', 'manual')
        data: Dictionary of transaction data
        hash_fields: Specific fields to include in hash (default: all non-empty)
        exclude_fields: Fields to exclude from hashing
        hash_length: Number of hex characters to use from hash (default: 8)
        line_number: Optional line number from source file for uniqueness
        source_file: Optional source file path for additional uniqueness

    Returns:
        Deterministic ID with readable structure
    """
    exclude_fields = exclude_fields or set()
    fields_to_hash = {}

    # Determine which fields to include
    if hash_fields:
        # Use specified fields only
        for field in hash_fields:
            if field in data and field not in exclude_fields:
                # Simple normalization for hashing - just convert to string
                value = str(data[field]).strip()
                if value and value not in EMPTY_VALUES:
                    # Normalize as string for consistent hashing
                    fields_to_hash[field] = normalize_string(value, preserve_case=True)
    else:
        # Use all non-empty fields
        for field, value in data.items():
            if field not in exclude_fields:
                # Simple normalization for hashing
                str_value = str(value).strip()
                if str_value and str_value not in EMPTY_VALUES:
                    # Normalize as string for consistent hashing
                    fields_to_hash[field] = normalize_string(str_value, preserve_case=True)

    # Sort fields for consistency
    sorted_fields = sorted(fields_to_hash.items())

    # Create canonical string representation
    id_components = []
    for key, value in sorted_fields:
        id_components.append(f"{key}:{value}")

    # Add source file to the hash if provided (like line_number, it's a salt for uniqueness)
    if source_file:
        # Use just the filename, not full path, for consistency
        import os
        filename = os.path.basename(source_file)
        id_components.append(f"_source_file:{filename}")

    id_string = "|".join(id_components)

    # Store the ID generation input for debugging
    data['_id_generation_input'] = id_string
    data['_id_generation_line_number'] = line_number
    data['_id_generation_source_file'] = source_file

    # Try to extract date for structured ID
    date_str = None
    for field in ['date', 'Date', 'trade_date', 'Trade Date', 'transaction_date']:
        if field in data:
            date_obj = ensure_date(data[field])
            if date_obj:
                date_str = str(date_obj)  # Returns YYYY-MM-DD format
                break

    # Build the ID prefix with optional date
    if date_str:
        id_prefix = f"{prefix}_{date_str}"
    else:
        id_prefix = prefix

    # Build the suffix with optional line number
    suffix = f"_L{line_number}" if line_number is not None else None

    # Generate the final ID using the core hashing function
    return _generate_hash_and_decorations(id_string, id_prefix, hash_length, suffix)


def generate_merged_envelope_id(envelopes: List[Any]) -> str:
    """
    Generate a deterministic ID for merged envelopes.

    Creates a stable ID based on the sorted source envelope IDs.
    Format: M{count}_{hash}

    Args:
        envelopes: List of envelopes being merged (must have envelope_id attribute)

    Returns:
        Deterministic merged ID like "M2_abc123de"
    """
    # Extract and sort envelope IDs for deterministic ordering
    source_ids = sorted([e.envelope_id for e in envelopes])
    merge_prefix = f"M{len(envelopes)}"

    # Create canonical string for hashing
    id_string = f"{merge_prefix}:{':'.join(source_ids)}"

    # Use the core hashing function
    return _generate_hash_and_decorations(id_string, merge_prefix, hash_length=8)


def check_and_disambiguate_ids(envelopes: List[Any]) -> tuple[List[Any], List[Dict[str, Any]]]:
    """
    Check envelope IDs for uniqueness and disambiguate duplicates.

    CRITICAL: This function prevents the duplication bug where envelopes without
    line numbers cause massive duplication (e.g., 73 copies of the same envelope).

    The function:
    1. Detects duplicate envelope IDs
    2. Disambiguates them by adding incremental suffixes
    3. Returns warnings about what was fixed

    Args:
        envelopes: List of envelopes to check (must have envelope_id attribute)

    Returns:
        Tuple of (fixed_envelopes, warnings) where warnings describe what was fixed
    """
    from collections import defaultdict
    import copy

    # Track ID occurrences
    id_counts = defaultdict(list)
    for i, env in enumerate(envelopes):
        if hasattr(env, 'envelope_id'):
            id_counts[env.envelope_id].append(i)
        else:
            # Should never happen but be defensive
            id_counts['NO_ID'].append(i)

    # Find duplicates
    duplicates = {id_: indices for id_, indices in id_counts.items() if len(indices) > 1}

    warnings = []
    fixed_envelopes = []

    if duplicates:
        # Create warning about duplicates found
        for dup_id, indices in duplicates.items():
            warnings.append({
                'severity': 'ERROR',
                'message': f'Found {len(indices)} envelopes with duplicate ID: {dup_id}',
                'details': {
                    'duplicate_id': dup_id,
                    'count': len(indices),
                    'indices': indices[:10],  # First 10 for brevity
                    'fix_applied': 'Added disambiguating suffixes'
                }
            })

        # Fix duplicates by adding disambiguating suffixes
        for i, env in enumerate(envelopes):
            if hasattr(env, 'envelope_id') and env.envelope_id in duplicates:
                # Find which duplicate this is
                dup_indices = duplicates[env.envelope_id]
                dup_position = dup_indices.index(i)

                if dup_position == 0:
                    # Keep first occurrence as-is
                    fixed_envelopes.append(env)
                else:
                    # Add disambiguating suffix for subsequent occurrences
                    # Deep copy to avoid modifying original
                    fixed_env = copy.deepcopy(env)

                    # Check if ID already has a line number
                    if '_L' in fixed_env.envelope_id:
                        # Has line number but still duplicate - add instance suffix
                        fixed_env.envelope_id = f"{fixed_env.envelope_id}_D{dup_position}"
                    else:
                        # Missing line number - add pseudo line number
                        fixed_env.envelope_id = f"{fixed_env.envelope_id}_L{i}"

                    # Add metadata about the fix
                    if hasattr(fixed_env, 'metadata') and fixed_env.metadata:
                        fixed_env.metadata['id_disambiguation'] = {
                            'original_id': env.envelope_id,
                            'reason': 'Duplicate ID detected',
                            'position': dup_position
                        }

                    fixed_envelopes.append(fixed_env)
            else:
                # Not a duplicate, keep as-is
                fixed_envelopes.append(env)

        # Summary warning
        total_dups = sum(len(indices) - 1 for indices in duplicates.values())
        warnings.append({
            'severity': 'WARNING',
            'message': f'Fixed {total_dups} duplicate envelope IDs across {len(duplicates)} unique IDs',
            'details': {
                'total_duplicates_fixed': total_dups,
                'unique_ids_affected': len(duplicates),
                'most_duplicated': max(duplicates.items(), key=lambda x: len(x[1])) if duplicates else None
            }
        })
    else:
        # No duplicates found
        fixed_envelopes = envelopes
        warnings.append({
            'severity': 'INFO',
            'message': f'All {len(envelopes)} envelope IDs are unique',
            'details': {'total_checked': len(envelopes)}
        })

    return fixed_envelopes, warnings
