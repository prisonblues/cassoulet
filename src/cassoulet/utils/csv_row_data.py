"""
CSV Row Data Structure

This module defines the universal intermediate data structure for parsed CSV data.
CSVRowData represents raw parsed values from CSV files, without any interpretation
or envelope pattern applied. It's the common format that CSVReader outputs and
importers consume.

Key principles:
- Raw values only (no interpretation of signs or types)
- No inbound/outbound pattern (that's the importer's job)
- No classification (happens post-merge)
- Institution-agnostic structure
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Dict, Optional


@dataclass
class CSVRowData:
    """
    Raw parsed CSV data - NO interpretation, NO envelope pattern yet.
    
    This is what CSVReader outputs for ALL institutions. It contains
    raw values extracted from CSV files, preserving original signs and
    formats. Importers then apply institution-specific rules to transform
    this into TransactionEnvelopes.
    
    Attributes:
        date: Transaction date from CSV
        narrative: Transaction narrative/description text
        payee: Payee or merchant name
        
        Amount fields (raw from CSV - not interpreted):
        - amount: Single signed amount column
        - debit: Separate debit column (if present)  
        - credit: Separate credit column (if present)
        - payment: Alternative naming for outflow
        - receipt: Alternative naming for inflow
        
        Additional fields:
        - balance: Account balance after transaction
        - reference: Transaction reference/ID
        - settlement_date: When funds actually settled
        
        Investment fields (if detected):
        - quantity: Number of shares/units
        - price: Price per unit
        - commodity: Security symbol/name
        
        Source tracking:
        - source_file: Path to source CSV file
        - row_number: Line number in source file
        - raw_row: Original CSV row dict for debugging
    """
    # Core fields from CSV
    date: Optional[date] = None
    narrative: Optional[str] = None    # Transaction narrative/description
    payee: Optional[str] = None        # Payee/merchant name
    
    # Raw amount data - NOT interpreted!
    # These preserve exactly what's in the CSV
    amount: Optional[Decimal] = None      # Single amount column (with sign)
    debit: Optional[Decimal] = None       # Separate debit column
    credit: Optional[Decimal] = None      # Separate credit column
    payment: Optional[Decimal] = None     # Alternative naming
    receipt: Optional[Decimal] = None     # Alternative naming
    
    # Additional fields
    balance: Optional[Decimal] = None
    reference: Optional[str] = None
    settlement_date: Optional[date] = None
    type: Optional[str] = None  # Transaction type (Buy, Sell, Purchase, etc.)
    
    # Investment-specific (if detected)
    quantity: Optional[Decimal] = None
    price: Optional[Decimal] = None
    commodity: Optional[str] = None          # Resolved commodity symbol
    commodity_raw: Optional[str] = None      # Raw description before lookup
    
    # Source tracking
    source_file: str = ""
    row_number: int = 0
    raw_row: Dict[str, str] = field(default_factory=dict)
    
    # Metadata for processing  
    metadata: Optional[Dict] = None
    
    # Explicitly NOT included:
    # - inbound_units/outbound_units (that's the importer's interpretation)
    # - transaction_type (classification happens post-merge)
    # - account (importers know their account)
