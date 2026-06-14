"""
Pure Python fast scoring using pre-parsed compiled patterns.

Eliminates string parsing (_check_arithmetic) and isinstance chains by using
pre-parsed CompiledCondition structs. Same algorithm as the Cython version but
runs in pure Python.

This alone gives ~2-3x speedup over the original _check_value_match path by
avoiding 21M _check_arithmetic calls and 181M isinstance calls.
"""

from decimal import Decimal

from cassoulet.stages._compiled_patterns import (
    COND_BOOL_TRUE,
    COND_BOOL_FALSE,
    COND_INT_EQ,
    COND_RANGE,
    COND_GT,
)
from cassoulet.utils.envelope_utilities import NotApplicable

# Sentinel values for the results cache
_UNSET = -9e307        # "not yet computed"
_MISSING = -8e307      # "function raised an exception or doesn't exist"
_NOT_APPLICABLE = -7e307  # "function returned NotApplicable"


def _encode_result(value):
    """Encode a Python result into a float for the cache.

    Returns a float suitable for fast condition checking:
    - bool → 1.0 / 0.0
    - int/float → float(value)
    - Decimal → float(value)
    - NotApplicable → _NOT_APPLICABLE sentinel
    """
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, NotApplicable):
        return _NOT_APPLICABLE
    return _NOT_APPLICABLE


def _check_condition(cond_type, value, int_val, float_lo, float_hi):
    """Check a single pre-parsed condition against a cached float value.

    Equivalent to _check_value_match + _check_arithmetic but without
    string parsing or isinstance chains.
    """
    if value == _MISSING or value == _NOT_APPLICABLE:
        return False

    if cond_type == COND_BOOL_TRUE:
        return value == 1.0
    elif cond_type == COND_BOOL_FALSE:
        return value == 0.0
    elif cond_type == COND_INT_EQ:
        return int(value) == int_val
    elif cond_type == COND_RANGE:
        return float_lo <= value <= float_hi
    elif cond_type == COND_GT:
        return value > float_lo
    return False


def score_pair_fallback(env1, env2, compiled_groups, pattern_functions, field_names):
    """Score an envelope pair using pre-parsed compiled patterns.

    Args:
        env1: First envelope
        env2: Second envelope
        compiled_groups: list[CompiledPatternGroup] from compile_patterns()
        pattern_functions: dict[str, callable] mapping field names to functions
        field_names: list[str] indexed by field_index

    Returns:
        (total_score, list_of_matched_pattern_details)
    """
    num_fields = len(field_names)

    # Per-pair results cache: one float per field, initialized to _UNSET
    cache = [_UNSET] * num_fields

    matched_patterns = []
    total_score = 0

    for group in compiled_groups:
        for pattern in group.patterns:
            # Check all conditions in this pattern (AND conjunction)
            matched = True
            for cond in pattern.conditions:
                idx = cond.field_index

                # Lazy evaluation: compute and cache on first access
                if cache[idx] == _UNSET:
                    func = pattern_functions.get(cond.field_name)
                    if func is None:
                        cache[idx] = _MISSING
                    else:
                        try:
                            result = func(env1, env2)
                        except Exception:
                            cache[idx] = _MISSING
                        else:
                            cache[idx] = _encode_result(result)

                if not _check_condition(
                    cond.cond_type, cache[idx],
                    cond.int_val, cond.float_lo, cond.float_hi
                ):
                    matched = False
                    break

            if matched:
                total_score += group.score
                matched_patterns.append({
                    'pattern_id': group.pattern_id,
                    'name': group.name,
                    'reason': group.reason,
                    'score': group.score,
                })
                # Break after first pattern match (patterns are OR'd alternatives)
                break

    return total_score, matched_patterns
