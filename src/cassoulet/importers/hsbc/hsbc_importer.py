"""HSBC Bank Importer.

HSBC has no institution-specific quirks, so it just inherits from BankImporter.
All configuration is in importers/config/importers.py.
"""

from cassoulet.base.bank_base import BankImporter


class HSBCImporter(BankImporter):
    """HSBC bank importer - purely configuration-driven."""
    pass  # No custom logic needed - everything is in the base class