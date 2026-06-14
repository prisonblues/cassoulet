"""
Corporate Action Data Structures

Represents corporate actions like stock splits that affect lot tracking
but are not regular buy/sell/transfer transactions.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional, Dict, Any, List


@dataclass
class StockSplit:
    """
    Represents a stock split corporate action.

    Instead of manually specifying all postings with precise cost basis,
    users specify the intent (10:1 split) and the system generates the
    postings by consulting the lot registry.

    Example Format A (Intent-Based):
        2024-08-08 * "MicroStrategy 10-for-1 stock split"
          corporate-action: "stock-split"
          split-commodity: "MSTR"
          split-account: "Assets:Broker:HL:SIPP"
          split-ratio: "10:1"

    Example Format B (Simplified Postings):
        2024-08-08 * "MicroStrategy 10-for-1 stock split"
          Assets:Broker:HL:SIPP  -46 MSTR {}
          Assets:Broker:HL:SIPP  460 MSTR {}
    """

    date: date
    commodity: str
    account: str
    ratio: Decimal  # e.g., 10 for a 10:1 split

    # Optional fields from transaction
    narration: Optional[str] = None
    payee: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

    # Source information
    source_file: Optional[str] = None
    line_number: Optional[int] = None

    # Detection mode
    input_format: Optional[str] = None  # 'intent-based' or 'simplified-postings'

    @classmethod
    def from_metadata(cls, txn, file_path: str = None) -> 'StockSplit':
        """
        Create a StockSplit from Format A (intent-based directive).

        Required metadata:
        - split-commodity: The commodity being split (e.g., "MSTR")
        - split-account: The account holding the commodity
        - split-ratio: The ratio (e.g., "10:1", "2:1")

        Args:
            txn: Beancount Transaction with corporate-action metadata
            file_path: Source file path

        Returns:
            StockSplit instance

        Raises:
            ValueError: If required metadata is missing
        """
        meta = txn.meta or {}

        # Validate required fields
        if 'split-commodity' not in meta:
            raise ValueError(f"Stock split on {txn.date} missing 'split-commodity' metadata")
        if 'split-account' not in meta:
            raise ValueError(f"Stock split on {txn.date} missing 'split-account' metadata")
        if 'split-ratio' not in meta:
            raise ValueError(f"Stock split on {txn.date} missing 'split-ratio' metadata")

        # Parse ratio (e.g., "10:1" -> 10)
        ratio_str = meta['split-ratio']
        if ':' in ratio_str:
            numerator, denominator = ratio_str.split(':')
            ratio = Decimal(numerator.strip()) / Decimal(denominator.strip())
        else:
            ratio = Decimal(ratio_str)

        return cls(
            date=txn.date,
            commodity=meta['split-commodity'],
            account=meta['split-account'],
            ratio=ratio,
            narration=txn.narration,
            payee=txn.payee,
            metadata=dict(meta),
            source_file=file_path,
            line_number=meta.get('lineno'),
            input_format='intent-based'
        )

    @classmethod
    def from_postings(cls, txn, file_path: str = None) -> 'StockSplit':
        """
        Create a StockSplit from Format B (simplified postings).

        Derives split parameters from posting structure.

        Args:
            txn: Beancount Transaction with simplified postings
            file_path: Source file path

        Returns:
            StockSplit instance
        """
        # Get commodity and account (already validated to be same for all)
        commodity = txn.postings[0].units.currency
        account = txn.postings[0].account

        # Calculate ratio from quantities
        total_positive = sum(p.units.number for p in txn.postings if p.units.number > 0)
        total_negative = sum(abs(p.units.number) for p in txn.postings if p.units.number < 0)
        ratio = total_positive / total_negative

        return cls(
            date=txn.date,
            commodity=commodity,
            account=account,
            ratio=ratio,
            narration=txn.narration,
            payee=txn.payee,
            metadata=dict(txn.meta) if txn.meta else {},
            source_file=file_path,
            line_number=txn.meta.get('lineno') if txn.meta else None,
            input_format='simplified-postings'
        )

    def __str__(self) -> str:
        """Human-readable representation."""
        return f"StockSplit({self.date}: {self.commodity} {self.ratio}:1 in {self.account})"


@dataclass
class CorporateActionResult:
    """
    Result of processing a corporate action through the lot system.

    Contains the lot transformations that occurred, which can be used
    to generate the output beancount postings.
    """

    action: StockSplit  # The original action
    lots_transformed: List[tuple] = field(default_factory=list)  # List of (old_lot, new_lot) tuples

    def generate_posting_data(self):
        """
        Generate posting data for the output file.

        For a stock split, this creates:
        - Negative postings removing old lots at original cost
        - Positive postings adding new lots at adjusted cost

        The acquisition dates are preserved from the original lots.

        Returns:
            List of posting dictionaries suitable for BeancountWriter
        """
        postings = []

        for old_lot, new_lot in self.lots_transformed:
            # Remove old shares at original cost
            postings.append({
                'account': self.action.account,
                'units': -old_lot['quantity'],
                'currency': self.action.commodity,
                'cost_per_unit': old_lot['cost_per_unit'],
                'cost_date': old_lot['date']
            })

            # Add new shares at adjusted cost
            postings.append({
                'account': self.action.account,
                'units': new_lot['quantity'],
                'currency': self.action.commodity,
                'cost_per_unit': new_lot['cost_per_unit'],
                'cost_date': new_lot['date']  # Preserved from original
            })

        return postings
