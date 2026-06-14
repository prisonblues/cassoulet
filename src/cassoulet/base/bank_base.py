"""Bank importer base class - Minimal version using CSVRowData.

Base class for all bank importers in the new architecture.
Banks are the simplest importers - just single-file cash transactions.
"""

from typing import List
from cassoulet.base.importer_base import Importer
from cassoulet.utils.csv_row_data import CSVRowData
from cassoulet.stages import Envelope


class BankImporter(Importer):
    """Base class for bank importers - minimal and clean.
    
    In the new architecture:
    - Banks are ALWAYS single-file (no multi-file complexity)
    - Banks have NO investment transactions (no corporate actions)
    - Banks just have cash flows (handled by CSVRowData)
    - Transfer detection happens in the pipeline (not here)
    - Classification happens in the pipeline (not here)
    - Balance assertions handled by balance_writer (not here)
    
    This is just a thin wrapper that:
    1. Parses CSV to CSVRowData (using base class)
    2. Allows institution-specific fixes via _process_row_data hook
    3. Converts to envelopes (using EnvelopeBuilder)
    """
    
    def extract(self, file_path: str) -> List[Envelope]:
        """Extract transactions from bank CSV file.

        Single-file importer: takes a file path string.

        Args:
            file_path: Path to the CSV file

        Returns:
            List of transaction envelopes
        """
        # Step 1: Parse CSV to CSVRowData using base class method
        rows = self.parse_csv_file(file_path)

        if not rows:
            return []

        # Step 2: Apply any institution-specific fixes (optional override)
        rows = self._process_row_data(rows)

        # Step 3: Convert to envelopes using standard builder
        from cassoulet.utils.envelope_builder import EnvelopeBuilder

        envelopes = []
        for i, row in enumerate(rows):
            try:
                envelope = EnvelopeBuilder.from_csv_row_data(
                    row,
                    account=self.account,
                    institution=self.institution,
                    sign_convention=self.config.get('sign_convention', 'standard')
                )

                if envelope:
                    # Preserve original order for Steel Thread compliance
                    envelope.original_order = i
                    envelopes.append(envelope)
            except ValueError as e:
                # STEEL THREAD: Log error but don't crash - allow processing to continue
                self.logger.warning(f"Skipping row {row.row_number}: {e}")
                continue

        # Step 4: Process balances (sorting, gap detection, remediation)
        from cassoulet.utils.envelope_utilities import has_balance_data
        if has_balance_data(envelopes):
            envelopes = self._process_balances(envelopes)

            # Step 5: Write balance assertions directly (more efficient than scanning later)
            self._write_balance_assertions(envelopes)

        self.logger.info(
            f"Extracted {len(envelopes)} transactions from {self.institution} file"
        )

        return envelopes
    
    def _process_row_data(self, rows: List[CSVRowData]) -> List[CSVRowData]:
        """Hook for institution-specific fixes.

        Most banks won't need this. Override only if your bank has quirks like:
        - Mislabeled transaction types
        - Empty reference fields that need synthetic values
        - Duplicate entries that need filtering

        Args:
            rows: List of parsed CSVRowData objects

        Returns:
            Processed list of CSVRowData objects
        """
        return rows  # Default: no processing needed

    def _process_balances(self, envelopes: List[Envelope]) -> List[Envelope]:
        """Handle all balance operations for this account.

        This includes:
        1. Sorting by balance constraints
        2. Computing implied balances
        3. Detecting gaps
        4. Creating gap remediations (marked as transfer_eligible=False)
        5. Marking balance assertion points

        Args:
            envelopes: List of envelopes with original_order set

        Returns:
            Processed list including remediation envelopes (marked transfer_eligible=False)
        """
        from cassoulet.utils.balance_sorter import solve_balance_ordering, compute_implied_balances
        from cassoulet.utils.balance_gap_detector import detect_gaps, create_remediations

        self.logger.debug(f"Processing balances for {len(envelopes)} envelopes")

        # Apply balance-aware ordering (returns new list with sort_order set)
        sorted_envs = solve_balance_ordering(envelopes)

        # Compute implied balances for envelopes without express balances (returns new list)
        sorted_envs = compute_implied_balances(sorted_envs)

        # Detect gaps
        gaps = detect_gaps(sorted_envs, self.account, self.institution)

        # Add gap remediations (marked as non-transfer-eligible)
        if gaps:
            self.logger.info(f"Found {len(gaps)} balance gaps, creating remediations")
            remediation_envs = create_remediations(gaps)

            # Add remediations to envelope list
            # They're properly marked with transfer_eligible=False
            sorted_envs.extend(remediation_envs)

            # Re-sort to get remediations in the right position
            sorted_envs = solve_balance_ordering(sorted_envs)

        # Mark balance assertion points - only the LAST express balance of each MONTH
        # This provides monthly reconciliation points without cluttering the ledger
        # CRITICAL: Use sort_order to find the true last transaction of the month,
        # not just the last one in the list (which may be wrong due to CSV ordering
        # or transfer matching removing transactions)
        from cassoulet.utils.envelope_utilities import enhance_envelope
        from collections import defaultdict

        # Group by year-month to find last express balance of each month
        by_month = defaultdict(list)
        for i, env in enumerate(sorted_envs):
            month_key = (env.date.year, env.date.month)
            by_month[month_key].append((i, env))

        # Identify which envelopes should be assertion points
        assertion_indices = set()
        for month_key, month_envs in by_month.items():
            # Find ALL envelopes with express balance for this month
            express_envs = [(idx, env) for idx, env in month_envs
                          if env.balance_type == 'express' and env.balance_after is not None]

            if express_envs:
                # CRITICAL: Find the last DAY of the month with express balances,
                # then pick the last balance of THAT day (highest sort_order)

                # Group express balances by date
                by_date = defaultdict(list)
                for idx, env in express_envs:
                    by_date[env.date].append((idx, env))

                # Find the last date with express balances
                last_date = max(by_date.keys())

                # Get all envelopes from that last date
                last_date_envs = by_date[last_date]

                # Pick the one with highest sort_order (end of day)
                last_idx, last_env = max(last_date_envs, key=lambda x: x[1].sort_order or 0)

                # Check if there are multiple balances on the last day with different values
                if len(last_date_envs) > 1:
                    balances = [env.balance_after for _, env in last_date_envs]
                    unique_balances = set(balances)
                    if len(unique_balances) > 1:
                        self.logger.warning(
                            f"Multiple different balances on {last_date} for {self.account}: "
                            f"{balances}. Using last one (sort_order={last_env.sort_order}): {last_env.balance_after}"
                        )

                # Debug logging for specific months
                if month_key == (2019, 7):
                    self.logger.info(f"July 2019 balance assertion selection:")
                    self.logger.info(f"  Found {len(express_envs)} express balances across {len(by_date)} dates")
                    self.logger.info(f"  Last date with balance: {last_date}")
                    if len(last_date_envs) > 1:
                        self.logger.info(f"  {len(last_date_envs)} transactions on {last_date}:")
                        for idx, env in last_date_envs:
                            selected = "SELECTED" if idx == last_idx else "not selected"
                            self.logger.info(f"    sort_order={env.sort_order}, balance={env.balance_after} ({selected})")
                    self.logger.info(f"  Final selection: index={last_idx}, date={last_env.date}, balance={last_env.balance_after}")

                assertion_indices.add(last_idx)

        # Apply the marking
        result = []
        for i, env in enumerate(sorted_envs):
            if i in assertion_indices:
                enhanced = enhance_envelope(env,
                                           reason="Mark as balance assertion point (last express balance of day)",
                                           metadata={**env.metadata, 'balance_assertion_point': True})
                result.append(enhanced)
            else:
                result.append(env)
        sorted_envs = result

        self.logger.debug(f"Balance processing complete, returning {len(sorted_envs)} envelopes")
        return sorted_envs

    def _write_balance_assertions(self, envelopes: List[Envelope]) -> None:
        """Write balance assertions for this account's envelopes.

        This is more efficient than scanning all envelopes later,
        as we already know which ones are assertion points.

        Args:
            envelopes: List of processed envelopes with balance assertion points marked
        """
        # Only write if we have assertion points
        assertion_points = [
            e for e in envelopes
            if e.metadata.get('balance_assertion_point')
        ]

        if not assertion_points:
            return

        # Get output directory from parent importer config
        # This MUST be set by the pipeline
        output_dir = self.config.get('output_dir')
        if not output_dir:
            from cassoulet.base.exceptions import ConfigurationError
            error = ConfigurationError('output_dir', 'BankImporter',
                                      'Pipeline must set output_dir in importer config')
            self.logger.error(str(error))
            return

        from cassoulet.stages.output.balance_assertion_writer import BalanceAssertionWriter

        # Write assertions for just this account's envelopes
        # monthly_only=True ensures we only write month-end assertions (we already selected the right ones)
        writer = BalanceAssertionWriter(output_dir)
        output_files = writer.write_balance_assertions(assertion_points, monthly_only=True)

        if output_files:
            self.logger.info(f"  Wrote balance assertions to: {output_files}")

