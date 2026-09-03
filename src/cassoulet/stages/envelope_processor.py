"""
Base Envelope Processor

This module provides the abstract base class for all envelope-native processors,
enforcing the Steel Thread Architecture for data integrity and error handling.

Part of the deferred posting architecture (Issue #79).
"""

import logging
from abc import ABC, abstractmethod
from typing import List, Tuple, Dict, Any, Optional
from datetime import datetime
from cassoulet.stages.envelope import Envelope
from cassoulet.base.exceptions import ProcessingWarning

logger = logging.getLogger(__name__)


def absorbed_source_ids(envelope) -> list:
    """Every envelope id this one records as lineage, whatever key it used.

    Merging and reconciliation are the same event for counting purposes - one
    envelope stands in for another - but they were written under different keys.
    Aggregation writes 'merged_from'; reconciliation keeps the manual entry and
    writes 'reconciled_with' naming the CSV row it absorbed. The integrity check
    only knew the first, so all 63 reconciliations in the reference ledger looked
    like envelopes that had simply vanished, and TransferMerger reported a
    CRITICAL on every run for doing exactly what it was supposed to do.

    Returns ids, not a count: the caller decides whether a source still present
    in the output was absorbed at all - see _represented_count.
    """
    metadata = envelope.metadata or {}
    ids = list(metadata.get('merged_from') or ())
    reconciled = metadata.get('reconciled_with')
    if reconciled:
        ids.extend([reconciled] if isinstance(reconciled, str) else reconciled)
    return ids


class EnvelopeProcessor(ABC):
    """
    Abstract base class for all envelope-native processors.
    
    Enforces the Steel Thread Architecture:
    1. Zero Silent Failures - All errors become warnings
    2. Complete Data Integrity - Input/output counting
    3. Total Operational Transparency - Full audit trail
    
    All envelope processors MUST inherit from this class.
    """
    
    def __init__(self, processor_name: Optional[str] = None):
        """
        Initialize the base envelope processor.
        
        Args:
            processor_name: Name of the processor (defaults to class name)
        """
        self.processor_name = processor_name or self.__class__.__name__
        self.stats = {
            'input_count': 0,
            'output_count': 0,
            'warnings_generated': 0,
            'errors_caught': 0
        }
    
    @staticmethod
    def _represented_count(envelopes: List[Envelope]) -> int:
        """How many ORIGINAL envelopes this list stands for.

        A merged envelope stands for the ones it consumed, so it counts as the
        length of its 'merged_from' list rather than as one. Applied to both
        sides of the integrity check so that a merge recorded by an earlier
        stage cancels out and only this call's own merging can change the total.
        """
        present = {e.envelope_id for e in envelopes}
        total = 0
        for envelope in envelopes:
            total += 1
            # Count only the sources this envelope actually ABSORBED - ones no
            # longer in the list. 'merged_from' is used for two different things
            # and only one of them removes anything:
            #
            #   a true merge, where one envelope consumes another and only the
            #   survivor remains; and
            #
            #   a matched PAIR that both survive, which is how a transfer routed
            #   through Assets:Transfer:InTransit is represented - the bank leg
            #   pays into the clearing account and the broker leg draws it out,
            #   so both postings are needed and each records the other as a
            #   source. 71 such pairs exist in the reference ledger.
            #
            # Counting the second kind as an absorption inflated the total by 79
            # and produced a CRITICAL on every run. It never indicated lost
            # money: the in-transit account nets to zero.
            for source_id in absorbed_source_ids(envelope):
                if source_id not in present:
                    total += 1
        return total

    def process_envelopes(
        self,
        envelopes: List[Envelope]
    ) -> Tuple[List[Envelope], List[ProcessingWarning]]:
        """
        Process a list of envelopes with Steel Thread guarantees.
        
        This method wraps the internal processing logic with:
        - Data integrity checks (input/output counting)
        - Exception handling (no silent failures)
        - Audit trail generation
        
        Args:
            envelopes: List of transaction envelopes to process
            
        Returns:
            Tuple of (processed_envelopes, warnings)
        """
        # Record input count
        self.stats['input_count'] = len(envelopes)
        # And what that input REPRESENTS, applying the same merge accounting used
        # on the output below. Comparing represented-output against raw-input was
        # wrong: 'merged_from' is permanent, so a merge performed once upstream
        # was re-counted by every processor downstream of it. That produced four
        # identical CRITICAL violations on every run of the reference ledger -
        # 911 envelopes carrying merged_from, and a reported discrepancy of
        # exactly 911 - from processors that had added nothing at all.
        # Represented-in against represented-out cancels prior merges on both
        # sides, so only merging done by THIS call can move the number.
        represented_input = self._represented_count(envelopes)
        
        # Initialize warnings list
        all_warnings = []
        
        try:
            # Call the internal processing method
            processed_envelopes, warnings = self._process_internal(envelopes)
            
            # Validate return type
            if not isinstance(processed_envelopes, list):
                raise TypeError(
                    f"{self.processor_name}._process_internal must return list of envelopes, "
                    f"got {type(processed_envelopes)}"
                )
            
            if not isinstance(warnings, list):
                raise TypeError(
                    f"{self.processor_name}._process_internal must return list of warnings, "
                    f"got {type(warnings)}"
                )
            
            # Record output count
            self.stats['output_count'] = len(processed_envelopes)
            self.stats['warnings_generated'] = len(warnings)
            
            # Check data integrity - accounting for legitimate merging
            # Look through merged envelopes to count actual source envelopes
            actual_envelope_count = 0
            merge_audit = []
            
            output_ids = {e.envelope_id for e in processed_envelopes}
            for envelope in processed_envelopes:
                source_ids = absorbed_source_ids(envelope)
                if source_ids:
                    source_count = len(source_ids)
                    # Count the envelope itself, plus only those sources it
                    # actually ABSORBED - ones no longer in the output. Sources
                    # still present are a matched pair rather than a merge, and
                    # they count themselves. Must match _represented_count above
                    # exactly, or the two sides of the check disagree by the
                    # number of surviving pairs.
                    actual_envelope_count += 1 + sum(
                        1 for sid in source_ids if sid not in output_ids
                    )
                    
                    merge_audit.append({
                        'merged_id': envelope.envelope_id,
                        'source_count': source_count,
                        'source_ids': source_ids
                    })
                    
                    # Verify merge_count metadata consistency.
                    #
                    # AGGREGATION ONLY. 'merge_count' is written by the
                    # aggregation path alongside 'merged_from'; reconciliation
                    # keeps the manual entry and writes 'reconciled_with' without
                    # one, so applying this check to a reconciled envelope
                    # compares a real count against None and always fails. Doing
                    # that raised 315 ERRORs on the reference ledger - the 63
                    # reconciliations, re-checked by each of five processors -
                    # for envelopes that were entirely correct.
                    merged_from = (envelope.metadata or {}).get('merged_from')
                    if merged_from and envelope.metadata.get('merge_count') != len(merged_from):
                        all_warnings.append(ProcessingWarning(
                            processor_name=self.processor_name,
                            severity='ERROR',
                            message=f"Merge metadata inconsistency in {envelope.envelope_id}",
                            source_transaction=None,
                            details={
                                'envelope_id': envelope.envelope_id,
                                'merge_count': envelope.metadata.get('merge_count'),
                                'actual_sources': len(merged_from)
                            }
                        ))
                else:
                    # Unmerged envelope counts as 1
                    actual_envelope_count += 1
            
            # Now check if we have the right total
            if actual_envelope_count != represented_input:
                # This is the REAL violation - envelopes were lost or gained
                discrepancy = actual_envelope_count - represented_input
                
                all_warnings.append(ProcessingWarning(
                    processor_name=self.processor_name,
                    severity='CRITICAL',
                    message=f"Steel Thread violation: {abs(discrepancy)} envelopes "
                            f"{'added' if discrepancy > 0 else 'lost'}",
                    source_transaction=None,
                    details={
                        'input_count': self.stats['input_count'],
                        'represented_input': represented_input,
                        'output_count': self.stats['output_count'],
                        'actual_envelope_count': actual_envelope_count,
                        'merge_audit': merge_audit,
                        'discrepancy': discrepancy
                    }
                ))
                
                logger.critical(
                    f"{self.processor_name}: STEEL THREAD VIOLATION - "
                    f"Input: {self.stats['input_count']} envelopes representing "
                    f"{represented_input}, output represents {actual_envelope_count}"
                )
            elif self.stats['input_count'] != self.stats['output_count']:
                # Count is different but accounted for by merging - this is OK
                reduction = self.stats['input_count'] - self.stats['output_count']
                logger.info(
                    f"{self.processor_name}: Legitimate reduction through merging - "
                    f"Input: {self.stats['input_count']}, Output: {self.stats['output_count']} "
                    f"(merged {reduction} envelopes)"
                )
            
            # Add processor stats to all envelopes
            for envelope in processed_envelopes:
                envelope.add_history(
                    state=envelope.state,
                    component=self.processor_name,
                    action="Processed",
                    details={
                        'stats': self.stats.copy(),
                        'timestamp': datetime.now().isoformat()
                    }
                )
            
            # Combine warnings
            all_warnings.extend(warnings)
            
            return processed_envelopes, all_warnings
            
        except Exception as e:
            # NO SILENT FAILURES - Convert exception to warning
            # Initialize stats if not present (defensive programming)
            if not hasattr(self, 'stats'):
                self.stats = {}
            if 'errors_caught' not in self.stats:
                self.stats['errors_caught'] = 0
            self.stats['errors_caught'] += 1
            
            error_warning = ProcessingWarning(
                processor_name=self.processor_name,
                severity='CRITICAL',
                message=f"Processor failed with exception: {str(e)}",
                source_transaction=None,
                details={
                    'exception_type': type(e).__name__,
                    'exception_message': str(e),
                    'input_count': self.stats['input_count']
                }
            )
            
            all_warnings.append(error_warning)
            
            logger.error(
                f"{self.processor_name}: Failed with exception: {e}",
                exc_info=True
            )
            
            # Return input unchanged on failure
            self.stats['output_count'] = len(envelopes)
            return envelopes, all_warnings
    
    @abstractmethod
    def _process_internal(
        self,
        envelopes: List[Envelope]
    ) -> Tuple[List[Envelope], List[ProcessingWarning]]:
        """
        Internal processing logic to be implemented by subclasses.
        
        This method must:
        1. Process the envelopes according to processor logic
        2. Generate appropriate warnings for any issues
        3. Return the same number of envelopes (modified or unmodified)
        
        Args:
            envelopes: List of transaction envelopes to process
            
        Returns:
            Tuple of (processed_envelopes, warnings)
            
        Raises:
            Any exception will be caught by process_envelopes and converted to a warning
        """
        pass
    
    def get_stats(self) -> Dict[str, Any]:
        """
        Get processing statistics.
        
        Returns:
            Dictionary of processing statistics
        """
        return self.stats.copy()
    
    def reset_stats(self):
        """Reset processing statistics."""
        self.stats = {
            'input_count': 0,
            'output_count': 0,
            'warnings_generated': 0,
            'errors_caught': 0
        }
    
    # ===== SAFE ENVELOPE MANIPULATION =====
    # Issue #118: Prevent field loss by using safe manipulation methods
    #
    # IMPORTANT: Use the utility functions from cassoulet.utils.envelope_utilities:
    #   - enhance_envelope(): For modifying existing envelopes
    #   - create_envelope(): For creating new envelopes
    #   - validate_no_field_loss(): For validating field preservation
    #
    # These functions provide safe, auditable envelope manipulation with full
    # field preservation and history tracking.