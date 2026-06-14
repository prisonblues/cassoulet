"""
Pre-parse transfer scoring pattern conditions into typed structs.

Transforms the declarative TRANSFER_SCORING_PATTERNS dict (string-based conditions
evaluated dynamically) into typed CompiledCondition objects that can be evaluated
without string parsing, isinstance chains, or Python dispatch overhead.

Used by both the Cython fast path (_fast_scoring.pyx) and the pure Python
fallback (_fast_scoring_fallback.py).
"""

from dataclasses import dataclass, field

# Condition type constants (used as integer discriminants in tight loops)
COND_BOOL_TRUE = 0
COND_BOOL_FALSE = 1
COND_INT_EQ = 2
COND_RANGE = 3
COND_GT = 4


@dataclass(slots=True)
class CompiledCondition:
    """A single pre-parsed condition within a pattern."""
    field_name: str
    field_index: int      # Index into results cache array
    cond_type: int        # COND_* constant
    int_val: int = 0      # For COND_INT_EQ
    float_lo: float = 0.0  # For COND_RANGE (min) or COND_GT (threshold)
    float_hi: float = 0.0  # For COND_RANGE (max)


@dataclass(slots=True)
class CompiledPattern:
    """A single AND-conjunction of conditions."""
    conditions: list = field(default_factory=list)  # list[CompiledCondition]


@dataclass(slots=True)
class CompiledPatternGroup:
    """A pattern group: OR of patterns, each an AND of conditions."""
    pattern_id: str
    name: str
    reason: str
    score: int
    patterns: list = field(default_factory=list)  # list[CompiledPattern]


def _parse_condition(field_name: str, field_index: int, expected) -> CompiledCondition:
    """Parse a single expected value into a CompiledCondition.

    Supports exactly the condition types used in TRANSFER_SCORING_PATTERNS:
    - bool: True/False
    - int: exact integer match (0, 1, 4)
    - str ranges: '0-39', '0.001-0.01'
    - str gt: '>10', '>4'

    Raises ValueError for unrecognized condition formats.
    """
    if isinstance(expected, bool):
        return CompiledCondition(
            field_name=field_name,
            field_index=field_index,
            cond_type=COND_BOOL_TRUE if expected else COND_BOOL_FALSE,
        )

    if isinstance(expected, int):
        return CompiledCondition(
            field_name=field_name,
            field_index=field_index,
            cond_type=COND_INT_EQ,
            int_val=expected,
        )

    if isinstance(expected, str):
        if expected.startswith('>'):
            threshold = float(expected[1:])
            return CompiledCondition(
                field_name=field_name,
                field_index=field_index,
                cond_type=COND_GT,
                float_lo=threshold,
            )

        if '-' in expected and not expected.startswith('-'):
            parts = expected.split('-', 1)
            if len(parts) == 2:
                lo = float(parts[0])
                hi = float(parts[1])
                return CompiledCondition(
                    field_name=field_name,
                    field_index=field_index,
                    cond_type=COND_RANGE,
                    float_lo=lo,
                    float_hi=hi,
                )

    raise ValueError(
        f"Unrecognized condition for field '{field_name}': {expected!r} "
        f"(type={type(expected).__name__}). Only bool, int, '>N', and 'lo-hi' "
        f"range formats are supported."
    )


def compile_patterns(patterns_config: dict) -> tuple:
    """Compile TRANSFER_SCORING_PATTERNS into typed structs.

    Args:
        patterns_config: The TRANSFER_SCORING_PATTERNS dict.

    Returns:
        (compiled_groups, field_index_map) where:
        - compiled_groups: list[CompiledPatternGroup]
        - field_index_map: dict[str, int] mapping field names to 0-based indices
    """
    # First pass: collect all unique field names and assign indices
    field_names: set[str] = set()
    for pattern_config in patterns_config.values():
        for pattern in pattern_config.get('patterns', []):
            field_names.update(pattern.keys())

    field_index_map = {name: i for i, name in enumerate(sorted(field_names))}

    # Second pass: compile each pattern group
    compiled_groups = []
    for pattern_id, pattern_config in patterns_config.items():
        score = pattern_config.get('metadata', {}).get('score', 0)
        group = CompiledPatternGroup(
            pattern_id=pattern_id,
            name=pattern_config.get('name', pattern_id),
            reason=pattern_config.get('reason', ''),
            score=score,
            patterns=[],
        )

        for pattern in pattern_config.get('patterns', []):
            compiled = CompiledPattern(conditions=[])
            for field_name, expected in pattern.items():
                idx = field_index_map[field_name]
                cond = _parse_condition(field_name, idx, expected)
                compiled.conditions.append(cond)
            group.patterns.append(compiled)

        compiled_groups.append(group)

    return compiled_groups, field_index_map
