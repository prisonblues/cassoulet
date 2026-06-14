"""Single-file broker importer base class.

Handles the common pattern for single-file broker importers like HL, II, etc.
All the logic is here - individual importers are just configuration.
"""

from typing import List, TYPE_CHECKING
from cassoulet.base.broker_base import BrokerImporter

if TYPE_CHECKING:
    from cassoulet.stages import Envelope

class SingleFileBrokerImporter(BrokerImporter):
    """Base class for single-file broker importers.
    
    Implements the standard pattern:
    1. Parse CSV file
    2. Detect corporate actions
    3. Convert to envelopes
    
    Subclasses only need to exist if they have institution-specific quirks.
    Otherwise, they can just be configuration in importers.py.
    """
    
    def extract(self, file_path: str) -> List['Envelope']:
        """Extract transactions from a single CSV file.

        Single-file importer: takes a file path string.

        Args:
            file_path: Path to the CSV file

        Returns:
            List of transaction envelopes
        """
        # Parse CSV file using base class method
        rows = self.parse_csv_file(file_path)

        if not rows:
            return []

        # Convert rows to envelopes using base class method
        # Corporate actions will be detected later via pattern matching
        # Sign convention is handled by the base class from investment_config
        return self.process_rows_to_envelopes(rows)