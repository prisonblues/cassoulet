"""Enhanced data structures with integrity verification for the import pipeline.

This module implements the "Highly Legible Failure" architecture from Plan 5,
providing mandatory integrity checks and severity-based warning classification.
"""
from dataclasses import dataclass, field
from beancount.core.data import Transaction
from typing import Literal, Optional, List, Protocol, TypedDict, runtime_checkable
from abc import ABC, abstractmethod


class DataLossError(Exception):
    """Raised when processing results in data loss."""
    pass


class InvalidWarningError(Exception):
    """Raised when a warning violates the contract."""
    pass

class IncompleteDataSetError(Exception):
    """Raised when a MultiFileImporter is missing a required file."""
    pass


class LotShortageError(Exception):
    """Raised when a SELL transaction cannot find sufficient lots to consume."""
    def __init__(self, commodity: str, account: str, needed: float, available: float, transaction_id: str = None):
        self.commodity = commodity
        self.account = account
        self.needed = needed
        self.available = available
        self.shortage = needed - available
        self.transaction_id = transaction_id
        super().__init__(
            f"Lot shortage for {commodity} in {account}: "
            f"needed {needed}, available {available}, SHORT {self.shortage}"
            + (f" (txn: {transaction_id})" if transaction_id else "")
        )


class ContractViolationError(TypeError):
    """Raised when a processor violates the steel thread contract."""
    pass


class ConfigurationError(Exception):
    """Raised when required configuration is missing or invalid."""
    def __init__(self, field: str, component: str, details: str = None):
        self.field = field
        self.component = component
        self.details = details
        message = f"Missing required configuration '{field}' in {component}"
        if details:
            message += f": {details}"
        super().__init__(message)


class GroupPostingError(Exception):
    """
    Raised when a manual transaction has ambiguous or unsupported posting groups.

    This helps users understand why their transaction cannot be automatically
    processed and how to fix it.
    """
    pass


class UnsafeMergeError(Exception):
    """
    Raised when a transfer merge would be unsafe due to amount mismatches
    that cannot be reconciled via transfer_leakage.

    This is not a fatal error - the merger can catch it and proceed with
    a warning, but it signals that the resulting transaction may be unbalanced.
    """
    def __init__(self, env1_id: str, env2_id: str, loss: float, env1_type: str, env2_type: str):
        self.env1_id = env1_id
        self.env2_id = env2_id
        self.loss = loss
        self.env1_type = env1_type
        self.env2_type = env2_type

        message = (
            f"Cannot safely merge {env1_id[:30]} + {env2_id[:30]}. "
            f"Amounts differ by {loss:.2f} but transfer_leakage cannot be applied. "
            f"Transaction types: {env1_type} + {env2_type}"
        )
        super().__init__(message)


class UnsupportedBeancountInput(Exception):
    """
    Raised when a beancount transaction cannot be fully represented in the envelope structure.

    This is a critical steel thread violation - we must never lose postings silently.
    Examples: stock splits with multiple lots, complex multi-commodity transactions.
    """
    def __init__(self, date: str, narration: str, reason: str, file_path: str = None, line_number: int = None):
        self.date = date
        self.narration = narration
        self.reason = reason
        self.file_path = file_path
        self.line_number = line_number

        message = f"Transaction on {date}: {reason}"
        if file_path:
            message = f"Unsupported transaction on {date}: {reason}\nFile: {file_path}"
            if line_number:
                message += f", Line: {line_number}"
        super().__init__(message)


# ===== CSV READER EXCEPTIONS =====

class CSVParseError(Exception):
    """Base exception for CSV parsing errors."""
    pass


class CSVNoHeadersError(CSVParseError):
    """Raised when CSV file has no headers."""
    def __init__(self, file_path: str):
        self.file_path = file_path
        super().__init__(f"No headers found in CSV file: {file_path}")


class CSVMissingDateError(CSVParseError):
    """Raised when a CSV row is missing required date field."""
    def __init__(self, row_number: int, row_content: list = None):
        self.row_number = row_number
        self.row_content = row_content
        super().__init__(
            f"Row {row_number} missing required date field"
            + (f": {row_content[:5]}" if row_content else "")
        )


class CSVDateFormatError(CSVParseError):
    """Raised when date parsing fails."""
    def __init__(self, value: str, row_number: int, detected_format: str = None):
        self.value = value
        self.row_number = row_number
        self.detected_format = detected_format
        format_info = f" (expected format: {detected_format})" if detected_format else ""
        super().__init__(
            f"Failed to parse date '{value}' at row {row_number}{format_info}"
        )


class CSVDataIntegrityError(CSVParseError):
    """Raised when CSV data integrity check fails."""
    def __init__(self, expected: int, actual: int, details: dict = None):
        self.expected = expected
        self.actual = actual
        self.details = details or {}
        super().__init__(
            f"Data integrity check failed: expected {expected} rows, got {actual}"
            + (f" (details: {details})" if details else "")
        )


class CommodityLookupError(Exception):
    """Error during commodity lookup - indicates missing or misconfigured commodity registry."""
    def __init__(self, description: str, reason: str = None):
        self.description = description
        self.reason = reason
        message = f"Failed to lookup commodity for: '{description}'"
        if reason:
            message += f" - {reason}"
        super().__init__(message)


# ===== TRANSFER MERGER EXCEPTIONS =====

class MultipleOutboundError(Exception):
    """Raised when multiple envelopes in a merge group have outbound data."""
    def __init__(self, envelope_ids: list, outbound_details: dict):
        self.envelope_ids = envelope_ids
        self.outbound_details = outbound_details
        details = ', '.join([f"{eid}: {outbound_details.get(eid, 'unknown')}" for eid in envelope_ids])
        super().__init__(
            f"Multiple envelopes have outbound data (only one allowed per merge group): {details}"
        )


class MultipleInboundError(Exception):
    """Raised when multiple envelopes in a merge group have inbound data."""
    def __init__(self, envelope_ids: list, inbound_details: dict):
        self.envelope_ids = envelope_ids
        self.inbound_details = inbound_details
        details = ', '.join([f"{eid}: {inbound_details.get(eid, 'unknown')}" for eid in envelope_ids])
        super().__init__(
            f"Multiple envelopes have inbound data (only one allowed per merge group): {details}"
        )


class ConflictingTypesError(Exception):
    """Raised when envelopes have conflicting commodity/currency types."""
    def __init__(self, field: str, types: dict):
        self.field = field
        self.types = types
        type_list = ', '.join([f"{eid}: {t}" for eid, t in types.items()])
        super().__init__(
            f"Conflicting {field} types in merge group: {type_list}"
        )


class ForexNotSupportedError(Exception):
    """Raised when a forex transaction is detected (not yet supported)."""
    def __init__(self, currency1: str, currency2: str, envelope_ids: list = None):
        self.currency1 = currency1
        self.currency2 = currency2
        self.envelope_ids = envelope_ids or []
        id_info = f" (envelopes: {', '.join(envelope_ids)})" if envelope_ids else ""
        super().__init__(
            f"Forex transaction detected ({currency1} <-> {currency2}) but not supported{id_info}"
        )


class InvalidOverrideError(Exception):
    """Raised when a manual override references a non-existent envelope ID."""
    def __init__(self, override_id: str, referenced_id: str, available_ids: list):
        self.override_id = override_id
        self.referenced_id = referenced_id
        self.available_ids = available_ids
        super().__init__(
            f"Envelope {override_id} references non-existent {referenced_id}. "
            f"Available IDs: {', '.join(available_ids[:5])}"
            f"{'...' if len(available_ids) > 5 else ''}"
        )


class MultipleSovereignError(Exception):
    """Raised when multiple manual/sovereign envelopes are in the same merge group."""
    def __init__(self, sovereign_ids: list, sources: dict):
        self.sovereign_ids = sovereign_ids
        self.sources = sources
        details = ', '.join([f"{sid} (source: {sources.get(sid, 'unknown')})" for sid in sovereign_ids])
        super().__init__(
            f"Multiple sovereign envelopes in merge group: {details}. "
            f"Only one manual/sovereign envelope allowed per group."
        )


class TransferAmountMismatchError(Exception):
    """Raised when transfer outbound/inbound amounts don't match."""
    def __init__(self, outbound_amount, inbound_amount, envelope_id: str, tolerance=None):
        self.outbound_amount = outbound_amount
        self.inbound_amount = inbound_amount
        self.envelope_id = envelope_id
        self.difference = abs(outbound_amount - inbound_amount)
        tolerance_info = f" (tolerance: {tolerance})" if tolerance else ""
        super().__init__(
            f"Transfer amounts don't match in {envelope_id}: "
            f"outbound={outbound_amount}, inbound={inbound_amount}, "
            f"difference={self.difference}{tolerance_info}"
        )


class TransferMissingAccountError(Exception):
    """Raised when a transfer is missing source or destination account."""
    def __init__(self, envelope_id: str, missing_field: str, present_field: str = None, present_value=None):
        self.envelope_id = envelope_id
        self.missing_field = missing_field
        info = f" (has {present_field}={present_value})" if present_field else ""
        super().__init__(
            f"Transfer {envelope_id} missing {missing_field}{info}"
        )


class CommodityTransferUnitMismatchError(Exception):
    """Raised when in-specie transfer units don't match."""
    def __init__(self, commodity: str, outbound_units, inbound_units, envelope_id: str):
        self.commodity = commodity
        self.outbound_units = outbound_units
        self.inbound_units = inbound_units
        self.envelope_id = envelope_id
        self.difference = abs(outbound_units - inbound_units)
        super().__init__(
            f"Commodity transfer units mismatch for {commodity} in {envelope_id}: "
            f"outbound={outbound_units}, inbound={inbound_units}, difference={self.difference}"
        )


class BuyMissingPriceError(Exception):
    """Raised when a BUY transaction is missing unit price for cost basis."""
    def __init__(self, envelope_id: str, commodity: str, units, amount):
        self.envelope_id = envelope_id
        self.commodity = commodity
        self.units = units
        self.amount = amount
        super().__init__(
            f"BUY transaction {envelope_id} missing unit_price for cost basis: "
            f"buying {units} {commodity} for {amount}, cannot calculate price"
        )


class MissingCostBasisError(Exception):
    """Raised when we receive securities but cannot determine their cost basis.

    This typically happens with:
    - In-specie transfers from external sources
    - Stock grants/RSUs without purchase price
    - Inherited securities
    - Transfers where the source account has no lots
    """
    def __init__(self,
                 envelope_id: str,
                 commodity: str,
                 units,
                 account: str,
                 reason: str,
                 transfer_type: str = None):
        self.envelope_id = envelope_id
        self.commodity = commodity
        self.units = units
        self.account = account
        self.reason = reason
        self.transfer_type = transfer_type

        message = (
            f"Cannot determine cost basis for {units} {commodity} "
            f"received into {account}"
        )
        if transfer_type:
            message += f" via {transfer_type}"
        message += f": {reason}"
        if envelope_id:
            message += f" (envelope: {envelope_id})"

        super().__init__(message)


class ExpenseMissingAccountError(Exception):
    """Raised when an EXPENSE is missing source account."""
    def __init__(self, envelope_id: str, amount, currency: str):
        self.envelope_id = envelope_id
        self.amount = amount
        self.currency = currency
        super().__init__(
            f"EXPENSE {envelope_id} missing outbound_account: {amount} {currency} with no source"
        )


class IncomeMissingAccountError(Exception):
    """Raised when an INCOME is missing destination account."""
    def __init__(self, envelope_id: str, amount, currency: str):
        self.envelope_id = envelope_id
        self.amount = amount
        self.currency = currency
        super().__init__(
            f"INCOME {envelope_id} missing inbound_account: {amount} {currency} with no destination"
        )


class NoPostingsCreatedError(Exception):
    """Raised when an envelope with flow cannot generate any postings."""
    def __init__(self, envelope_id: str, has_flow: bool, details: str = None):
        self.envelope_id = envelope_id
        self.has_flow = has_flow
        self.details = details
        message = f"No postings created for envelope {envelope_id}"
        if has_flow:
            message += " (has flow but missing critical account information)"
        else:
            message += " (no flow detected)"
        if details:
            message += f": {details}"
        super().__init__(message)


# ===== PATH AND FILE SYSTEM EXCEPTIONS =====

class PathNotFoundWarning(Exception):
    """
    Steel Thread compliant warning for missing paths.
    Not a critical error - the system can continue without the path.
    """
    def __init__(self, path: str, purpose: str):
        self.path = path
        self.purpose = purpose
        super().__init__(
            f"Path not found for {purpose}: {path}. No {purpose} will be loaded."
        )


class PathNotDirectoryError(Exception):
    """Raised when a path exists but is not a directory when one is expected."""
    def __init__(self, path: str, purpose: str):
        self.path = path
        self.purpose = purpose
        super().__init__(
            f"Path exists but is not a directory for {purpose}: {path}"
        )


class DirectoryCreationError(Exception):
    """Raised when directory creation fails."""
    def __init__(self, path: str, purpose: str, original_error: Exception):
        self.path = path
        self.purpose = purpose
        self.original_error = original_error
        super().__init__(
            f"Failed to create {purpose} directory at {path}: {original_error}"
        )


class NoFilesFoundInfo(Exception):
    """
    Steel Thread compliant info for no files matching pattern.
    This is informational - not an error condition.
    """
    def __init__(self, directory: str, pattern: str, purpose: str):
        self.directory = directory
        self.pattern = pattern
        self.purpose = purpose
        super().__init__(
            f"No files matching '{pattern}' found in {directory}. No {purpose} to load."
        )


@dataclass
class ProcessingWarning:
    """A structured warning for non-fatal processing issues."""
    processor_name: str
    severity: Literal['INFO', 'WARNING', 'ERROR', 'CRITICAL']
    message: str
    source_transaction: Optional[Transaction] = None
    details: dict = field(default_factory=dict)

    def is_data_loss_risk(self) -> bool:
        """Check if this warning indicates potential data loss."""
        return self.severity in ['ERROR', 'CRITICAL']


@dataclass
class LotTrackingWarning(ProcessingWarning):
    """A specialized warning for lot tracking and cost basis issues.

    This indicates problems with tracking the cost basis of investments,
    which is critical for:
    - Accurate capital gains/loss calculations
    - Tax reporting compliance
    - Portfolio performance tracking
    """
    lot_issue_type: Literal['missing_cost_basis', 'incomplete_transfer', 'lot_shortage', 'orphaned_sale'] = 'missing_cost_basis'
    affected_commodity: Optional[str] = None
    affected_account: Optional[str] = None

    def __post_init__(self):
        """Ensure processor_name indicates lot tracking."""
        if not self.processor_name:
            self.processor_name = 'EnvelopeLotProcessor'


@dataclass
class UnsafeMergeWarning(ProcessingWarning):
    """A structured warning for unsafe merges that can't apply transfer_leakage.

    This replaces UnsafeMergeError - it's not a fatal error but needs reporting.
    Merging continues with metadata marking the issue.
    """
    env1_id: str = None
    env2_id: str = None
    loss: float = 0.0
    env1_type: str = None
    env2_type: str = None
    envelope_details: list = field(default_factory=list)
    match_score: Optional[int] = None
    match_confidence: Optional[str] = None
    matched_patterns: list = field(default_factory=list)

    def __post_init__(self):
        """Build the message and details from the structured data."""
        if not self.processor_name:
            self.processor_name = 'TransferMerger'

        # Build message
        if not self.message and self.env1_id and self.env2_id:
            self.message = f"Unsafe merge: {self.env1_id} + {self.env2_id} differ by {self.loss:.2f}"

        # Build details
        if not self.details:
            self.details = {
                'reason': f"Cannot apply transfer_leakage to {self.env1_type} + {self.env2_type}",
                'amount_discrepancy': f"{self.loss:.2f}",
                'env1': {
                    'id': self.env1_id,
                    'type': self.env1_type,
                    'details': self.envelope_details[0] if len(self.envelope_details) > 0 else None
                },
                'env2': {
                    'id': self.env2_id,
                    'type': self.env2_type,
                    'details': self.envelope_details[1] if len(self.envelope_details) > 1 else None
                },
                'match_score': self.match_score,
                'match_confidence': self.match_confidence,
                'matched_patterns': self.matched_patterns[:3] if self.matched_patterns else []
            }
    
@dataclass
class TransferAmountMismatchWarning(ProcessingWarning):
    """Warning for transfer amount mismatches that can be tolerated."""
    outbound_amount: float = 0.0
    inbound_amount: float = 0.0
    envelope_id: str = None
    tolerance: float = None

    def __post_init__(self):
        """Build message and details from structured data."""
        if not self.processor_name:
            self.processor_name = 'TransferMerger'

        self.difference = abs(self.outbound_amount - self.inbound_amount)

        if not self.message:
            self.message = (
                f"Transfer amount mismatch in {self.envelope_id}: "
                f"difference of {self.difference:.2f}"
            )

        if not self.details:
            self.details = {
                'envelope_id': self.envelope_id,
                'outbound_amount': self.outbound_amount,
                'inbound_amount': self.inbound_amount,
                'difference': self.difference,
                'tolerance': self.tolerance
            }


@dataclass
class LotShortageWarning(ProcessingWarning):
    """Warning for insufficient lots when selling - important for tax but not fatal."""
    commodity: str = None
    account: str = None
    needed: float = 0.0
    available: float = 0.0
    transaction_id: str = None

    def __post_init__(self):
        """Build message and details."""
        if not self.processor_name:
            self.processor_name = 'SellTransactionProcessor'

        self.shortage = self.needed - self.available

        if not self.message:
            self.message = (
                f"Lot shortage for {self.commodity} in {self.account}: "
                f"SHORT {self.shortage:.4f}"
            )
            if self.transaction_id:
                self.message += f" (txn: {self.transaction_id})"

        if not self.details:
            self.details = {
                'commodity': self.commodity,
                'account': self.account,
                'needed': self.needed,
                'available': self.available,
                'shortage': self.shortage,
                'transaction_id': self.transaction_id
            }


@dataclass
class MissingAccountWarning(ProcessingWarning):
    """Warning for missing account information - can use suspense account."""
    envelope_id: str = None
    missing_field: str = None
    transaction_type: str = None
    amount: float = None
    currency: str = None

    def __post_init__(self):
        """Build message and details."""
        if not self.processor_name:
            self.processor_name = 'PostingResolver'

        if not self.message:
            self.message = (
                f"{self.transaction_type} {self.envelope_id} missing {self.missing_field}"
            )
            if self.amount and self.currency:
                self.message += f": {self.amount} {self.currency}"

        if not self.details:
            self.details = {
                'envelope_id': self.envelope_id,
                'missing_field': self.missing_field,
                'transaction_type': self.transaction_type,
                'amount': self.amount,
                'currency': self.currency,
                'resolution': 'Will use suspense account'
            }


@dataclass
class MultipleFlowWarning(ProcessingWarning):
    """Warning when multiple envelopes have conflicting flows."""
    envelope_ids: list = field(default_factory=list)
    flow_type: str = None  # 'inbound' or 'outbound'
    flow_details: dict = field(default_factory=dict)

    def __post_init__(self):
        """Build message and details."""
        if not self.processor_name:
            self.processor_name = 'TransferMerger'

        if not self.message:
            self.message = (
                f"Multiple envelopes have {self.flow_type} data "
                f"(only one allowed per merge group)"
            )

        if not self.details:
            self.details = {
                'envelope_ids': self.envelope_ids,
                'flow_type': self.flow_type,
                'flow_details': self.flow_details,
                'resolution': 'Will use first envelope as primary'
            }


@dataclass
class ProcessingResult:
    """The standard return type for all data processing components."""
    transactions: list[Transaction]
    warnings: list[ProcessingWarning] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    
    # Mandatory integrity tracking
    input_count: int = 0
    output_count: int = 0
    
    def validate_integrity(self) -> bool:
        """Verify no transactions were lost during processing."""
        return self.input_count == self.output_count
    
    def has_critical_issues(self) -> bool:
        """Check if any critical warnings exist."""
        return any(w.severity == 'CRITICAL' for w in self.warnings)
    
    def get_error_rate(self) -> float:
        """Calculate the rate of ERROR/CRITICAL warnings."""
        if self.input_count == 0:
            return 0.0
        error_count = sum(1 for w in self.warnings if w.is_data_loss_risk())
        return error_count / self.input_count