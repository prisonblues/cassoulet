"""Hargreaves Lansdown (HL) Importer.

HL has no institution-specific quirks, so it just inherits from SingleFileBrokerImporter.
All configuration is in importers/config/importers.py.
"""

from cassoulet.base.single_file_broker_importer import SingleFileBrokerImporter


class HLImporter(SingleFileBrokerImporter):
    """Hargreaves Lansdown importer - purely configuration-driven."""
    pass  # No custom logic needed - everything is in the base class