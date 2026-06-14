"""Enhanced Base Importer with CSV parsing.

Base importer class that provides CSV parsing functionality
to all importers (bank, broker, single-file, multi-file).
"""

import logging
from pathlib import Path
from typing import List, Optional, Dict
from abc import ABC, abstractmethod

from cassoulet.utils.csv_reader import CSVReader
from cassoulet.utils.csv_row_data import CSVRowData
from cassoulet.stages import Envelope
from cassoulet.utils.envelope_builder import EnvelopeBuilder
from cassoulet.utils.accounts import account_institution


class Importer(ABC):
    """Enhanced base class for all importers.
    
    Provides:
    - CSV parsing via CSVReader
    - Common configuration handling
    - Logging setup
    """
    
    def __init__(self, account: str, file_identifier: Optional[str] = None, 
                 config: Optional[Dict] = None, **kwargs):
        """Initialize base importer.
        
        Args:
            account: Account for transactions
            file_identifier: String to identify files this importer handles
            config: Configuration dictionary
        """
        self.account = account
        self.file_identifier = file_identifier
        self.config = config or {}
        # Derive institution from account path if not explicitly provided
        self.institution = kwargs.get('institution') or account_institution(account) or ''
        
        # Initialize CSV reader (available to all importers)
        self.csv_reader = CSVReader(self.account, self.config)
        
        # Setup logging
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")
    
    def parse_csv_file(self, file_path: str) -> List[CSVRowData]:
        """Parse a CSV file into CSVRowData objects using Steel Thread compliant reader.
        
        This is the standard way for any importer to parse CSV files.
        Available to all importers - bank, broker, single-file, multi-file.
        
        Args:
            file_path: Path to CSV file
            
        Returns:
            List of parsed CSVRowData objects
        """
        if not file_path or not Path(file_path).exists():
            self.logger.warning(f"File not found: {file_path}")
            return []
        
        try:
            # Use the Steel Thread compliant read_file method
            result = self.csv_reader.read_file(file_path)
            
            # Log any warnings from CSV processing
            for warning in result.warnings:
                if warning.severity == 'ERROR':
                    self.logger.error(f"CSV {warning.type}: {warning.message}")
                elif warning.severity == 'WARNING':
                    self.logger.warning(f"CSV {warning.type}: {warning.message}")
                else:
                    self.logger.debug(f"CSV {warning.type}: {warning.message}")
            
            self.logger.info(
                f"Parsed {len(result.rows)} rows from {file_path} "
                f"({result.metadata.get('failed_rows', 0)} failed, "
                f"{result.metadata.get('empty_rows', 0)} empty)"
            )
            
            return result.rows
                
        except Exception as e:
            self.logger.error(f"Error parsing file {file_path}: {e}")
            return []
    
    def identify(self, file_path: str) -> bool:
        """Check if this importer can handle the file.
        
        Default implementation checks if file_identifier is in the file path.
        Override this for more complex identification logic.
        
        Args:
            file_path: Path to file
            
        Returns:
            True if this importer can handle the file
        """
        if self.file_identifier:
            return self.file_identifier in file_path
        return False
    
    @abstractmethod
    def extract(self, file_path: str) -> List[Envelope]:
        """Extract transactions from file.
        
        Args:
            file_path: Path to file
            
        Returns:
            List of transaction envelopes
        """
        pass