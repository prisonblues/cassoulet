"""

Starling Bank Importer

"""

from cassoulet.base.bank_base import BankImporter

class StarlingImporter(BankImporter):
    """
    Starling Bank importer.
    """
    
    def __init__(self, account: str, file_identifier: str, config: dict = None, debug: bool = False):
        """Initialize Starling importer."""
        super().__init__(
            account=account,
            file_identifier=file_identifier,
            config=config,
            debug=debug
        )
        
