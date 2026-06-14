"""
Envelope - V7 Core Data Structure

This is the fundamental data structure for V7. Every transaction entering the system
is wrapped in an envelope that tracks its complete processing history, enabling
full introspection and debugging.

Key features:
- Immutable transaction core
- Mutable processing history
- State transitions tracking
- Source tagging
- Metadata accumulation
"""

from dataclasses import dataclass, field
from datetime import datetime, date
from enum import Enum
from typing import List, Optional, Dict, Any, Set, TYPE_CHECKING
from decimal import Decimal
import json

if TYPE_CHECKING:
    from beancount.core.data import Transaction


class EnvelopeState(Enum):
    """States a transaction can be in during processing."""
    INGESTED = "ingested"           # Just read from source
    CLASSIFIED = "classified"        # Intent determined (standalone vs match_expected)
    GROUPED = "grouped"             # Added to a reconciliation set
    RECONCILED = "reconciled"       # Authoritative version selected
    ENHANCED = "enhanced"           # Post-processing complete
    WRITTEN = "written"             # Written to output file
    DISCARDED = "discarded"         # Dropped during reconciliation


@dataclass
class HistoryEntry:
    """A single entry in the processing history."""
    timestamp: datetime
    state: EnvelopeState
    component: str  # Which component made this change
    action: str     # What action was taken
    details: Dict[str, Any] = field(default_factory=dict)  # Additional context
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            'timestamp': self.timestamp.isoformat(),
            'state': self.state.value if hasattr(self.state, 'value') else self.state,
            'component': self.component,
            'action': self.action,
            'details': self.details
        }


# Phase 3: Partial Immutability
# We cannot use frozen=True yet because internal methods need to modify state,
# classification, and reconciliation fields. Full immutability requires refactoring
# these methods to return new envelopes instead of modifying in place.
# For now, we rely on processors using replace() for field modifications (enforced
# by Phase 1) while allowing internal methods to work.
@dataclass
class Envelope:
    """
    Core V7 data structure that flows through the entire pipeline.
    
    In the deferred posting architecture, this envelope carries transaction
    data through the pipeline WITHOUT a Beancount Transaction object.
    The Transaction is only created at the very end by PostingWriter.
    """
    
    # Transaction data (no Transaction object until the very end!)
    date: date
    narration: str
    payee: Optional[str] = None
    flag: str = '*'
    
    # Financial data - INBOUND/OUTBOUND PATTERN
    outbound_units: Optional[Decimal] = None
    outbound_type: Optional[str] = None  # Currency or commodity symbol
    outbound_account: Optional[str] = None
    inbound_units: Optional[Decimal] = None
    inbound_type: Optional[str] = None  # Currency or commodity symbol
    inbound_account: Optional[str] = None
    unit_price: Optional[Decimal] = None  # For BUY/SELL transactions
    
    # Transaction type (still needed for routing decisions)
    transaction_type: Optional[str] = None  # BUY, SELL, DIVIDEND, etc.

    # Additional postings (for 3+ posting transactions)
    # Each dict contains: {'units': Decimal, 'type': str, 'account': str}
    additional_postings: List[Dict[str, Any]] = field(default_factory=list)

    # Balance tracking
    balance_after: Optional[Decimal] = None     # Balance after this transaction
    balance_type: Optional[str] = None          # 'express' (from CSV) or 'implied' (computed)

    # Ordering preservation (Steel Thread compliance)
    sort_order: Optional[Decimal] = None        # Computed balance-aware order
    original_order: Optional[int] = None        # Original position in source file

    # Links and tags (for Beancount)
    links: Optional[Set[str]] = None
    beancount_tags: Optional[Set[str]] = None  # Renamed to avoid conflict with processing tags

    # NO TRANSACTION UNTIL THE END! PostingWriter will create it!

    # Unique identifier (deterministic hash)
    envelope_id: str = ""
    
    # Source information
    source: str = ""  # Short identifier: "ajbell_sipp", "manual_transaction", etc.
    source_type: Optional[str] = None  # "beancount" (manual) or "csv" (importer) - set by reader/importer
    source_file_path: Optional[str] = None  # Full path to source file
    source_line_number: Optional[int] = None  # Line in source file
    
    # Current state
    state: EnvelopeState = EnvelopeState.INGESTED
    
    # Processing history (append-only)
    history: List[HistoryEntry] = field(default_factory=list)
    
    # Classification (from ManualTransactionClassifier or other sources)
    classification: Optional[str] = None  # "standalone", "match_expected", etc.
    classification_confidence: float = 0.0
    classification_reasoning: List[str] = field(default_factory=list)
    
    # Reconciliation information
    reconciliation_set_id: Optional[str] = None
    reconciliation_score: float = 0.0
    reconciliation_matched_with: List[str] = field(default_factory=list)  # Other envelope IDs
    
    # Accumulated metadata (from various processors)
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    # Warnings accumulated during processing (can be strings or ProcessingWarning objects)
    warnings: List[Any] = field(default_factory=list)
    
    # Tags for filtering and debugging
    tags: Set[str] = field(default_factory=set)
    
    # Lot tracking information
    lot_created: Optional[Dict[str, Any]] = None  # For BUY transactions
    consumed_lots: Optional[List[Dict[str, Any]]] = None  # For SELL transactions
    transferred_lots: Optional[List[Dict[str, Any]]] = None  # For TRANSFER transactions
    lot_lineages: List[Dict[str, Any]] = field(default_factory=list)  # Legacy/detailed tracking
    
    def add_history(self, state: EnvelopeState, component: str, action: str, 
                   details: Optional[Dict[str, Any]] = None) -> None:
        """Add a history entry and update current state."""
        entry = HistoryEntry(
            timestamp=datetime.now(),
            state=state,
            component=component,
            action=action,
            details=details or {}
        )
        self.history.append(entry)
        self.state = state
    
    def add_warning(self, warning: str, component: str) -> None:
        """Add a warning with source component tracking."""
        formatted_warning = f"[{component}] {warning}"
        self.warnings.append(formatted_warning)
        self.add_history(
            state=self.state,  # Don't change state for warnings
            component=component,
            action="Warning added",
            details={'warning': warning}
        )
    
    def set_classification(self, classification: str, confidence: float, 
                          reasoning: List[str], component: str) -> None:
        """Set the transaction classification with full context."""
        self.classification = classification
        self.classification_confidence = confidence
        self.classification_reasoning = reasoning
        self.add_history(
            state=EnvelopeState.CLASSIFIED,
            component=component,
            action=f"Classified as {classification}",
            details={
                'classification': classification,
                'confidence': confidence,
                'reasoning': reasoning
            }
        )
    
    def mark_reconciled(self, set_id: str, score: float, matched_with: List[str],
                       component: str, is_authoritative: bool = False) -> None:
        """Mark this transaction as reconciled."""
        self.reconciliation_set_id = set_id
        self.reconciliation_score = score
        self.reconciliation_matched_with = matched_with
        self.add_history(
            state=EnvelopeState.RECONCILED,
            component=component,
            action="Reconciled" if is_authoritative else "Discarded in reconciliation",
            details={
                'set_id': set_id,
                'score': score,
                'matched_with': matched_with,
                'is_authoritative': is_authoritative
            }
        )
        if not is_authoritative:
            self.state = EnvelopeState.DISCARDED
    
    def set_lot_lineages(self, lineages: List[Dict[str, Any]], component: str) -> None:
        """Set lot lineage information for commodity tracking."""
        self.lot_lineages = lineages
        self.metadata['has_lot_lineage'] = True
        self.metadata['lot_count'] = len(lineages)
        self.add_history(
            state=self.state,  # Don't change state
            component=component,
            action="Lot lineage tracked",
            details={
                'lot_count': len(lineages),
                'commodities': list(set(l.get('commodity', '') for l in lineages))
            }
        )
    
    def is_discarded(self) -> bool:
        """Check if this envelope has been discarded."""
        return self.state == EnvelopeState.DISCARDED
    
    
    def log_critical_decision(
        self,
        component: str,
        decision: str,
        reasoning: str,
        metadata: Optional[Dict] = None
    ) -> None:
        """
        FIX #13: Log critical decision to envelope for forensic analysis.
        
        Creates a comprehensive audit trail of all critical processing decisions
        for this transaction, enabling root cause analysis of any issues.
        
        Args:
            component: Component making the decision (e.g., 'ReconciliationEngine')
            decision: The decision made (e.g., 'BLOCKED_MERGE')
            reasoning: Human-readable reasoning for the decision
            metadata: Additional context data
        """
        # Initialize critical decisions list if needed
        if 'critical_decisions' not in self.metadata:
            self.metadata['critical_decisions'] = []
        
        decision_record = {
            'timestamp': datetime.now().isoformat(),
            'component': component,
            'decision': decision,
            'reasoning': reasoning
        }
        
        if metadata:
            decision_record['context'] = metadata
        
        self.metadata['critical_decisions'].append(decision_record)
        
        # Also add to history for visibility
        self.add_history(
            state=self.state,  # Keep current state
            component=component,
            action=f"Critical Decision: {decision}",
            details={'reasoning': reasoning, 'metadata': metadata}
        )
        
    def get_discard_reason(self) -> str:
        """Extract discard reason from history."""
        for entry in reversed(self.history):
            if 'discard' in entry.action.lower() or entry.state == EnvelopeState.DISCARDED:
                return entry.details.get('reason', 'Discarded during reconciliation')
        return 'Not discarded'
    
    def get_history_summary(self) -> str:
        """Get a human-readable summary of the processing history."""
        lines = [f"Envelope {self.envelope_id} History:"]
        for entry in self.history:
            lines.append(
                f"  {entry.timestamp.strftime('%H:%M:%S')} "
                f"[{entry.component}] {entry.action} → {entry.state.value}"
            )
        return "\n".join(lines)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert envelope to dictionary for serialization/debugging."""
        return {
            'envelope_id': self.envelope_id,
            'source': self.source,
            'source_type': self.source_type,  # CRITICAL: needed for reconciliation checks
            'state': self.state.value if hasattr(self.state, 'value') else self.state,
            'classification': self.classification,
            'classification_confidence': self.classification_confidence,
            'reconciliation_set_id': self.reconciliation_set_id,
            'reconciliation_score': self.reconciliation_score,
            'warnings': self.warnings,
            'tags': list(self.tags),
            'lot_lineages': self.lot_lineages,
            'history': [entry.to_dict() for entry in self.history],
            'metadata': self.metadata,
            # Lot tracking fields (Issue #118)
            'consumed_lots': self.consumed_lots,
            'lot_created': self.lot_created,
            'transferred_lots': self.transferred_lots,
            # Transaction data from envelope fields (no Transaction object!)
            'transaction_data': {
                'date': str(self.date),
                'payee': self.payee,
                'narration': self.narration,
                'outbound_units': str(self.outbound_units) if self.outbound_units else None,
                'outbound_type': self.outbound_type,
                'outbound_account': self.outbound_account,
                'inbound_units': str(self.inbound_units) if self.inbound_units else None,
                'inbound_type': self.inbound_type,
                'inbound_account': self.inbound_account,
                'unit_price': str(self.unit_price) if self.unit_price else None,
                'transaction_type': self.transaction_type,
                # Balance tracking for FBAR intra-day max calculation
                'balance_after': str(self.balance_after) if self.balance_after is not None else None,
                'balance_type': self.balance_type
            }
        }
    
    @staticmethod
    def generate_envelope_id(date_val: date, narration: str, source: str,
                           outbound_units: Optional[Decimal] = None,
                           inbound_units: Optional[Decimal] = None,
                           outbound_account: Optional[str] = None,
                           transaction_id: Optional[str] = None,
                           source_file: Optional[str] = None,
                           line_number: Optional[int] = None) -> str:
        """Generate a deterministic envelope ID from raw data.

        Uses the centralized deterministic ID generation system.
        Includes line number and source file to prevent duplicates.

        CRITICAL: We NEVER use provided transaction_ids - we ALWAYS generate our own.
        The provided transaction_id is stored as metadata only.
        """
        from cassoulet.utils.deterministic_id import generate_id

        # CRITICAL FIX: NEVER use provided transaction_id as envelope ID
        # It's unreliable and often lacks line numbers causing massive duplication
        # The transaction_id parameter is kept for backward compatibility but ignored

        # Build data dict for ID generation
        data = {
            'date': date_val,
            'narration': narration,
            'source': source,
            'outbound_account': outbound_account,
            'amount': outbound_units or inbound_units,
        }
        # Add source file to data if provided (for uniqueness)
        if source_file:
            data['source_file'] = source_file

        # Generate deterministic ID by hashing all the data
        # Line number is passed separately to be appended as _L{n}
        return generate_id(
            prefix="env",
            data=data,
            line_number=line_number,  # Critical for preventing duplicates
            hash_length=16  # Longer hash for envelopes
        )
    
    @classmethod
    def create(cls,
              date_val: date,
              narration: str,
              source: str,
              payee: Optional[str] = None,
              flag: str = '*',
              # Inbound/outbound pattern
              outbound_units: Optional[Decimal] = None,
              outbound_type: Optional[str] = None,
              outbound_account: Optional[str] = None,
              inbound_units: Optional[Decimal] = None,
              inbound_type: Optional[str] = None,
              inbound_account: Optional[str] = None,
              unit_price: Optional[Decimal] = None,
              transaction_type: Optional[str] = None,
              transaction_id: Optional[str] = None,
              links: Optional[Set[str]] = None,
              beancount_tags: Optional[Set[str]] = None,
              source_file_path: Optional[str] = None,
              source_line_number: Optional[int] = None,
              metadata: Optional[Dict[str, Any]] = None) -> 'Envelope':
        """
        Factory method to create an envelope from raw data.

        This is the PRIMARY way to create envelopes. It uses the safe
        create_envelope() utility internally to ensure proper initialization
        and audit trail.

        Args:
            date_val: Transaction date
            narration: Transaction description
            source: Canonical source identifier (e.g., "ajb_sipp_txn.csv")
            payee: Optional payee
            flag: Transaction flag (default '*')
            outbound_units: Outbound amount
            outbound_type: Outbound currency/commodity
            outbound_account: Source account
            inbound_units: Inbound amount
            inbound_type: Inbound currency/commodity
            inbound_account: Destination account
            unit_price: Price per unit (for investment transactions)
            transaction_type: Type of transaction (BUY, SELL, DIVIDEND, etc.)
            transaction_id: Optional unique transaction ID from source
            links: Optional Beancount links
            beancount_tags: Optional Beancount tags
            source_file_path: Optional full path to source file
            source_line_number: Optional line number in source file
            metadata: Optional additional metadata

        Returns:
            A new Envelope with the provided data
        """
        from cassoulet.utils.envelope_utilities import create_envelope

        # Use the safe creation utility (it handles ID generation)
        return create_envelope(
            reason=f"Data ingestion from {source}",
            # Core data (note: parameter name mapping)
            date=date_val,
            narration=narration,
            payee=payee,
            flag=flag,
            # Inbound/outbound pattern
            outbound_units=outbound_units,
            outbound_type=outbound_type,
            outbound_account=outbound_account,
            inbound_units=inbound_units,
            inbound_type=inbound_type,
            inbound_account=inbound_account,
            unit_price=unit_price,
            transaction_type=transaction_type,
            # Beancount-specific
            links=links,
            beancount_tags=beancount_tags,
            # Envelope metadata
            source=source,
            source_file_path=source_file_path,
            source_line_number=source_line_number,
            transaction_id=transaction_id,
            metadata=metadata or {}
        )
    