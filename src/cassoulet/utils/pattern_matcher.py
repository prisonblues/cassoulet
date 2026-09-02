"""
Universal pattern matching engine for transaction envelopes.

This module provides a high-performance pattern matching system that works
on envelopes at any stage of processing (CSV import or post-merge).

Key features:
- Optimized matching order: equality → arithmetic → regex
- Pre-compiled patterns for performance
- Fast-path wildcard matching (string ops for simple patterns)
- Utility methods for common checks (account_is_sipp, etc.)
- Works with any rule set (no_match, classification, posting)

Standalone utility functions (use these directly):
- match_wildcard(text, pattern) - Fast wildcard matching with * and ?
- check_amount(value, condition) - Numeric condition checking
- text_contains_any(text, keywords) - Keyword search
"""

import re
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional, Any, Pattern as RePattern, Union, Tuple

# Import utilities we'll delegate to
# Import entire module for dynamic lookup
import cassoulet.utils.envelope_utilities as env_utils
from cassoulet.utils.envelope_utilities import (
    # Sentinel - needed directly
    NOT_APPLICABLE, NotApplicable,
)


# ============================================================================
# STANDALONE PATTERN MATCHING UTILITIES
# These are the canonical implementations - import from here, not elsewhere
# ============================================================================

# Pattern cache - compile/analyze once, reuse many times
_wildcard_cache: Dict[str, Tuple[bool, str, Any]] = {}
_amount_cache: Dict[str, Tuple[str, Optional[Decimal], Optional[Decimal]]] = {}


def _get_compiled_wildcard(pattern: str) -> Tuple[bool, str, Any]:
    """Get or create compiled wildcard pattern with fast-path detection.

    Returns:
        (is_simple, normalized_pattern, compiled_data)

    For simple patterns like '*TESCO*', we use string operations.
    Only patterns with '?' or complex wildcards need regex.
    """
    if pattern in _wildcard_cache:
        return _wildcard_cache[pattern]

    upper_pattern = pattern.upper()

    # Fast path: simple patterns like '*KEYWORD*' don't need regex
    if '?' not in pattern:
        stripped = upper_pattern.strip('*')
        if '*' not in stripped:
            # Pattern is just '*KEYWORD*' or 'KEYWORD*' or '*KEYWORD' or 'KEYWORD'
            is_prefix = not pattern.startswith('*')
            is_suffix = not pattern.endswith('*')
            result = (True, stripped, (is_prefix, is_suffix))
            _wildcard_cache[pattern] = result
            return result

    # Complex pattern - compile regex
    regex_pattern = re.escape(upper_pattern).replace(r'\*', '.*').replace(r'\?', '.')
    compiled = re.compile(f'^{regex_pattern}$')
    result = (False, '', compiled)
    _wildcard_cache[pattern] = result
    return result


def match_wildcard(text: str, pattern: str) -> bool:
    """Match text against wildcard pattern using fastest available method.

    Supports:
    - '*' for any characters (uses fast string ops when possible)
    - '?' for single character (requires regex, cached)

    Args:
        text: Text to match against
        pattern: Wildcard pattern (e.g., '*TESCO*', 'JANE*SMITH*')

    Returns:
        True if text matches pattern
    """
    if not text:
        return False

    text_upper = text.upper()
    is_simple, normalized, data = _get_compiled_wildcard(pattern)

    if is_simple:
        # Fast path: simple string operations
        is_prefix, is_suffix = data
        if is_prefix and is_suffix:
            return text_upper == normalized  # Exact match
        elif is_prefix:
            return text_upper.startswith(normalized)  # PREFIX*
        elif is_suffix:
            return text_upper.endswith(normalized)  # *SUFFIX
        else:
            return normalized in text_upper  # *CONTAINS*
    else:
        # Slow path: regex (cached)
        return bool(data.match(text_upper))


_WORD_START_CACHE: Dict[str, Any] = {}


def _word_start_pattern(keyword: str):
    """Compiled matcher for a keyword appearing at the START of a word."""
    cached = _WORD_START_CACHE.get(keyword)
    if cached is None:
        cached = re.compile(r'(?<![A-Z0-9])' + re.escape(keyword.upper()))
        _WORD_START_CACHE[keyword] = cached
    return cached


def text_contains_any(text: str, keywords: List[str]) -> bool:
    """Check whether any keyword begins a word in text (case-insensitive).

    WORD START, not bare substring. A keyword may run on into the rest of the
    word - "AMZN" still matches "AMZNMKTPLACE" - but it may not begin midway
    through one.

    Plain substring matching was quietly miscategorising thousands of pounds,
    because these keywords are short brand names and short brand names hide
    inside ordinary words:

        'ESSO'  matched  "MontESSOri"     GBP 26,004 of nursery fees as petrol
        'BP '   matched  "VISION DIRECT GBP BRISTOL"
        'SSE'   matched  "RuSSEll Square"
        'EON'   matched  "LEON Cheapside"          a restaurant, as an energy bill
        'AWS'   matched  "LAWSon Kitanoh"
        'VUE'   matched  "BelleVUE Bicycles"
        'SKY'   matched  "whiSKYexchange"
        'EE '   matched  "instalment FEE ", "ad frEE for", "gumtrEE table"

    Roughly 400 postings and GBP 40,000 across a dozen accounts, all invisible:
    every one produced a confident, specific, wrong category.

    The cost of the fix is compound merchant strings where the brand does not
    start the word - "HailoCab" no longer matches 'CAB'. That is the right
    trade: such cases are rare, and they are fixed by naming the merchant,
    whereas substring collisions are silent and unbounded.
    """
    if not text:
        return False
    text_upper = text.upper()
    return any(_word_start_pattern(str(kw)).search(text_upper) for kw in keywords)


def parse_amount_condition(condition: str) -> Tuple[str, Optional[Decimal], Optional[Decimal]]:
    """Parse amount condition string into operator and values.

    Args:
        condition: Amount condition string (e.g., '<30', '20-100', 'positive')

    Returns:
        (operator, value1, value2) tuple

    Examples:
        '<30' -> ('lt', Decimal('30'), None)
        '>=100' -> ('gte', Decimal('100'), None)
        '20-100' -> ('range', Decimal('20'), Decimal('100'))
        'positive' -> ('positive', None, None)
    """
    if condition in _amount_cache:
        return _amount_cache[condition]

    cond = str(condition).strip()
    result: Tuple[str, Optional[Decimal], Optional[Decimal]]

    # Named conditions
    if cond == 'positive':
        result = ('positive', None, None)
    elif cond == 'negative':
        result = ('negative', None, None)
    elif cond == 'zero':
        result = ('zero', None, None)
    elif cond == 'zero_or_none':
        result = ('zero_or_none', None, None)
    # Range: '20-100' (but not '-30' which is negative number)
    elif '-' in cond and not cond.startswith('-') and not cond.startswith('<') and not cond.startswith('>'):
        parts = cond.split('-')
        if len(parts) == 2:
            try:
                result = ('range', Decimal(parts[0].strip()), Decimal(parts[1].strip()))
            except InvalidOperation:
                result = ('invalid', None, None)
        else:
            result = ('invalid', None, None)
    # Operators
    elif cond.startswith('>='):
        try:
            result = ('gte', Decimal(cond[2:].strip()), None)
        except InvalidOperation:
            result = ('invalid', None, None)
    elif cond.startswith('<='):
        try:
            result = ('lte', Decimal(cond[2:].strip()), None)
        except InvalidOperation:
            result = ('invalid', None, None)
    elif cond.startswith('!='):
        try:
            result = ('neq', Decimal(cond[2:].strip()), None)
        except InvalidOperation:
            result = ('invalid', None, None)
    elif cond.startswith('>'):
        try:
            result = ('gt', Decimal(cond[1:].strip()), None)
        except InvalidOperation:
            result = ('invalid', None, None)
    elif cond.startswith('<'):
        try:
            result = ('lt', Decimal(cond[1:].strip()), None)
        except InvalidOperation:
            result = ('invalid', None, None)
    # Exact value
    else:
        try:
            result = ('eq', Decimal(cond), None)
        except InvalidOperation:
            result = ('invalid', None, None)

    _amount_cache[condition] = result
    return result


def check_amount(value: Any, condition: str) -> bool:
    """Check if a numeric value matches a condition string.

    Args:
        value: Numeric value to check (Decimal, int, float, or None)
        condition: Condition string (e.g., '<30', '>500', '20-100', 'positive')

    Returns:
        True if value matches condition
    """
    # Handle NOT_APPLICABLE
    if isinstance(value, NotApplicable):
        return False

    op, val1, val2 = parse_amount_condition(condition)

    # Named conditions that handle None specially
    if op == 'zero_or_none':
        return value is None or value == 0
    if op == 'zero':
        if value is None:
            return True  # None counts as zero for single-sided transactions
        try:
            return float(value) == 0
        except (TypeError, ValueError):
            return False
    if op == 'positive':
        if value is None:
            return False
        try:
            return float(value) > 0
        except (TypeError, ValueError):
            return False
    if op == 'negative':
        if value is None:
            return False
        try:
            return float(value) < 0
        except (TypeError, ValueError):
            return False

    # For other conditions, None fails
    if value is None:
        return False
    if op == 'invalid':
        return False

    try:
        amount = abs(Decimal(str(value)))
    except (InvalidOperation, TypeError, ValueError):
        return False

    if op == 'lt':
        return amount < val1
    if op == 'lte':
        return amount <= val1
    if op == 'gt':
        return amount > val1
    if op == 'gte':
        return amount >= val1
    if op == 'neq':
        return amount != val1
    if op == 'range':
        return val1 <= amount <= val2
    if op == 'eq':
        return amount == val1

    return False


# ============================================================================
# PATTERN MATCHER CLASS
# ============================================================================


class PatternMatcher:
    """Universal pattern matching engine with performance optimizations."""
    
    def __init__(self, rules: dict):
        """Initialize with a specific rule set.
        
        Args:
            rules: Pattern rules from config files (no_match_patterns.py, 
                   envelope_classifier_patterns.py, etc.)
        """
        self.rules = rules
        self.compiled_patterns = self._optimize_and_compile_patterns()
    
    def _optimize_and_compile_patterns(self) -> List[Dict[str, Any]]:
        """Optimize patterns for performance - cheap filters first.
        
        Returns patterns sorted by cost:
        1. Equality checks (fastest) - includes envelope_type
        2. Arithmetic comparisons
        3. Regex matches (slowest)
        """
        optimized = []
        
        for pattern_id, config in self.rules.items():
            for pattern in config.get('patterns', []):
                # Separate checks by cost
                equality_checks = {}
                arithmetic_checks = {}
                regex_checks = {}
                envelope_type = None

                for field, value in pattern.items():
                    # Handle envelope_type specially
                    if field == 'envelope_type':
                        envelope_type = value
                    elif self._is_equality_check(value):
                        equality_checks[field] = value
                    elif self._is_arithmetic_check(value):
                        arithmetic_checks[field] = value
                    elif self._is_text_field(field, value):
                        # Text patterns need regex
                        regex_checks[field] = self._compile_text_patterns(value)
                    else:
                        # Default to equality for unknown types
                        equality_checks[field] = value

                optimized.append({
                    'pattern_id': pattern_id,
                    'config': config,
                    'envelope_type': envelope_type,  # Store envelope_type separately for fast check
                    'equality': equality_checks,
                    'arithmetic': arithmetic_checks,
                    'regex': regex_checks
                })
        
        return optimized
    
    def match_envelope(self, envelope: dict) -> dict:
        """Match an envelope against all patterns and return metadata.
        
        Works at ANY stage - CSV import or post-merge.
        
        Args:
            envelope: Envelope data as dictionary
            
        Returns:
            Matched metadata or empty dict if no match
        """
        # Check all patterns
        for pattern_data in self.compiled_patterns:
            # Check envelope type requirement first (cheapest check)
            if not self._check_envelope_type(envelope, pattern_data.get('envelope_type')):
                continue
                
            # Check pattern matching
            if self._matches_pattern(envelope, pattern_data):
                # Return metadata from matched pattern
                config = pattern_data['config']
                result = config.get('metadata', {}).copy()
                result['pattern_applied'] = {
                    'id': pattern_data['pattern_id'],
                    'name': config.get('name'),
                    'reason': config.get('reason')
                }
                return result  # First match wins
        
        return {}  # No match
    
    def _check_envelope_type(self, envelope: dict, required_type: str) -> bool:
        """Check if envelope matches the required structure type.
        
        Args:
            envelope: Envelope data
            required_type: One of 'inbound_only', 'outbound_only', 'both', or None
            
        Returns:
            True if envelope structure matches requirement
        """
        if not required_type:
            return True  # No requirement, any structure matches
        
        has_inbound = envelope.get('inbound_account') is not None
        has_outbound = envelope.get('outbound_account') is not None
        
        if required_type == 'inbound_only':
            # Must have inbound but NOT outbound (one-sided income)
            return has_inbound and not has_outbound
        elif required_type == 'outbound_only':
            # Must have outbound but NOT inbound (one-sided expense)
            return has_outbound and not has_inbound
        elif required_type == 'both':
            # Must have BOTH (matched transfer)
            return has_inbound and has_outbound
        else:
            # Unknown type, be permissive
            return True
    
    def _get_field_value(self, envelope: dict, field: str) -> Any:
        """Get field value from envelope, supporting nested access with dot notation.

        Examples:
            'amount' -> envelope['amount']
            'metadata.quantity' -> envelope['metadata']['quantity']
        """
        if '.' not in field:
            return envelope.get(field)

        # Handle nested field access
        parts = field.split('.')
        value = envelope
        for part in parts:
            if isinstance(value, dict):
                value = value.get(part)
            else:
                return None
            if value is None:
                return None
        return value

    def _matches_pattern(self, envelope: dict, pattern_data: dict) -> bool:
        """Match with optimized order: equality, arithmetic, then regex.

        Short-circuits on first failure for performance.

        Note: This method handles direct field matching for single envelopes.
        For envelope pair patterns with utility functions (used by TransferScorer),
        see the TransferScorer._pattern_matches method.
        """
        # 1. Check equality conditions first (cheapest)
        for field, value in pattern_data.get('equality', {}).items():
            envelope_value = self._get_field_value(envelope, field)
            # Special case: when checking for 0, treat None as 0
            # (for single-sided transactions)
            if value == 0 and envelope_value is None:
                continue  # Pass this check
            if envelope_value != value:
                return False

        # 2. Check arithmetic conditions (medium cost)
        for field, condition in pattern_data.get('arithmetic', {}).items():
            if not self._check_arithmetic(self._get_field_value(envelope, field), condition):
                return False

        # 3. Check regex patterns last (most expensive)
        for field, patterns in pattern_data.get('regex', {}).items():
            field_value = str(self._get_field_value(envelope, field) or '')
            if not any(p.search(field_value) for p in patterns):
                return False

        return True  # All checks passed
    
    def _is_equality_check(self, value: Any) -> bool:
        """Check if this is a simple equality check."""
        # None values, booleans, and simple strings/numbers without operators
        if value is None or isinstance(value, bool):
            return True
        if isinstance(value, (int, float, Decimal)):
            return True
        if isinstance(value, str):
            # Check if it's an arithmetic operator
            arithmetic_ops = ['positive', 'negative', 'zero', '>', '<', '>=', '<=', '!=']
            if any(value.startswith(op) for op in arithmetic_ops) or value in arithmetic_ops:
                return False
            # Check if it contains wildcards
            if '*' in value or '?' in value:
                return False
            # Simple string equality
            return True
        if isinstance(value, list):
            # List of exact values
            return all(self._is_equality_check(v) for v in value)
        return False
    
    def _is_arithmetic_check(self, value: Any) -> bool:
        """Check if this is an arithmetic comparison."""
        if isinstance(value, str):
            arithmetic_patterns = ['positive', 'negative', 'zero']
            if value in arithmetic_patterns:
                return True
            if any(value.startswith(op) for op in ['>', '<', '>=', '<=', '!=']):
                return True
        return False
    
    def _is_text_field(self, field: str, value: Any) -> bool:
        """Determine if a field should be treated as text for pattern matching.
        
        Default to text unless:
        1. It's a known numeric field (amount, balance, quantity, etc.)
        2. The pattern uses numeric operators (>, <, positive, negative, etc.)
        """
        # Known numeric fields
        numeric_fields = {'amount', 'balance', 'quantity', 'units', 'price', 'value', 'total'}
        if field in numeric_fields:
            return False
        
        # Check if the pattern value indicates numeric comparison
        if isinstance(value, (int, float, Decimal)):
            return False
        
        if isinstance(value, str):
            # Check for numeric operators
            if self._is_arithmetic_check(value):
                return False
            # Has wildcards - text pattern
            if '*' in value or '?' in value:
                return True
        
        if isinstance(value, list):
            # If all values are numbers, not a text field
            if all(isinstance(v, (int, float, Decimal)) for v in value):
                return False
            # If any value has wildcards, it's text
            if any(isinstance(v, str) and ('*' in v or '?' in v) for v in value):
                return True
        
        # Default to text if it has wildcard patterns
        if isinstance(value, str) and ('*' in value or '?' in value):
            return True
        
        return False
    
    def _compile_text_patterns(self, patterns) -> List[RePattern]:
        """Compile text patterns to regex, handling wildcards."""
        if not isinstance(patterns, list):
            patterns = [patterns]
        
        compiled = []
        for pattern in patterns:
            if isinstance(pattern, str):
                # Escape special regex characters except our wildcards
                escaped = re.escape(pattern)
                # Convert wildcards to regex
                # * → .* (any characters)
                # ? → . (single character)
                regex_pattern = escaped.replace(r'\*', '.*').replace(r'\?', '.')
                compiled.append(re.compile(regex_pattern, re.IGNORECASE))
        
        return compiled
    
    def _check_arithmetic(self, value: Any, condition: str) -> bool:
        """Check numeric conditions like 'positive', '>100', '5-10', etc.

        Supports:
        - Named conditions: 'positive', 'negative', 'zero', 'zero_or_none'
        - Operators: '>5', '>=5', '<5', '<=5', '!=5', '==5'
        - Ranges: '5-10' (inclusive)
        - Direct values: 5 (exact match)
        - NOT_APPLICABLE: Returns False (pattern doesn't match)
        """
        # Handle NOT_APPLICABLE
        if isinstance(value, NotApplicable):
            return False  # Pattern doesn't match when calculation doesn't apply

        # Special named conditions
        if condition == 'zero_or_none':
            return value is None or value == 0
        if condition == 'positive':
            if value is None:
                return False
            try:
                return float(value) > 0
            except (TypeError, ValueError):
                return False
        if condition == 'negative':
            if value is None:
                return False
            try:
                return float(value) < 0
            except (TypeError, ValueError):
                return False
        if condition == 'zero':
            # Special case: None counts as zero for single-sided transactions
            if value is None:
                return True
            try:
                return float(value) == 0
            except (TypeError, ValueError):
                return False

        # For other conditions, None fails
        if value is None:
            return False

        try:
            num_value = float(value)
        except (TypeError, ValueError):
            return False

        # Handle string conditions with operators
        if isinstance(condition, str):
            condition = condition.strip()

            # Check for range syntax (e.g., '5-10')
            if '-' in condition and not condition.startswith('-'):
                parts = condition.split('-')
                if len(parts) == 2:
                    try:
                        min_val = float(parts[0].strip())
                        max_val = float(parts[1].strip())
                        return min_val <= num_value <= max_val
                    except ValueError:
                        pass

            # Check for operators
            if condition.startswith('>='):
                return num_value >= float(condition[2:].strip())
            elif condition.startswith('<='):
                return num_value <= float(condition[2:].strip())
            elif condition.startswith('!='):
                return num_value != float(condition[2:].strip())
            elif condition.startswith('=='):
                return num_value == float(condition[2:].strip())
            elif condition.startswith('>'):
                return num_value > float(condition[1:].strip())
            elif condition.startswith('<'):
                return num_value < float(condition[1:].strip())
            elif condition.startswith('='):
                return num_value == float(condition[1:].strip())

        # Try direct numeric comparison
        try:
            expected = float(condition) if isinstance(condition, str) else condition
            return num_value == expected
        except (TypeError, ValueError):
            return False
    
    # ========================================================================
    # Dynamic pattern evaluation - delegates to envelope_utilities
    # ========================================================================

    def __getattr__(self, name):
        """Dynamically delegate to envelope_utilities for pattern functions.

        This allows patterns to use any function from envelope_utilities
        without hardcoding them here.
        """
        # Check if the function exists in envelope_utilities
        if hasattr(env_utils, name):
            func = getattr(env_utils, name)
            # Direct delegation - envelope_utilities functions know their own signatures
            return func
        raise AttributeError(f"'{self.__class__.__name__}' object has no attribute '{name}'")


    # All envelope operations are now handled by __getattr__ delegation to envelope_utilities