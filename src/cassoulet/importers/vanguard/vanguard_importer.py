"""Vanguard UK Importer - Thin Version.

Minimal importer that only handles Vanguard-specific quirks.
All heavy lifting is done by MultiFileBrokerImporter.
"""

from typing import Dict, List

from cassoulet.base.multi_file_broker_importer import MultiFileBrokerImporter
from cassoulet.utils.csv_row_data import CSVRowData


class VanguardImporter(MultiFileBrokerImporter):
    """Thin Vanguard importer - just institution-specific quirks."""
    
    def __init__(self, account: str, file_identifier: Dict[str, str] = None,
                 config: dict = None, debug: bool = False):
        """Initialize Vanguard importer."""
        super().__init__(
            account=account,
            file_patterns=file_identifier,
            config=config,
            debug=debug
        )
        
        # Institution derived from account path
        # Logger initialized by base class
    
    # get_file_types() no longer needed - extracted from config by MultiFileBrokerImporter
    
    def _process_row_data(self, parsed_data: Dict[str, List[CSVRowData]]) -> Dict[str, List[CSVRowData]]:
        """Process CSVRowData to apply Vanguard-specific fixes.

        Overrides the base class hook to filter duplicates, fix empty references,
        and handle in-specie transfers that Vanguard misclassifies.
        """
        # Apply Vanguard-specific fixes to the CSVRowData objects
        if 'cash' in parsed_data:
            parsed_data['cash'] = self._filter_duplicate_cash_entries(parsed_data['cash'])
            self._handle_empty_references(parsed_data['cash'])

        if 'transaction' in parsed_data:
            self._handle_empty_references(parsed_data['transaction'])
            # Fix in-specie transfers misclassified as sells
            self._fix_in_specie_transfers(parsed_data['transaction'])

        return parsed_data
    
    def _filter_duplicate_cash_entries(self, cash_rows: List[CSVRowData]) -> List[CSVRowData]:
        """Filter out Vanguard's duplicate 'Bought' and 'Sold' entries."""
        filtered = []
        for row in cash_rows:
            if row.narrative:
                narration_lower = row.narrative.lower()
                if narration_lower.startswith('bought ') or narration_lower.startswith('sold '):
                    self.logger.debug(f"Skipping duplicate: {row.narrative}")
                    continue
            filtered.append(row)
        return filtered
    
    def _handle_empty_references(self, rows: List[CSVRowData]) -> None:
        """Generate synthetic references for Vanguard's empty references in-place."""
        for row in rows:
            if not row.reference and row.date:
                # Generate synthetic reference from date + amount
                amount_str = str(abs(row.amount)) if row.amount else '0'
                row.reference = f"VG_{row.date.strftime('%Y%m%d')}_{amount_str}"
                if row.metadata is None:
                    row.metadata = {}
                row.metadata['synthetic_reference'] = True

    def _fix_in_specie_transfers(self, rows: List[CSVRowData]) -> None:
        """Fix Vanguard's in-specie transfers that are misclassified.

        Vanguard marks in-specie transfers with Transaction type "Unknown"
        and includes the market value as the amount, which causes them to be
        misinterpreted as SELL transactions. We need to clear the amount field
        for these transfers so they're correctly classified as COMMODITY_TRANSFER.

        Special handling for tiny amounts (< 5p) which are likely rounding adjustments.
        """
        from decimal import Decimal

        for row in rows:
            # Check if this is an in-specie transfer or rounding adjustment
            # Indicators: Transaction type is "Unknown" and we have quantity and commodity
            if (hasattr(row, 'raw_row') and
                row.raw_row.get('Transaction') == 'Unknown' and
                row.quantity and row.commodity):

                # Check if this is a tiny amount (likely rounding adjustment)
                if row.amount and abs(Decimal(str(row.amount))) < Decimal('0.05'):
                    # Tiny "Unknown" transaction - treat as rounding adjustment
                    self.logger.info(
                        f"Detected rounding adjustment on {row.date}: "
                        f"{row.quantity} {row.commodity} (value: {row.amount})"
                    )

                    if row.metadata is None:
                        row.metadata = {}
                    row.metadata['rounding_adjustment'] = True
                    row.metadata['adjustment_value'] = row.amount

                    # For rounding adjustments, we'll create a complete two-legged envelope
                    # by setting up metadata that the envelope builder will use
                    rounding_account = 'Equity:Rounding:Vanguard'

                    # Store the complete transaction structure in metadata
                    # The envelope builder will recognize this and create both legs
                    row.metadata['complete_transaction'] = {
                        'outbound_account': rounding_account,
                        'outbound_units': abs(row.quantity),
                        'outbound_type': row.commodity,
                        'inbound_account': self.account,
                        'inbound_units': abs(row.quantity),
                        'inbound_type': row.commodity
                    }

                    # Clear the amount - this is not a cash transaction
                    row.amount = None

                    self.logger.debug(
                        f"Created complete rounding adjustment: {row.commodity} from {rounding_account} to {self.account}"
                    )
                else:
                    # Regular in-specie transfer
                    # The amount is just market value, not actual cash flow
                    self.logger.info(
                        f"Detected in-specie transfer on {row.date}: "
                        f"{row.quantity} {row.commodity} (market value: {row.amount})"
                    )

                    # Clear the amount field to prevent misclassification as SELL
                    # This will cause envelope builder to correctly classify as COMMODITY_TRANSFER
                    if row.amount:
                        if row.metadata is None:
                            row.metadata = {}
                        row.metadata['market_value_at_transfer'] = row.amount
                        row.metadata['transfer_type'] = 'in_specie'
                        row.amount = None  # Clear amount so it's not treated as cash

                        self.logger.debug(
                            f"Cleared amount field for in-specie transfer to prevent SELL classification"
                        )