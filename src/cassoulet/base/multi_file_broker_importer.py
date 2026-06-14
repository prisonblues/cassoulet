"""Multi-File Broker Importer

MultiFileBrokerImporter that inherits directly from BrokerImporter,
"""

import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from cassoulet.base.broker_base import BrokerImporter
from cassoulet.utils.csv_row_data import CSVRowData
from cassoulet.stages import Envelope
from cassoulet.utils.envelope_builder import EnvelopeBuilder
from cassoulet.utils.csv_row_data import CSVRowData
# from cassoulet.utils.reference_matcher import ReferenceMatcher  # Commented out - user doesn't want to use


class MultiFileBrokerImporter(BrokerImporter):
    """Base class for brokers that use multiple files (transaction + cash).
    
    Inherits corporate action detection from BrokerImporter.
    Adds multi-file specific features:
    - Reference matching between files
    - File pattern matching
    - Paired file processing
    """
    
    def __init__(
        self,
        account: str,
        file_patterns: Optional[Dict[str, str]] = None,
        **kwargs
    ):
        """Initialize multi-file broker importer.

        Args:
            account: Account for transactions
            file_patterns: Dict mapping file types to patterns
                          e.g., {'transaction': '*_txn.csv', 'cash': '*_cash.csv'}
        """
        super().__init__(account=account, **kwargs)

        # Get file patterns from config's file_identifier if not provided
        if file_patterns is None:
            file_identifier = self.config.get('file_identifier', {})
            if isinstance(file_identifier, dict):
                # Convert file identifiers to patterns
                self.file_patterns = file_identifier
            else:
                self.file_patterns = {}
        else:
            self.file_patterns = file_patterns

        # Also store as file_identifier for pipeline compatibility
        self.file_identifier = self.file_patterns

    def get_file_types(self) -> List[str]:
        """Get list of file types this importer handles.

        Extracted from file_patterns/config, no longer abstract.

        Returns:
            List of file type strings (e.g., ['transaction', 'cash'])
        """
        return list(self.file_patterns.keys())
    
    def identify(self, file_path: str) -> bool:
        """Check if this importer can handle the file.
        
        For multi-file importers, returns True if the file matches
        any of the expected patterns.
        
        Args:
            file_path: Path to file
            
        Returns:
            True if this importer can handle the file
        """
        filename = Path(file_path).name
        
        for file_type, pattern in self.file_patterns.items():
            if self._matches_pattern(filename, pattern):
                return True
        
        return False
    
    def _matches_pattern(self, filename: str, pattern: str) -> bool:
        """Check if filename matches pattern.
        
        Simple pattern matching supporting * wildcards.
        
        Args:
            filename: Name of file
            pattern: Pattern to match (e.g., '*_txn.csv')
            
        Returns:
            True if matches
        """
        import fnmatch
        return fnmatch.fnmatch(filename.lower(), pattern.lower())
    
    def extract(self, file_dict: Dict[str, str]) -> List[Envelope]:
        """Extract transactions from a file set.

        Multi-file importer: takes a dict mapping file types to paths.
        This is called ONCE by the pipeline with all related files.

        Args:
            file_dict: Dict mapping file types to file paths
                      e.g., {'transaction': 'path/to/txn.csv', 'cash': 'path/to/cash.csv'}

        Returns:
            List of transaction envelopes
        """
        if not file_dict:
            self.logger.warning(f"No files provided to extract")
            return []

        self.logger.info(f"Processing file set: {list(file_dict.values())}")
        return self.process_file_set(file_dict)

    def process_file_set(self, file_dict: Dict[str, str]) -> List[Envelope]:
        """Process a set of related files.

        CRITICAL: This method aggregates data from multiple CSV files BEFORE creating
        envelopes. Each logical transaction should result in ONE envelope with ONE ID,
        not multiple envelopes that need deduplication.

        Args:
            file_dict: Dictionary mapping file types to file paths

        Returns:
            List of transaction envelopes
        """
        envelopes = []

        # Get source identifier from config (the canonical prefix for this importer)
        self._source_identifier = self.config.get('prefix', f"{self.institution.lower()}_unknown")

        # Load and parse CSV files into CSVRowData objects
        parsed_data = self._load_and_parse_files(file_dict)

        if not parsed_data:
            return envelopes

        # Let subclasses process/fix the CSVRowData if needed
        processed_data = self._process_row_data(parsed_data)

        # Extract transaction and cash data
        transaction_rows = processed_data.get('transaction', [])
        cash_rows = processed_data.get('cash', [])

        # Match transaction rows with cash rows
        matched_pairs, unmatched_txns, unmatched_cash = self._match_rows(
            transaction_rows, cash_rows
        )

        # ISSUE #26 FIX: Filter out non-cash corporate actions
        # Build index of cash references for structural detection
        cash_references = {row.reference for row in cash_rows if row.reference}

        # Filter unmatched transactions to remove corporate actions
        real_unmatched_txns = []
        corporate_actions = []

        for txn_row in unmatched_txns:
            # Structural detection: quantity=0 AND reference NOT in cash = corporate action
            is_corporate_action = (
                txn_row.quantity == 0 and
                txn_row.reference and
                txn_row.reference not in cash_references
            )

            if is_corporate_action:
                corporate_actions.append(txn_row)
                self.logger.info(
                    f"Corporate action detected (structural): {txn_row.narrative} "
                    f"with notional value {txn_row.amount}"
                )
            else:
                real_unmatched_txns.append(txn_row)

        if corporate_actions:
            self.logger.info(
                f"Filtered out {len(corporate_actions)} non-cash corporate actions "
                f"(accumulation distributions, etc.)"
            )

        # Create ONE envelope per matched pair
        for txn_row, cash_row in matched_pairs:
            envelope = self._create_envelope_from_matched_pair(txn_row, cash_row)
            if envelope:
                envelopes.append(envelope)

        # Handle unmatched rows - these represent real transactions that don't have
        # corresponding entries in the other file (e.g., fees, interest, dividends)
        # Each unmatched row correctly becomes its own envelope with its own ID

        # Create envelopes for unmatched transactions (excluding corporate actions)
        for txn_row in real_unmatched_txns:
            envelope = self._create_envelope_from_transaction(txn_row)
            if envelope:
                envelopes.append(envelope)

        # Create envelopes for unmatched cash (usually fees, interest, etc.)
        for cash_row in unmatched_cash:
            envelope = self._create_envelope_from_cash(cash_row)
            if envelope:
                envelopes.append(envelope)

        return envelopes
    
    def _match_rows(
        self,
        transaction_rows: List[CSVRowData],
        cash_rows: List[CSVRowData]
    ) -> Tuple[List[Tuple[CSVRowData, CSVRowData]], List[CSVRowData], List[CSVRowData]]:
        """Match transaction rows with cash rows based on date and reference.

        Uses a simple matching algorithm:
        1. Try to match by reference if both have references
        2. Fall back to matching by date and amount
        3. Unmatched rows are returned separately

        Args:
            transaction_rows: List of transaction CSVRowData
            cash_rows: List of cash CSVRowData

        Returns:
            Tuple of (matched_pairs, unmatched_transactions, unmatched_cash)
        """
        matched_pairs = []
        unmatched_txns = []
        unmatched_cash = cash_rows.copy()  # Will remove matched ones

        for txn_row in transaction_rows:
            matched = False

            # First try to match by reference
            if txn_row.reference:
                for i, cash_row in enumerate(unmatched_cash):
                    if cash_row.reference and cash_row.reference == txn_row.reference:
                        matched_pairs.append((txn_row, cash_row))
                        unmatched_cash.pop(i)
                        matched = True
                        self.logger.debug(
                            f"Matched by reference: {txn_row.reference} "
                            f"({txn_row.date} {txn_row.narrative})"
                        )
                        break

            # If no reference match, try date and amount
            if not matched and txn_row.date and txn_row.amount:
                for i, cash_row in enumerate(unmatched_cash):
                    # Match if same date and similar amount (within tolerance)
                    if (cash_row.date == txn_row.date and
                        cash_row.amount and
                        abs(cash_row.amount - txn_row.amount) < 0.01):
                        matched_pairs.append((txn_row, cash_row))
                        unmatched_cash.pop(i)
                        matched = True
                        self.logger.debug(
                            f"Matched by date/amount: {txn_row.date} "
                            f"{txn_row.amount} ({txn_row.narrative})"
                        )
                        break

            # If still no match, it's an unmatched transaction
            if not matched:
                unmatched_txns.append(txn_row)
                self.logger.debug(
                    f"Unmatched transaction: {txn_row.date} {txn_row.narrative}"
                )

        self.logger.info(
            f"Row matching results: {len(matched_pairs)} matched, "
            f"{len(unmatched_txns)} unmatched transactions, "
            f"{len(unmatched_cash)} unmatched cash"
        )

        return matched_pairs, unmatched_txns, unmatched_cash

    def _load_and_parse_files(self, file_dict: Dict[str, str]) -> Dict[str, List[CSVRowData]]:
        """Load and parse CSV files into CSVRowData objects.
        
        Uses file-type-specific CSV mappings if available, otherwise falls back
        to the default mappings.
        
        Args:
            file_dict: Dictionary mapping file types to file paths
            
        Returns:
            Dictionary mapping file types to lists of CSVRowData objects
        """
        parsed_data = {}
        
        for file_type, file_path in file_dict.items():
            # Check if we have file-type-specific mappings
            mappings = self.config.get('csv_mappings', {})
            if file_type in mappings:
                # Temporarily update csv_reader with file-type-specific mappings
                original_config = self.csv_reader.config.copy()
                self.csv_reader.config.update(mappings[file_type])
                rows = self.parse_csv_file(file_path)
                # Restore original config
                self.csv_reader.config = original_config
            else:
                # Use default mappings
                rows = self.parse_csv_file(file_path)

            # Don't apply sign fixing here - it will be done after combining rows
            # We only resolve commodities at this stage since that's just symbol lookup
            if rows:
                # Resolve commodity symbols (but NOT sign fixing yet)
                rows = self.resolve_commodities(rows)
            
            if rows:
                parsed_data[file_type] = rows
                self.logger.info(
                    f"Parsed {len(rows)} rows from {file_type} file"
                )
        
        return parsed_data
    
    def _create_envelope_from_matched_pair(
        self,
        txn_row: CSVRowData,
        cash_row: CSVRowData
    ) -> Optional[Envelope]:
        """Create envelope from matched transaction and cash rows.

        CRITICAL: This creates ONE envelope from the matched pair.
        The transaction row provides the primary source/line number for ID generation.
        The cash row provides supplementary data (like settlement date).

        Args:
            txn_row: Transaction row (primary source)
            cash_row: Cash row (supplementary data)

        Returns:
            Single transaction envelope with combined data
        """
        # Combine the data at the CSVRowData level
        # Transaction row is PRIMARY - it provides the line number for ID generation
        combined_row = CSVRowData(
            date=txn_row.date,
            narrative=txn_row.narrative,
            amount=txn_row.amount or cash_row.amount,
            debit=txn_row.debit or cash_row.debit,
            credit=txn_row.credit or cash_row.credit,
            payment=txn_row.payment or cash_row.payment,
            receipt=txn_row.receipt or cash_row.receipt,
            balance=txn_row.balance or cash_row.balance,
            reference=txn_row.reference or cash_row.reference,
            settlement_date=cash_row.settlement_date or txn_row.settlement_date,  # Cash file usually has settlement
            quantity=txn_row.quantity,
            price=txn_row.price,
            commodity=txn_row.commodity,
            payee=txn_row.payee or cash_row.payee,
            type=txn_row.type,  # Preserve transaction type for sign fixing
            commodity_raw=txn_row.commodity_raw,  # Preserve for commodity resolution
            source_file=txn_row.source_file,  # PRIMARY source file
            row_number=txn_row.row_number,     # PRIMARY row number for ID generation
            raw_row={**txn_row.raw_row, **{f"cash_{k}": v for k, v in cash_row.raw_row.items()}},
            metadata={
                'matched_pair': True,
                'cash_source_file': cash_row.source_file,
                'cash_row_number': cash_row.row_number,
                'txn_row_number': txn_row.row_number,
                '_source_identifier': self._source_identifier  # Pass this through metadata
            }
        )

        # Use the base class method which handles sign fixing and commodity resolution
        envelopes = self.process_rows_to_envelopes([combined_row])
        envelope = envelopes[0] if envelopes else None

        # Add history to track the matching
        if envelope:
            envelope.add_history(
                state=envelope.state,
                component='multi_file_broker_importer',
                action='Matched transaction and cash rows',
                details={
                    'txn_file': txn_row.source_file,
                    'txn_row': txn_row.row_number,
                    'cash_file': cash_row.source_file,
                    'cash_row': cash_row.row_number
                }
            )

        return envelope
    
    def _create_envelope_from_transaction(
        self,
        txn_row: CSVRowData
    ) -> Optional[Envelope]:
        """Create envelope from unmatched transaction row.

        Args:
            txn_row: Transaction row

        Returns:
            Transaction envelope or None
        """
        # Add source identifier to metadata
        if not txn_row.metadata:
            txn_row.metadata = {}
        txn_row.metadata['_source_identifier'] = self._source_identifier

        # Use the base class method which handles sign fixing and commodity resolution
        # (process_rows_to_envelopes has Steel Thread try/except protection)
        envelopes = self.process_rows_to_envelopes([txn_row])
        return envelopes[0] if envelopes else None

    def _create_envelope_from_cash(
        self,
        cash_row: CSVRowData
    ) -> Optional[Envelope]:
        """Create envelope from unmatched cash row.

        Args:
            cash_row: Cash row

        Returns:
            Transaction envelope or None
        """
        # Get sign_convention from config - cash files may use payment/receipt columns
        sign_convention = self.config.get('sign_convention', 'standard')

        try:
            return EnvelopeBuilder.from_csv_row_data(
                cash_row,
                account=self.account,
                institution=self.institution,
                sign_convention=sign_convention,
                source_file=self._source_identifier  # Use common prefix, not individual file
            )
        except ValueError as e:
            # STEEL THREAD: Log error but don't crash - allow processing to continue
            self.logger.warning(f"Skipping cash row {cash_row.row_number}: {e}")
            return None
    
    def _process_row_data(self, parsed_data: Dict[str, List[CSVRowData]]) -> Dict[str, List[CSVRowData]]:
        """Hook for subclasses to process/fix CSVRowData objects.
        
        Override this in subclasses to apply institution-specific fixes
        to the CSVRowData objects (e.g., fix mislabeled transactions,
        generate synthetic references, filter duplicates, etc.).
        
        Args:
            parsed_data: Dictionary mapping file types to lists of CSVRowData
            
        Returns:
            Processed CSVRowData (may be modified in-place)
        """
        return parsed_data