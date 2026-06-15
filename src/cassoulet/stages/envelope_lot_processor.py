"""
Envelope-Native Lot Processor

A minimalist lot tracking processor that trusts envelope data completely.
Uses envelope utilities and focuses on doing ONE thing well.
"""

import logging
from decimal import Decimal
from typing import Dict, List, Tuple, Any

from cassoulet.stages.envelope_processor import EnvelopeProcessor
from cassoulet.stages.envelope import Envelope
from cassoulet.stages.corporate_action import StockSplit, CorporateActionResult
from cassoulet.base.exceptions import MissingCostBasisError, LotTrackingWarning
from cassoulet.utils.envelope_utilities import (
    enhance_envelope,
    get_broker_commission,
    get_commodity,
    get_commodity_units,
    get_currency,
)
from cassoulet.utils.deterministic_id import generate_id

logger = logging.getLogger(__name__)


class EnvelopeLotProcessor(EnvelopeProcessor):
    """
    Minimalist envelope-native lot tracking.

    Trusts envelope classification completely:
    - BUY transactions create lots
    - SELL transactions consume lots (FIFO)
    - COMMODITY_TRANSFER transactions move lots between accounts

    No validation, no debug code, just simple lot tracking.
    """

    def __init__(self, corporate_actions: List[StockSplit] = None):
        """Initialize with empty lot registry and optional corporate actions.

        Args:
            corporate_actions: List of stock splits to process chronologically
        """
        super().__init__(processor_name="EnvelopeLotProcessor")

        # Simple registry: (account, commodity) -> list of lots
        # Each lot is a dict with: date, quantity, cost_per_unit, envelope_id
        self.lots: Dict[Tuple[str, str], List[dict]] = {}

        # Track lot movement history for debugging
        self.lot_movements: List[Dict[str, Any]] = []

        # Corporate actions to process chronologically
        self.corporate_actions = sorted(corporate_actions or [], key=lambda a: a.date)

        # Track processed actions with their results
        self.processed_actions: List[CorporateActionResult] = []

    def _process_internal(
        self, envelopes: List[Envelope]
    ) -> Tuple[List[Envelope], List[LotTrackingWarning]]:
        """Process envelopes and corporate actions chronologically."""
        warnings = []
        processed_envelopes = []

        # Sort envelopes by date for chronological processing
        sorted_envelopes = sorted(
            envelopes, key=lambda e: (e.date, e.original_order or 0)
        )

        # Merge envelopes and corporate actions, sorted by date
        envelope_idx = 0
        action_idx = 0

        while envelope_idx < len(sorted_envelopes) or action_idx < len(
            self.corporate_actions
        ):
            # Determine which comes first: envelope or corporate action
            process_envelope = False

            if envelope_idx >= len(sorted_envelopes):
                # No more envelopes, process remaining actions
                process_envelope = False
            elif action_idx >= len(self.corporate_actions):
                # No more actions, process remaining envelopes
                process_envelope = True
            else:
                # Compare dates
                envelope_date = sorted_envelopes[envelope_idx].date
                action_date = self.corporate_actions[action_idx].date

                if envelope_date <= action_date:
                    process_envelope = True
                else:
                    process_envelope = False

            if process_envelope:
                # Process envelope
                envelope = sorted_envelopes[envelope_idx]
                envelope_idx += 1

                try:
                    if envelope.transaction_type == "BUY":
                        envelope = self._handle_buy(envelope)
                    elif envelope.transaction_type == "SELL":
                        envelope = self._handle_sell(envelope, warnings)
                    elif envelope.transaction_type == "COMMODITY_TRANSFER":
                        envelope = self._handle_transfer(envelope, warnings)

                    processed_envelopes.append(envelope)

                except MissingCostBasisError as e:
                    # Log the error but continue processing other envelopes
                    logger.error(f"Cost basis error for {envelope.envelope_id}: {e}")

                    # Determine the lot issue type based on the error
                    lot_issue_type = "missing_cost_basis"
                    if "incomplete_transfer" in str(e):
                        lot_issue_type = "incomplete_transfer"
                    elif "lot shortage" in str(e).lower():
                        lot_issue_type = "lot_shortage"

                    warnings.append(
                        LotTrackingWarning(
                            processor_name=self.processor_name,
                            severity="ERROR",
                            message=f"Cannot track cost basis for investment transaction: {e}",
                            source_transaction=envelope.envelope_id,
                            lot_issue_type=lot_issue_type,
                            affected_commodity=getattr(e, "commodity", None),
                            affected_account=getattr(e, "account", None),
                            details={
                                "envelope_id": envelope.envelope_id,
                                "error_type": "MissingCostBasisError",
                                "error_message": str(e),
                                "impact": "Transaction will be recorded without cost basis - may cause tax calculation errors",
                            },
                        )
                    )
                    # Add the envelope unmodified so we don't lose it
                    processed_envelopes.append(envelope)

                except Exception as e:
                    # Catch any other unexpected errors
                    logger.error(
                        f"Unexpected error processing {envelope.envelope_id}: {e}"
                    )
                    warnings.append(
                        LotTrackingWarning(
                            processor_name=self.processor_name,
                            severity="ERROR",
                            message=f"Failed to process envelope: {e}",
                            source_transaction=envelope.envelope_id,
                            details={
                                "envelope_id": envelope.envelope_id,
                                "error_type": type(e).__name__,
                                "error_message": str(e),
                            },
                        )
                    )
                    # Add the envelope unmodified
                    processed_envelopes.append(envelope)

            else:
                # Process corporate action
                action = self.corporate_actions[action_idx]
                action_idx += 1

                logger.info(f"Processing stock split: {action}")
                result = self._handle_stock_split(action, warnings)
                self.processed_actions.append(result)

        logger.info(
            f"EnvelopeLotProcessor: Processed {len(processed_envelopes)} envelopes, "
            f"tracking {sum(len(lots) for lots in self.lots.values())} active lots"
        )

        return processed_envelopes, warnings

    def get_lot_registry(self) -> Dict[str, Any]:
        """
        Export the current lot registry for debugging and analysis.

        Returns:
            Dictionary containing all lots organized by account and commodity
        """
        registry = {}
        for key, lots in self.lots.items():
            account, commodity = key
            key_str = f"{account}:{commodity}"

            # Convert lots to serializable format
            registry[key_str] = []
            for lot in lots:
                registry[key_str].append(
                    {
                        "lot_id": lot["lot_id"],
                        "date": str(lot["date"]),
                        "quantity": str(lot["quantity"]),
                        "cost_per_unit": str(lot["cost_per_unit"]),
                        "currency": lot.get("currency", "GBP"),
                        "envelope_id": lot.get("envelope_id", "unknown"),
                    }
                )

        return {
            "lot_registry": registry,
            "lot_movements": self.lot_movements,  # Full history of all lot operations
            "stats": {
                "total_accounts": len(set(k[0] for k in self.lots.keys())),
                "total_commodities": len(set(k[1] for k in self.lots.keys())),
                "total_lots": sum(len(lots) for lots in self.lots.values()),
                "total_movements": len(self.lot_movements),
                "movement_types": {
                    "creates": sum(
                        1 for m in self.lot_movements if m["action"] == "CREATE"
                    ),
                    "consumes": sum(
                        1 for m in self.lot_movements if m["action"] == "CONSUME"
                    ),
                    "transfers": sum(
                        1 for m in self.lot_movements if m["action"] == "TRANSFER"
                    ),
                },
            },
        }

    def _handle_buy(self, envelope: Envelope) -> Envelope:
        """
        Create lot from BUY envelope.

        For BUY transactions:
        - Commodity comes IN (inbound_units, inbound_type)
        - Cash goes OUT (outbound_units)
        - Cost basis is calculated from cash/quantity
        """
        if not envelope.inbound_units or not envelope.inbound_type:
            return envelope  # No commodity to track

        # Calculate unit price if not provided
        # For BUY: cost per unit = total cash out / quantity in
        #
        # Commission handling depends on the source:
        # - Manual (beancount) envelopes: commission is already in outbound_units
        # - CSV envelopes: commission may be in a separate column and NOT in
        #   outbound_units — handled below by checking get_broker_commission()
        if envelope.unit_price:
            cost_per_unit = envelope.unit_price
        elif envelope.outbound_units and envelope.inbound_units:
            # Cost basis = total cash outflow / quantity
            cost_per_unit = abs(envelope.outbound_units) / abs(envelope.inbound_units)
        else:
            # Can't calculate, default to 0 (will cause validation errors)
            cost_per_unit = Decimal("0")

        # Include separately-tracked fees/commission in cost basis.
        # For CSV-imported envelopes, commission may be in a separate CSV column
        # and not included in outbound_units. For tax purposes (e.g. HMRC Section
        # 104), dealing charges are part of the acquisition cost.
        # Manual (beancount-sourced) envelopes already include commission in
        # outbound_units, so we skip those to avoid double-counting.
        if envelope.source_type == "csv":
            commission_info = get_broker_commission(envelope)
            if commission_info:
                commission_amount, _ = commission_info
                quantity = abs(envelope.inbound_units)
                total_cost = cost_per_unit * quantity + commission_amount
                cost_per_unit = total_cost / quantity

                # Adjust outbound_units to include commission so the output
                # transaction balances (cost basis total = cash outflow).
                if envelope.outbound_units:
                    adjusted_outbound = abs(envelope.outbound_units) + commission_amount
                else:
                    # cost_per_unit already includes commission; total_cost is
                    # the full acquisition cost we need outbound_units to reflect.
                    adjusted_outbound = total_cost
                envelope = enhance_envelope(
                    envelope,
                    reason="Included separate commission in cost basis",
                    outbound_units=adjusted_outbound,
                    unit_price=cost_per_unit,
                )

                logger.info(
                    f"Included {commission_amount} commission in cost basis "
                    f"for {envelope.envelope_id}: "
                    f"cost per unit now {cost_per_unit}"
                )

        # Create the lot with deterministic ID
        lot_data = {
            "account": envelope.inbound_account,
            "commodity": envelope.inbound_type,
            "date": envelope.date,
            "quantity": str(envelope.inbound_units),
            "cost_per_unit": str(cost_per_unit),
            "source_envelope": envelope.envelope_id,
        }

        lot = {
            "lot_id": generate_id("LOT", lot_data),
            "date": envelope.date,
            "quantity": envelope.inbound_units,
            "cost_per_unit": cost_per_unit,
            "currency": get_currency(envelope) or "GBP",
            "envelope_id": envelope.envelope_id,
        }

        # Add to registry
        key = (envelope.inbound_account, envelope.inbound_type)
        if key not in self.lots:
            self.lots[key] = []
        self.lots[key].append(lot)

        # Track lot movement
        self.lot_movements.append(
            {
                "date": envelope.date,
                "action": "CREATE",
                "envelope_id": envelope.envelope_id,
                "lot_id": lot["lot_id"],
                "account": envelope.inbound_account,
                "commodity": envelope.inbound_type,
                "quantity": str(envelope.inbound_units),
                "cost_per_unit": str(cost_per_unit),
                "narration": envelope.narration
                or f"Purchase of {envelope.inbound_type}",
            }
        )

        # Enhance envelope with lot info (store a copy to avoid mutation)
        updated_metadata = dict(envelope.metadata) if envelope.metadata else {}
        updated_metadata["lot_created"] = dict(lot)  # Copy to avoid mutation

        # Also set the unit_price on the envelope if we calculated it
        if not envelope.unit_price and cost_per_unit != Decimal("0"):
            envelope = enhance_envelope(
                envelope,
                reason="Added lot creation info and calculated unit price",
                metadata=updated_metadata,
                unit_price=cost_per_unit,
            )
        else:
            envelope = enhance_envelope(
                envelope, reason="Added lot creation info", metadata=updated_metadata
            )

        logger.debug(
            f"Created lot for {envelope.inbound_units} {envelope.inbound_type} "
            f"@ {cost_per_unit} in {envelope.inbound_account}"
        )

        return envelope

    def _handle_sell(
        self, envelope: Envelope, warnings: List[LotTrackingWarning]
    ) -> Envelope:
        """
        Consume lots for SELL envelope using FIFO.

        For SELL transactions:
        - Commodity goes OUT (outbound_units, outbound_type)
        - Cash comes IN (inbound_units)
        """
        if not envelope.outbound_units or not envelope.outbound_type:
            return envelope  # No commodity to consume

        key = (envelope.outbound_account, envelope.outbound_type)
        quantity_needed = abs(envelope.outbound_units)

        # Check if we have lots
        if key not in self.lots or not self.lots[key]:
            warnings.append(
                LotTrackingWarning(
                    processor_name=self.processor_name,
                    severity="ERROR",
                    message=f"No lots available for {envelope.outbound_type} sale",
                    source_transaction=None,
                    details={
                        "envelope_id": envelope.envelope_id,
                        "account": envelope.outbound_account,
                        "commodity": envelope.outbound_type,
                        "quantity": str(quantity_needed),
                    },
                )
            )
            return envelope

        # FIFO consumption
        consumed_lots = []
        remaining = quantity_needed

        # Sort lots by date for FIFO
        self.lots[key].sort(key=lambda x: x["date"])

        for lot in self.lots[key]:
            if remaining <= 0:
                break

            available = lot["quantity"]
            if available <= 0:
                continue

            # Take what we need from this lot
            take = min(remaining, available)
            consumed_lots.append(
                {
                    "lot_id": lot["lot_id"],
                    "quantity_consumed": take,
                    "cost_per_unit": lot["cost_per_unit"],
                    "currency": lot.get("currency", "GBP"),
                    "acquisition_date": lot["date"],  # Keep as date object
                }
            )

            # Update lot quantity
            lot["quantity"] -= take
            remaining -= take

            # Track lot movement
            self.lot_movements.append(
                {
                    "date": envelope.date,
                    "action": "CONSUME",
                    "envelope_id": envelope.envelope_id,
                    "lot_id": lot["lot_id"],
                    "account": envelope.outbound_account,
                    "commodity": envelope.outbound_type,
                    "quantity_consumed": str(take),
                    "quantity_remaining": str(lot["quantity"]),
                    "cost_per_unit": str(lot["cost_per_unit"]),
                    "narration": envelope.narration
                    or f"Sale of {envelope.outbound_type}",
                }
            )

        # Remove exhausted lots
        self.lots[key] = [x for x in self.lots[key] if x["quantity"] > 0]

        # Check if we consumed enough
        if remaining > 0:
            warnings.append(
                LotTrackingWarning(
                    processor_name=self.processor_name,
                    severity="WARNING",
                    message=f"Partial lot consumption: needed {quantity_needed}, consumed {quantity_needed - remaining}",
                    source_transaction=None,
                    details={
                        "envelope_id": envelope.envelope_id,
                        "shortage": str(remaining),
                    },
                )
            )

        # Enhance envelope with consumed lots
        if consumed_lots:
            updated_metadata = dict(envelope.metadata) if envelope.metadata else {}
            updated_metadata["consumed_lots"] = consumed_lots
            envelope = enhance_envelope(
                envelope, reason="Added consumed lot info", metadata=updated_metadata
            )

            logger.debug(
                f"Consumed {len(consumed_lots)} lots for sale of "
                f"{quantity_needed} {envelope.outbound_type}"
            )

        return envelope

    def _handle_transfer(
        self, envelope: Envelope, warnings: List[LotTrackingWarning]
    ) -> Envelope:
        """
        Transfer lots between our own accounts, preserving cost basis.

        This ONLY handles transfers between accounts we control.
        External transfers should not exist - they indicate missing purchase data.

        For transfers, the envelope MUST have:
        - outbound_account: source account
        - inbound_account: destination account
        - The commodity and quantity in the appropriate fields
        """
        from cassoulet.base.exceptions import MissingCostBasisError
        from cassoulet.utils.envelope_utilities import (
            get_outbound_account,
            get_inbound_account,
        )

        # Use envelope utilities to get commodity and quantity
        commodity = get_commodity(envelope)
        quantity = get_commodity_units(envelope)

        if not commodity or not quantity:
            return envelope  # Nothing to transfer

        # Use proper envelope utilities to get accounts (includes metadata checking)
        from_account = get_outbound_account(envelope)
        to_account = get_inbound_account(envelope)

        # Both accounts MUST exist for a valid transfer
        if not from_account or not to_account:
            # This is not a valid transfer - it's missing data
            raise MissingCostBasisError(
                envelope_id=envelope.envelope_id,
                commodity=commodity,
                units=quantity,
                account=to_account or from_account or "unknown",
                reason=f"Transfer missing {'source' if not from_account else 'destination'} account - likely missing historical purchase data",
                transfer_type="incomplete_transfer",
            )

        # Source and destination keys
        from_key = (from_account, commodity)
        to_key = (to_account, commodity)

        # Check if we have lots to transfer
        if from_key not in self.lots or not self.lots[from_key]:
            warnings.append(
                LotTrackingWarning(
                    processor_name=self.processor_name,
                    severity="WARNING",
                    message=f"No lots to transfer for {commodity}",
                    source_transaction=None,
                    details={
                        "envelope_id": envelope.envelope_id,
                        "from_account": from_account,
                        "commodity": commodity,
                    },
                )
            )
            return envelope

        # Transfer using FIFO
        transferred_lots = []
        remaining = abs(quantity)

        # Sort for FIFO
        self.lots[from_key].sort(key=lambda x: x["date"])

        for lot in self.lots[from_key][:]:  # Copy list since we'll modify
            if remaining <= 0:
                break

            available = lot["quantity"]
            if available <= 0:
                continue

            # Transfer what we can from this lot
            transfer_qty = min(remaining, available)

            # Create new lot in destination with deterministic ID
            new_lot_data = {
                "account": to_account,
                "commodity": commodity,
                "date": lot["date"],
                "quantity": str(transfer_qty),
                "cost_per_unit": str(lot["cost_per_unit"]),
                "source_lot": lot["lot_id"],
                "transfer_envelope": envelope.envelope_id,
            }

            new_lot = {
                "lot_id": generate_id("LOT", new_lot_data),
                "date": lot["date"],  # Preserve original acquisition date
                "quantity": transfer_qty,
                "cost_per_unit": lot["cost_per_unit"],  # Preserve cost basis
                "currency": lot.get("currency", "GBP"),
                "envelope_id": envelope.envelope_id,
                "transferred_from": lot["lot_id"],
            }

            # Add to destination
            if to_key not in self.lots:
                self.lots[to_key] = []
            self.lots[to_key].append(new_lot)

            # Record transfer
            transferred_lots.append(
                {
                    "from_lot_id": lot["lot_id"],
                    "to_lot_id": new_lot["lot_id"],
                    "quantity": transfer_qty,
                    "cost_per_unit": lot["cost_per_unit"],
                    "currency": lot.get("currency", "GBP"),
                    "acquisition_date": lot["date"],  # Keep as date object
                }
            )

            # Track lot movement
            self.lot_movements.append(
                {
                    "date": envelope.date,
                    "action": "TRANSFER",
                    "envelope_id": envelope.envelope_id,
                    "from_lot_id": lot["lot_id"],
                    "to_lot_id": new_lot["lot_id"],
                    "from_account": from_account,
                    "to_account": to_account,
                    "commodity": commodity,
                    "quantity_transferred": str(transfer_qty),
                    "cost_per_unit": str(lot["cost_per_unit"]),
                    "narration": envelope.narration or f"Transfer of {commodity}",
                }
            )

            # Reduce source lot
            lot["quantity"] -= transfer_qty
            remaining -= transfer_qty

        # Remove exhausted lots from source
        self.lots[from_key] = [x for x in self.lots[from_key] if x["quantity"] > 0]

        # Enhance envelope with transfer info
        if transferred_lots:
            updated_metadata = dict(envelope.metadata) if envelope.metadata else {}
            updated_metadata["transferred_lots"] = transferred_lots
            envelope = enhance_envelope(
                envelope, reason="Added transfer lot info", metadata=updated_metadata
            )

            logger.debug(
                f"Transferred {len(transferred_lots)} lots of {commodity} "
                f"from {from_account} to {to_account}"
            )

        return envelope

    def _handle_stock_split(
        self, split: StockSplit, warnings: List[LotTrackingWarning]
    ) -> CorporateActionResult:
        """
        Transform all lots for a stock split.

        For a 10:1 split:
        - 23 shares @ 625.34 -> 230 shares @ 62.534
        - Acquisition date preserved
        - Total cost basis preserved (23 * 625.34 = 230 * 62.534)

        Args:
            split: StockSplit corporate action
            warnings: List to append warnings to

        Returns:
            CorporateActionResult with lot transformations
        """
        key = (split.account, split.commodity)

        if key not in self.lots or not self.lots[key]:
            warnings.append(
                LotTrackingWarning(
                    processor_name=self.processor_name,
                    severity="WARNING",
                    message=f"No lots found for stock split: {split.commodity} in {split.account}",
                    source_transaction=None,
                    details={
                        "split_date": str(split.date),
                        "commodity": split.commodity,
                        "account": split.account,
                        "ratio": str(split.ratio),
                    },
                )
            )
            return CorporateActionResult(action=split, lots_transformed=[])

        # Transform each lot
        transformed_lots = []
        new_lots = []

        logger.info(
            f"Transforming {len(self.lots[key])} lots for {split.commodity} split ({split.ratio}:1)"
        )

        for old_lot in self.lots[key]:
            # Transform: multiply quantity, divide cost per unit
            # This preserves total cost basis: old_qty * old_cost = new_qty * new_cost
            new_lot_data = {
                "account": split.account,
                "commodity": split.commodity,
                "date": old_lot["date"],
                "quantity": str(old_lot["quantity"] * split.ratio),
                "cost_per_unit": str(old_lot["cost_per_unit"] / split.ratio),
                "split_from": old_lot["lot_id"],
                "split_date": str(split.date),
                "split_ratio": str(split.ratio),
            }

            new_lot = {
                "lot_id": generate_id("LOT", new_lot_data),
                "date": old_lot["date"],  # PRESERVE acquisition date
                "quantity": old_lot["quantity"] * split.ratio,
                "cost_per_unit": old_lot["cost_per_unit"] / split.ratio,
                "currency": old_lot.get("currency", "GBP"),
                "envelope_id": f"split_{split.date}_{split.commodity}",
                "split_from": old_lot["lot_id"],
                "split_ratio": split.ratio,
            }

            new_lots.append(new_lot)
            # CRITICAL: Save COPIES so later lot consumption doesn't modify the transformation record
            transformed_lots.append((old_lot.copy(), new_lot.copy()))

            # Track the transformation
            self.lot_movements.append(
                {
                    "date": split.date,
                    "action": "SPLIT",
                    "old_lot_id": old_lot["lot_id"],
                    "new_lot_id": new_lot["lot_id"],
                    "ratio": str(split.ratio),
                    "commodity": split.commodity,
                    "account": split.account,
                    "old_quantity": str(old_lot["quantity"]),
                    "new_quantity": str(new_lot["quantity"]),
                    "old_cost": str(old_lot["cost_per_unit"]),
                    "new_cost": str(new_lot["cost_per_unit"]),
                    "narration": split.narration or f"Stock split {split.ratio}:1",
                }
            )

            logger.debug(
                f"Transformed lot: {old_lot['quantity']} @ {old_lot['cost_per_unit']} "
                f"-> {new_lot['quantity']} @ {new_lot['cost_per_unit']}"
            )

        # Replace old lots with transformed lots
        self.lots[key] = new_lots

        logger.info(
            f"Stock split complete: Transformed {len(transformed_lots)} lots of {split.commodity} "
            f"({split.ratio}:1 ratio)"
        )

        return CorporateActionResult(action=split, lots_transformed=transformed_lots)
