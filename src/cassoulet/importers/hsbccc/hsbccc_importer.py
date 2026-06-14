"""HSBC Credit Card Importer.

HSBC Credit Card has no institution-specific quirks, so it just inherits from BankImporter.
All configuration is in importers/config/importers.py.

The credit card data is extracted from PDF statements using scripts/hsbc_cc_pdf_extractor.py
and outputs CSV with semantic column names for auto-detection.
"""

from cassoulet.base.bank_base import BankImporter


class HSBCCCImporter(BankImporter):
    """HSBC credit card importer - purely configuration-driven.

    The CSV uses semantic column names that auto-detect:
    - Transaction ID → reference
    - Date → date
    - Description → narrative
    - Amount → amount (signed: positive=expense, negative=payment)
    - Type → type

    Extra metadata columns (ignored by import):
    - Original Currency/Amount/Exchange Rate: FX data
    - FX Fee For: Links NON-STERLING FEE to parent transaction
    - Statement Order: Original position in PDF
    """
    pass  # No custom logic needed - everything is in the base class
