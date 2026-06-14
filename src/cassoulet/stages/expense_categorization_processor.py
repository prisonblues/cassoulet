"""
Expense Categorization Processor

Pipeline processor that applies expense categorization patterns to envelopes.
Adds expense accounts, categories, and tags based on declarative pattern matching.

Follows the same pattern as InvestmentClassifier - inline pattern matching
with utility functions.

Part of the expense categorization system (Issue #135).
"""

import logging
from dataclasses import dataclass, field
from typing import List, Tuple, Dict, Any, Optional

from cassoulet.stages.envelope import Envelope
from cassoulet.stages.envelope_processor import EnvelopeProcessor
from cassoulet.base.exceptions import ProcessingWarning
from cassoulet.utils.envelope_utilities import (
    is_transfer_envelope,
    is_expense_envelope,
)
from cassoulet.utils.expense_utilities import match_expense_pattern

logger = logging.getLogger(__name__)


@dataclass
class ExpenseMatch:
    """Result of expense categorization."""

    matched: bool
    pattern_id: Optional[str] = None
    name: Optional[str] = None
    reason: Optional[str] = None

    # Account assignment
    expense_account: Optional[str] = None
    income_account: Optional[str] = None

    # Category information
    category: Optional[str] = None
    subcategory: Optional[str] = None

    # Tags for filtering/reporting
    tags: List[str] = field(default_factory=list)

    # Matching metadata
    priority: int = 0

    # Extra metadata from pattern (e.g., creates_director_loan, company flags)
    extra_metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def no_match(cls) -> 'ExpenseMatch':
        """Create a no-match result."""
        return cls(matched=False)

    def to_envelope_metadata(self) -> Dict[str, Any]:
        """Convert to metadata dict for envelope enhancement."""
        meta = {}
        if self.expense_account:
            meta['expense_account'] = self.expense_account
        if self.income_account:
            meta['income_account'] = self.income_account
        if self.category:
            meta['category'] = self.category
        if self.subcategory:
            meta['subcategory'] = self.subcategory
        if self.tags:
            meta['expense_tags'] = self.tags
        if self.pattern_id:
            meta['expense_pattern_id'] = self.pattern_id
        if self.name:
            meta['expense_pattern_name'] = self.name
        if self.reason:
            meta['expense_classification_reason'] = self.reason
        # Pass through extra metadata (company, creates_director_loan, etc.)
        if self.extra_metadata:
            meta.update(self.extra_metadata)
        return meta


class ExpenseCategorizationProcessor(EnvelopeProcessor):
    """
    Processor that categorizes expense/income envelopes using declarative patterns.

    Follows the same architecture as InvestmentClassifier:
    - Patterns loaded from config module
    - Inline pattern matching using utility functions
    - No separate "categorizer" class layer

    This processor:
    1. Identifies expense/income envelopes (outbound-only or inbound-only)
    2. Applies pattern matching to find the best category
    3. Enriches envelope metadata with expense account, category, tags

    Follows Steel Thread Architecture - no data loss, full audit trail.
    """

    def __init__(self, patterns: Dict[str, Dict] = None, enabled: bool = True):
        """
        Initialize the expense categorization processor.

        Args:
            patterns: Optional custom patterns. If None, loads from config.
            enabled: If False, processor passes through without categorization.
        """
        super().__init__(processor_name="ExpenseCategorizationProcessor")
        self.enabled = enabled

        # Load and compile patterns
        if enabled:
            if patterns is None:
                patterns = self._load_default_patterns()
            self._compiled_patterns = self._compile_patterns(patterns)
        else:
            self._compiled_patterns = []

        # Extended stats for categorization
        self.stats.update({
            'categorized': 0,
            'uncategorized': 0,
            'skipped_transfers': 0,
            'categories_assigned': {},
        })

    def _load_default_patterns(self) -> Dict[str, Dict]:
        """Load default patterns from config."""
        try:
            from cassoulet.config.expense_category_patterns import EXPENSE_CATEGORY_PATTERNS
            return EXPENSE_CATEGORY_PATTERNS
        except ImportError:
            logger.warning("No expense_category_patterns.py found, using empty patterns")
            return {}

    def _compile_patterns(self, patterns: Dict[str, Dict]) -> List[Dict[str, Any]]:
        """Compile and sort patterns by priority.

        Returns patterns sorted by priority (highest first) with
        pre-processed metadata for faster matching.
        """
        compiled = []

        for pattern_id, config in patterns.items():
            priority = config.get('priority', 0)
            name = config.get('name', pattern_id)
            reason = config.get('reason', '')
            category = config.get('category', '')
            metadata = config.get('metadata', {})

            for pattern in config.get('patterns', []):
                compiled.append({
                    'pattern_id': pattern_id,
                    'name': name,
                    'reason': reason,
                    'category': category,
                    'priority': priority,
                    'pattern': pattern,
                    'metadata': metadata,
                })

        # Sort by priority (highest first)
        compiled.sort(key=lambda x: x['priority'], reverse=True)
        return compiled

    def _process_internal(
        self,
        envelopes: List[Envelope]
    ) -> Tuple[List[Envelope], List[ProcessingWarning]]:
        """
        Categorize expense/income envelopes.

        Args:
            envelopes: List of envelopes to process

        Returns:
            Tuple of (processed_envelopes, warnings)
        """
        warnings = []

        if not self.enabled:
            logger.info("ExpenseCategorizationProcessor: Disabled, passing through")
            return envelopes, warnings

        logger.info(f"ExpenseCategorizationProcessor: Processing {len(envelopes)} envelopes")

        processed = []
        for envelope in envelopes:
            # Skip transfers (both inbound and outbound) - they're not expenses
            if is_transfer_envelope(envelope):
                self.stats['skipped_transfers'] += 1
                processed.append(envelope)
                continue

            # Categorize the envelope
            match = self._categorize_envelope(envelope)

            if match.matched:
                # Apply categorization to envelope
                self._apply_categorization(envelope, match)
                self.stats['categorized'] += 1

                # Track category distribution
                category = match.category or 'Unknown'
                self.stats['categories_assigned'][category] = \
                    self.stats['categories_assigned'].get(category, 0) + 1
            else:
                self.stats['uncategorized'] += 1

                # Add info warning for unrecognized expenses
                if is_expense_envelope(envelope):
                    warnings.append(ProcessingWarning(
                        processor_name=self.processor_name,
                        severity='INFO',
                        message=f"Uncategorized expense: {envelope.payee or envelope.narration}",
                        source_transaction=None,
                        details={
                            'envelope_id': envelope.envelope_id,
                            'date': str(envelope.date),
                            'amount': str(envelope.outbound_units),
                            'payee': envelope.payee,
                            'narration': envelope.narration,
                        }
                    ))

            processed.append(envelope)

        # Log summary
        logger.info(
            f"ExpenseCategorizationProcessor: Categorized {self.stats['categorized']}, "
            f"Uncategorized {self.stats['uncategorized']}, "
            f"Skipped transfers {self.stats['skipped_transfers']}"
        )

        if self.stats['categories_assigned']:
            logger.info("  Categories assigned:")
            for cat, count in sorted(self.stats['categories_assigned'].items()):
                logger.info(f"    {cat}: {count}")

        return processed, warnings

    def _categorize_envelope(self, envelope: Envelope) -> ExpenseMatch:
        """Categorize an envelope using compiled patterns.

        Returns ExpenseMatch with account, category, tags, etc.
        """
        for compiled in self._compiled_patterns:
            if match_expense_pattern(envelope, compiled['pattern']):
                return self._create_match(compiled)

        return ExpenseMatch.no_match()

    def _create_match(self, compiled: Dict) -> ExpenseMatch:
        """Create ExpenseMatch from compiled pattern data."""
        metadata = compiled['metadata']

        # Standard fields that are mapped to ExpenseMatch attributes
        standard_fields = {
            'expense_account', 'income_account', 'category', 'subcategory', 'tags'
        }

        # Extract extra metadata (company, creates_director_loan, etc.)
        extra = {k: v for k, v in metadata.items() if k not in standard_fields}

        return ExpenseMatch(
            matched=True,
            pattern_id=compiled['pattern_id'],
            name=compiled['name'],
            reason=compiled['reason'],
            expense_account=metadata.get('expense_account'),
            income_account=metadata.get('income_account'),
            category=metadata.get('category', compiled['category']),
            subcategory=metadata.get('subcategory'),
            tags=metadata.get('tags', []).copy(),
            priority=compiled['priority'],
            extra_metadata=extra,
        )

    def _apply_categorization(self, envelope: Envelope, match: ExpenseMatch) -> None:
        """Apply categorization results to envelope metadata."""
        # Get metadata dict from match
        meta = match.to_envelope_metadata()

        # Apply to envelope
        envelope.metadata.update(meta)

        # Also set convenience fields if they exist on envelope
        if hasattr(envelope, 'category'):
            envelope.category = match.category

        # Add to envelope history
        envelope.add_history(
            state=envelope.state,
            component=self.processor_name,
            action="Categorized",
            details={
                'pattern_id': match.pattern_id,
                'category': match.category,
                'expense_account': match.expense_account,
            }
        )

    def get_uncategorized_summary(self, envelopes: List[Envelope]) -> List[Dict[str, Any]]:
        """
        Get summary of uncategorized envelopes for pattern development.

        Useful for identifying gaps in pattern coverage.

        Args:
            envelopes: List of envelopes to analyze

        Returns:
            List of dicts with uncategorized envelope details
        """
        summary = []
        for env in envelopes:
            # Skip transfers
            if is_transfer_envelope(env):
                continue

            # Only include expense envelopes
            if not is_expense_envelope(env):
                continue

            # Check if categorized
            match = self._categorize_envelope(env)
            if match.matched:
                continue

            summary.append({
                'date': str(env.date),
                'payee': env.payee,
                'narration': env.narration,
                'amount': str(env.outbound_units) if env.outbound_units else None,
                'account': env.outbound_account,
                'envelope_id': env.envelope_id,
            })

        return summary
