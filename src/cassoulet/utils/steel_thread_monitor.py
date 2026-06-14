"""
Steel Thread Monitor

Runtime monitoring for Steel Thread violations.
Provides circuit breaker functionality and forensic reporting.
"""

import logging
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field
from datetime import datetime
from collections import defaultdict
from contextlib import contextmanager

from beancount.core.data import Transaction
from cassoulet.base.exceptions import ProcessingResult, ProcessingWarning

logger = logging.getLogger(__name__)


class CriticalViolationError(Exception):
    """Raised when critical violations exceed threshold."""
    pass


@dataclass
class ViolationRecord:
    """Record of a Steel Thread violation."""
    timestamp: datetime
    processor: str
    severity: str
    message: str
    details: Dict[str, Any] = field(default_factory=dict)


class SteelThreadMonitor:
    """
    Runtime monitoring for Steel Thread violations.
    
    Tracks violations, provides circuit breaker functionality,
    and generates forensic reports.
    """
    
    def __init__(
        self,
        critical_threshold: int = 5,
        error_threshold: int = 20,
        enable_circuit_breaker: bool = True,
        enforce_safe_envelopes: bool = True
    ):
        """
        Initialize the monitor.
        
        Args:
            critical_threshold: Number of CRITICAL violations before circuit breaker
            error_threshold: Number of ERROR violations before circuit breaker
            enable_circuit_breaker: Whether to raise exception on threshold breach
            enforce_safe_envelopes: Whether to enforce safe envelope manipulation (Issue #118)
        """
        self.critical_threshold = critical_threshold
        self.error_threshold = error_threshold
        self.enable_circuit_breaker = enable_circuit_breaker
        self.enforce_safe_envelopes = enforce_safe_envelopes
        
        # Violation tracking
        self.violations: List[ViolationRecord] = []
        self.violation_counts = defaultdict(int)
        self.processor_violations = defaultdict(list)
        
        # Statistics
        self.total_transactions_processed = 0
        self.total_postings_processed = 0
        self.empty_transaction_count = 0
        self.data_loss_events = 0
        
    def check_result(self, result: ProcessingResult) -> None:
        """
        Check a processing result for violations.
        
        Args:
            result: ProcessingResult to check
            
        Raises:
            CriticalViolationError: If circuit breaker threshold is breached
        """
        processor = result.metadata.get('processor_name', 'Unknown')
        
        # Update statistics
        self.total_transactions_processed += result.input_count
        
        # Check for data loss
        if result.input_count != result.output_count:
            lost = result.input_count - result.output_count
            if lost > 0 and not self._has_explanation(result, lost):
                self.data_loss_events += 1
                self._record_violation(
                    processor=processor,
                    severity='CRITICAL',
                    message=f"DATA LOSS: {lost} transactions lost",
                    details={
                        'input_count': result.input_count,
                        'output_count': result.output_count,
                        'lost': lost
                    }
                )
        
        # Check posting statistics if available
        posting_stats = result.metadata.get('posting_stats', {})
        if posting_stats:
            self.total_postings_processed += posting_stats.get('input_postings', 0)
            empty_txns = posting_stats.get('empty_transactions', 0)
            if empty_txns > 0:
                self.empty_transaction_count += empty_txns
                self._record_violation(
                    processor=processor,
                    severity='CRITICAL',
                    message=f"EMPTY TRANSACTIONS: {empty_txns} transactions have no postings (Issue #91)",
                    details=posting_stats
                )
        
        # Process warnings
        for warning in result.warnings:
            self.violation_counts[warning.severity] += 1
            self.processor_violations[processor].append(warning)
            
            if warning.severity == 'CRITICAL':
                self._record_violation(
                    processor=processor,
                    severity='CRITICAL',
                    message=warning.message,
                    details=warning.details
                )
                self._alert(f"CRITICAL: {processor}: {warning.message}")
            
            elif warning.severity == 'ERROR':
                self._record_violation(
                    processor=processor,
                    severity='ERROR',
                    message=warning.message,
                    details=warning.details
                )
        
        # Check for 3-way merge warnings (Issue #87)
        for warning in result.warnings:
            if 'multi-way' in warning.message.lower() or '3-way' in warning.message.lower():
                self._record_violation(
                    processor=processor,
                    severity='WARNING',
                    message=f"MULTI-WAY MERGE DETECTED: {warning.message}",
                    details=warning.details
                )
                logger.warning(f"Issue #87 prevention: {warning.message}")
        
        # Circuit breaker check
        if self.enable_circuit_breaker:
            critical_count = self.violation_counts.get('CRITICAL', 0)
            error_count = self.violation_counts.get('ERROR', 0)
            
            if critical_count > self.critical_threshold:
                raise CriticalViolationError(
                    f"Steel Thread circuit breaker triggered: {critical_count} CRITICAL violations "
                    f"(threshold: {self.critical_threshold})"
                )
            
            if error_count >= self.error_threshold:
                raise CriticalViolationError(
                    f"Steel Thread circuit breaker triggered: {error_count} ERROR violations "
                    f"(threshold: {self.error_threshold})"
                )
    
    def _record_violation(
        self,
        processor: str,
        severity: str,
        message: str,
        details: Dict[str, Any]
    ) -> None:
        """Record a violation."""
        violation = ViolationRecord(
            timestamp=datetime.now(),
            processor=processor,
            severity=severity,
            message=message,
            details=details
        )
        self.violations.append(violation)
    
    def _has_explanation(self, result: ProcessingResult, lost: int) -> bool:
        """Check if warnings explain the data loss."""
        # Look for merge warnings that explain reduction
        merge_count = sum(
            1 for w in result.warnings
            if 'merge' in w.message.lower() and w.severity == 'INFO'
        )
        return merge_count >= lost
    
    def _alert(self, message: str) -> None:
        """Send alert (log, email, etc.)."""
        logger.critical(f"STEEL THREAD VIOLATION: {message}")
    
    def generate_forensic_report(self) -> str:
        """
        Generate a detailed forensic analysis report.
        
        Returns:
            Formatted forensic report string
        """
        report = []
        report.append("=" * 80)
        report.append("STEEL THREAD FORENSIC ANALYSIS REPORT")
        report.append("=" * 80)
        report.append(f"Generated: {datetime.now().isoformat()}")
        report.append("")
        
        # Executive Summary
        report.append("EXECUTIVE SUMMARY")
        report.append("-" * 40)
        report.append(f"Total Transactions Processed: {self.total_transactions_processed}")
        report.append(f"Total Postings Processed: {self.total_postings_processed}")
        report.append(f"Data Loss Events: {self.data_loss_events}")
        report.append(f"Empty Transaction Count: {self.empty_transaction_count}")
        report.append("")
        
        # Violation Summary
        report.append("VIOLATION SUMMARY")
        report.append("-" * 40)
        for severity in ['CRITICAL', 'ERROR', 'WARNING', 'INFO']:
            count = self.violation_counts.get(severity, 0)
            if count > 0:
                report.append(f"{severity:8}: {count:5} violations")
        report.append("")
        
        # Critical Violations Detail
        critical_violations = [v for v in self.violations if v.severity == 'CRITICAL']
        if critical_violations:
            report.append("CRITICAL VIOLATIONS (Immediate Action Required)")
            report.append("-" * 40)
            for i, violation in enumerate(critical_violations, 1):
                report.append(f"\n{i}. [{violation.timestamp.strftime('%H:%M:%S')}] {violation.processor}")
                report.append(f"   {violation.message}")
                if violation.details:
                    for key, value in violation.details.items():
                        report.append(f"   - {key}: {value}")
            report.append("")
        
        # Issue-Specific Checks
        report.append("ISSUE-SPECIFIC VIOLATIONS")
        report.append("-" * 40)
        
        # Issue #91 Check (Empty Transactions)
        issue_91_violations = [
            v for v in self.violations
            if 'empty transaction' in v.message.lower() or 'no posting' in v.message.lower()
        ]
        if issue_91_violations:
            report.append(f"Issue #91 (Empty Transactions): {len(issue_91_violations)} violations")
            for v in issue_91_violations[:3]:  # Show first 3
                report.append(f"  - {v.processor}: {v.message}")
        else:
            report.append("Issue #91 (Empty Transactions): ✓ No violations")
        
        # Issue #87 Check (3-way merges)
        issue_87_violations = [
            v for v in self.violations
            if 'multi-way' in v.message.lower() or '3-way' in v.message.lower()
        ]
        if issue_87_violations:
            report.append(f"Issue #87 (Multi-way Merges): {len(issue_87_violations)} violations")
            for v in issue_87_violations[:3]:  # Show first 3
                report.append(f"  - {v.processor}: {v.message}")
        else:
            report.append("Issue #87 (Multi-way Merges): ✓ No violations")
        
        # Issue #79 Check (PostingWriter)
        posting_resolver_violations = [
            v for v in self.violations
            if v.processor == 'PostingWriter'
        ]
        if posting_resolver_violations:
            report.append(f"Issue #79 (PostingWriter): {len(posting_resolver_violations)} violations")
            for v in posting_resolver_violations[:3]:  # Show first 3
                report.append(f"  - {v.severity}: {v.message}")
        else:
            report.append("Issue #79 (PostingWriter): ✓ No violations")
        
        # Issue #118 Check (Envelope Field Loss)
        envelope_violations = [
            v for v in self.violations
            if 'Issue #118' in v.message or 'envelope instantiation' in v.message.lower()
        ]
        if envelope_violations:
            report.append(f"Issue #118 (Envelope Field Loss): {len(envelope_violations)} violations")
            for v in envelope_violations[:3]:  # Show first 3
                report.append(f"  - {v.processor}: {v.message}")
        else:
            report.append("Issue #118 (Envelope Field Loss): ✓ No violations")
        report.append("")
        
        # Processor Analysis
        report.append("PROCESSOR ANALYSIS")
        report.append("-" * 40)
        for processor, warnings in self.processor_violations.items():
            if warnings:
                critical = sum(1 for w in warnings if w.severity == 'CRITICAL')
                error = sum(1 for w in warnings if w.severity == 'ERROR')
                warning = sum(1 for w in warnings if w.severity == 'WARNING')
                report.append(f"\n{processor}:")
                report.append(f"  CRITICAL: {critical}, ERROR: {error}, WARNING: {warning}")
                
                # Show most severe warning
                most_severe = next(
                    (w for w in warnings if w.severity == 'CRITICAL'),
                    next((w for w in warnings if w.severity == 'ERROR'), None)
                )
                if most_severe:
                    report.append(f"  Most severe: {most_severe.message[:80]}")
        
        # Recommendations
        report.append("\nRECOMMENDATIONS")
        report.append("-" * 40)
        
        if self.data_loss_events > 0:
            report.append("1. CRITICAL: Data loss detected. Review processor implementations.")
        
        if self.empty_transaction_count > 0:
            report.append("2. CRITICAL: Empty transactions found (Issue #91). Check PostingWriter.")
        
        if issue_87_violations:
            report.append("3. WARNING: Multi-way merges detected. Review TransferMatcher logic.")
        
        if self.violation_counts.get('CRITICAL', 0) > 0:
            report.append("4. Review and fix all CRITICAL violations before proceeding.")
        
        report.append("\n" + "=" * 80)
        report.append("END OF FORENSIC REPORT")
        report.append("=" * 80)
        
        return "\n".join(report)
    
    def reset(self) -> None:
        """Reset the monitor for a new run."""
        self.violations.clear()
        self.violation_counts.clear()
        self.processor_violations.clear()
        self.total_transactions_processed = 0
        self.total_postings_processed = 0
        self.empty_transaction_count = 0
        self.data_loss_events = 0
    
    def has_violations(self) -> bool:
        """
        Check if any violations have been recorded.
        
        Returns:
            True if there are any violations
        """
        return len(self.violations) > 0 or self.violation_counts['CRITICAL'] > 0 or self.violation_counts['ERROR'] > 0
    
    def validate_transaction_integrity(self, transaction: Transaction) -> List[ProcessingWarning]:
        """
        Validate that a transaction has required structure.
        
        Args:
            transaction: Transaction to validate
            
        Returns:
            List of warnings if validation fails
        """
        warnings = []
        
        # Check for postings
        if not transaction.postings:
            warnings.append(ProcessingWarning(
                processor_name="SteelThreadMonitor",
                severity='CRITICAL',
                message="Transaction has no postings",
                source_transaction=transaction,
                details={
                    'date': str(transaction.date),
                    'narration': transaction.narration[:50] if transaction.narration else 'None',
                    'transaction_id': transaction.meta.get('transaction_id', 'unknown') if transaction.meta else 'unknown'
                }
            ))
            self.empty_transaction_count += 1
        elif len(transaction.postings) == 1:
            warnings.append(ProcessingWarning(
                processor_name="SteelThreadMonitor",
                severity='ERROR',
                message="Transaction has only one posting",
                source_transaction=transaction,
                details={
                    'account': transaction.postings[0].account,
                    'amount': str(transaction.postings[0].units) if transaction.postings[0].units else 'None'
                }
            ))
        
        # Record warnings
        for warning in warnings:
            self._record_violation(
                processor="SteelThreadMonitor",
                severity=warning.severity,
                message=warning.message,
                details=warning.details
            )
        
        return warnings
    
    @contextmanager
    def monitor_processor(self, processor):
        """
        Context manager for monitoring processor execution.
        
        Performs static analysis on processor code and monitors execution.
        
        Args:
            processor: The processor being monitored
            
        Yields:
            The processor for execution
        """
        processor_name = getattr(processor, 'processor_name', processor.__class__.__name__)
        
        # Phase 1: Static analysis check for unsafe patterns (Issue #118)
        unsafe_patterns = self.check_for_unsafe_envelope_instantiation(processor.__class__)
        if unsafe_patterns:
            error_msg = (
                f"CRITICAL: {processor_name} contains {len(unsafe_patterns)} "
                f"unsafe TransactionEnvelope instantiation patterns that will cause field loss (Issue #118). "
                f"Use enhance_envelope() or create_envelope() from envelope_utilities instead."
            )
            
            # Log the violations
            logger.critical(error_msg)
            for pattern in unsafe_patterns[:3]:  # Log first 3
                logger.critical(f"  - {pattern}")
            
            # Record as critical violation
            self._record_violation(
                processor=processor_name,
                severity='CRITICAL',
                message=error_msg,
                details={
                    'unsafe_patterns': unsafe_patterns,
                    'issue': '#118'
                }
            )
            
            # Enforce: Refuse to proceed with unsafe code
            if self.enforce_safe_envelopes and self.enable_circuit_breaker:
                raise CriticalViolationError(
                    f"Steel Thread enforcement: {processor_name} contains unsafe envelope instantiation. "
                    f"Fix the code before proceeding. Details:\n" + "\n".join(unsafe_patterns[:3])
                )
        
        try:
            # Log processor start
            logger.debug(f"Steel Thread Monitor: Starting {processor_name}")
            yield processor
            # Log processor completion
            logger.debug(f"Steel Thread Monitor: Completed {processor_name}")
        except Exception as e:
            # Record exception as critical violation
            self._record_violation(
                processor=processor_name,
                severity='CRITICAL',
                message=f"Processor raised exception: {str(e)}",
                details={'exception': str(e), 'type': type(e).__name__}
            )
            raise
        finally:
            # Check if circuit breaker should trip
            if self.enable_circuit_breaker:
                critical_count = self.violation_counts.get('CRITICAL', 0)
                error_count = self.violation_counts.get('ERROR', 0)
                
                if critical_count >= self.critical_threshold:
                    logger.critical(f"Circuit breaker tripped: {critical_count} CRITICAL violations")
                    if self.enable_circuit_breaker:
                        raise CriticalViolationError(
                            f"Critical violation threshold exceeded: {critical_count}/{self.critical_threshold}"
                        )
                elif error_count >= self.error_threshold:
                    logger.error(f"Circuit breaker warning: {error_count} ERROR violations")
    
    @property
    def violation_count(self) -> int:
        """Total number of violations recorded."""
        return len(self.violations)
    
    def check_for_unsafe_envelope_instantiation(self, processor_module) -> List[str]:
        """
        Phase 1: Static check for unsafe TransactionEnvelope instantiation.
        
        Scans processor source code for direct instantiation patterns that
        bypass safe envelope manipulation methods.
        
        Args:
            processor_module: The processor module to check
            
        Returns:
            List of warning messages for unsafe patterns found
        """
        import inspect
        import re
        
        warnings = []
        
        try:
            source = inspect.getsource(processor_module)
            
            # Pattern to find TransactionEnvelope instantiation
            # Excludes safe patterns like replace() and our safe methods
            unsafe_pattern = r'TransactionEnvelope\s*\([^)]*\)'
            safe_patterns = [
                r'replace\s*\(',
                r'enhance_envelope',
                r'create_envelope',
                r'TransactionEnvelope\.create\(',
            ]
            
            # Find all matches
            matches = re.finditer(unsafe_pattern, source)
            
            for match in matches:
                context = source[max(0, match.start()-50):min(len(source), match.end()+50)]
                
                # Check if it's actually unsafe
                is_safe = any(re.search(pattern, context) for pattern in safe_patterns)
                
                if not is_safe:
                    line_num = source[:match.start()].count('\n') + 1
                    
                    # Record as violation
                    self._record_violation(
                        processor=processor_module.__name__ if hasattr(processor_module, '__name__') else str(processor_module),
                        severity='WARNING',
                        message=f"Line {line_num}: Direct TransactionEnvelope instantiation detected (Issue #118)",
                        details={
                            'line_number': line_num,
                            'context': context.strip(),
                            'recommendation': 'Use enhance_envelope() or create_envelope() from envelope_utilities instead'
                        }
                    )
                    
                    warnings.append(
                        f"Line {line_num}: Direct TransactionEnvelope instantiation detected. "
                        f"Use enhance_envelope() or create_envelope() from envelope_utilities instead."
                    )
        
        except Exception as e:
            logger.debug(f"Could not perform static analysis: {e}")
        
        return warnings