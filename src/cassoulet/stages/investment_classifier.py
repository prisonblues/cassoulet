"""
Investment Transaction Classifier

Classifies transactions using pattern matching with envelope utility functions.
"""

import logging
from typing import List, Tuple, Dict, Any

from cassoulet.stages.envelope_processor import EnvelopeProcessor
from cassoulet.stages.envelope import Envelope
from cassoulet.base.exceptions import ProcessingWarning
from cassoulet.utils.envelope_utilities import (
    enhance_envelope,
    match_pattern_single
)
from cassoulet.config.transaction_classification_patterns import TRANSACTION_CLASSIFICATION_PATTERNS

logger = logging.getLogger(__name__)


class InvestmentClassifier(EnvelopeProcessor):
    """
    Classifies transactions using pattern matching with utility functions.

    Uses the same pattern matching approach as TransferScorer but for
    single envelopes rather than pairs.
    """

    def __init__(self):
        """Initialize with classification patterns."""
        super().__init__(processor_name="InvestmentClassifier")

    def _process_internal(
        self,
        envelopes: List[Envelope]
    ) -> Tuple[List[Envelope], List[ProcessingWarning]]:
        """Apply classification patterns to envelopes."""
        warnings = []
        processed_envelopes = []
        classified_count = 0

        for envelope in envelopes:
            # Skip if already classified
            if envelope.transaction_type:
                processed_envelopes.append(envelope)
                continue

            # Apply patterns
            tx_type = self._classify_envelope(envelope)

            if tx_type:
                envelope = enhance_envelope(
                    envelope,
                    reason=f"Classified as {tx_type}",
                    transaction_type=tx_type
                )
                classified_count += 1

                logger.debug(
                    f"Classified {envelope.envelope_id} as {tx_type}"
                )

            processed_envelopes.append(envelope)

        logger.info(
            f"InvestmentClassifier: Classified {classified_count} transactions "
            f"out of {len(processed_envelopes)} envelopes"
        )

        return processed_envelopes, warnings

    def _classify_envelope(self, envelope: Envelope) -> str:
        """
        Classify a single envelope using patterns.

        Returns transaction_type or None.
        """
        # Check each pattern configuration
        for pattern_id, pattern_config in TRANSACTION_CLASSIFICATION_PATTERNS.items():
            # Check each pattern variant
            for pattern in pattern_config.get('patterns', []):
                if match_pattern_single(envelope, pattern):
                    # Extract transaction type from metadata
                    tx_type = pattern_config.get('metadata', {}).get('transaction_type')
                    if tx_type:
                        logger.debug(
                            f"Pattern '{pattern_config.get('name', pattern_id)}' matched: {tx_type}"
                        )
                        return tx_type
                    break  # First match wins within a pattern group

        return None