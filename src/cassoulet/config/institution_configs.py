"""
Institution-specific configuration overrides.

This module provides a registry for institution-specific configuration tuning.
Each institution can override any configuration parameter. Common overrides include:
- Date tolerances (some institutions process faster/slower)
- Confidence thresholds (based on data quality)
- Scoring weights (based on field reliability)
- Amount tolerances (based on precision provided)

Usage:
    # Register your own institution configs at startup
    from cassoulet.config.institution_configs import register_institution_configs

    register_institution_configs({
        'HSBC': {
            'matching': {'date_tolerance_days': 5},
            'scoring': {'high_confidence': 85.0},
        },
    })
"""

import logging
from typing import Dict, Any, List

logger = logging.getLogger(__name__)

# Global registry — starts empty, populated by calling register_institution_configs()
INSTITUTION_CONFIGS: Dict[str, Dict[str, Any]] = {}


def register_institution_configs(configs: Dict[str, Dict[str, Any]]) -> None:
    """
    Register institution-specific configuration overrides.

    Merges into the global registry (later calls override earlier ones
    for the same institution key).

    Args:
        configs: Mapping of institution name to configuration overrides.
    """
    INSTITUTION_CONFIGS.update(configs)
    logger.debug(f"Registered configs for institutions: {list(configs.keys())}")


def get_institution_config(institution: str) -> Dict[str, Any]:
    """
    Get configuration overrides for a specific institution.

    Args:
        institution: Name of the institution

    Returns:
        Dictionary of configuration overrides, or empty dict if not found
    """
    return INSTITUTION_CONFIGS.get(institution, {})


def validate_institution_configs() -> List[str]:
    """
    Validate all institution configurations for consistency.

    Returns:
        List of validation warnings (empty if all valid)
    """
    warnings = []

    for institution, config in INSTITUTION_CONFIGS.items():
        if 'matching' in config:
            date_tol = config['matching'].get('date_tolerance_days')
            if date_tol and (date_tol < 1 or date_tol > 30):
                warnings.append(
                    f"{institution}: Unusual date tolerance {date_tol} days"
                )

        if 'scoring' in config:
            high_conf = config['scoring'].get('high_confidence')
            if high_conf and (high_conf < 70 or high_conf > 95):
                warnings.append(
                    f"{institution}: Unusual high confidence threshold {high_conf}"
                )

            weights = config['scoring'].get('scoring_weights')
            if weights:
                total = sum(weights.values())
                if abs(total - 100.0) > 0.01:
                    warnings.append(
                        f"{institution}: Scoring weights sum to {total}, not 100"
                    )

    return warnings
