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
            
            for envelope in processed_envelopes:
                if envelope.metadata and 'merged_from' in envelope.metadata:
                    # This is a merged envelope - count its sources
                    source_ids = envelope.metadata['merged_from']
                    source_count = len(source_ids)
                    actual_envelope_count += source_count
                    
                    merge_audit.append({
                        'merged_id': envelope.envelope_id,
                        'source_count': source_count,
                        'source_ids': source_ids
                    })
                    
                    # Verify merge_count metadata consistency
                    if envelope.metadata.get('merge_count') != source_count:
                        all_warnings.append(ProcessingWarning(
                            processor_name=self.processor_name,
                            severity='ERROR',
                            message=f"Merge metadata inconsistency in {envelope.envelope_id}",
                            source_transaction=None,
                            details={
                                'envelope_id': envelope.envelope_id,
                                'merge_count': envelope.metadata.get('merge_count'),
                                'actual_sources': source_count
                            }
                        ))
                else:
                    # Unmerged envelope counts as 1
                    actual_envelope_count += 1
            
            # Now check if we have the right total
            if actual_envelope_count != self.stats['input_count']:
                # This is the REAL violation - envelopes were lost or gained
                discrepancy = actual_envelope_count - self.stats['input_count']
                
                all_warnings.append(ProcessingWarning(
                    processor_name=self.processor_name,
                    severity='CRITICAL',
                    message=f"Steel Thread violation: {abs(discrepancy)} envelopes "
                            f"{'added' if discrepancy > 0 else 'lost'}",
                    source_transaction=None,
                    details={
                        'input_count': self.stats['input_count'],
                        'output_count': self.stats['output_count'],
                        'actual_envelope_count': actual_envelope_count,
                        'merge_audit': merge_audit,
                        'discrepancy': discrepancy
                    }
                ))
                
                logger.critical(
                    f"{self.processor_name}: STEEL THREAD VIOLATION - "
                    f"Input: {self.stats['input_count']}, "
                    f"Output represents: {actual_envelope_count} envelopes"
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