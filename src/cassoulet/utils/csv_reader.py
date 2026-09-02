"""
CSV Reader - Steel Thread Compliant

This module handles the complete CSV processing pipeline:
1. Open file and detect structure
2. Analyze headers for semantic meaning
3. Detect date formats
4. Process rows with clean column iteration

STEEL THREAD COMPLIANCE:
- Returns 3-tuple (rows, warnings, metadata)
- Accounts for EVERY row (no silent drops)
- Generates warnings for all processing decisions
- Tracks complete integrity metrics
"""

import csv
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Type, NamedTuple
from decimal import Decimal
from datetime import date
from cassoulet.utils.csv_row_data import CSVRowData
from cassoulet.utils.csv_utils import detect_semantic_field, _squash
from cassoulet.utils.dates import parse_date_strict, detect_date_format
from cassoulet.utils.cleaner import clean_string, parse_amount, EMPTY_VALUES
from cassoulet.base.exceptions import CSVNoHeadersError, CSVDataIntegrityError

logger = logging.getLogger(__name__)


@dataclass
class CSVWarning:
    """Structured warning for CSV processing issues."""
    type: str  # 'empty_row', 'missing_date', 'balance_row', 'parse_error', etc.
    message: str
    row_number: Optional[int] = None
    severity: str = 'INFO'  # INFO, WARNING, ERROR
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CSVProcessingResult:
    """Steel Thread compliant result from CSV processing."""
    rows: List[CSVRowData]
    warnings: List[CSVWarning]
    metadata: Dict[str, Any]
    
    @property
    def has_errors(self) -> bool:
        """Check if any ERROR severity warnings exist."""
        return any(w.severity == 'ERROR' for w in self.warnings)


@dataclass
class ColumnMapping:
    """Mapping for a single CSV column."""
    column_index: int
    column_name: str
    semantic_type: Optional[str]
    data_type: Type
    cleaning_hints: Dict[str, Any]


class CSVReader:
    """
    Clean CSV reader with one-time analysis and efficient row processing.
    
    The complete pipeline in one place:
    - Analyze CSV structure once
    - Process rows with simple column iteration
    - No repeated detection or complex fallbacks
    """
    
    # Semantic field types and their data types
    # These semantic types match CSVRowData attribute names directly
    SEMANTIC_DATA_TYPES = {
        'date': date,
        'settlement_date': date,
        'narrative': str,    # Transaction narrative/description
        'payee': str,
        'amount': Decimal,
        'debit': Decimal,
        'credit': Decimal,
        'payment': Decimal,     # Alternative naming for debit
        'receipt': Decimal,     # Alternative naming for credit
        'balance': Decimal,
        'type': str,
        'reference': str,
        'currency': str,
        'commodity_raw': str,  # Raw commodity string (needs resolution)
        'quantity': Decimal,
        'price': Decimal,
        'category': str,
        'notes': str,
    }
    
    
    def __init__(self, account: str, config: dict = None, debug: bool = False):
        """
        Initialize CSV reader.
        
        Args:
            account: Account for transactions
            config: Optional configuration
            debug: Enable debug logging
        """
        self.account = account
        self.config = config or {}
        self.debug = debug
        
        # Will be populated during analysis
        self.column_mappings: List[ColumnMapping] = []
        self.semantic_map: Dict[str, ColumnMapping] = {}
        self.date_format: Optional[str] = None
    
    def read_file(self, file_path: str, encoding: str = 'utf-8-sig') -> CSVProcessingResult:
        """
        Read and process a CSV file.
        
        Complete pipeline:
        1. Analyze structure
        2. Detect formats
        3. Process all rows
        
        Args:
            file_path: Path to CSV file
            encoding: File encoding
            
        Returns:
            CSVProcessingResult with (rows, warnings, metadata) - Steel Thread compliant
        """
        file_path = str(file_path)
        rows = []
        warnings = []
        total_rows = 0
        empty_rows = 0
        failed_rows = 0
        
        # Open file once and do everything
        with open(file_path, 'r', encoding=encoding) as f:
            # Detect dialect
            sample = f.read(4096)
            f.seek(0)
            
            try:
                dialect = csv.Sniffer().sniff(sample)
            except csv.Error:
                dialect = csv.excel()
                warnings.append(CSVWarning(
                    type='dialect_detection',
                    message='Could not detect CSV dialect, using excel format',
                    severity='INFO',
                    details={'file': file_path}
                ))
            
            reader = csv.reader(f, dialect=dialect)
            
            # Read first row - should be headers
            headers = next(reader, [])
            if not headers:
                # No headers found - this is a critical error
                raise CSVNoHeadersError(file_path)
            
            # ASSUMPTION: Data starts on row 2 (immediately after headers)
            # Read all data rows for processing
            sample_rows = []
            all_rows = []
            for i, row in enumerate(reader):
                total_rows += 1
                if row and any(cell.strip() for cell in row):  # Non-empty row
                    all_rows.append(row)
                    if i < 50:  # First 50 rows for date format detection
                        sample_rows.append(row)
                else:
                    empty_rows += 1
        
        # Analyze structure based on headers
        self._analyze_headers(headers)
        self._detect_date_format(sample_rows)
        
        # Log semantic mappings for debugging
        logger.debug("Semantic mappings:")
        for semantic_type, mapping in self.semantic_map.items():
            logger.debug(f"  {semantic_type}: {mapping.column_name} "
                        f"(col {mapping.column_index}, type {mapping.data_type.__name__})")
        if self.date_format:
            logger.debug(f"Date format: {self.date_format}")
            warnings.append(CSVWarning(
                type='date_format',
                message=f'Detected date format: {self.date_format}',
                severity='INFO',
                details={'format': self.date_format}
            ))
        
        # Process all data rows
        # Line numbers start at 2 since row 1 was headers
        balance_rows = 0
        for line_num, row in enumerate(all_rows, start=2):
            # Note: Empty check already done when building all_rows
            row_data, skip_reason = self._process_row(row, line_num, file_path)
            if row_data:
                rows.append(row_data)
            elif skip_reason == 'balance_row':
                balance_rows += 1
                warnings.append(CSVWarning(
                    type='balance_row',
                    message='Skipped balance row',
                    row_number=line_num,
                    severity='INFO',
                    details={'row_content': row[:5] if row else []}
                ))
            elif skip_reason == 'missing_date':
                failed_rows += 1
                warnings.append(CSVWarning(
                    type='missing_date',
                    message='Failed to process row (missing required date)',
                    row_number=line_num,
                    severity='WARNING',
                    details={'row_content': row[:5] if row else []}
                ))
            elif skip_reason == 'date_parse_error':
                failed_rows += 1
                warnings.append(CSVWarning(
                    type='date_parse_error',
                    message='Failed to parse date in row',
                    row_number=line_num,
                    severity='WARNING',
                    details={'row_content': row[:5] if row else []}
                ))
            else:
                # Unknown failure
                failed_rows += 1
                warnings.append(CSVWarning(
                    type='parse_error',
                    message=f'Failed to process row: {skip_reason or "unknown error"}',
                    row_number=line_num,
                    severity='WARNING',
                    details={'row_content': row[:5] if row else []}
                ))
        
        # Report empty rows if any
        if empty_rows > 0:
            warnings.append(CSVWarning(
                type='empty_rows',
                message=f'Skipped {empty_rows} empty rows',
                severity='INFO',
                details={'count': empty_rows}
            ))
        
        # Build comprehensive metadata
        metadata = {
            'file': file_path,
            'encoding': encoding,
            'dialect': dialect.__class__.__name__,
            'input_rows': total_rows + 1,  # Total lines in file (data rows + 1 header row)
            'header_rows': 1,  # We successfully read headers
            'data_rows': total_rows,
            'empty_rows': empty_rows,
            'balance_rows': balance_rows,
            'failed_rows': failed_rows,
            'output_rows': len(rows),
            'semantic_mappings': {k: v.column_name for k, v in self.semantic_map.items()},
            'date_format': self.date_format
        }
        
        # Verify data integrity
        expected_output = total_rows - empty_rows - balance_rows - failed_rows
        if len(rows) != expected_output:
            # This is a critical error - our counting logic is wrong
            raise CSVDataIntegrityError(
                expected=expected_output,
                actual=len(rows),
                details={
                    'total_rows': total_rows,
                    'empty_rows': empty_rows,
                    'balance_rows': balance_rows,
                    'failed_rows': failed_rows,
                    'file': file_path
                }
            )
        
        logger.info(
            f"CSV Reader: Processed {total_rows} data rows from {file_path} -> "
            f"{len(rows)} output rows ({empty_rows} empty, {failed_rows} failed)"
        )
        
        return CSVProcessingResult(rows, warnings, metadata)
    
    def _analyze_headers(self, headers: List[str]):
        """
        Analyze headers and create semantic mappings.
        
        Args:
            headers: List of column headers
        """
        self.column_mappings = []
        self.semantic_map = {}
        
        for idx, header in enumerate(headers):
            # Check config for explicit mapping
            semantic_type = None
            explicit_mapping = False
            if 'column_mappings' in self.config:
                if header in self.config['column_mappings']:
                    semantic_type = self.config['column_mappings'][header]
                    explicit_mapping = True

            # Auto-detect if not explicitly mapped (including explicit None to skip)
            if not explicit_mapping:
                semantic_type = detect_semantic_field(header)
            
            # Determine data type
            data_type = self.SEMANTIC_DATA_TYPES.get(semantic_type, str)
            
            # Create mapping - store original semantic type
            mapping = ColumnMapping(
                column_index=idx,
                column_name=header,
                semantic_type=semantic_type,  # Keep original for skip checks etc.
                data_type=data_type,
                cleaning_hints={}
            )
            
            self.column_mappings.append(mapping)
            
            # Store in semantic map
            if semantic_type:
                # Two columns claiming one meaning is a data-loss bug, not a
                # preference: the later column wins and the earlier one is
                # discarded with no trace. Monzo's 'Name' was configured as the
                # narrative and then overwritten by 'Description', which silently
                # threw away every counterparty in the file.
                previous = self.semantic_map.get(semantic_type)
                if previous is not None and previous.column_name != header:
                    logger.warning(
                        "Columns '%s' and '%s' both map to '%s'; '%s' wins and "
                        "'%s' is discarded. Map one of them explicitly.",
                        previous.column_name, header, semantic_type,
                        header, previous.column_name,
                    )
                self.semantic_map[semantic_type] = mapping

        # Post-processing: For investment transactions, ensure commodity_raw is populated
        # commodity_raw = unresolved commodity string from CSV (any column)
        # commodity = resolved symbol (populated later by broker importer)
        has_investment_data = (
            'quantity' in self.semantic_map or
            'price' in self.semantic_map
        )
        has_commodity_source = 'commodity_raw' in self.semantic_map
        has_narrative = 'narrative' in self.semantic_map

        if has_investment_data and not has_commodity_source and has_narrative:
            # Best effort: use narrative/description as commodity source for investment transactions
            narrative_mapping = self.semantic_map['narrative']

            logger.debug(f"Investment data detected: using '{narrative_mapping.column_name}' as commodity source")

            # Create a mapping for commodity_raw (the unresolved commodity string)
            commodity_mapping = ColumnMapping(
                column_index=narrative_mapping.column_index,
                column_name=narrative_mapping.column_name,
                semantic_type='commodity_raw',  # Raw commodity string to be resolved
                data_type=str,
                cleaning_hints={}
            )
            # Add to both column_mappings and semantic_map
            self.column_mappings.append(commodity_mapping)
            self.semantic_map['commodity_raw'] = commodity_mapping
            # Note: We keep 'narrative' mapping too, so both fields get populated from same column

        self._adopt_name_column_as_payee()

    def _adopt_name_column_as_payee(self):
        """Use a bare 'Name' column as the payee when nothing better exists.

        Monzo names the counterparty column 'Name' and puts the bank's own
        reference in 'Description'. With no payee detected the importer kept the
        reference and discarded the name, so GBP 82,620 of payments to a column
        that plainly said "Coinbase" were stored as "CBAGBPXQFYSTGW" and could
        be neither categorised nor matched to anything.

        'Name' is NOT in HEADER_PATTERNS because it is too weak to trust on its
        own: detection is substring-based for patterns over three characters, so
        it would also swallow 'Account Name' and 'Holder Name'. It is only safe
        here, where the whole header set is visible and two things can be
        checked - that no real payee column was found, and that this is not a
        holdings file, where 'Name' is the security name rather than a
        counterparty (one such file in the reference data is
        'Symbol,Name,Qty,Price,...').
        """
        if 'payee' in self.semantic_map:
            return
        if 'quantity' in self.semantic_map or 'price' in self.semantic_map:
            return  # holdings/positions file - 'Name' is the instrument
        for mapping in self.column_mappings:
            if mapping.semantic_type is not None:
                continue
            if _squash(mapping.column_name) != 'name':
                continue
            payee_mapping = ColumnMapping(
                column_index=mapping.column_index,
                column_name=mapping.column_name,
                semantic_type='payee',
                data_type=str,
                cleaning_hints={},
            )
            self.column_mappings.append(payee_mapping)
            self.semantic_map['payee'] = payee_mapping
            logger.debug("No payee column detected; adopting '%s' as payee",
                         mapping.column_name)
            return

    def _detect_date_format(self, sample_rows: List[List[str]]):
        """
        Detect date format from sample data.
        
        Args:
            sample_rows: Sample rows for format detection
        """
        date_mapping = self.semantic_map.get('date')
        if not date_mapping:
            return
        
        # Collect date samples - need enough to find unambiguous dates (day > 12)
        # This helps distinguish between DD/MM/YYYY and MM/DD/YYYY formats
        date_samples = []
        for row in sample_rows:
            if len(row) > date_mapping.column_index:
                date_str = row[date_mapping.column_index]
                if date_str and date_str not in EMPTY_VALUES:
                    date_samples.append(date_str)
                    # Stop once we have enough samples with unambiguous dates
                    if len(date_samples) >= 50:  # More samples = better detection
                        break
        
        if date_samples:
            # Date format is CSV-wide - all date columns use the same format
            self.date_format = detect_date_format(date_samples)
    
    def _process_row(self, row: List[str], line_num: int, source_file: str) -> tuple[Optional[CSVRowData], Optional[str]]:
        """
        Process a single CSV row and return skip reason if failed.
        
        Args:
            row: CSV row as list
            line_num: Line number
            source_file: Source file path
            
        Returns:
            Tuple of (CSVRowData or None, skip_reason or None)
        """
        row_data = CSVRowData()
        row_data.row_number = line_num
        row_data.source_file = source_file

        # Store raw row as dictionary for metadata preservation
        raw_row_dict = {}
        for mapping in self.column_mappings:
            if mapping.column_index < len(row):
                raw_row_dict[mapping.column_name] = row[mapping.column_index]
        row_data.raw_row = raw_row_dict

        # Clean iteration through columns
        for mapping in self.column_mappings:
            if mapping.column_index >= len(row):
                continue
            
            if not mapping.semantic_type:
                continue
            
            # Extract value
            raw_value = row[mapping.column_index]

            if not raw_value or raw_value in EMPTY_VALUES:
                continue

            # Clean value based on type
            if mapping.data_type == Decimal:
                # Always preserve the sign from the CSV
                # Importers can adjust signs based on their conventions
                value = parse_amount(raw_value, preserve_sign=True)
            
            elif mapping.data_type == date:
                # Use CSV-wide date format
                try:
                    value = parse_date_strict(raw_value, self.date_format)
                except ValueError as e:
                    # Failed to parse date - log and skip row
                    logger.debug(f"Failed to parse date at row {line_num}: {e}")
                    return None, 'date_parse_error'
            
            elif mapping.data_type == str:
                value = clean_string(raw_value, remove_quotes=True, remove_bom=True)
                        
                # Check for skip patterns when we process narrative text
                if mapping.semantic_type == 'narrative':
                    value_lower = value.lower()
                    skip_patterns = [
                        'opening balance', 'closing balance',
                        'beginning balance', 'ending balance',
                        'starting balance', 'final balance'
                    ]
                    if any(pattern in value_lower for pattern in skip_patterns):
                        logger.debug(f"Skipping line {line_num} (balance row)")
                        return None, 'balance_row'
                
            # Set attribute directly - semantic type matches attribute name
            setattr(row_data, mapping.semantic_type, value)
        
        # Check if row has minimum required data
        # A row must have at least a date to be valid
        if not row_data.date:
            return None, 'missing_date'  # Missing required field
        
        return row_data, None  # Success, no skip reason
    