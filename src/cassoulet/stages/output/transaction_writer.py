"""
Envelope-native output writer for the final pipeline stage.

Converts envelopes directly to Beancount transactions and writes them to
appropriate output files, handling dual-placement for cross-institution transfers.
"""

import logging
from typing import List, Tuple, Dict, Optional, Set
from collections import defaultdict
from pathlib import Path
from datetime import datetime, timedelta, date
from decimal import Decimal

from beancount.core import data
from beancount.core.amount import Amount
from beancount.parser import printer

from cassoulet.stages.envelope_processor import EnvelopeProcessor
from cassoulet.stages.envelope import Envelope
from cassoulet.base.exceptions import ProcessingWarning, NoPostingsCreatedError
from cassoulet.utils.envelope_utilities import (
    get_accounts,
    get_primary_account,
    get_currency,
    is_cross_institution_transfer,
    is_cash_only,
    has_outbound_units,
    has_flow,
    envelope_has_broker_commission,
    get_broker_commission,
    is_manual_envelope
)
from cassoulet.utils.accounts import (
    AccountMetadataRegistry,
    account_is_broker,
    account_is_bank,
    account_is_asset,
    account_is_liability,
)
from cassoulet.config.component_config import ComponentConfig
from cassoulet.stages.output.file_generator import FileGenerator

logger = logging.getLogger(__name__)

class TransactionWriter(EnvelopeProcessor):
    """
    Final pipeline stage that converts envelopes to Beancount files in one pass.

    For each envelope:
    1. Creates the Beancount transaction with postings
    2. Determines output file from envelope.source
    3. Handles dual-placement for cross-institution transfers
    4. Generates balance assertions
    5. Writes everything to organized output files
    """

    def __init__(self, output_dir: str, config: ComponentConfig = None):
        """Initialize the output writer.

        Args:
            output_dir: Directory to write output files
            config: Optional configuration (ComponentConfig instance)
        """
        super().__init__(processor_name="EnvelopeWriter")

        if not output_dir:
            from cassoulet.base.exceptions import ConfigurationError
            raise ConfigurationError('output_dir', 'EnvelopeWriter',
                                   'Output directory is required')

        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Store config, using default if not provided
        self.config = config or ComponentConfig()

        # Initialize account registry for institution lookups
        self.account_registry = AccountMetadataRegistry(
            accounts_file=self.config.get_accounts_file() if hasattr(self.config, 'get_accounts_file') else None
        )
        self.account_registry.load()

        # Initialize main file generator
        self.main_generator = FileGenerator(
            output_dir,
            template_dir=self.config.get_template_dir() if hasattr(self.config, 'get_template_dir') else None,
            manual_dir=self.config.get_manual_dir() if hasattr(self.config, 'get_manual_dir') else None,
        )

        # Track statistics
        self.stats = {
            'total_envelopes': 0,
            'transactions_created': 0,
            'dual_placed': 0,
            'balance_assertions': 0,
            'files_written': 0,
            'discarded': 0
        }

        # Store created transactions for error reporting
        self.created_transactions = []

        # Persistent file queue for transactions - (year, source) -> [(txn, is_commented)]
        self.by_file = defaultdict(list)

    def _process_internal(
        self,
        envelopes: List[Envelope]
    ) -> Tuple[List[Envelope], List[ProcessingWarning]]:
        """Process envelopes and write output files."""
        warnings = []

        self.stats['total_envelopes'] = len(envelopes)
        logger.info(f"EnvelopeWriter: Processing {len(envelopes)} envelopes")

        # Setup output directory structure
        self._setup_output_structure()

        # Clear any previous file queue (in case of re-runs)
        self.by_file.clear()
        balance_assertions = []  # [(year, account, date, amount)]

        skipped_count = 0
        discarded_count = 0

        for i, envelope in enumerate(envelopes):
            if i > 0 and i % 500 == 0:
                logger.info(f"  Progress: {i}/{len(envelopes)} envelopes, {self.stats['transactions_created']} transactions created")

            # Debug first few envelopes
            if i < 3:
                logger.debug(f"Envelope {i}: id={envelope.envelope_id[:8] if envelope.envelope_id else 'None'}, "
                           f"date={envelope.date}, narration={envelope.narration[:30] if envelope.narration else 'None'}, "
                           f"outbound={envelope.outbound_units}/{envelope.outbound_type}, "
                           f"inbound={envelope.inbound_units}/{envelope.inbound_type}")

            if envelope.is_discarded():
                self.stats['discarded'] += 1
                discarded_count += 1
                continue

            # Skip envelopes that shouldn't generate transactions
            if self._should_skip_transaction(envelope):
                skipped_count += 1
                continue

            # Create Beancount transaction(s) from envelope
            try:
                transactions = self._create_transactions_from_envelope(envelope, warnings)
                if not transactions:
                    continue

                # Process each transaction
                for txn in transactions:
                    self.stats['transactions_created'] += 1
                    self.created_transactions.append(txn)  # Store for error reporting

                    # Determine output placement
                    year = envelope.date.year

                    # Check if needs dual placement (cross-institution transfers)
                    if self._is_cross_institution_transfer(envelope):
                        self._handle_dual_placement(envelope, txn, year, self.by_file)
                        self.stats['dual_placed'] += 1
                    else:
                        # Normal single placement
                        source_file = self._get_output_file_identifier(envelope)
                        self.by_file[(year, source_file)].append((txn, False))

            except Exception as e:
                # Get more context about the failing envelope
                envelope_info = {
                    'envelope_id': envelope.envelope_id,
                    'date': str(envelope.date) if envelope.date else 'None',
                    'narration': envelope.narration[:50] if envelope.narration else 'None',
                    'outbound': f"{envelope.outbound_units}/{envelope.outbound_type}" if envelope.outbound_units else 'None',
                    'inbound': f"{envelope.inbound_units}/{envelope.inbound_type}" if envelope.inbound_units else 'None',
                    'error': str(e),
                    'error_type': type(e).__name__
                }
                warnings.append(ProcessingWarning(
                    processor_name=self.processor_name,
                    severity='ERROR',
                    message=f"Failed to create transaction for {envelope.envelope_id}: {type(e).__name__}: {e}",
                    source_transaction=envelope.envelope_id,
                    details=envelope_info
                ))

            # Balance assertions are now handled by bank importers during extraction
            # This ensures they use the correct balance data before transfer matching
            # (TransactionWriter used to create assertions here, but that was after transfers
            #  were matched and moved to other files, causing incorrect balance selection)

        # Write all output files
        self._write_output_files(self.by_file)

        # Balance assertions are written by bank importers, not here

        # Generate importers.beancount file
        self._generate_importers_file(self.by_file.keys())

        # Generate main.beancount using FileGenerator
        transaction_files = self._get_transaction_files(self.by_file.keys())
        balance_files = self._get_balance_files()
        main_file = self.main_generator.generate_main_file(
            transaction_files,
            balance_files,
            include_orphans=False
        )
        logger.info(f"Generated main file: {main_file}")

        # Log summary with issues
        logger.info(
            f"EnvelopeWriter complete: "
            f"{self.stats['total_envelopes']} envelopes -> "
            f"{self.stats['transactions_created']} transactions created"
        )
        logger.info(
            f"  Discarded: {discarded_count}, Skipped: {skipped_count}, "
            f"Dual-placed: {self.stats['dual_placed']}, "
            f"Balance assertions: {self.stats['balance_assertions']}, "
            f"Files written: {self.stats['files_written']}"
        )

        if self.stats['transactions_created'] == 0:
            logger.error("WARNING: No transactions were created from envelopes!")
            warnings.append(ProcessingWarning(
                processor_name=self.processor_name,
                severity='ERROR',
                message=f"No transactions created from {self.stats['total_envelopes']} envelopes",
                source_transaction=None,
                details={'discarded': discarded_count, 'skipped': skipped_count}
            ))

        return envelopes, warnings

    def get_created_transactions(self) -> List[data.Transaction]:
        """Get the transactions that were created during processing.

        This is needed for error reporting which requires both envelopes
        and the canonical transactions.
        """
        return self.created_transactions

    def _should_skip_transaction(self, envelope: Envelope) -> bool:
        """Check if this envelope should not generate a transaction."""
        # Skip balance-only envelopes
        if envelope.metadata.get('balance_only'):
            return True

        # Skip if marked as non-transaction
        if envelope.metadata.get('skip_transaction'):
            return True

        return False

    def _create_transactions_from_envelope(
        self,
        envelope: Envelope,
        warnings: List[ProcessingWarning]
    ) -> List[data.Transaction]:
        """
        Create one or more Beancount Transactions from an envelope.

        Most envelopes generate a single transaction, but manual BUY transactions
        with commission generate two transactions:
        1. The main purchase with total cost (for tax basis)
        2. A virtual commission tracking transaction (for expense visibility)

        Returns:
            List of transactions (empty list if envelope should be skipped)
        """
        transactions = []

        # Check if envelope has required fields
        if not envelope.date:
            logger.warning(f"Envelope {envelope.envelope_id} has no date, skipping")
            return []

        # Special handling for manual BUY with commission - generate two transactions
        if (envelope.transaction_type == 'BUY' and
            is_manual_envelope(envelope) and
            envelope_has_broker_commission(envelope)):

            # Get commission info
            commission_info = get_broker_commission(envelope)
            if commission_info:
                commission_amount, commission_account = commission_info

                # Transaction 1: Main purchase with total cost
                main_txn = self._create_main_buy_transaction(envelope, commission_amount, warnings)
                if main_txn:
                    transactions.append(main_txn)

                # Transaction 2: Commission tracking (virtual, net-zero P&L) - GitHub issue #127
                tracking_txn = self._create_commission_tracking_transaction(
                    envelope, commission_amount, commission_account
                )
                if tracking_txn:
                    transactions.append(tracking_txn)

                return transactions

        # Regular transaction creation for all other cases
        txn = self._create_single_transaction_from_envelope(envelope, warnings)
        if txn:
            transactions.append(txn)

        return transactions

    def _create_base_transaction(
        self,
        envelope: Envelope,
        narration_override: Optional[str] = None
    ) -> data.Transaction:
        """
        Create the base transaction structure shared by all transaction types.

        Args:
            envelope: The source envelope
            narration_override: Optional narration to use instead of envelope's

        Returns:
            A Transaction with metadata but no postings
        """
        return data.Transaction(
            meta=self._create_transaction_metadata(envelope),
            date=envelope.date,
            flag='*',
            payee=envelope.payee,
            narration=narration_override or envelope.narration or '',
            tags=self._create_transaction_tags(envelope),
            links=set(envelope.links) if envelope.links else None,
            postings=[]
        )

    def _create_single_transaction_from_envelope(
        self,
        envelope: Envelope,
        warnings: List[ProcessingWarning]
    ) -> Optional[data.Transaction]:
        """
        Create a single Beancount Transaction from an envelope.
        This handles the standard case (non-commission transactions).
        """
        # Create base transaction
        txn = self._create_base_transaction(envelope)

        # Create postings based on envelope pattern
        try:
            postings = self._create_postings_from_envelope(envelope, warnings)
        except NoPostingsCreatedError as e:
            # Log this as an ERROR since it indicates missing account data
            warnings.append(ProcessingWarning(
                processor_name=self.processor_name,
                severity='ERROR',
                message=str(e),
                source_transaction=envelope.envelope_id,
                details={'has_flow': e.has_flow, 'details': e.details}
            ))
            return None

        if not postings:
            warnings.append(ProcessingWarning(
                processor_name=self.processor_name,
                severity='WARNING',
                message="No postings created for envelope",
                source_transaction=envelope.envelope_id
            ))
            return None

        txn = txn._replace(postings=postings)
        return txn

    def _create_main_buy_transaction(
        self,
        envelope: Envelope,
        commission_amount: Decimal,
        warnings: List[ProcessingWarning]
    ) -> Optional[data.Transaction]:
        """
        Create the main BUY transaction with total cost including commission.

        This transaction correctly records the cost basis for tax purposes.
        The total cost includes both principal and commission.
        """
        from beancount.core import amount
        from decimal import Decimal

        # For manual transactions, envelope.outbound_units is already the total (principal + commission)
        # We don't need to add commission again
        total_cost = abs(envelope.outbound_units) if envelope.outbound_units else Decimal('0')

        # Create the base transaction
        txn = self._create_base_transaction(envelope)

        # Create postings: commodity with total cost and cash payment
        postings = []

        # Commodity posting with total cost syntax
        if envelope.inbound_units and envelope.inbound_account and envelope.inbound_type:
            cost_spec = data.CostSpec(
                number_per=None,
                number_total=total_cost,
                currency=envelope.outbound_type or 'GBP',
                date=None,
                label=None,
                merge=False
            )
            postings.append(data.Posting(
                account=envelope.inbound_account,
                units=amount.Amount(abs(envelope.inbound_units), envelope.inbound_type),
                cost=cost_spec,
                price=None,
                flag=None,
                meta=None
            ))

        # Cash posting for total amount (principal + commission)
        if envelope.outbound_account:
            postings.append(data.Posting(
                account=envelope.outbound_account,
                units=amount.Amount(-total_cost, envelope.outbound_type or 'GBP'),
                cost=None,
                price=None,
                flag=None,
                meta=None
            ))

        if not postings:
            warnings.append(ProcessingWarning(
                processor_name=self.processor_name,
                severity='ERROR',
                message=f"Failed to create postings for main BUY transaction",
                source_transaction=envelope.envelope_id
            ))
            return None

        txn = txn._replace(postings=postings)
        return txn

    def _create_commission_tracking_transaction(
        self,
        envelope: Envelope,
        commission_amount: Decimal,
        commission_account: str
    ) -> Optional[data.Transaction]:
        """
        Create the virtual commission tracking transaction.

        This transaction has net-zero P&L impact but allows tracking of
        commission expenses for reporting purposes.
        """
        from beancount.core import amount

        # Create the base transaction with modified narration
        txn = self._create_base_transaction(
            envelope,
            narration_override=f"Commission for: {envelope.narration or 'Trade'}"
        )

        # Create two postings that net to zero
        postings = [
            # Commission expense
            data.Posting(
                account=commission_account,
                units=amount.Amount(commission_amount, envelope.outbound_type or 'GBP'),
                cost=None,
                price=None,
                flag=None,
                meta=None
            ),
            # Contra income account (capitalized commission)
            data.Posting(
                account=self.config.accounts.capitalized_commissions_income,
                units=amount.Amount(-commission_amount, envelope.outbound_type or 'GBP'),
                cost=None,
                price=None,
                flag=None,
                meta=None
            )
        ]

        txn = txn._replace(postings=postings)
        return txn

    def _create_transaction_metadata(self, envelope: Envelope) -> Dict:
        """Create transaction metadata from envelope.

        TODO: Currently includes ALL metadata fields. A whitelist/filter needs to be
        established in transaction_writer to only include relevant fields for
        Beancount output. Consider fields like:
        - Reconciliation: original_narration, match_confidence, transfer_type
        - Gap remediation: gap_type, gap_amount, expected_balance, actual_balance
        - Investment: lot_created, consumed_lots, cost_basis
        - Import tracking: original_line_number, csv_row_number
        """
        # Always include these core fields
        meta = {
            'filename': envelope.source_file_path or '',
            'lineno': 0,
            'envelope_id': envelope.envelope_id,
        }

        # Add source if available
        if envelope.source:
            meta['source'] = envelope.source

        # TODO: For now, add ALL metadata fields (needs filtering)
        if envelope.metadata:
            for key, value in envelope.metadata.items():
                # Skip envelope objects and other non-serializable items
                if key == 'envelope':
                    continue

                # Sanitize key for Beancount - must be lowercase with underscores
                # Replace any non-alphanumeric chars with underscores, convert to lowercase
                import re
                sanitized_key = re.sub(r'[^a-zA-Z0-9_]', '_', key).lower()
                # Remove leading/trailing underscores and collapse multiple underscores
                sanitized_key = re.sub(r'_+', '_', sanitized_key).strip('_')

                # Convert all values to appropriate types for Beancount
                # Beancount metadata only accepts: str, date, Amount, bool
                if isinstance(value, (list, dict)):
                    meta[sanitized_key] = str(value)
                elif isinstance(value, (int, float, Decimal)):
                    # Convert numbers to strings for Beancount metadata
                    meta[sanitized_key] = str(value)
                elif isinstance(value, bool):
                    # Booleans need to be strings in Beancount metadata
                    meta[sanitized_key] = str(value)
                elif isinstance(value, date):
                    # Dates are OK
                    meta[sanitized_key] = value
                elif value is not None:
                    # Everything else as string
                    meta[sanitized_key] = str(value)

        # Persist bank-reported running balance for FBAR/FATCA peak value computation.
        # This captures intra-day peaks that Beancount's end-of-day realization misses.
        if envelope.balance_after is not None and envelope.balance_type == 'express':
            meta['canonical_balance'] = str(envelope.balance_after)

        return meta

    def _create_transaction_tags(self, envelope: Envelope) -> Optional[Set[str]]:
        """Create transaction tags from envelope.

        Simply passes through tags that were set during processing.
        The balance_gap_detector and other processors are responsible
        for setting appropriate tags.
        """
        tags = set()

        # Combine all tags from envelope
        if envelope.tags:
            tags.update(envelope.tags)
        if envelope.beancount_tags:
            tags.update(envelope.beancount_tags)

        return tags if tags else None

    def _create_postings_from_envelope(
        self,
        envelope: Envelope,
        warnings: List[ProcessingWarning]  # TODO: Use for posting-level warnings
    ) -> List[data.Posting]:
        """
        Create postings from envelope using the universal inbound/outbound pattern.

        This is the core of the deferred posting architecture - all information
        needed to create correct postings has been accumulated in the envelope.
        """
        postings = []
        need_capital_gains = False  # Track if we need capital gains posting

        # Ensure all numeric fields are Decimals before processing
        from cassoulet.utils.cleaner import ensure_envelope_decimals
        envelope = ensure_envelope_decimals(envelope)

        # Note: Manual BUY transactions with commission are now handled by
        # _create_transactions_from_envelope which generates two separate transactions

        # Special handling for commodity transfers (in-specie transfers)
        # According to comprehensive testing (lessons.md), these MUST use explicit matching costs
        if envelope.transaction_type == 'COMMODITY_TRANSFER' and envelope.metadata and 'transferred_lots' in envelope.metadata:
            return self._create_commodity_transfer_postings(envelope, warnings)

        # Handle outbound (what goes out) - outbound is always negative in accounting
        if envelope.outbound_units and envelope.outbound_account:
            # Check if this is an investment transaction needing cost basis
            if envelope.transaction_type == 'SELL' and not is_cash_only(envelope):
                # SELL transaction - need to book against lots
                if envelope.metadata and 'consumed_lots' in envelope.metadata:
                    try:
                        consumed_lots = envelope.metadata['consumed_lots']

                        # Create individual postings for each consumed lot
                        for lot in consumed_lots:
                            quantity = Decimal(str(lot['quantity_consumed']))
                            cost_per_unit = Decimal(str(lot['cost_per_unit']))
                            acquisition_date = lot.get('acquisition_date')

                            # Parse acquisition_date if it's a string
                            if isinstance(acquisition_date, str):
                                from datetime import datetime
                                acquisition_date = datetime.strptime(acquisition_date, '%Y-%m-%d').date()

                            # Create specific cost for this lot
                            cost = data.Cost(
                                number=cost_per_unit,
                                currency='GBP',  # TODO: Get from lot metadata
                                date=acquisition_date,
                                label=None
                            )

                            posting = data.Posting(
                                account=envelope.outbound_account,
                                units=Amount(-abs(quantity), envelope.outbound_type),
                                cost=cost,
                                price=None,
                                flag=None,
                                meta=None
                            )
                            postings.append(posting)

                        # For SELL transactions, we need to add capital gains/loss posting
                        need_capital_gains = True
                    except Exception as e:
                        # CRITICAL: SELL transaction should have consumed_lots from lot processor
                        # This is a Steel Thread violation - we do NOT fall back to FIFO
                        # Instead, we create no posting and let the transaction fail bean-check
                        from cassoulet.base.exceptions import LotTrackingWarning
                        warnings.append(LotTrackingWarning(
                            processor_name=self.processor_name,
                            severity='ERROR',
                            message=f"Failed to parse consumed_lots for SELL - lot tracking failure",
                            source_transaction=envelope.envelope_id,
                            lot_issue_type='orphaned_sale',
                            affected_commodity=envelope.outbound_type,
                            affected_account=envelope.outbound_account,
                            details={'error': str(e), 'consumed_lots_raw': str(envelope.metadata.get('consumed_lots'))}
                        ))
                        # NO FALLBACK: Transaction will fail bean-check, which is correct
                        # We cannot safely determine which lots to use without consumed_lots
                else:
                    # CRITICAL: SELL transaction should have consumed_lots from lot processor
                    # This is a Steel Thread violation - we do NOT fall back to FIFO
                    from cassoulet.base.exceptions import LotTrackingWarning
                    warnings.append(LotTrackingWarning(
                        processor_name=self.processor_name,
                        severity='ERROR',
                        message=f"SELL transaction missing consumed_lots - lot processor failure",
                        source_transaction=envelope.envelope_id,
                        lot_issue_type='orphaned_sale',
                        affected_commodity=envelope.outbound_type,
                        affected_account=envelope.outbound_account,
                        details={
                            'has_metadata': bool(envelope.metadata),
                            'units': str(envelope.outbound_units),
                            'reason': 'Lot processor did not populate consumed_lots metadata'
                        }
                    ))
                    # NO FALLBACK: Transaction will fail bean-check, which is correct
                    # We cannot safely determine which lots to use without consumed_lots
            else:
                # Regular outbound posting - always negative (money leaving)
                # In Beancount, liabilities are negative (you owe money)
                # A purchase on credit card makes the liability MORE negative
                posting = data.Posting(
                    account=envelope.outbound_account,
                    units=Amount(-abs(envelope.outbound_units), envelope.outbound_type),
                    cost=None,
                    price=None,
                    flag=None,
                    meta=None
                )
                postings.append(posting)

        # Handle inbound (what comes in) - inbound is always positive in accounting
        if envelope.inbound_units and envelope.inbound_account:
            # Check if this is a BUY transaction needing cost basis
            if envelope.transaction_type == 'BUY' and not is_cash_only(envelope):
                # BUY transaction - record cost basis
                # Note: Manual BUY transactions with commission are handled separately
                # by _create_transactions_from_envelope which generates two transactions.
                # This path is only for regular BUY transactions without explicit commission.

                # Regular per-unit cost
                cost = data.Cost(
                    number=envelope.unit_price,
                    currency=envelope.outbound_type if envelope.outbound_type else 'GBP',
                    date=None,
                    label=None
                )
                posting = data.Posting(
                    account=envelope.inbound_account,
                    units=Amount(abs(envelope.inbound_units), envelope.inbound_type),
                    cost=cost,
                    price=None,
                    flag=None,
                    meta=None
                )
            else:
                # Regular inbound posting - ensure positive
                posting = data.Posting(
                    account=envelope.inbound_account,
                    units=Amount(abs(envelope.inbound_units), envelope.inbound_type),
                    cost=None,
                    price=None,
                    flag=None,
                    meta=None
                )
            postings.append(posting)

        # Handle transfer leakage (fees/losses on transfers)
        if envelope.metadata and 'transfer_leakage' in envelope.metadata:
            leakage = envelope.metadata.get('transfer_leakage', Decimal('0'))

            # ensure_envelope_decimals should have converted this already
            if abs(leakage) > Decimal('0.001'):  # Only if significant
                # Use get_currency() utility to determine currency
                currency = envelope.metadata.get('leakage_currency') or get_currency(envelope) or 'GBP'
                # Add posting for the fee/loss
                if leakage > 0:
                    # Loss/fee on transfer
                    postings.append(data.Posting(
                        account=self.config.accounts.transfer_leakage_expense,
                        units=Amount(abs(leakage), currency),
                        cost=None, price=None, flag=None, meta=None
                    ))
                else:
                    # Gain on transfer (rare but possible)
                    postings.append(data.Posting(
                        account='Income:TransferLeakage',
                        units=Amount(abs(leakage), currency),
                        cost=None, price=None, flag=None, meta=None
                    ))

        # Add capital gains posting for SELL transactions
        if need_capital_gains:
            # Calculate the capital gain/loss from consumed lots
            capital_gain = self._calculate_capital_gain(envelope)

            if capital_gain is not None:
                # Add explicit capital gain/loss posting
                # Positive = loss (balances against lower sale proceeds)
                # Negative = gain (balances against higher sale proceeds)
                postings.append(data.Posting(
                    account='Income:CapitalGains',
                    units=Amount(capital_gain, 'GBP'),
                    cost=None,
                    price=None,
                    flag=None,
                    meta=None
                ))
            else:
                # Fallback: Let Beancount try to calculate (may fail)
                postings.append(data.Posting(
                    account='Income:CapitalGains',
                    units=None,  # Let Beancount calculate
                    cost=None,
                    price=None,
                    flag=None,
                    meta=None
                ))

        # Handle additional postings (for 3+ posting transactions)
        if envelope.additional_postings:
            for ap in envelope.additional_postings:
                # Each additional posting dict has: units, type, account
                # Units preserve sign for proper accounting
                posting = data.Posting(
                    account=ap['account'],
                    units=Amount(ap['units'], ap['type']),
                    cost=None,
                    price=None,
                    flag=None,
                    meta=None
                )
                postings.append(posting)
                logger.debug(f"Added additional posting: {ap['account']} {ap['units']} {ap['type']}")

        # Check if we have an unbalanced transaction
        if len(postings) == 0:
            # No postings created - check if envelope has any flow
            if not has_flow(envelope):
                # No flow - might be a metadata-only envelope (like gap remediation marker)
                logger.debug(f"Envelope {envelope.envelope_id} has no flow - skipping transaction creation")
                return []  # Return empty, will be skipped

            # Has flow but no postings - this is a data integrity issue
            # Raise a specific exception so it can be tracked properly
            details = f"outbound={envelope.outbound_units}/{envelope.outbound_type}, inbound={envelope.inbound_units}/{envelope.inbound_type}"
            raise NoPostingsCreatedError(
                envelope_id=envelope.envelope_id,
                has_flow=True,
                details=details
            )

        elif len(postings) == 1:
            # Single posting created - need balancing
            # This is normal for single-leg transactions like dividends, fees, etc.
            existing = postings[0]

            if existing.units.number < 0:
                # Outbound - balance with appropriate expense/income account
                balance_account = self._get_balance_account_for_type(envelope)
                postings.append(data.Posting(
                    account=balance_account,
                    units=Amount(-existing.units.number, existing.units.currency),
                    cost=None, price=None, flag=None, meta=None
                ))
            else:
                # Inbound - balance with appropriate income/expense account
                balance_account = self._get_balance_account_for_type(envelope)
                postings.append(data.Posting(
                    account=balance_account,
                    units=Amount(-existing.units.number, existing.units.currency),
                    cost=None, price=None, flag=None, meta=None
                ))

        return postings

    def _calculate_capital_gain(self, envelope: Envelope):
        """
        Calculate capital gain/loss from consumed lots metadata.

        Returns:
            Decimal: The capital gain (negative) or loss (positive) amount
            None: If unable to calculate
        """
        if not envelope.metadata or 'consumed_lots' not in envelope.metadata:
            return None

        from decimal import Decimal
        import ast

        try:
            # Parse consumed_lots if it's a string
            consumed_lots = envelope.metadata['consumed_lots']
            if isinstance(consumed_lots, str):
                consumed_lots = ast.literal_eval(consumed_lots)

            # Calculate total cost basis
            total_cost_basis = Decimal('0')
            for lot in consumed_lots:
                quantity = Decimal(str(lot['quantity_consumed']))
                cost_per_unit = Decimal(str(lot['cost_per_unit']))
                total_cost_basis += quantity * cost_per_unit

            # Get sale proceeds (the cash received)
            sale_proceeds = Decimal(str(envelope.inbound_units)) if envelope.inbound_units else Decimal('0')

            # Calculate gain/loss
            # Loss = cost_basis - sale_proceeds (positive when loss)
            # Gain = sale_proceeds - cost_basis (negative when gain, to balance)
            capital_gain_loss = total_cost_basis - sale_proceeds

            logger.debug(
                f"Capital gain/loss for {envelope.envelope_id}: "
                f"cost_basis={total_cost_basis}, proceeds={sale_proceeds}, "
                f"gain_loss={capital_gain_loss}"
            )

            return capital_gain_loss

        except Exception as e:
            logger.warning(f"Failed to calculate capital gain for {envelope.envelope_id}: {e}")
            return None

    def _create_commodity_transfer_postings(
        self,
        envelope: Envelope,
        warnings: List[ProcessingWarning]
    ) -> List[data.Posting]:
        """
        Create postings for commodity transfers (in-specie transfers).

        According to comprehensive testing (lessons.md), the ONLY acceptable solution
        for in-specie transfers is to use explicit matching costs on BOTH sides:

        2021-08-06 * "In-specie transfer"
          Assets:Broker:Destination   6261.153 LGTACC {3.194299835 GBP}
          Assets:Broker:Source       -6261.153 LGTACC {3.194299835 GBP}

        This preserves cost basis without creating taxable event appearances.
        """
        postings = []
        transferred_lots = envelope.metadata.get('transferred_lots', [])

        if not transferred_lots:
            # No lot information - fall back to regular posting creation
            warnings.append(ProcessingWarning(
                processor_name=self.processor_name,
                severity='WARNING',
                message=f"Commodity transfer without lot information",
                source_transaction=None,
                details={'envelope_id': envelope.envelope_id}
            ))
            # Create simple transfer without cost basis (not ideal but better than nothing)
            if envelope.outbound_units and envelope.outbound_account:
                postings.append(data.Posting(
                    account=envelope.outbound_account,
                    units=Amount(-abs(envelope.outbound_units), envelope.outbound_type),
                    cost=None,
                    price=None,
                    flag=None,
                    meta=None
                ))
            if envelope.inbound_units and envelope.inbound_account:
                postings.append(data.Posting(
                    account=envelope.inbound_account,
                    units=Amount(abs(envelope.inbound_units), envelope.inbound_type),
                    cost=None,
                    price=None,
                    flag=None,
                    meta=None
                ))
            return postings

        # Create postings with explicit matching costs from transferred lots
        for lot in transferred_lots:
            quantity = lot['quantity']
            cost_per_unit = lot['cost_per_unit']
            acquisition_date = lot.get('acquisition_date')

            # Create the explicit cost for both sides
            cost = data.Cost(
                number=cost_per_unit,
                currency='GBP',  # TODO: Get from envelope or lot metadata
                date=acquisition_date,  # Use original acquisition date from lot metadata
                label=None
            )

            # Outbound posting (source) - negative with explicit cost
            if envelope.outbound_account:
                postings.append(data.Posting(
                    account=envelope.outbound_account,
                    units=Amount(-abs(quantity), envelope.outbound_type),
                    cost=cost,
                    price=None,
                    flag=None,
                    meta=None
                ))

            # Inbound posting (destination) - positive with same explicit cost
            if envelope.inbound_account:
                postings.append(data.Posting(
                    account=envelope.inbound_account,
                    units=Amount(abs(quantity), envelope.inbound_type or envelope.outbound_type),
                    cost=cost,
                    price=None,
                    flag=None,
                    meta=None
                ))

        if not postings:
            # Shouldn't happen but handle gracefully
            warnings.append(ProcessingWarning(
                processor_name=self.processor_name,
                severity='ERROR',
                message=f"Failed to create commodity transfer postings",
                source_transaction=None,
                details={
                    'envelope_id': envelope.envelope_id,
                    'lots': len(transferred_lots)
                }
            ))

        return postings

    def _get_balance_account_for_type(self, envelope: Envelope) -> str:
        """Get the appropriate balancing account based on transaction type.

        For asset accounts (banks, brokers):
          - Positive (inbound) = Income
          - Negative (outbound) = Expense

        For liability accounts (credit cards, loans):
          - Positive (debt increase) = Expense (you spent money)
          - Negative (debt decrease) = Income (refund) or transfer from Assets (payment)

        Checks expense categorization metadata first if available.
        """
        # Check for expense categorization (from ExpenseCategorizationProcessor)
        if envelope.metadata.get('expense_account'):
            return envelope.metadata['expense_account']
        if envelope.metadata.get('income_account'):
            return envelope.metadata['income_account']

        if envelope.transaction_type == 'DIVIDEND':
            return 'Income:Dividends'
        elif envelope.transaction_type == 'INTEREST':
            return 'Income:Interest'
        elif envelope.transaction_type == 'FEE':
            return 'Expenses:Fees'
        elif envelope.transaction_type == 'EXPENSE':
            return 'Expenses:UK:Unknown'
        elif envelope.transaction_type == 'INCOME':
            return 'Income:Other'
        else:
            # Try to guess from narration
            narr_lower = (envelope.narration or '').lower()
            if 'dividend' in narr_lower:
                return 'Income:Dividends'
            elif 'interest' in narr_lower:
                return 'Income:Interest'
            elif 'fee' in narr_lower or 'charge' in narr_lower:
                return 'Expenses:Fees'

            # Determine account type to apply correct logic
            primary_account = (envelope.inbound_account or envelope.outbound_account or '')

            # For liability accounts (credit cards, loans, mortgages),
            # the semantics are inverted: positive = expense, negative = income/payment
            if account_is_liability(primary_account):
                if envelope.inbound_units and envelope.inbound_units > 0:
                    # Debt increasing = you spent money = Expense
                    return 'Expenses:UK:Unknown'
                else:
                    # Debt decreasing = payment or refund
                    return 'Income:Other'

            # For asset accounts, standard logic applies
            # Default based on flow direction
            if envelope.inbound_units:
                return 'Income:Other'
            else:
                return 'Expenses:UK:Unknown'

    def _get_balance_account_for_single_leg(self, envelope: Envelope) -> str:
        """Get the balancing account for single-leg transactions.

        Args:
            envelope: The envelope to balance

        Returns:
            Appropriate balancing account
        """
        # First try the standard type-based account
        type_account = self._get_balance_account_for_type(envelope)
        if type_account != 'Expenses:UK:Unknown' and type_account != 'Income:Other':
            return type_account

        # For unknown transaction types, determine from flow direction
        if has_outbound_units(envelope):
            return 'Expenses:UK:Unknown'
        else:
            return 'Income:Other'

    def _is_cross_institution_transfer(self, envelope: Envelope) -> bool:
        """Check if envelope represents a transfer between different institutions."""
        # Use the envelope utility function
        return is_cross_institution_transfer(envelope)

    def _handle_dual_placement(
        self,
        envelope: Envelope,
        txn: data.Transaction,
        year: int,
        by_file: Dict
    ):
        """Handle dual placement for cross-institution transfers."""
        outbound_acct, inbound_acct = get_accounts(envelope)

        # Check if this involves a company account — consolidate to company file
        company = self._get_company_tag(envelope)
        if company:
            by_file[(year, company)].append((txn, False))
            logger.debug(
                f"Company transfer: placed in {company} file for {envelope.envelope_id}"
            )
            return

        # Get output file identifiers for transfers
        source_file = self.account_registry.get_output_file_prefix(outbound_acct)
        dest_file = self.account_registry.get_output_file_prefix(inbound_acct)

        # Determine which file gets the active entry
        # Rule: Broker/investment accounts get active entry
        if account_is_broker(inbound_acct):
            # Destination (broker) gets active, source gets commented
            by_file[(year, dest_file)].append((txn, False))   # Active
            by_file[(year, source_file)].append((txn, True))  # Commented

            logger.debug(
                f"Dual placement: {dest_file} (active), {source_file} (commented) "
                f"for transfer {envelope.envelope_id}"
            )
        else:
            # Source gets active, destination gets commented
            by_file[(year, source_file)].append((txn, False))  # Active
            by_file[(year, dest_file)].append((txn, True))     # Commented

            logger.debug(
                f"Dual placement: {source_file} (active), {dest_file} (commented) "
                f"for transfer {envelope.envelope_id}"
            )

    def _get_company_tag(self, envelope: Envelope) -> Optional[str]:
        """Get company tag from envelope metadata, if any.

        Returns a short identifier (e.g., 'acme') for company-specific envelopes,
        or None for personal transactions. Company tags are set by custom
        processors via envelope.metadata['company'].

        Used to consolidate all company-related transactions into a single
        output file per company.
        """
        if not envelope.metadata:
            return None

        company = envelope.metadata.get('company')
        if company:
            # Convert company name to a clean file identifier
            return company.lower().replace(' ', '_').replace('.', '')

        return None

    def _get_output_file_identifier(self, envelope: Envelope) -> str:
        """Get the output file identifier for an envelope.

        Generates cleaner filenames in the format:
        - institution_account_identifier (e.g., vanguard_jack_isa)
        - institution_product (e.g., ajbell_sipp)
        - special cases (e.g., balance_remediations, manual_transaction)
        """
        # Check if this is a balance gap remediation
        from cassoulet.utils.envelope_utilities import is_remediation_envelope
        if is_remediation_envelope(envelope):
            return 'balance_remediations'

        # Check for manual transactions
        if envelope.source and envelope.source.lower() == 'manual_transaction':
            return 'manual_transaction'

        # Consolidate company-tagged transactions into a per-company file
        company_tag = self._get_company_tag(envelope)
        if company_tag:
            return company_tag

        # Try to build a better filename from institution and source/account
        institution = None
        if envelope.metadata and 'institution' in envelope.metadata:
            institution = envelope.metadata.get('institution', '').lower().replace(' ', '_')

        # Clean up the source filename if available
        if envelope.source:
            source = envelope.source.lower()

            # Remove .csv extension if present
            if source.endswith('.csv'):
                source = source[:-4]

            # For multi-file importers like AJ Bell, clean up the filename
            # e.g., "jack_ajb_sipp_txn.csv" -> "ajbell_jack_sipp"
            if institution:
                # Common patterns to clean up:
                # - Remove "_txn" or "_cash" suffix
                # - Convert "ajb" to full institution name if needed
                # - PRESERVE person names
                parts = source.split('_')
                cleaned_parts = []

                for part in parts:
                    # Skip only file-type suffixes, NOT person names
                    if part in ['txn', 'cash', 'csv']:
                        continue
                    # Convert abbreviations
                    if part == 'ajb':
                        part = 'ajbell'
                    elif part == 'hl':
                        part = 'hargreaves'
                    cleaned_parts.append(part)

                if cleaned_parts:
                    # If we have an institution, prepend it if not already there
                    inst_key = institution.lower().replace(' ', '').replace('_', '')
                    first_part = cleaned_parts[0].replace('_', '')

                    if inst_key in ['ajbell', 'vanguarduk', 'hargreaveslansdown', 'interactiveinvestor']:
                        # For known brokers, use institution_personname_account format
                        # But avoid duplicating institution name if already present
                        if inst_key == 'ajbell':
                            if 'ajbell' not in cleaned_parts:
                                return f"ajbell_{'_'.join(cleaned_parts)}"
                            else:
                                # Already has ajbell, just use cleaned parts
                                return '_'.join(cleaned_parts)
                        elif inst_key == 'vanguarduk':
                            # Remove vanguard if present to avoid duplication
                            non_vanguard = [p for p in cleaned_parts if p != 'vanguard']
                            return f"vanguard_{'_'.join(non_vanguard)}"
                        elif inst_key == 'hargreaveslansdown':
                            # Remove hl/hargreaves if present to avoid duplication
                            non_hl = [p for p in cleaned_parts if p not in ['hl', 'hargreaves']]
                            return f"hl_{'_'.join(non_hl)}"
                        elif inst_key == 'interactiveinvestor':
                            # Remove ii if present to avoid duplication
                            non_ii = [p for p in cleaned_parts if p != 'ii']
                            return f"ii_{'_'.join(non_ii)}"

                    return '_'.join(cleaned_parts)

            return source

        # Try to determine from primary account
        account = get_primary_account(envelope)
        if account:
            return self.account_registry.get_output_file_prefix(account)

        # Default
        return 'unknown'

    def _should_generate_balance_assertion(self, envelope: Envelope) -> bool:
        """Check if this envelope should generate a balance assertion.

        Only generate assertions for envelopes explicitly marked with 'balance_assertion_point'
        by the bank importer. This prevents duplicate assertions.

        Bank importers mark the appropriate monthly balance assertion points during their
        processing, so TransactionWriter just needs to write those out.
        """
        # Only create assertions for explicitly marked envelopes
        # Bank importers set this metadata on the envelopes they've selected
        return envelope.metadata.get('balance_assertion_point', False)

    def _create_balance_assertion(self, envelope: Envelope) -> Tuple:
        """Create balance assertion data from envelope."""
        account = get_primary_account(envelope)
        if not account:
            return None

        # Use next day for assertion (Beancount checks at START of day)
        assertion_date = envelope.date + timedelta(days=1)

        return (
            envelope.date.year,
            account,
            assertion_date,
            envelope.balance_after,
            envelope.date,  # Original date for reference
            envelope.sort_order or Decimal('0')  # Sort order for deduplication
        )

    def _setup_output_structure(self):
        """Setup the output directory structure."""
        # Create year directories if needed
        # Copy template files if needed
        # This would be expanded based on requirements
        pass

    def _write_output_files(self, by_file: Dict):
        """Write transactions to their respective files."""
        for (year, source), transactions in by_file.items():
            # Create year directory
            year_dir = self.output_dir / str(year) / 'machine_generated'
            year_dir.mkdir(parents=True, exist_ok=True)

            # Output file path
            filename = f"{source}_{year}.beancount"
            filepath = year_dir / filename

            with open(filepath, 'w', encoding='utf-8') as f:
                # Write header
                f.write(f"; {source.upper()} Transactions for {year}\n")
                f.write(f"; Generated: {datetime.now().isoformat()}\n")
                f.write(f"; Total transactions: {len(transactions)}\n\n")

                # Separate active and commented
                active_txns = [txn for txn, commented in transactions if not commented]
                commented_txns = [txn for txn, commented in transactions if commented]

                # Write active transactions
                if active_txns:
                    f.write("; Active transactions\n\n")
                    for txn in sorted(active_txns, key=lambda t: (t.date, t.narration)):
                        f.write(printer.format_entry(txn))
                        f.write("\n")

                # Write commented transactions
                if commented_txns:
                    f.write("\n; Commented transfers (active in destination file)\n\n")
                    for txn in sorted(commented_txns, key=lambda t: (t.date, t.narration)):
                        # Write as commented
                        entry_str = printer.format_entry(txn)
                        commented_str = '\n'.join(f"; {line}" for line in entry_str.split('\n'))
                        f.write(commented_str)
                        f.write("\n\n")

            self.stats['files_written'] += 1
            logger.info(f"Wrote {len(active_txns)} active and {len(commented_txns)} commented transactions to {filepath}")

    def _write_balance_assertions(self, assertions: List[Tuple]):
        """Write balance assertions to files."""
        # First, deduplicate assertions - keep only the last balance for each account/date
        # The "last" balance is the one with the highest sort_order (end-of-day balance)
        by_account_date = {}
        for assertion in assertions:
            if assertion:  # Skip None entries
                year, account, assertion_date, amount, original_date, sort_order = assertion
                key = (account, assertion_date)

                logger.debug(f"Processing assertion: date={original_date}, account={account}, amount={amount}, sort_order={sort_order}")

                # Keep the assertion with the highest sort_order for this account/date
                # This ensures we use the end-of-day balance when multiple transactions
                # occur on the same day
                if key not in by_account_date:
                    logger.debug(f"  -> New key, storing assertion")
                    by_account_date[key] = (assertion, sort_order)
                elif sort_order > by_account_date[key][1]:
                    logger.debug(f"  -> Higher sort_order ({sort_order} > {by_account_date[key][1]}), replacing")
                    by_account_date[key] = (assertion, sort_order)
                else:
                    logger.debug(f"  -> Lower sort_order ({sort_order} <= {by_account_date[key][1]}), keeping existing")

        # Now group deduplicated assertions by year
        by_year = defaultdict(list)
        for assertion, _ in by_account_date.values():
            year = assertion[0]
            by_year[year].append(assertion)

        for year, year_assertions in by_year.items():
            year_dir = self.output_dir / str(year)
            year_dir.mkdir(parents=True, exist_ok=True)

            filepath = year_dir / f"bank_balance_assertions_{year}.beancount"

            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(f"; Bank Balance Assertions for {year}\n")
                f.write(f"; Generated: {datetime.now().isoformat()}\n")
                f.write("; Note: Assertions use next-day dates (Beancount checks at START of day)\n\n")

                # Sort by account and date
                sorted_assertions = sorted(year_assertions, key=lambda a: (a[1], a[2]))

                current_account = None
                for _, account, assertion_date, amount, original_date, sort_order in sorted_assertions:
                    if account != current_account:
                        f.write(f"\n; {account}\n")
                        current_account = account

                    f.write(f"; End-of-day balance for {original_date}\n")
                    f.write(f"{assertion_date} balance {account}  {amount:.2f} GBP\n")

            logger.info(f"Wrote {len(year_assertions)} balance assertions to {filepath}")

    def _generate_importers_file(self, file_keys: List[Tuple[int, str]]):
        """Generate the importers include file."""
        main_path = self.output_dir / "importers.beancount"

        # Group files by year
        by_year = defaultdict(set)
        for year, source in file_keys:
            by_year[year].add(source)

        with open(main_path, 'w', encoding='utf-8') as f:
            f.write("; Main importers include file\n")
            f.write(f"; Generated: {datetime.now().isoformat()}\n\n")

            # Include files by year
            for year in sorted(by_year.keys()):
                f.write(f"; {year} imports\n")

                # Include balance assertions
                balance_file = f"{year}/balance_assertions_{year}.beancount"
                if (self.output_dir / balance_file).exists():
                    f.write(f'include "{balance_file}"\n')

                # Include transaction files
                for source in sorted(by_year[year]):
                    txn_file = f"{year}/machine_generated/{source}_{year}.beancount"
                    f.write(f'include "{txn_file}"\n')

                f.write("\n")

        logger.info(f"Generated importers include file: {main_path}")

    def _get_transaction_files(self, file_keys: List[Tuple[int, str]]) -> List[str]:
        """Get list of transaction file paths for main.beancount."""
        files = []
        for year, source in file_keys:
            txn_file = f"{year}/machine_generated/{source}_{year}.beancount"
            files.append(txn_file)
        return files

    def _get_balance_files(self) -> List[str]:
        """Get list of balance assertion file paths."""
        balance_files = []
        # Find all bank balance assertion files
        for path in self.output_dir.glob("**/bank_balance_assertions_*.beancount"):
            # Get relative path from output_dir
            relative = path.relative_to(self.output_dir)
            balance_files.append(str(relative))
        # Find all manual balance assertion files
        for path in self.output_dir.glob("**/manual_balance_assertions_*.beancount"):
            # Get relative path from output_dir
            relative = path.relative_to(self.output_dir)
            balance_files.append(str(relative))
        return sorted(balance_files)

    def write_stock_splits(self, processed_actions: List) -> List[ProcessingWarning]:
        """
        Write stock split transactions from processed corporate actions.

        Args:
            processed_actions: List of CorporateActionResult objects

        Returns:
            List of warnings encountered during writing
        """
        from cassoulet.stages.corporate_action import CorporateActionResult
        warnings = []

        for result in processed_actions:
            try:
                # Create beancount transaction for this stock split
                split = result.action
                year = split.date.year

                # Create postings from lot transformations
                postings = []
                for old_lot, new_lot in result.lots_transformed:
                    # Remove old shares at original cost
                    postings.append(data.Posting(
                        account=split.account,
                        units=Amount(-old_lot['quantity'], split.commodity),
                        cost=data.CostSpec(
                            number_per=old_lot['cost_per_unit'],
                            number_total=None,
                            currency='GBP',  # TODO: Get from account metadata
                            date=old_lot['date'],
                            label=None,
                            merge=False
                        ),
                        price=None,
                        flag=None,
                        meta=None
                    ))

                    # Add new shares at adjusted cost (with preserved acquisition date)
                    postings.append(data.Posting(
                        account=split.account,
                        units=Amount(new_lot['quantity'], split.commodity),
                        cost=data.CostSpec(
                            number_per=new_lot['cost_per_unit'],
                            number_total=None,
                            currency='GBP',  # TODO: Get from account metadata
                            date=new_lot['date'],  # Preserved acquisition date
                            label=None,
                            merge=False
                        ),
                        price=None,
                        flag=None,
                        meta=None
                    ))

                # Create transaction
                meta = {
                    'corporate-action': 'stock-split',
                    'split-ratio': f"{split.ratio}:1",
                    'input-format': split.input_format or 'unknown'
                }
                if split.line_number:
                    meta['lineno'] = split.line_number
                if split.source_file:
                    meta['filename'] = split.source_file

                txn = data.Transaction(
                    meta=meta,
                    date=split.date,
                    flag='*',
                    payee=split.payee,
                    narration=split.narration or f"{split.commodity} stock split ({split.ratio}:1)",
                    tags=set(),
                    links=set(),
                    postings=postings
                )

                # Determine source for file placement
                # Stock splits go to stock_splits source
                source = 'stock_splits'

                # Add to the file queue - (txn, is_commented=False)
                file_key = (year, source)
                self.by_file[file_key].append((txn, False))
                self.stats['transactions_created'] += 1
                self.created_transactions.append(txn)  # Store for error reporting

                logger.info(f"  Created stock split transaction for {split.commodity} on {split.date}")

            except Exception as e:
                logger.error(f"Failed to write stock split for {result.action}: {e}")
                warnings.append(ProcessingWarning(
                    processor_name=self.processor_name,
                    severity='ERROR',
                    message=f"Failed to write stock split: {e}",
                    source_transaction=f"split_{result.action.commodity}_{result.action.date}",
                    details={
                        'error_type': type(e).__name__,
                        'error_message': str(e),
                        'split_date': str(result.action.date),
                        'commodity': result.action.commodity
                    }
                ))

        # Write the stock split transactions to files
        if processed_actions:
            self._write_output_files(self.by_file)

            # Regenerate importers file with stock split files included
            self._generate_importers_file(self.by_file.keys())

            # Regenerate main.beancount with stock split files
            transaction_files = self._get_transaction_files(self.by_file.keys())
            balance_files = self._get_balance_files()
            self.main_generator.generate_main_file(
                transaction_files,
                balance_files,
                self.output_dir / "main.beancount"
            )

        return warnings