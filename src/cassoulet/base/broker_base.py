"""Broker Importer Base Class.

Base class for all broker importers (both single and multi-file).
"""

import logging
from typing import List, Optional

from cassoulet.base.importer_base import Importer
from cassoulet.utils.csv_row_data import CSVRowData
from cassoulet.stages import Envelope


class BrokerImporter(Importer):
    """Enhanced base class for all broker importers.
    
    Includes:
    - Investment transaction handling
    - Cost basis tracking
    - Common broker patterns
    """
    
    def __init__(self, account: str, **kwargs):
        """Initialize broker importer."""
        super().__init__(account=account, **kwargs)

        # Investment configuration
        self.investment_config = self.config.get('investment_config', {})
        self.derive_unit_cost = self.investment_config.get('derive_unit_cost', True)

        # Sign convention for commodity transactions
        # 'standard': both amount and quantity correctly signed
        # 'all_positive': both always positive (e.g., AJ Bell)
        # 'amount_positive': amount always positive, quantity signed (e.g., Vanguard)
        self.commodity_sign_convention = self.investment_config.get('sign_convention', 'standard')
        if self.commodity_sign_convention != 'standard':
            self.logger.info(f"BrokerImporter {self.account}: Using {self.commodity_sign_convention} sign convention")

    def fix_investment_signs(self, rows: List[CSVRowData]) -> List[CSVRowData]:
        """Normalize investment transaction signs to standard convention.

        This is the single place where config-driven sign normalization happens.

        Process:
        1. Normalize transaction type strings (CSV → canonical types, including Unknown → Transfer)
        2. Apply broker's sign convention to normalize all signs
        3. Validate final signs are correct

        Different brokers have different sign conventions:
        - 'standard': Both amount and quantity correctly signed, no fixing needed
        - 'all_positive': Both always positive (e.g., AJ Bell) - fix based on transaction type
        - 'amount_positive': Amount always positive, quantity already signed (e.g., Vanguard)

        After this method, all transactions follow standard conventions:
        - BUY: negative amount (cash out), positive quantity (shares in)
        - SELL: positive amount (cash in), negative quantity (shares out)
        - TRANSFER IN: positive quantity, amount ≈ 0
        - TRANSFER OUT: negative quantity, amount ≈ 0

        Args:
            rows: List of CSVRowData to process

        Returns:
            Same list with signs normalized and validated
        """
        self.logger.debug(f"fix_investment_signs called with convention={self.commodity_sign_convention}, rows={len(rows)}")

        # Step 1: Normalize transaction type strings from CSV to canonical types
        self._normalize_transaction_types(rows)

        # Step 2: Apply sign convention normalization
        if self.commodity_sign_convention == 'standard':
            # Signs are already correct, skip to validation
            self.logger.debug(f"Standard convention, no fixing needed")
        else:
            # Apply broker-specific sign fixing
            self._apply_sign_convention(rows)

        # Step 3: Validate final signs (single pass)
        self._validate_signs(rows)

        return rows

    def _apply_sign_convention(self, rows: List[CSVRowData]) -> None:
        """Apply broker-specific sign convention to normalize signs.

        Args:
            rows: List of CSVRowData to fix in-place
        """
        for row in rows:
            # Trust the CSV reader to have populated the type field
            if not row.type:
                continue

            txn_type = row.type.lower()

            self.logger.debug(f"Processing row type={txn_type}, amount={row.amount}, quantity={row.quantity}")

            # Skip ambiguous transfers - we don't know direction, so preserve signs as-is
            if txn_type == 'transfer':
                self.logger.debug(f"Skipping sign fixing for ambiguous transfer (preserving raw signs)")
                continue

            # Handle explicit transfer types (validate but don't fix cash amounts)
            # Transfer In: positive quantity (receiving), Transfer Out: negative quantity (sending)
            if txn_type in ('transfer in', 'transfer out'):
                self.logger.debug(f"Processing transfer type: {txn_type}, quantity={row.quantity}")
                # Transfers typically have no cash amount or amount=0
                # The quantity sign indicates direction
                if txn_type == 'transfer in' and row.quantity and row.quantity < 0:
                    self.logger.warning(
                        f"Transfer In has negative quantity (should be positive): "
                        f"date={row.date}, quantity={row.quantity}, commodity={row.commodity}"
                    )
                elif txn_type == 'transfer out' and row.quantity and row.quantity > 0:
                    # Fix the sign for Transfer Out
                    old_quantity = row.quantity
                    row.quantity = -abs(row.quantity)
                    self.logger.info(
                        f"Fixed Transfer Out quantity to negative: {row.narrative} {old_quantity} -> {row.quantity}"
                    )
                continue

            if txn_type in ('purchase', 'buy'):
                # Purchases: Cash should be negative (outflow), quantity should be positive

                # Always fix amount for BUY (both 'all_positive' and 'amount_positive')
                if row.amount and row.amount > 0:
                    old_amount = row.amount
                    row.amount = -abs(row.amount)
                    self.logger.info(
                        f"Fixed BUY cash to negative: {row.narrative} {old_amount} -> {row.amount}"
                    )

                # Only fix quantity if using 'all_positive' convention
                if self.commodity_sign_convention == 'all_positive':
                    if row.quantity and row.quantity < 0:
                        old_quantity = row.quantity
                        row.quantity = abs(row.quantity)
                        self.logger.info(
                            f"Fixed BUY quantity to positive: {row.narrative} {old_quantity} -> {row.quantity}"
                        )
                # For 'amount_positive' (Vanguard), quantity is already correct

            elif txn_type in ('sale', 'sell'):
                # Sales: Cash should be positive (inflow), quantity should be negative

                # Fix amount if needed (both conventions)
                if row.amount and row.amount < 0:
                    old_amount = row.amount
                    row.amount = abs(row.amount)
                    self.logger.info(
                        f"Fixed SELL cash to positive: {row.narrative} {old_amount} -> {row.amount}"
                    )

                # Only fix quantity if using 'all_positive' convention
                if self.commodity_sign_convention == 'all_positive':
                    if row.quantity and row.quantity > 0:
                        old_quantity = row.quantity
                        row.quantity = -abs(row.quantity)
                        self.logger.info(
                            f"Fixed SELL quantity to negative: {row.narrative} {old_quantity} -> {row.quantity}"
                        )
                # For 'amount_positive' (Vanguard), quantity is already correct (negative)

    def _normalize_transaction_types(self, rows: List[CSVRowData]) -> None:
        """Normalize raw transaction type strings from CSV to canonical types.

        Maps various broker-specific strings to our canonical types:
        - BUY: purchase, buy, bought, acquisition
        - SELL: sale, sell, sold, disposal
        - TRANSFER IN: transfer in, in-specie in, received
        - TRANSFER OUT: transfer out, in-specie out, sent
        - UNKNOWN: unknown, unclassified, other

        Warns if we encounter an unmapped type.

        Args:
            rows: List of CSVRowData to normalize (modifies type field in-place)
        """
        # Semantic mappings from CSV strings to canonical types
        type_mappings = {
            # BUY variants
            'buy': 'Buy',
            'bought': 'Buy',
            'purchase': 'Buy',
            'purchased': 'Buy',
            'acquisition': 'Buy',

            # SELL variants
            'sell': 'Sell',
            'sold': 'Sell',
            'sale': 'Sell',
            'disposal': 'Sell',

            # TRANSFER IN variants
            'transfer in': 'Transfer In',
            'in-specie in': 'Transfer In',
            'transfer-in': 'Transfer In',
            'received': 'Transfer In',
            'receipt': 'Transfer In',

            # TRANSFER OUT variants
            'transfer out': 'Transfer Out',
            'in-specie out': 'Transfer Out',
            'transfer-out': 'Transfer Out',
            'sent': 'Transfer Out',
            'send': 'Transfer Out',

            # Unknown/Other - treat as ambiguous transfer
            'unknown': 'Transfer',
            'unclassified': 'Transfer',
            'other': 'Transfer',

            # Dividends/Interest (not sign-normalized, but recognized)
            'dividend': 'Dividend',
            'interest': 'Interest',
            'accumulation distribution': 'Dividend',
        }

        for row in rows:
            if not row.type:
                continue

            original_type = row.type
            normalized_key = row.type.lower().strip()

            if normalized_key in type_mappings:
                row.type = type_mappings[normalized_key]
                if row.type != original_type:
                    self.logger.debug(
                        f"Normalized transaction type: '{original_type}' → '{row.type}'"
                    )
            else:
                # Unknown type - warn and treat as ambiguous transfer
                self.logger.warning(
                    f"Unmapped transaction type '{original_type}' - please add to type_mappings. "
                    f"Treating as 'Transfer'. Date: {row.date}, commodity: {row.commodity}"
                )
                row.type = 'Transfer'

    def _validate_signs(self, rows: List[CSVRowData]) -> None:
        """Validate normalized signs after fixing.

        This is the single validation pass that checks the final state.

        Expected patterns (standard convention):
        - BUY: amount negative (cash out), quantity positive (shares in)
        - SELL: amount positive (cash in), quantity negative (shares out)
        - TRANSFER IN: quantity positive (receiving shares), amount ≈ 0
        - TRANSFER OUT: quantity negative (sending shares), amount ≈ 0
        - TRANSFER (ambiguous): signs preserved as-is, no validation

        Args:
            rows: List of CSVRowData to validate (after normalization)
        """
        for row in rows:
            # Skip rows without type
            if not row.type:
                continue

            txn_type = row.type.lower()

            # Skip ambiguous transfers - we preserved raw signs, can't validate direction
            if txn_type == 'transfer':
                # Just check that cash amount is minimal
                if row.amount and abs(row.amount) > 0.01:
                    self.logger.warning(
                        f"Sign validation: Ambiguous TRANSFER has significant cash (might be BUY/SELL): "
                        f"date={row.date}, amount={row.amount}, quantity={row.quantity}, commodity={row.commodity}"
                    )
                continue

            # For investment transactions, we need both amount and quantity
            if not row.quantity:
                continue

            # Check BUY transactions
            if txn_type in ('purchase', 'buy'):
                if row.amount and row.amount > 0:
                    self.logger.warning(
                        f"Sign validation: BUY transaction has positive amount (should be negative): "
                        f"date={row.date}, amount={row.amount}, commodity={row.commodity}"
                    )
                if row.quantity < 0:
                    self.logger.warning(
                        f"Sign validation: BUY transaction has negative quantity (should be positive): "
                        f"date={row.date}, quantity={row.quantity}, commodity={row.commodity}"
                    )

            # Check SELL transactions
            elif txn_type in ('sale', 'sell'):
                if row.amount and row.amount < 0:
                    self.logger.warning(
                        f"Sign validation: SELL transaction has negative amount (should be positive): "
                        f"date={row.date}, amount={row.amount}, commodity={row.commodity}"
                    )
                if row.quantity > 0:
                    self.logger.warning(
                        f"Sign validation: SELL transaction has positive quantity (should be negative): "
                        f"date={row.date}, quantity={row.quantity}, commodity={row.commodity}"
                    )

            # Check TRANSFER IN
            elif txn_type == 'transfer in':
                if row.quantity < 0:
                    self.logger.warning(
                        f"Sign validation: TRANSFER IN has negative quantity (should be positive): "
                        f"date={row.date}, quantity={row.quantity}, commodity={row.commodity}"
                    )
                if row.amount and abs(row.amount) > 0.01:  # Allow small rounding
                    self.logger.info(
                        f"Sign validation: TRANSFER IN has non-zero amount (unusual): "
                        f"date={row.date}, amount={row.amount}, commodity={row.commodity}"
                    )

            # Check TRANSFER OUT
            elif txn_type == 'transfer out':
                if row.quantity > 0:
                    self.logger.warning(
                        f"Sign validation: TRANSFER OUT has positive quantity (should be negative): "
                        f"date={row.date}, quantity={row.quantity}, commodity={row.commodity}"
                    )
                if row.amount and abs(row.amount) > 0.01:  # Allow small rounding
                    self.logger.info(
                        f"Sign validation: TRANSFER OUT has non-zero amount (unusual): "
                        f"date={row.date}, amount={row.amount}, commodity={row.commodity}"
                    )

            # Log validated transactions at debug level for confirmation
            else:
                self.logger.debug(
                    f"Sign validation: type={txn_type}, amount={row.amount}, "
                    f"quantity={row.quantity}, commodity={row.commodity}"
                )

    def resolve_commodities(self, rows: List[CSVRowData]) -> List[CSVRowData]:
        """Resolve commodity symbols for investment transactions.

        This shared method looks up commodity symbols from descriptions
        using the CommodityRegistry.

        Args:
            rows: List of CSVRowData to process

        Returns:
            Same list with commodities resolved in-place
        """
        from cassoulet.base.exceptions import CommodityLookupError

        for row in rows:
            # If we have investment data but no resolved commodity, try to look it up
            if row.quantity and not row.commodity and row.commodity_raw:
                # CSV reader should have set commodity_raw if it's an investment transaction
                # with commodity info in the narrative/description column
                try:
                    resolved_commodity = self._lookup_commodity(row.commodity_raw)
                    if resolved_commodity:
                        row.commodity = resolved_commodity
                        self.logger.debug(
                            f"Resolved commodity '{row.commodity_raw}' -> '{resolved_commodity}'"
                        )
                except CommodityLookupError as e:
                    # Log the error but continue processing
                    # The transaction will be created with UNKNOWN commodity
                    self.logger.error(f"Commodity lookup error: {e}")
                    row.commodity = "UNKNOWN"

        return rows

    def process_rows_to_envelopes(
        self,
        rows: List[CSVRowData]
    ) -> List[Envelope]:
        """Process CSVRowData rows into transaction envelopes.

        Common pattern for converting rows to envelopes:
        - Consolidate debit/credit columns into amount (if using debit_credit convention)
        - Fix investment signs based on commodity_sign_convention
        - Resolve commodities for investment transactions
        - Convert to envelopes using EnvelopeBuilder

        Args:
            rows: List of CSVRowData objects

        Returns:
            List of transaction envelopes
        """
        from cassoulet.utils.envelope_builder import EnvelopeBuilder

        # Pre-processing: Consolidate debit/credit into amount if needed
        # This allows the rest of the pipeline to work with a unified 'amount' field
        sign_convention = self.config.get('sign_convention', 'standard')
        if sign_convention == 'debit_credit':
            self.logger.debug("Consolidating debit/credit columns into amount field")
            for row in rows:
                if row.amount is None:  # Only consolidate if amount isn't already set
                    if row.debit is not None:
                        row.amount = -abs(row.debit)  # Debit = money out (negative)
                    elif row.credit is not None:
                        row.amount = abs(row.credit)   # Credit = money in (positive)

        # First pass: Fix investment signs based on config
        # (only if commodity_sign_convention is 'all_positive')
        rows = self.fix_investment_signs(rows)

        # Second pass: Resolve commodities using shared method
        rows = self.resolve_commodities(rows)

        envelopes = []
        for row in rows:
            # Check if there's a special source identifier (from MultiFileBrokerImporter)
            source_file = None
            if row.metadata and '_source_identifier' in row.metadata:
                source_file = row.metadata['_source_identifier']

            # Create envelope from CSV row
            # Signs have already been normalized, so use 'standard'
            try:
                envelope = EnvelopeBuilder.from_csv_row_data(
                    row,
                    account=self.account,
                    institution=self.institution,
                    sign_convention='standard',  # Already normalized
                    source_file=source_file
                )

                if envelope:
                    envelopes.append(envelope)
            except ValueError as e:
                # STEEL THREAD: Log error but don't crash - allow processing to continue
                self.logger.warning(f"Skipping row {row.row_number}: {e}")
                continue

        return envelopes
    
    def _lookup_commodity(self, description: str) -> Optional[str]:
        """
        Look up commodity symbol from description using CommodityRegistry.
        
        This is broker-specific logic - banks don't need commodity lookup.
        
        Args:
            description: Raw commodity description from CSV
            
        Returns:
            Resolved commodity symbol or None if not found
            
        Raises:
            CommodityLookupError: If commodity registry is missing or misconfigured
        """
        from cassoulet.base.exceptions import CommodityLookupError
        
        try:
            from cassoulet.utils.commodity_registry import get_commodity_registry
        except ImportError as e:
            raise CommodityLookupError(
                description, 
                reason="CommodityRegistry module not found - check commodities/ directory"
            ) from e
        
        try:
            registry = get_commodity_registry()
        except Exception as e:
            raise CommodityLookupError(
                description,
                reason=f"Failed to initialize CommodityRegistry: {e}"
            ) from e
        
        try:
            result = registry.lookup_commodity(description)
            
            if result and result != 'UNKNOWN':
                self.logger.debug(f"Commodity lookup: '{description}' -> '{result}'")
                return result
            else:
                self.logger.debug(f"Commodity lookup failed for: '{description}'")
                return None
        except AttributeError as e:
            raise CommodityLookupError(
                description,
                reason=f"CommodityRegistry missing lookup_commodity method: {e}"
            ) from e
        except Exception as e:
            raise CommodityLookupError(
                description,
                reason=f"Unexpected error during lookup: {e}"
            ) from e