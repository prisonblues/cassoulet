"""

Santander Importer

"""

from cassoulet.base.bank_base import BankImporter

class SantanderImporter(BankImporter):
    
    def __init__(self, account: str, file_identifier: str, config: dict = None, debug: bool = False):
        """Initialize Santander importer."""
        super().__init__(
            account=account,
            file_identifier=file_identifier,
            config=config,
            debug=debug
        )
        
