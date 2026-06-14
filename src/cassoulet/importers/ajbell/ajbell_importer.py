"""AJ Bell Importer - Thin Version.

Minimal importer that only handles AJ Bell-specific quirks.
All heavy lifting is done by MultiFileBrokerImporter.
"""

from typing import Dict, List

from cassoulet.base.multi_file_broker_importer import MultiFileBrokerImporter
from cassoulet.utils.csv_row_data import CSVRowData


class AJBellImporter(MultiFileBrokerImporter):
    """Thin AJ Bell importer - just institution-specific quirks."""

    def __init__(self, account: str, file_identifier: Dict[str, str] = None,
                 config: dict = None, debug: bool = False):
        """Initialize with file patterns from config."""
        # Extract file patterns
        if config and 'file_identifier' in config:
            file_patterns = config['file_identifier']
        else:
            file_patterns = file_identifier or {}

        super().__init__(
            account=account,
            file_patterns=file_patterns,
            config=config,
            debug=debug
        )

    def _process_row_data(self, parsed_data: Dict[str, List[CSVRowData]]) -> Dict[str, List[CSVRowData]]:
        """Process CSVRowData to apply AJ Bell-specific fixes.

        Filters out balance forward entries (opening balances for tax year).
        """
        # Filter balance forwards from cash entries
        if 'cash' in parsed_data:
            parsed_data['cash'] = self._filter_balance_forwards(parsed_data['cash'])

        return parsed_data

    def _filter_balance_forwards(self, cash_rows: List[CSVRowData]) -> List[CSVRowData]:
        """Filter out AJ Bell balance forward entries.

        AJ Bell includes "* BALANCE B/F *" entries at the start of each tax year
        (early April) which are bookkeeping entries showing opening balances,
        not actual transactions.

        Detection:
        - Structural pattern: receipt/payment = balance (not a transaction, just stating balance)
        - Narrative contains "balance b/f"
        """
        filtered = []
        balance_forwards_count = 0

        for row in cash_rows:
            # Check structural pattern
            matches_pattern = False
            if row.balance is not None:
                # Positive balance: receipt equals balance, payment is 0 or None
                if (row.receipt is not None and
                    abs(row.receipt - row.balance) < 0.01 and
                    (row.payment is None or abs(row.payment) < 0.01)):
                    matches_pattern = True

                # Negative balance: payment equals balance, receipt is 0 or None
                elif (row.payment is not None and
                      abs(row.payment - row.balance) < 0.01 and
                      (row.receipt is None or abs(row.receipt) < 0.01)):
                    matches_pattern = True

            # Check if it's a balance forward
            if matches_pattern and row.narrative and 'balance b/f' in row.narrative.lower():
                balance_forwards_count += 1
                self.logger.info(
                    f"Balance forward entry filtered: {row.date} "
                    f"{row.narrative} (opening balance: {row.balance})"
                )
            else:
                filtered.append(row)

        if balance_forwards_count > 0:
            self.logger.info(
                f"Filtered out {balance_forwards_count} AJ Bell balance forward entries"
            )

        return filtered

    # TODO: Review if still needed - pattern matching might be better approach
    # def _fix_mislabeled_contributions(self, cash_rows: List[CSVRowData]) -> None:
    #     """Fix AJ Bell's mislabeled contributions in-place.
    #
    #     'Transfer from SIPP' is actually a contribution, not a transfer.
    #
    #     NOTE: This might be better handled by pattern matching:
    #     - Create a pattern for 'sipp_contribution' that matches this narration
    #     - Avoids modifying data early in the pipeline
    #     - More transparent and configurable
    #     """
    #     for row in cash_rows:
    #         if row.narration and 'transfer from' in row.narration.lower() and 'sipp' in row.narration.lower():
    #             # Mark as contribution using metadata
    #             if row.metadata is None:
    #                 row.metadata = {}
    #             row.metadata['contribution'] = True
    #             row.metadata['contribution_type'] = 'sipp_contribution'
    #             row.metadata['ajbell_correction'] = 'transfer_to_contribution'
    #             self.logger.debug(f"Fixed mislabeled contribution: {row.narration}")