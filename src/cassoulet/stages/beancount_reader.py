"""
Beancount Reader

Reads transactions from Beancount files and creates Envelopes.
Manual transactions are SOVEREIGN - they are explicit user assertions that
take precedence over all CSV data.

Handles multi-commodity transactions by creating separate envelopes for
each commodity movement.

TODO: Future Enhancement - Transaction Splitting and Recombination
Currently, complex multi-posting transactions (like SIPP contributions with
automatic investment) are split into separate envelopes for pipeline processing.
A better approach would be:
1. Detect splittable transactions at ingestion (e.g., contribution + investment)
2. Split them into simple envelopes with transaction-group metadata
3. Process through pipeline normally (enables proper lot tracking)
4. Recombine at output stage using transaction-group metadata
This would preserve the original transaction structure while allowing proper
processing through our inbound/outbound envelope pattern.
"""

import logging
from pathlib import Path
from typing import List, Dict, Any, Optional

from beancount.core.data import Transaction
from cassoulet.utils.beancount_parser import safe_parse_file, extract_posting_data
from cassoulet.utils.dates import ensure_date
from cassoulet.stages.envelope import Envelope
from cassoulet.stages.corporate_action import StockSplit, CorporateActionResult
from cassoulet.base.exceptions import GroupPostingError, UnsupportedBeancountInput
from cassoulet.utils.currencies import is_currency
from cassoulet.utils.transaction_utilities import (
    PostingClassification,  # Data structure for classification results
    get_transaction_commodities,
    transaction_is_cash_only,
    separate_commodity_and_cash_postings,
    classify_commodity_transaction,
    classify_cash_only_transaction,
)

logger = logging.getLogger(__name__)


def classify_transaction_postings(transaction: Transaction) -> PostingClassification:
    """
    Classify transaction postings into main flow and additional postings.

    This is the main entry point for determining how to populate an envelope
    from a transaction with potentially 3+ postings.

    Args:
        transaction: Beancount transaction to classify

    Returns:
        PostingClassification with main flow and additional postings
    """
    postings = transaction.postings

    # Separate by type
    commodity_postings, cash_postings = separate_commodity_and_cash_postings(postings)

    # Count unique commodities
    num_commodities = len(get_transaction_commodities(transaction))

    # Check for unsupported patterns
    if num_commodities > 1:
        return PostingClassification(
            main_outbound=None,
            main_inbound=None,
            additional=[],
            classification_type='unsupported',
            error_message=f"Multiple commodities not supported: {get_transaction_commodities(transaction)}"
        )

    # Single commodity transaction (BUY/SELL/TRANSFER)
    if num_commodities == 1:
        return classify_commodity_transaction(commodity_postings, cash_postings, postings)

    # Cash-only transaction
    elif transaction_is_cash_only(transaction):
        return classify_cash_only_transaction(postings)

    # Shouldn't reach here but be defensive
    return PostingClassification(
        main_outbound=None,
        main_inbound=None,
        additional=[],
        classification_type='unsupported',
        error_message="Unable to classify transaction pattern"
    )



class BeancountReader:
    """
    Reads Beancount files and creates Envelopes.
    """
    
    def __init__(self):
        self.stats = {
            'files_processed': 0,
            'transactions_loaded': 0,
            'corporate_actions_detected': 0,
            'errors_encountered': 0
        }
    
    def load_transactions(self, path_or_dir: str = "entries/manual"):
        """
        Load manual transactions and corporate actions from beancount files.

        Args:
            path_or_dir: Path to a single .beancount file or directory

        Returns:
            Tuple of (envelopes, corporate_actions)
        """
        envelopes = []
        corporate_actions = []
        manual_files = self._find_manual_files(path_or_dir)

        if not manual_files:
            return envelopes, corporate_actions

        logger.info(f"Loading manual transactions from {len(manual_files)} files")

        for file_path in manual_files:
            file_envelopes, file_actions = self._process_file(file_path)
            envelopes.extend(file_envelopes)
            corporate_actions.extend(file_actions)
            self.stats['files_processed'] += 1

        logger.info(
            f"Loaded {len(envelopes)} manual transactions and "
            f"{len(corporate_actions)} corporate actions from {self.stats['files_processed']} files"
        )
        return envelopes, corporate_actions
    
    def _find_manual_files(self, path_or_dir: str) -> List[Path]:
        """Find all manual transaction files to process."""
        path = Path(path_or_dir)
        
        if not path.exists():
            logger.warning(f"Manual transactions path not found: {path_or_dir}")
            return []
        
        if path.is_file():
            if path.suffix == '.disabled':
                logger.debug(f"Skipping disabled file: {path.name}")
                return []
            return [path]
        else:
            # Directory - find all .beancount files
            files = [f for f in path.glob("*.beancount") 
                    if f.name != "manual_includes.beancount" 
                    and not f.name.endswith('.disabled')]
            return sorted(files)  # Sort for consistent processing order
    
    def _process_file(self, file_path: Path):
        """Process a single manual transaction file.

        Returns:
            Tuple of (envelopes, corporate_actions)
        """
        logger.info(f"Processing {file_path.name}")
        envelopes = []
        corporate_actions = []

        try:
            # For corporate action detection, we need RAW parser data (before booking)
            # because beancount's loader auto-fills empty costs {} from inventory
            from beancount.parser import parser
            raw_entries, raw_errors, _ = parser.parse_file(str(file_path))

            # For envelope creation, use safe_parse_file (with booking)
            entries, errors, _ = safe_parse_file(
                str(file_path),
                preserve_raw_postings=True
            )

            if errors:
                logger.debug(f"Found {len(errors)} validation errors in {file_path.name} (expected)")
                self.stats['errors_encountered'] += len(errors)

            # Process only transactions from RAW parser for corporate action detection
            raw_transactions = [e for e in raw_entries if isinstance(e, Transaction)]

            # Process only transactions from loader for envelope creation
            transactions = [e for e in entries if isinstance(e, Transaction)]
            logger.info(f"Found {len(transactions)} transactions in {file_path.name}")

            # Build a map of raw transactions by (date, narration) for lookup
            raw_txn_map = {}
            for raw_txn in raw_transactions:
                key = (raw_txn.date, raw_txn.narration)
                raw_txn_map[key] = raw_txn

            for txn in transactions:
                # Check for corporate actions FIRST (before envelope classification)
                is_corporate_action = False

                # Format A: Intent-based directive (metadata + zero postings)
                if self._is_corporate_action_directive(txn):
                    action = self._parse_corporate_action_directive(txn, file_path)
                    if action:
                        corporate_actions.append(action)
                        self.stats['corporate_actions_detected'] += 1
                        is_corporate_action = True

                # Format B: Simplified postings (structure without cost basis)
                # Use RAW transaction for detection (before cost auto-filling)
                if not is_corporate_action:
                    key = (txn.date, txn.narration)
                    raw_txn = raw_txn_map.get(key)
                    if raw_txn and self._is_stock_split_simplified(raw_txn):
                        action = StockSplit.from_postings(raw_txn, str(file_path))
                        corporate_actions.append(action)
                        self.stats['corporate_actions_detected'] += 1
                        logger.info(f"Detected Format B stock split: {action}")
                        is_corporate_action = True

                # Only try to create envelope if it's not a corporate action
                if not is_corporate_action:
                    txn_envelopes = self._create_envelope_from_transaction(txn, file_path)
                    if txn_envelopes:
                        envelopes.extend(txn_envelopes)
                        self.stats['transactions_loaded'] += len(txn_envelopes)

        except Exception as e:
            logger.error(f"Failed to process {file_path}: {e}")
            self.stats['errors_encountered'] += 1

        return envelopes, corporate_actions
    
    def _create_envelope_from_transaction(self, txn: Transaction, file_path: Path) -> List[Envelope]:
        """
        Create an Envelope from a beancount Transaction using classification.

        Now returns a single envelope since multi-commodity transactions are not supported.
        """
        try:
            # Use the classification system
            classification = classify_transaction_postings(txn)

            # Handle unsupported patterns
            if classification.classification_type == 'unsupported':
                logger.error(f"Unsupported transaction on {txn.date}: {classification.error_message}")
                raise UnsupportedBeancountInput(
                    date=str(txn.date),
                    narration=txn.narration or "No narration",
                    reason=classification.error_message or "Unknown classification failure",
                    file_path=str(file_path),
                    line_number=txn.meta.get('lineno') if txn.meta else None
                )

            # Build metadata
            meta = self._build_metadata(txn, file_path, None)

            # Create envelope from classification
            envelope = self._create_envelope_from_classification(
                txn, classification, meta
            )

            # Mark as from beancount
            envelope.metadata['from_beancount'] = True
            envelope.metadata['beancount_file'] = str(file_path.name)

            # CRITICAL STEEL THREAD CHECK: Verify all postings are represented
            total_postings = len(txn.postings)
            represented_postings = 0

            # Count main flow postings
            if classification.main_inbound:
                represented_postings += 1
            if classification.main_outbound:
                represented_postings += 1

            # Count additional postings
            represented_postings += len(classification.additional)

            # Check for data loss
            if represented_postings < total_postings:
                logger.error(
                    f"STEEL THREAD VIOLATION: Lost {total_postings - represented_postings} postings! "
                    f"Transaction has {total_postings} postings, envelope represents {represented_postings}"
                )
                raise UnsupportedBeancountInput(
                    date=str(txn.date),
                    narration=txn.narration or "No narration",
                    reason=f"Cannot fully represent all {total_postings} postings (only {represented_postings} captured). "
                           f"This is a critical data loss condition.",
                    file_path=str(file_path),
                    line_number=txn.meta.get('lineno') if txn.meta else None
                )

            return [envelope]  # Still return list for compatibility

        except Exception as e:
            logger.error(f"Failed to create envelope for transaction on {txn.date}: {e}")
            return []  # Return empty list on error
    
    def _build_metadata(self, 
                        txn: Transaction, 
                        file_path: Path,
                        investment_data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Build comprehensive metadata for the envelope."""
        meta = dict(txn.meta) if txn.meta else {}
        
        # Add file information
        meta['manual_file'] = str(file_path)
        meta['is_manual'] = True
        # Line number is already in meta from beancount parser
        # Just ensure it's available as source_line for consistency
        meta['source_line'] = meta.get('lineno')
        
        # Add investment data if present
        if investment_data:
            meta.update(investment_data)
        
        # Extract posting data for later recreation
        posting_data = extract_posting_data(txn)
        if posting_data:
            meta['manual_postings'] = posting_data
        
        # Preserve any manual IDs
        if 'manual-id' in meta:
            meta['manual_id'] = meta['manual-id']
        elif 'manual_id' not in meta:
            # Generate a stable ID
            meta['manual_id'] = f"manual_{txn.date}_{file_path.stem}_{hash(str(txn))}"
        
        return meta
    
    
    def _create_envelope_from_classification(self, txn, classification, meta):
        """Create an envelope from posting classification.

        This implementation uses the classification system to properly
        handle 3+ posting transactions with additional_postings.
        """

        # Extract source filename without path or extension
        from pathlib import Path
        source_file = meta.get('manual_file', 'unknown')
        source_name = Path(source_file).stem if source_file else 'unknown'

        # Base envelope parameters
        envelope_params = {
            'date': txn.date,  # Note: changed from date_val to match Envelope field
            'narration': txn.narration,
            'source': source_name,  # e.g., "manual_txn", "txn_for_matching"
            'source_type': 'beancount',  # Explicit: from beancount file
            'payee': txn.payee,
            'flag': txn.flag,
            'links': txn.links,
            'beancount_tags': txn.tags,
            'metadata': meta,
            # Critical for preventing duplicate IDs
            'source_file_path': str(source_file),
            'source_line_number': meta.get('source_line')
        }

        # Determine transaction type based on classification
        if classification.classification_type == 'commodity':
            # Determine if it's BUY or SELL based on commodity direction
            if classification.main_inbound and not is_currency(classification.main_inbound.units.currency):
                envelope_params['transaction_type'] = 'BUY'
            elif classification.main_outbound and not is_currency(classification.main_outbound.units.currency):
                envelope_params['transaction_type'] = 'SELL'
        elif classification.classification_type == 'in-specie':
            envelope_params['transaction_type'] = 'COMMODITY_TRANSFER'

        # Set main flow from classification
        if classification.main_outbound:
            envelope_params['outbound_units'] = abs(classification.main_outbound.units.number)
            envelope_params['outbound_type'] = classification.main_outbound.units.currency
            envelope_params['outbound_account'] = classification.main_outbound.account

            # Collect consumed lots for SELL transactions
            if (classification.classification_type == 'commodity' and
                classification.main_outbound.cost and
                hasattr(classification.main_outbound.cost, 'number')):
                envelope_params['consumed_lots'] = [{
                    'quantity': abs(classification.main_outbound.units.number),
                    'cost_per_unit': classification.main_outbound.cost.number,
                    'acquisition_date': ensure_date(classification.main_outbound.cost.date)
                        if hasattr(classification.main_outbound.cost, 'date') and classification.main_outbound.cost.date else None
                }]

        if classification.main_inbound:
            envelope_params['inbound_units'] = abs(classification.main_inbound.units.number)
            envelope_params['inbound_type'] = classification.main_inbound.units.currency
            envelope_params['inbound_account'] = classification.main_inbound.account

        # Convert additional postings to the expected format
        if classification.additional:
            envelope_params['additional_postings'] = [
                {
                    'units': posting.units.number,  # Keep sign for proper reconstruction
                    'type': posting.units.currency,
                    'account': posting.account
                }
                for posting in classification.additional
            ]

        # Create envelope using constructor directly (Envelope.create doesn't exist)
        envelope = Envelope(**envelope_params)

        # Generate a deterministic ID for the manual transaction
        # Use manual_id if available, otherwise generate from transaction data
        from cassoulet.utils.deterministic_id import generate_id

        # Create hash data from key transaction fields
        hash_data = {
            'date': str(txn.date),
            'narration': txn.narration or '',
            'payee': txn.payee or '',
        }

        # Add amounts to make it unique
        if classification.main_outbound:
            hash_data['outbound'] = f"{classification.main_outbound.units.number}_{classification.main_outbound.units.currency}"
        if classification.main_inbound:
            hash_data['inbound'] = f"{classification.main_inbound.units.number}_{classification.main_inbound.units.currency}"

        envelope.envelope_id = generate_id(
            prefix="manual",
            data=hash_data,
            line_number=meta.get('source_line'),
            source_file=meta.get('manual_file')
        )

        return envelope  # Return single envelope, not list

    def _is_corporate_action_directive(self, txn: Transaction) -> bool:
        """
        Detect Format A: Intent-based directive.

        Has corporate-action metadata and NO postings.
        """
        meta = txn.meta or {}
        return 'corporate-action' in meta and len(txn.postings) == 0

    def _parse_corporate_action_directive(self, txn: Transaction, file_path: Path):
        """Parse Format A: Intent-based directive from metadata."""
        meta = txn.meta or {}
        action_type = meta.get('corporate-action')

        if action_type == 'stock-split':
            action = StockSplit.from_metadata(txn, str(file_path))
            logger.info(f"Detected Format A stock split: {action}")
            return action
        else:
            logger.warning(f"Unknown corporate action type: {action_type}")
            return None

    def _is_stock_split_simplified(self, txn: Transaction) -> bool:
        """
        Detect Format B: Simplified stock split postings.

        Characteristics:
        - Same commodity (not currency) in all postings
        - Same account in all postings
        - No cash postings
        - Postings have no cost basis OR empty cost {}
        - Valid split ratio
        """
        from decimal import Decimal
        from beancount.core.number import MISSING

        if len(txn.postings) < 2:
            return False

        # Check if explicitly marked (can be inferred but explicit is fine)
        meta = txn.meta or {}
        if meta.get('corporate-action') == 'stock-split':
            # If marked, just validate basic structure
            logger.debug(f"Transaction {txn.date} explicitly marked as stock-split")

        # Get all commodities and accounts
        commodities = set()
        accounts = set()
        has_positive = False
        has_negative = False
        has_cost_basis = False

        for posting in txn.postings:
            currency = posting.units.currency

            # Must not be a currency (cash)
            if is_currency(currency):
                logger.debug(f"  Posting has currency {currency}, not a stock split")
                return False

            commodities.add(currency)
            accounts.add(posting.account)

            # Check for cost basis - Format B should NOT have detailed costs
            # Empty {} creates CostSpec with number_per=MISSING (not None)
            if posting.cost:
                if hasattr(posting.cost, 'number_per'):
                    # CostSpec - check if it has an actual number (not MISSING)
                    # MISSING is a class, so use 'is' not '=='
                    if posting.cost.number_per is not MISSING and posting.cost.number_per is not None:
                        has_cost_basis = True
                        logger.debug(f"  Posting has cost basis: {posting.cost.number_per}")
                elif hasattr(posting.cost, 'number'):
                    # Cost - check if it has an actual number
                    if posting.cost.number is not None:
                        has_cost_basis = True
                        logger.debug(f"  Posting has cost number: {posting.cost.number}")

            if posting.units.number > 0:
                has_positive = True
            elif posting.units.number < 0:
                has_negative = True

        # Format B: no cost basis details (empty {} or no cost at all)
        if has_cost_basis:
            logger.debug(f"  Transaction has cost basis, not Format B")
            return False  # Has detailed costs - not Format B

        # Must have exactly one commodity and one account
        if len(commodities) != 1 or len(accounts) != 1:
            logger.debug(f"  Multiple commodities ({commodities}) or accounts ({accounts})")
            return False

        # Must have both positive and negative postings
        if not (has_positive and has_negative):
            logger.debug(f"  Missing positive or negative postings")
            return False

        # Check for valid split ratio
        total_positive = sum(p.units.number for p in txn.postings if p.units.number > 0)
        total_negative = sum(abs(p.units.number) for p in txn.postings if p.units.number < 0)

        if total_negative == 0:
            return False

        ratio = total_positive / total_negative

        # Check if it's a common split ratio
        is_valid = self._is_valid_split_ratio(ratio)
        if is_valid:
            logger.debug(f"  Detected Format B stock split: ratio={ratio}")
        else:
            logger.debug(f"  Ratio {ratio} not a valid split ratio")

        return is_valid

    def _is_valid_split_ratio(self, ratio) -> bool:
        """Check if ratio matches common split ratios."""
        from decimal import Decimal

        common_ratios = [2, 3, 5, 10, 20, 100]
        reverse_ratios = [Decimal('0.5'), Decimal('0.2'), Decimal('0.1')]

        # Allow small tolerance for floating point
        tolerance = Decimal('0.01')

        for common in common_ratios:
            if abs(ratio - Decimal(common)) < tolerance:
                return True

        for reverse in reverse_ratios:
            if abs(ratio - reverse) < tolerance:
                return True

        return False
