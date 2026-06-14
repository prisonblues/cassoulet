"""
Envelope Builder - Steel Thread Compliant Envelope Creation

This module provides the central utility for creating Envelopes from various sources,
ensuring Steel Thread compliance with full audit trail and history tracking.

Key features:
- Deterministic ID generation
- Sign convention handling (standard, reversed, debit_credit, payment_receipt)
- Investment transaction support
- Multi-file enrichment support
- Complete history tracking from creation
"""

from typing import Optional, Dict, Any
from decimal import Decimal
from datetime import datetime
import os
import logging

from cassoulet.utils.currencies import is_currency, is_commodity
from cassoulet.utils.csv_row_data import CSVRowData
from cassoulet.utils.deterministic_id import generate_id
from cassoulet.utils.envelope_utilities import create_envelope
from cassoulet.stages import Envelope, EnvelopeState

logger = logging.getLogger(__name__)


def csv_row_has_amount_data(row_data: CSVRowData) -> bool:
    """Check if a CSV row has any amount data.

    Used by importers to filter out non-transaction rows.
    """
    return any([
        row_data.amount is not None,
        row_data.debit is not None,
        row_data.credit is not None,
        row_data.payment is not None,
        row_data.receipt is not None
    ])


def csv_row_has_investment_data(row_data: CSVRowData) -> bool:
    """Check if a CSV row has investment-specific data.

    Used by broker importers to identify investment transactions.
    """
    return any([
        row_data.quantity is not None,
        row_data.price is not None,
        row_data.commodity is not None
    ])


class EnvelopeBuilder:
    """
    Central utility for creating Envelopes from various sources.

    This class provides consistent envelope creation from CSVRowData,
    handling institution-specific sign conventions and data enrichment.

    Steel Thread Compliance:
    - Every envelope gets a deterministic ID
    - Every envelope starts with INGESTED state
    - Every envelope has initial history entry
    - All metadata preserved for forensic analysis
    """

    @staticmethod
    def from_csv_row_data(row_data: CSVRowData,
                          account: str,
                          institution: str,
                          sign_convention: str = 'standard',
                          source_file: Optional[str] = None) -> Envelope:
        """
        Create an envelope from a single CSVRowData row.

        NOTE: We trust that CSVRowData contains clean data. The CSV reader
        is responsible for cleaning during parsing. We don't clean again here
        to avoid redundant processing and maintain data traceability.

        Args:
            row_data: Parsed CSV data (assumed to be already cleaned)
            account: Account path (e.g., "Assets:Bank:HSBC:Checking")
            institution: Institution name (e.g., "HSBC")
            sign_convention: How to interpret amounts:
                - 'standard': negative = outflow, positive = inflow
                - 'reversed': negative = inflow, positive = outflow
                - 'debit_credit': use debit/credit columns
                - 'payment_receipt': use payment/receipt columns
            source_file: Optional source file path for tracking

        Returns:
            Envelope with inbound/outbound properly set and Steel Thread tracking
        """
        # Determine inbound/outbound based on sign convention
        outbound_units = None
        inbound_units = None

        if sign_convention == 'standard':
            # Standard: negative = outflow, positive = inflow
            if row_data.amount is not None:
                if row_data.amount < 0:
                    outbound_units = abs(row_data.amount)
                else:
                    inbound_units = row_data.amount

        elif sign_convention == 'reversed':
            # Some institutions use opposite convention
            if row_data.amount is not None:
                if row_data.amount < 0:
                    inbound_units = abs(row_data.amount)
                else:
                    outbound_units = row_data.amount

        elif sign_convention == 'debit_credit':
            # Separate debit/credit columns
            if row_data.debit is not None:
                outbound_units = abs(row_data.debit)
            if row_data.credit is not None:
                inbound_units = abs(row_data.credit)

        elif sign_convention == 'payment_receipt':
            # Alternative naming
            if row_data.payment is not None:
                outbound_units = abs(row_data.payment)
            if row_data.receipt is not None:
                inbound_units = abs(row_data.receipt)

        # Generate envelope ID using all available data
        # Pass raw_row directly - generate_id will handle field selection
        # and filtering of empty values
        if row_data.raw_row:
            hash_data = dict(row_data.raw_row)  # Copy to avoid modifying original
        else:
            # Fallback if raw_row is not populated (shouldn't happen with proper CSV reader)
            hash_data = {
                'date': row_data.date,
                'narrative': row_data.narrative,
                'amount': row_data.amount,
                'reference': row_data.reference
            }

        # Generate envelope ID using deterministic ID
        # Source file and line number act as salts for uniqueness
        envelope_id = generate_id(
            prefix=institution.lower(),
            data=hash_data,
            line_number=row_data.row_number,
            source_file=row_data.source_file  # Pass as parameter, not in data
        )

        # Extract debugging info that was added to hash_data
        id_generation_input = hash_data.pop('_id_generation_input', None)
        id_generation_line_number = hash_data.pop('_id_generation_line_number', None)
        id_generation_source_file = hash_data.pop('_id_generation_source_file', None)

        # Use the actual source file from CSVRowData
        actual_source_file = source_file or row_data.source_file
        if actual_source_file:
            source_identifier = os.path.basename(actual_source_file)
        else:
            # Log warning - source file should always be provided
            logger.warning(
                f"No source file provided for envelope creation "
                f"(institution={institution}, row={row_data.row_number})"
            )
            source_identifier = f"{institution.lower()}_unknown"

        # Create metadata for Steel Thread compliance
        metadata = {
            'transaction_id': envelope_id,  # Keep for backward compatibility
            'source_file': actual_source_file,
            'line_number': row_data.row_number,  # Actual line number from CSV
            'institution': institution,
            'import_timestamp': datetime.now().isoformat(),
            # Add ID generation debugging info
            'id_generation_input': id_generation_input,
            'id_generation_line_number': id_generation_line_number,
            'id_generation_source_file': id_generation_source_file
        }

        # Add all fields from raw_row that aren't used for envelope construction
        if row_data.raw_row:
            # Whitelist: fields used for envelope construction (excluded from metadata)
            envelope_construction_fields = {
                'date', 'narrative', 'payee', 'amount', 'debit', 'credit',
                'payment', 'receipt', 'quantity', 'price', 'commodity', 'balance'
            }

            # Everything else goes into metadata as-is
            for key, value in row_data.raw_row.items():
                # Skip empty values and fields used for envelope construction
                if key.lower() not in envelope_construction_fields and value not in ['', None, 'n/a', 'N/A']:
                    # Simple key sanitization for safety
                    safe_key = key.replace(' ', '_').replace('-', '_')
                    metadata[safe_key] = str(value)

        # Preserve balance data if available
        # Balance from CSV is 'express' (from the bank), vs 'implied' (calculated by us)
        balance_after = row_data.balance if row_data.balance is not None else None
        balance_type = 'express' if balance_after is not None else None

        # Check if row metadata specifies a complete transaction (e.g., rounding adjustments)
        if row_data.metadata and row_data.metadata.get('complete_transaction'):
            # Use the complete transaction structure provided by the importer
            complete = row_data.metadata['complete_transaction']
            final_outbound_account = complete.get('outbound_account')
            final_outbound_units = complete.get('outbound_units')
            final_outbound_type = complete.get('outbound_type')
            final_inbound_account = complete.get('inbound_account', account)
            final_inbound_units = complete.get('inbound_units')
            final_inbound_type = complete.get('inbound_type')
            transaction_type = 'COMMODITY_TRANSFER'

            # Preserve the rounding adjustment metadata
            metadata.update(row_data.metadata)

            logger.debug(
                f"Using complete transaction from metadata: {final_outbound_type} "
                f"from {final_outbound_account} to {final_inbound_account}"
            )
        # Initialize envelope fields with cash values
        # For pure cash transactions, these are the final values
        # For investment transactions (BUY/SELL), these represent the cash leg,
        # and the commodity leg will be set below
        else:
            transaction_type = 'cash_transaction'
            final_outbound_units = outbound_units
            final_outbound_type = 'GBP' if outbound_units else None
            final_outbound_account = account if outbound_units else None
            final_inbound_units = inbound_units
            final_inbound_type = 'GBP' if inbound_units else None
            final_inbound_account = account if inbound_units else None

        # Adjust for investment transactions (skip if we already have a complete transaction)
        if csv_row_has_investment_data(row_data) and row_data.commodity and not (row_data.metadata and row_data.metadata.get('complete_transaction')):
            if outbound_units:
                # BUY transaction - cash out, commodity in
                transaction_type = 'BUY'
                final_inbound_units = abs(row_data.quantity) if row_data.quantity else None
                final_inbound_type = row_data.commodity
                final_inbound_account = account

                # Note: Unit price calculation happens in lot processor
                # which can properly account for fees and other factors

            elif inbound_units:
                # SELL transaction - commodity out, cash in
                transaction_type = 'SELL'
                final_outbound_units = abs(row_data.quantity) if row_data.quantity else None
                final_outbound_type = row_data.commodity
                final_outbound_account = account

                # For SELL, we don't need unit_price (use FIFO booking)
                # But we could calculate sale price if needed for reporting
                # sale_price = inbound_units / final_outbound_units

            else:
                # In-specie transfer - commodity movement without cash
                # This is ALWAYS a COMMODITY_TRANSFER
                # Whether it's internal (between our accounts) or external (from/to outside)
                # will be determined after merge phase by checking if it's matched

                if row_data.quantity and row_data.quantity > 0:
                    # Receiving commodity (could be from internal or external source)
                    transaction_type = 'COMMODITY_TRANSFER'
                    final_inbound_units = abs(row_data.quantity)
                    final_inbound_type = row_data.commodity
                    final_inbound_account = account
                    # No cash movement
                    final_outbound_units = None
                    final_outbound_type = None
                    final_outbound_account = None
                elif row_data.quantity and row_data.quantity < 0:
                    # Sending commodity (could be to internal or external destination)
                    transaction_type = 'COMMODITY_TRANSFER'
                    final_outbound_units = abs(row_data.quantity)
                    final_outbound_type = row_data.commodity
                    final_outbound_account = account
                    # No cash movement
                    final_inbound_units = None
                    final_inbound_type = None
                    final_inbound_account = None
                else:
                    # Edge case: has commodity but no quantity
                    # This shouldn't happen but log it as a cash transaction
                    logger.warning(
                        f"Row {row_data.row_number}: Has commodity '{row_data.commodity}' "
                        f"but no quantity and no cash movement"
                    )

        # Create envelope using safe creation method
        envelope = create_envelope(
            reason=f"CSV import from {institution}",
            date=row_data.date,
            narration=row_data.narrative or '',
            payee=row_data.payee,
            outbound_units=final_outbound_units,
            outbound_type=final_outbound_type,
            outbound_account=final_outbound_account,
            inbound_units=final_inbound_units,
            inbound_type=final_inbound_type,
            inbound_account=final_inbound_account,
            transaction_type=transaction_type if csv_row_has_investment_data(row_data) else None,
            balance_after=balance_after,
            balance_type=balance_type,
            envelope_id=envelope_id,  # We already generated it
            source=source_identifier,
            source_type='csv',  # Explicit classification
            source_file_path=actual_source_file,
            source_line_number=row_data.row_number,
            metadata=metadata
        )

        # Add initial history for Steel Thread compliance
        history_action = f'Created {transaction_type} envelope from {source_identifier}'
        history_details = {'row_number': row_data.row_number, 'transaction_type': transaction_type}

        envelope.add_history(
            state=EnvelopeState.INGESTED,
            component='EnvelopeBuilder',
            action=history_action,
            details=history_details
        )

        # CRITICAL VALIDATION: Envelope must have at least one posting
        # This catches configuration errors like wrong sign_convention
        if not final_outbound_units and not final_inbound_units:
            error_msg = (
                f"CRITICAL: Envelope has no postings! "
                f"Row {row_data.row_number} from {source_identifier} "
                f"(institution={institution}, account={account}). "
                f"This usually indicates a configuration error:\n"
                f"  - Check 'sign_convention' in importer config\n"
                f"  - CSV has Debit/Credit columns? Use sign_convention='debit_credit'\n"
                f"  - CSV has single Amount column? Use sign_convention='standard'\n"
                f"  - CSV has Payment/Receipt columns? Use sign_convention='payment_receipt'\n"
                f"Raw data: amount={row_data.amount}, debit={row_data.debit}, "
                f"credit={row_data.credit}, payment={row_data.payment}, receipt={row_data.receipt}"
            )
            logger.error(error_msg)
            raise ValueError(error_msg)

        return envelope

