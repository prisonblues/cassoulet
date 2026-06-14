"""
Smart Logging System - Phase-Aware Logging for v4 Architecture

This module implements intelligent logging that:
1. Suppresses intermediate validation noise during Phase 1
2. Collects all validation issues silently
3. Shows consolidated report after Phase 2
4. Provides clear error categorization
5. Supports enhanced debug mode with configuration tracing

Key features:
- Phase-aware logging levels
- Error aggregation and categorization
- Configuration rule tracing
- Transaction flow visualization
- Actionable error reporting
"""

import logging
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Set, Any

from beancount.core.data import Transaction


class ErrorCategory(Enum):
    """Categories of errors for smart grouping."""
    BALANCE_ASSERTION = "Balance Assertion"
    INVENTORY_REDUCTION = "Inventory/Reduction"
    TRANSACTION_BALANCE = "Transaction Balance"
    ACCOUNT_DEFINITION = "Account Definition"
    COMMODITY_DEFINITION = "Commodity Definition"
    VALIDATION_RULE = "Validation Rule"
    DUPLICATE_ENTRY = "Duplicate Entry"
    FILE_PROCESSING = "File Processing"
    CONFIGURATION = "Configuration"
    OTHER = "Other"


class LogLevel(Enum):
    """Custom log levels for phase-aware logging."""
    PHASE_TRANSITION = 45  # Between WARNING and ERROR
    VALIDATION_DEFERRED = 35  # Between WARNING and INFO
    CONFIG_TRACE = 15  # Between INFO and DEBUG
    TRANSACTION_FLOW = 12  # Very detailed


@dataclass
class ValidationIssue:
    """A single validation issue to be reported later."""
    category: ErrorCategory
    severity: str  # 'error', 'warning', 'info'
    message: str
    transaction: Optional[Transaction] = None
    filename: Optional[str] = None
    line_number: Optional[int] = None
    details: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class PhaseStatistics:
    """Statistics for a processing phase."""
    phase_name: str
    start_time: datetime
    end_time: Optional[datetime] = None
    transactions_processed: int = 0
    errors_collected: int = 0
    warnings_collected: int = 0
    files_processed: int = 0
    

class SmartLogger:
    """
    Smart logging system that provides phase-aware, noise-reduced logging.
    
    This logger collects issues during processing and presents them in a
    consolidated, actionable format at the end.
    """
    
    def __init__(self, base_logger: Optional[logging.Logger] = None,
                 debug_mode: bool = False):
        """
        Initialize the smart logger.
        
        Args:
            base_logger: Base logger to use (creates one if not provided)
            debug_mode: Enable enhanced debug logging
        """
        self.base_logger = base_logger or logging.getLogger(__name__)
        self.debug_mode = debug_mode
        
        # Phase tracking
        self.current_phase = None
        self.phase_stats = {}
        
        # Issue collection
        self.issues = defaultdict(list)  # category -> list of issues
        self.deferred_validations = []
        
        # Configuration tracing
        self.config_matches = []
        self.transaction_flow = []
        
        # Error suppression patterns
        self.suppressed_patterns = [
            r"expected .* != accumulated 0",  # Intermediate balance warnings
            r"Duplicate filename parsed",  # Expected from includes
            r"No position matches",  # Often fixed by Phase 2
            r"Transaction does not balance",  # May be fixed by matching
        ]
        
        # Statistics
        self.stats = defaultdict(int)
        
    def start_phase(self, phase_name: str):
        """Start a new processing phase."""
        if self.current_phase:
            self.end_phase()
            
        self.current_phase = phase_name
        self.phase_stats[phase_name] = PhaseStatistics(
            phase_name=phase_name,
            start_time=datetime.now()
        )
        
        # Log phase transition prominently
        self._log_phase_transition(f"STARTING {phase_name}")
        
    def end_phase(self):
        """End the current processing phase."""
        if not self.current_phase:
            return
            
        stats = self.phase_stats[self.current_phase]
        stats.end_time = datetime.now()
        duration = (stats.end_time - stats.start_time).total_seconds()
        
        # Log phase summary
        self._log_phase_transition(
            f"COMPLETED {self.current_phase} in {duration:.1f}s"
        )
        self.log_phase_summary(stats)
        
        self.current_phase = None
        
    def log(self, level: str, message: str, **kwargs):
        """
        Smart logging that may defer or suppress messages based on phase.
        
        Args:
            level: Log level ('debug', 'info', 'warning', 'error')
            message: Log message
            **kwargs: Additional context (transaction, filename, etc.)
        """
        # Check if this should be suppressed
        if self._should_suppress(message):
            self.stats['suppressed_messages'] += 1
            if self.debug_mode:
                self.base_logger.debug(f"[SUPPRESSED] {message}")
            return
            
        # Check if this is a validation issue to defer
        if self._is_validation_issue(message) and self.current_phase == "Phase 1":
            self._defer_validation(level, message, **kwargs)
            return
            
        # Otherwise, log normally but with phase context
        phase_prefix = f"[{self.current_phase}] " if self.current_phase else ""
        getattr(self.base_logger, level)(f"{phase_prefix}{message}")
        
    def add_issue(self, category: ErrorCategory, severity: str, 
                  message: str, **kwargs):
        """Add an issue to be reported later."""
        issue = ValidationIssue(
            category=category,
            severity=severity,
            message=message,
            **kwargs
        )
        self.issues[category].append(issue)
        self.stats[f'{severity}_count'] += 1
        
        # Update phase statistics
        if self.current_phase and self.current_phase in self.phase_stats:
            stats = self.phase_stats[self.current_phase]
            if severity == 'error':
                stats.errors_collected += 1
            elif severity == 'warning':
                stats.warnings_collected += 1
                
    def trace_config_match(self, transaction: Transaction, rule: str, 
                          matched: bool, reason: str):
        """Trace configuration rule matching for debug mode."""
        if not self.debug_mode:
            return
            
        self.config_matches.append({
            'transaction': f"{transaction.date} {transaction.narration}",
            'rule': rule,
            'matched': matched,
            'reason': reason,
            'timestamp': datetime.now()
        })
        
    def trace_transaction_flow(self, transaction: Transaction, 
                              phase: str, action: str):
        """Trace transaction flow through pipeline."""
        if not self.debug_mode:
            return
            
        self.transaction_flow.append({
            'transaction': f"{transaction.date} {transaction.narration}",
            'phase': phase,
            'action': action,
            'timestamp': datetime.now()
        })
        
    def _should_suppress(self, message: str) -> bool:
        """Check if a message should be suppressed."""
        import re
        
        # During Phase 1, suppress most validation warnings
        if self.current_phase == "Phase 1":
            for pattern in self.suppressed_patterns:
                if re.search(pattern, message, re.IGNORECASE):
                    return True
                    
        return False
        
    def _is_validation_issue(self, message: str) -> bool:
        """Check if this is a validation issue that should be deferred."""
        validation_keywords = [
            'balance', 'assertion', 'inventory', 'position',
            'does not balance', 'no position matches', 'accumulated'
        ]
        
        message_lower = message.lower()
        return any(keyword in message_lower for keyword in validation_keywords)
        
    def _defer_validation(self, level: str, message: str, **kwargs):
        """Defer a validation issue for later reporting."""
        # Categorize the issue
        category = self._categorize_message(message)
        
        self.add_issue(
            category=category,
            severity=level if level != 'warning' else 'warning',
            message=message,
            **kwargs
        )
        
        if self.debug_mode:
            self.base_logger.debug(f"[DEFERRED] {message}")
            
    def _categorize_message(self, message: str) -> ErrorCategory:
        """Categorize an error message."""
        message_lower = message.lower()
        
        if 'balance assertion' in message_lower:
            return ErrorCategory.BALANCE_ASSERTION
        elif 'inventory' in message_lower or 'reduction' in message_lower or 'position' in message_lower:
            return ErrorCategory.INVENTORY_REDUCTION
        elif 'does not balance' in message_lower:
            return ErrorCategory.TRANSACTION_BALANCE
        elif 'account' in message_lower and ('not found' in message_lower or 'unknown' in message_lower):
            return ErrorCategory.ACCOUNT_DEFINITION
        elif 'commodity' in message_lower:
            return ErrorCategory.COMMODITY_DEFINITION
        elif 'duplicate' in message_lower:
            return ErrorCategory.DUPLICATE_ENTRY
        elif 'file' in message_lower or 'read' in message_lower:
            return ErrorCategory.FILE_PROCESSING
        elif 'config' in message_lower or 'rule' in message_lower:
            return ErrorCategory.CONFIGURATION
        else:
            return ErrorCategory.OTHER
            
    def _log_phase_transition(self, message: str):
        """Log a phase transition prominently."""
        border = "="*60
        self.base_logger.info("")
        self.base_logger.info(border)
        self.base_logger.info(message.center(60))
        self.base_logger.info(border)
        self.base_logger.info("")
        
    def log_phase_summary(self, stats: PhaseStatistics):
        """Log a summary for a completed phase."""
        self.base_logger.info(f"Phase Summary for {stats.phase_name}:")
        self.base_logger.info(f"  Files processed: {stats.files_processed}")
        self.base_logger.info(f"  Transactions: {stats.transactions_processed}")
        self.base_logger.info(f"  Issues collected: {stats.errors_collected} errors, {stats.warnings_collected} warnings")
        

# Convenience functions for integration
_smart_logger_instance = None


def get_smart_logger(debug: bool = False) -> SmartLogger:
    """Get or create the global smart logger instance."""
    global _smart_logger_instance
    if _smart_logger_instance is None:
        _smart_logger_instance = SmartLogger(debug_mode=debug)
    return _smart_logger_instance


def reset_smart_logger():
    """Reset the global smart logger instance."""
    global _smart_logger_instance
    _smart_logger_instance = None