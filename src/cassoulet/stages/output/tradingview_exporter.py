"""
TradingView CSV Exporter

This module exports transaction data to TradingView-compatible CSV format.
Creates one CSV file per account owner with transactions formatted for
TradingView portfolio import.

TradingView CSV Format:
Symbol,Side,Qty,Fill Price,Commission,Closing Time

Symbol: Exchange:ticker format (e.g., NASDAQ:AAPL) or $CASH for cash transactions
Side: Buy, Sell, Deposit, Withdrawal, Taxes and fees
Qty: Quantity of shares or cash amount
Fill Price: Price per share (empty for cash transactions)
Commission: Transaction fees (empty if no commission)
Closing Time: Transaction timestamp (YYYY-MM-DD H:MM:SS)
"""

import logging
import csv
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import List, Dict, Optional, Any
from collections import defaultdict
from datetime import datetime

from cassoulet.stages.envelope import Envelope
from cassoulet.utils.commodity_registry import get_commodity_registry


logger = logging.getLogger(__name__)


class TransactionSide(Enum):
    """TradingView transaction sides."""
    BUY = "Buy"
    SELL = "Sell"
    DEPOSIT = "Deposit"
    WITHDRAWAL = "Withdrawal"
    TAXES_AND_FEES = "Taxes and fees"


@dataclass
class TradingViewTransaction:
    """Represents a single TradingView transaction."""
    symbol: str  # Exchange:ticker or $CASH
    side: TransactionSide
    qty: Decimal
    fill_price: Optional[Decimal]
    commission: Optional[Decimal]
    closing_time: str  # YYYY-MM-DD H:MM:SS format

    def to_csv_row(self) -> List[str]:
        """Convert to CSV row format."""
        return [
            self.symbol,
            self.side.value,
            str(self.qty),
            str(self.fill_price) if self.fill_price is not None else "",
            str(self.commission) if self.commission is not None else "",
            self.closing_time
        ]


class TradingViewExporter:
    """
    Exports transaction envelopes to TradingView CSV format.

    Processes envelopes during the import pipeline and writes CSV files
    grouped by account owner.
    """

    def __init__(self, output_dir: Optional[str] = None):
        """
        Initialize the TradingView exporter.

        Args:
            output_dir: Directory for CSV output. Defaults to entries/output/tradingview/
        """
        if output_dir is None:
            output_dir = "entries/output/tradingview"

        self.output_dir = Path(output_dir)
        self.commodity_registry = get_commodity_registry()

        # Statistics
        self.stats = {
            'total_processed': 0,
            'total_exported': 0,
            'skipped': 0,
            'by_owner': defaultdict(int),
            'by_type': defaultdict(int),
            'owners': set()
        }

        # Accumulate transactions by owner
        self.transactions_by_owner: Dict[str, List[TradingViewTransaction]] = defaultdict(list)

        logger.info(f"TradingView exporter initialized. Output: {self.output_dir}")

    def process_envelopes(self, envelopes: List[Envelope]) -> None:
        """
        Process envelopes and accumulate TradingView transactions.

        Args:
            envelopes: List of transaction envelopes to process
        """
        logger.info(f"Processing {len(envelopes)} envelopes for TradingView export")

        for envelope in envelopes:
            self.stats['total_processed'] += 1

            # Extract owner from metadata (default to 'personal')
            owner = self._get_owner(envelope)
            self.stats['owners'].add(owner)

            # Convert envelope to TradingView transaction
            tv_txn = self._envelope_to_tradingview(envelope, owner)

            if tv_txn:
                self.transactions_by_owner[owner].append(tv_txn)
                self.stats['total_exported'] += 1
                self.stats['by_owner'][owner] += 1
                self.stats['by_type'][envelope.transaction_type or 'UNKNOWN'] += 1
            else:
                self.stats['skipped'] += 1
                logger.debug(
                    f"Skipped envelope {envelope.envelope_id}: "
                    f"type={envelope.transaction_type}"
                )

        logger.info(
            f"TradingView processing complete. "
            f"Exported: {self.stats['total_exported']}, "
            f"Skipped: {self.stats['skipped']}"
        )

    def _get_owner(self, envelope: Envelope) -> str:
        """
        Extract owner from envelope metadata.

        Args:
            envelope: Transaction envelope

        Returns:
            Owner identifier (defaults to 'personal')
        """
        if envelope.metadata and 'owner' in envelope.metadata:
            return envelope.metadata['owner']
        return 'personal'

    def _envelope_to_tradingview(
        self,
        envelope: Envelope,
        owner: str
    ) -> Optional[TradingViewTransaction]:
        """
        Convert envelope to TradingView transaction.

        Args:
            envelope: Transaction envelope
            owner: Account owner

        Returns:
            TradingView transaction or None if should be skipped
        """
        txn_type = envelope.transaction_type

        # Skip transaction types not supported by TradingView
        # (dividends, interest, transfers, etc.)
        skip_types = ('DIVIDEND', 'INTEREST', 'TRANSFER', None)
        if txn_type in skip_types:
            return None

        # Handle different transaction types
        if txn_type == 'BUY':
            return self._handle_buy(envelope)
        elif txn_type == 'SELL':
            return self._handle_sell(envelope)
        elif txn_type == 'CASH_TRANSFER':
            return self._handle_cash_transfer(envelope)
        elif txn_type == 'FEE':
            return self._handle_fee(envelope)
        else:
            # Log unknown types at debug level to avoid noise
            logger.debug(f"Skipping unknown transaction type: {txn_type}")
            return None

    def _handle_buy(self, envelope: Envelope) -> Optional[TradingViewTransaction]:
        """Convert BUY envelope to TradingView Buy transaction."""
        if not envelope.inbound_type or not envelope.inbound_units:
            logger.warning(f"BUY envelope missing inbound data: {envelope.envelope_id}")
            return None

        # Extract commission from additional_postings if present
        commission = self._extract_commission(envelope)

        return TradingViewTransaction(
            symbol=self._get_exchange_symbol(envelope.inbound_type),
            side=TransactionSide.BUY,
            qty=envelope.inbound_units,
            fill_price=envelope.unit_price,
            commission=commission,
            closing_time=self._format_datetime(envelope.date)
        )

    def _handle_sell(self, envelope: Envelope) -> Optional[TradingViewTransaction]:
        """Convert SELL envelope to TradingView Sell transaction."""
        if not envelope.outbound_type or not envelope.outbound_units:
            logger.warning(f"SELL envelope missing outbound data: {envelope.envelope_id}")
            return None

        # Extract commission from additional_postings if present
        commission = self._extract_commission(envelope)

        # For TradingView, we want the SELL price (proceeds), not cost basis
        # Try unit_price first, then calculate from proceeds
        fill_price = envelope.unit_price
        if not fill_price and envelope.inbound_units and envelope.outbound_units:
            # Calculate actual sale price from proceeds (inbound cash / outbound shares)
            fill_price = envelope.inbound_units / envelope.outbound_units

        return TradingViewTransaction(
            symbol=self._get_exchange_symbol(envelope.outbound_type),
            side=TransactionSide.SELL,
            qty=envelope.outbound_units,
            fill_price=fill_price,
            commission=commission,
            closing_time=self._format_datetime(envelope.date)
        )

    def _handle_cash_transfer(self, envelope: Envelope) -> Optional[TradingViewTransaction]:
        """Convert CASH_TRANSFER envelope to Deposit or Withdrawal."""
        # Determine if deposit (inbound) or withdrawal (outbound)
        if envelope.inbound_units and envelope.inbound_type:
            # Cash coming in = Deposit
            return TradingViewTransaction(
                symbol="$CASH",
                side=TransactionSide.DEPOSIT,
                qty=envelope.inbound_units,
                fill_price=None,
                commission=None,
                closing_time=self._format_datetime(envelope.date)
            )
        elif envelope.outbound_units and envelope.outbound_type:
            # Cash going out = Withdrawal
            return TradingViewTransaction(
                symbol="$CASH",
                side=TransactionSide.WITHDRAWAL,
                qty=envelope.outbound_units,
                fill_price=None,
                commission=None,
                closing_time=self._format_datetime(envelope.date)
            )
        else:
            logger.warning(
                f"CASH_TRANSFER envelope missing flow data: {envelope.envelope_id}"
            )
            return None

    def _handle_fee(self, envelope: Envelope) -> Optional[TradingViewTransaction]:
        """Convert FEE envelope to Taxes and fees."""
        if not envelope.outbound_units:
            logger.warning(f"FEE envelope missing outbound data: {envelope.envelope_id}")
            return None

        return TradingViewTransaction(
            symbol="$CASH",
            side=TransactionSide.TAXES_AND_FEES,
            qty=envelope.outbound_units,
            fill_price=None,
            commission=None,
            closing_time=self._format_datetime(envelope.date)
        )

    def _extract_commission(self, envelope: Envelope) -> Optional[Decimal]:
        """
        Extract commission from additional_postings if present.

        Args:
            envelope: Transaction envelope

        Returns:
            Commission amount or None
        """
        if not envelope.additional_postings:
            return None

        for posting in envelope.additional_postings:
            account = posting.get('account', '')
            if 'Commissions' in account or 'Expenses:Commissions' in account:
                return posting.get('units')

        return None

    def _calculate_weighted_avg_cost(self, consumed_lots: List[Dict[str, Any]]) -> Optional[Decimal]:
        """
        Calculate weighted average cost from consumed lots.

        Uses the sophisticated lot tracking system to determine the actual
        cost basis for SELL transactions.

        Args:
            consumed_lots: List of consumed lot dictionaries with cost_per_unit and quantity_consumed

        Returns:
            Weighted average cost per unit or None
        """
        if not consumed_lots:
            return None

        total_cost = Decimal('0')
        total_quantity = Decimal('0')

        for lot in consumed_lots:
            quantity = lot.get('quantity_consumed', Decimal('0'))
            cost_per_unit = lot.get('cost_per_unit', Decimal('0'))

            if isinstance(quantity, str):
                quantity = Decimal(quantity)
            if isinstance(cost_per_unit, str):
                cost_per_unit = Decimal(cost_per_unit)

            total_cost += quantity * cost_per_unit
            total_quantity += quantity

        if total_quantity > 0:
            return total_cost / total_quantity

        return None

    def _get_exchange_symbol(self, commodity: str) -> str:
        """
        Convert commodity symbol to exchange:ticker format.

        Args:
            commodity: Commodity symbol (e.g., AAPL, VLS80)

        Returns:
            Exchange-prefixed symbol (e.g., NASDAQ:AAPL, LSE:VLS80)
        """
        # Check if this is a currency (GBP, USD, etc.)
        if commodity in ('GBP', 'USD', 'EUR'):
            return "$CASH"

        # Look up commodity info
        info = self.commodity_registry.get_commodity_info(commodity)

        if info and info.exchange:
            return f"{info.exchange}:{commodity}"

        # Fallback: guess exchange based on quote_currency
        if info and info.quote_currency:
            if info.quote_currency == 'USD':
                return f"NASDAQ:{commodity}"  # Default US to NASDAQ
            elif info.quote_currency == 'GBP':
                return f"LSE:{commodity}"  # Default UK to LSE
            elif info.quote_currency == 'EUR':
                return f"XETRA:{commodity}"  # Default EU to XETRA

        # Final fallback: return as-is
        logger.warning(
            f"No exchange mapping for commodity {commodity}, using as-is"
        )
        return commodity

    def _format_datetime(self, txn_date) -> str:
        """
        Format date to TradingView datetime string.

        Args:
            txn_date: Transaction date

        Returns:
            Formatted datetime string (YYYY-MM-DD H:MM:SS)
        """
        # TradingView format: YYYY-MM-DD H:MM:SS (note single-digit hours)
        return f"{txn_date} 0:00:00"

    def _group_by_owner(self, envelopes: List[Envelope]) -> Dict[str, List[Envelope]]:
        """
        Group envelopes by owner.

        Args:
            envelopes: List of envelopes

        Returns:
            Dictionary mapping owner to list of envelopes
        """
        grouped = defaultdict(list)
        for envelope in envelopes:
            owner = self._get_owner(envelope)
            grouped[owner].append(envelope)
        return dict(grouped)

    def write_csv_files(self) -> Dict[str, Path]:
        """
        Write accumulated transactions to CSV files, one per owner.

        Returns:
            Dictionary mapping owner to CSV file path
        """
        if not self.transactions_by_owner:
            logger.info("No TradingView transactions to write")
            return {}

        # Ensure output directory exists
        self.output_dir.mkdir(parents=True, exist_ok=True)

        written_files = {}

        for owner, transactions in self.transactions_by_owner.items():
            # Sanitize owner name for filename
            safe_owner = self._sanitize_filename(owner)
            filename = f"tradingview_{safe_owner}.csv"
            filepath = self.output_dir / filename

            # Write CSV file
            with open(filepath, 'w', newline='') as f:
                writer = csv.writer(f)

                # Write header
                writer.writerow([
                    'Symbol', 'Side', 'Qty', 'Fill Price', 'Commission', 'Closing Time'
                ])

                # Write transactions
                for txn in transactions:
                    writer.writerow(txn.to_csv_row())

            written_files[owner] = filepath
            logger.info(
                f"Wrote {len(transactions)} transactions to {filepath} for owner '{owner}'"
            )

        return written_files

    def _sanitize_filename(self, owner: str) -> str:
        """
        Sanitize owner name for use in filename.

        Args:
            owner: Owner identifier

        Returns:
            Safe filename component
        """
        # Replace special characters with underscores
        safe = owner.lower()
        safe = safe.replace(':', '_')
        safe = safe.replace(' ', '_')
        safe = safe.replace('/', '_')
        safe = safe.replace('\\', '_')
        return safe

    def get_statistics(self) -> Dict[str, Any]:
        """
        Get export statistics.

        Returns:
            Dictionary with statistics
        """
        return {
            'total_processed': self.stats['total_processed'],
            'total_exported': self.stats['total_exported'],
            'skipped': self.stats['skipped'],
            'owners': sorted(list(self.stats['owners'])),
            'by_owner': dict(self.stats['by_owner']),
            'by_type': dict(self.stats['by_type'])
        }
