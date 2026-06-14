"""
Balance Assertion Writer for Envelopes

Generates Beancount balance assertions from envelopes marked as assertion points.
Works with envelope-native data and produces properly formatted Beancount directives.
"""

from typing import List, Dict, Optional
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from datetime import datetime, timedelta
import logging

from beancount.core.data import Balance
from beancount.core.amount import Amount
from beancount.parser import printer

from cassoulet.stages.envelope import Envelope
from cassoulet.utils.envelope_utilities import get_primary_account
from cassoulet.utils.accounts import account_is_bank

logger = logging.getLogger(__name__)


class BalanceAssertionWriter:
    """
    Writes balance assertions to organized Beancount files.

    Generates proper Balance directives from envelope data,
    handles monthly filtering, and respects Beancount's date semantics.
    """

    def __init__(self, output_dir: str):
        """Initialize the balance assertion writer.

        Args:
            output_dir: Directory to write output files
        """
        if not output_dir:
            from cassoulet.base.exceptions import ConfigurationError
            raise ConfigurationError('output_dir', 'BalanceAssertionWriter',
                                   'Output directory is required')

        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def write_balance_assertions(
        self,
        envelopes: List[Envelope],
        monthly_only: bool = True
    ) -> List[str]:
        """
        Write balance assertions to output files.

        Groups assertions by year and writes to separate files.

        Args:
            envelopes: List of envelopes to process
            monthly_only: If True, only generate monthly assertions

        Returns:
            List of output file paths created
        """
        # Generate Balance directives
        balance_assertions = self.generate_balance_directives(envelopes, monthly_only)

        if not balance_assertions:
            logger.info("No balance assertions to write")
            return []

        logger.info(f"Writing {len(balance_assertions)} balance assertions")

        # Group by year
        grouped = self._group_by_year(balance_assertions)

        # Write year-specific balance files
        output_files = []
        for year, assertions in grouped.items():
            filepath = self._write_year_balances(year, assertions)
            output_files.append(str(filepath))

        return output_files

    def generate_balance_directives(
        self,
        envelopes: List[Envelope],
        monthly_only: bool = True
    ) -> List[Balance]:
        """
        Generate Balance directive objects from envelopes.

        Args:
            envelopes: List of envelopes to process
            monthly_only: If True, only generate monthly assertions

        Returns:
            List of Balance directive objects
        """
        directives = []

        # Filter to assertion points
        assertion_points = [
            e for e in envelopes
            if (e.metadata.get('balance_assertion_point') or
                (e.balance_after is not None and e.balance_type == 'express'))
        ]

        if not assertion_points:
            logger.debug("No balance assertion points found")
            return directives

        # Group by account and date
        by_account_date = defaultdict(list)
        for env in assertion_points:
            account = get_primary_account(env)
            if account:
                # Only generate balance assertions for bank accounts, not broker accounts
                # Users create manual balance assertions for brokers from quarterly reports
                if account_is_bank(account):
                    by_account_date[(account, env.date)].append(env)
                else:
                    logger.debug(f"Skipping balance assertion for non-bank account: {account}")

        # Apply monthly filtering if requested
        if monthly_only:
            by_account_date = self._filter_to_monthly(by_account_date)

        # Generate Balance directives with proper date semantics
        for (account, date), envs in sorted(by_account_date.items()):
            # Use last balance of day
            sorted_envs = sorted(envs, key=lambda e: e.sort_order or Decimal('0'))
            last_env = sorted_envs[-1]

            if last_env.balance_after is not None:
                # CRITICAL: Use next day for assertion (Beancount checks at START of day)
                assertion_date = date + timedelta(days=1)

                # Create Balance directive
                balance = Balance(
                    meta={
                        'filename': last_env.source_file_path or '',
                        'lineno': 0,
                        'end_of_day_for': str(date),  # Document what we're checking
                    },
                    date=assertion_date,
                    account=account,
                    amount=Amount(last_env.balance_after, 'GBP'),
                    tolerance=None,
                    diff_amount=None
                )
                directives.append(balance)

                logger.debug(f"Generated assertion for {account} on {date}: {last_env.balance_after:.2f}")

        return directives

    def _filter_to_monthly(
        self,
        by_account_date: Dict
    ) -> Dict:
        """Filter to keep only month-end balances."""
        by_account_month = defaultdict(list)

        for (account, date), envs in by_account_date.items():
            # Group by year-month
            month_key = (account, date.year, date.month)
            by_account_month[month_key].append((date, envs))

        # Keep only the last date of each month
        filtered = {}
        for (account, year, month), date_envs_list in by_account_month.items():
            # Sort by date and take the last one
            date_envs_list.sort(key=lambda x: x[0])
            last_date, last_envs = date_envs_list[-1]
            filtered[(account, last_date)] = last_envs

        return filtered

    def _group_by_year(
        self,
        balance_assertions: List[Balance]
    ) -> Dict[int, List[Balance]]:
        """Group balance assertions by year."""
        grouped = defaultdict(list)

        for assertion in balance_assertions:
            year = assertion.date.year
            grouped[year].append(assertion)

        return grouped

    def _write_year_balances(
        self,
        year: int,
        assertions: List[Balance]
    ) -> Path:
        """Write balance assertions for a specific year.

        IMPORTANT: This method APPENDS to existing files to allow multiple bank importers
        to write to the same yearly balance assertion file without overwriting each other.
        """
        # Create year directory if needed
        year_dir = self.output_dir / str(year)
        year_dir.mkdir(parents=True, exist_ok=True)

        filename = f"bank_balance_assertions_{year}.beancount"
        filepath = year_dir / filename

        # Check if file exists to determine whether to write header
        file_exists = filepath.exists()

        # Determine open mode: write for new files, append for existing
        mode = 'a' if file_exists else 'w'

        with open(filepath, mode, encoding='utf-8') as f:
            # Write header only for new files
            if not file_exists:
                f.write(f"; Bank Balance Assertions for {year}\n")
                f.write(f"; Generated: {datetime.now().isoformat()}\n")
                f.write(";\n")
                f.write("; IMPORTANT: Beancount checks balance at START of date.\n")
                f.write("; These assertions use next-day dates to check end-of-day balances.\n")
                f.write("; The 'end_of_day_for' metadata shows which day's closing balance is being checked.\n")
                f.write(f"; Total assertions: {len(assertions)}\n\n")

            # Group by account for better organization
            by_account = defaultdict(list)
            for assertion in assertions:
                by_account[assertion.account].append(assertion)

            # Sort accounts
            sorted_accounts = sorted(by_account.keys())

            # Write assertions grouped by account
            for account in sorted_accounts:
                f.write(f"\n; {account}\n")

                # Sort assertions by date
                account_assertions = sorted(
                    by_account[account],
                    key=lambda a: a.date
                )

                for assertion in account_assertions:
                    # Add comment showing what we're checking
                    if 'end_of_day_for' in assertion.meta:
                        f.write(f"; End-of-day balance for {assertion.meta['end_of_day_for']}\n")

                    # Write the assertion
                    f.write(printer.format_entry(assertion))
                    f.write("\n")

        action = "Appended" if file_exists else "Wrote"
        logger.info(f"  {action} {len(assertions)} balance assertions to {filename}")
        return filepath


# Legacy functions for backward compatibility
def generate_balance_assertions(envelopes: List[Envelope],
                                monthly_only: bool = True) -> List[str]:
    """
    Generate Beancount balance assertion directives from envelopes.

    Only generates assertions for envelopes marked as balance assertion points.
    By default, limits to monthly assertions to avoid spam.

    IMPORTANT: Beancount checks balance at START of date, so we use next day
    to check end-of-day balance.

    Args:
        envelopes: List of envelopes to process
        monthly_only: If True, only generate monthly assertions (last of month)

    Returns:
        List of balance assertion directive strings with explanatory comments
    """
    assertions = []

    # Filter to assertion points (marked by bank importer or with express balances)
    assertion_points = [
        e for e in envelopes
        if (e.metadata.get('balance_assertion_point') or
            (e.balance_after is not None and e.balance_type == 'express'))
    ]

    if not assertion_points:
        logger.debug("No balance assertion points found")
        return assertions

    # Group by account and date
    by_account_date = defaultdict(list)
    for env in assertion_points:
        # Determine account from envelope
        account = get_primary_account(env)
        if account:
            by_account_date[(account, env.date)].append(env)

    # If monthly_only, filter to month-end dates
    if monthly_only:
        by_account_month = defaultdict(list)
        for (account, date), envs in by_account_date.items():
            # Group by year-month
            month_key = (account, date.year, date.month)
            by_account_month[month_key].extend([(date, envs)])

        # Keep only the last date of each month
        filtered_account_date = {}
        for (account, year, month), date_envs_list in by_account_month.items():
            # Sort by date and take the last one
            date_envs_list.sort(key=lambda x: x[0])
            last_date, last_envs = date_envs_list[-1]
            filtered_account_date[(account, last_date)] = last_envs

        by_account_date = filtered_account_date

    # Generate assertions with proper date semantics
    for (account, date), envs in sorted(by_account_date.items()):
        # Use last balance of day (if multiple transactions)
        sorted_envs = sorted(envs, key=lambda e: e.sort_order or Decimal('0'))
        last_env = sorted_envs[-1]

        if last_env.balance_after is not None:
            # CRITICAL: Use next day for assertion (Beancount checks at START of day)
            from datetime import timedelta
            assertion_date = date + timedelta(days=1)

            # Add explanatory comment
            comment = f"; End-of-day balance for {date} (checking at start of {assertion_date})"
            assertion = f"{assertion_date} balance {account}  {last_env.balance_after:.2f} GBP"

            assertions.append(comment)
            assertions.append(assertion)

            logger.debug(f"Generated monthly assertion for {account} on {date}: {last_env.balance_after:.2f}")

    logger.info(f"Generated {len(assertions)//2} monthly balance assertions")
    return assertions


def write_balance_assertions(envelopes: List[Envelope],
                            output_file: str,
                            monthly_only: bool = True) -> int:
    """
    Write balance assertions to a file.

    Args:
        envelopes: List of envelopes to process
        output_file: Path to output file
        monthly_only: If True, only generate monthly assertions

    Returns:
        Number of assertions written
    """
    assertions = generate_balance_assertions(envelopes, monthly_only=monthly_only)

    if not assertions:
        logger.info(f"No balance assertions to write to {output_file}")
        return 0

    try:
        with open(output_file, 'w') as f:
            # Write header comment
            f.write("; Balance assertions generated from bank statement data\n")
            f.write("; Generated by balance_assertion_writer.py\n")
            f.write(";\n")
            f.write("; IMPORTANT: Beancount checks balance at START of date.\n")
            f.write("; These assertions use next-day dates to check end-of-day balances.\n")
            f.write("; E.g., '2024-04-01 balance' checks the balance AFTER all 2024-03-31 transactions.\n\n")

            # Write assertions
            for assertion in assertions:
                f.write(assertion + "\n")

        logger.info(f"Wrote {len(assertions)} balance assertions to {output_file}")
        return len(assertions)

    except Exception as e:
        logger.error(f"Failed to write balance assertions to {output_file}: {e}")
        raise


