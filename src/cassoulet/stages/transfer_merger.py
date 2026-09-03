"""
Transfer Merger

Trusts the matcher completely and focuses on merging mechanics.
"""

import logging
import dataclasses
from typing import List, Tuple, Set
from datetime import datetime

from cassoulet.base.exceptions import ProcessingWarning, UnsafeMergeError, UnsafeMergeWarning
from cassoulet.stages.envelope import Envelope
from cassoulet.stages.envelope_processor import EnvelopeProcessor
from cassoulet.utils.envelope_utilities import (
    classify_transaction_type,
    find_sovereign,
    enhance_envelope,
    transfer_delay,
    transfer_loss,
    get_manual_and_csv_envelopes,
    units_match,
    can_reconcile,
    get_transfer_amounts,
    NOT_APPLICABLE,
)
from cassoulet.utils.deterministic_id import generate_merged_envelope_id
from cassoulet.utils.currencies import is_currency

logger = logging.getLogger(__name__)


class TransferMerger(EnvelopeProcessor):
    """
    Simple transfer merger that trusts the matcher.

    Just merges matched envelopes with proper precedence.
    """

    def __init__(self, config=None, debug: bool = False):
        """Initialize the transfer merger."""
        super().__init__(processor_name="TransferMerger")
        # Keep for compatibility but unused
        _ = (config, debug)

    def _process_internal(
        self,
        envelopes: List[Envelope]
    ) -> Tuple[List[Envelope], List[ProcessingWarning]]:
        """Process envelopes - merge matched ones, pass through unmatched."""
        warnings = []
        merged = []
        processed_ids = set()

        # DEBUG: Track reconciliation details
        reconciliation_consumed = []

        for env in envelopes:
            if env.envelope_id in processed_ids:
                logger.debug(f"⏭️ Skipping {env.envelope_id[:40]} - already processed")
                continue

            # Check if matched
            if env.metadata.get('matched_with'):
                # Collect and merge the group
                logger.debug(f"🔍 Processing matched envelope: {env.envelope_id[:40]}")
                group = self._collect_group(env, envelopes, processed_ids)
                logger.debug(f"   Group size: {len(group)}, IDs: {[e.envelope_id[:30] for e in group]}")

                # Check if this is reconciliation BEFORE merging
                is_reconciliation = (len(group) == 2 and can_reconcile(group[0], group[1]))

                merged_envs, merge_warnings = self._merge_group(group)
                # _merge_group now returns a list (can be 1 or 2 envelopes for in-transit)
                merged.extend(merged_envs)
                warnings.extend(merge_warnings)

                # Track what was consumed in reconciliation
                if is_reconciliation:
                    # Get the primary envelope ID(s) from the merged result
                    merged_ids = {env.envelope_id for env in merged_envs}
                    for e in group:
                        if e.envelope_id not in merged_ids:
                            reconciliation_consumed.append(e.envelope_id)
                            logger.info(f"✅ CONSUMED via reconciliation: {e.envelope_id}")

                # Mark all as processed
                for e in group:
                    processed_ids.add(e.envelope_id)
                    logger.debug(f"   Added to processed_ids: {e.envelope_id[:30]}")
            else:
                # No match - pass through
                merged.append(env)
                processed_ids.add(env.envelope_id)

        # Report reconciliation summary
        if reconciliation_consumed:
            logger.info(f"🔄 RECONCILIATION SUMMARY: Consumed {len(reconciliation_consumed)} CSV envelopes")

        logger.info(f"TransferMerger: {len(envelopes)} -> {len(merged)} envelopes")
        return merged, warnings

    def _collect_group(
        self,
        envelope: Envelope,
        all_envelopes: List[Envelope],
        processed_ids: Set[str]
    ) -> List[Envelope]:
        """Collect envelopes to merge."""
        group = [envelope]
        matched_id = envelope.metadata.get('matched_with')

        logger.debug(f"📋 Collecting group for {envelope.envelope_id[:40]}")
        logger.debug(f"   matched_with: {matched_id[:40] if matched_id and isinstance(matched_id, str) else matched_id}")

        if matched_id:
            # Handle list or single ID
            ids = matched_id if isinstance(matched_id, list) else [matched_id]
            logger.debug(f"   Looking for IDs: {[id[:30] for id in ids]}")

            found_count = 0
            for env in all_envelopes:
                if env.envelope_id in ids:
                    if env.envelope_id not in processed_ids:
                        group.append(env)
                        found_count += 1
                        logger.debug(f"   ✅ Found and added: {env.envelope_id[:40]}")
                    else:
                        logger.debug(f"   ⚠️ Found but already processed: {env.envelope_id[:40]}")

            if found_count == 0:
                logger.warning(f"   ❌ No matches found! This will be a single-envelope group")
                logger.debug(f"   Processed IDs so far: {list(processed_ids)[:5]}...")

        if len(group) > 1:
            logger.debug(f"Group formed: {len(group)} envelopes - {[e.envelope_id[:20] for e in group]}")
        else:
            logger.debug(f"Single envelope group: {envelope.envelope_id[:40]}")

        return group

    def _should_apply_transfer_leakage(self, env1: Envelope, env2: Envelope) -> bool:
        """
        Whitelist check: determine if transfer_leakage can be safely applied.

        Transfer leakage should ONLY be applied to aggregation of partial legs.
        For reconciliation or complex transactions, applying leakage creates problems.

        Whitelist conditions (ALL must be true):
        1. NOT reconciliation (checked first)
        2. Can extract transfer amounts (get_transfer_amounts works)
        3. Neither has additional_postings
        4. Neither is an investment transaction (BUY/SELL/DIVIDEND)

        Returns:
            True if transfer_leakage is safe to apply, False otherwise
        """
        # 1. NEVER apply transfer_leakage for reconciliation
        # Reconciliation = one manual + one CSV
        if get_manual_and_csv_envelopes(env1, env2):
            logger.debug(f"Cannot apply transfer_leakage: this is reconciliation, not aggregation")
            return False

        # 2. Must be able to extract transfer amounts
        amounts = get_transfer_amounts(env1, env2)
        if amounts == NOT_APPLICABLE:
            logger.debug(f"Cannot apply transfer_leakage: cannot extract transfer amounts")
            return False

        # 3. Neither should have additional_postings
        if env1.additional_postings or env2.additional_postings:
            logger.debug(f"Cannot apply transfer_leakage: envelope has additional_postings "
                        f"(env1={bool(env1.additional_postings)}, env2={bool(env2.additional_postings)})")
            return False

        # 4. Neither should be investment transaction
        investment_types = {'BUY', 'SELL', 'DIVIDEND', 'INTEREST', 'FEE'}
        if (env1.transaction_type in investment_types or
            env2.transaction_type in investment_types):
            logger.debug(f"Cannot apply transfer_leakage: investment transaction "
                        f"(env1={env1.transaction_type}, env2={env2.transaction_type})")
            return False

        # All conditions met - safe to apply leakage
        logger.debug(f"Transfer_leakage is SAFE to apply for {env1.envelope_id} + {env2.envelope_id}")
        return True

    def _merge_group(self, group: List[Envelope]) -> Tuple[List[Envelope], List[ProcessingWarning]]:
        """Merge envelopes - detect reconciliation vs aggregation.

        Returns:
            Tuple of (list of envelopes, warnings). Usually returns 1 envelope,
            but can return 2 for in-transit transfers with different settlement dates.
        """
        warnings = []

        if len(group) == 1:
            return [group[0]], warnings

        # Check for reconciliation (only applies to pairs)
        if len(group) == 2:
            # Check if this is a valid reconciliation (manual+CSV, shared account, type overlap)
            can_rec = can_reconcile(group[0], group[1])
            if can_rec:
                logger.debug(f"✅ Reconciliation detected: {group[0].envelope_id[:30]} + {group[1].envelope_id[:30]}")
                # This is reconciliation - validate and mark
                return [self._reconcile_envelopes(group[0], group[1])], warnings
            else:
                # Log why reconciliation failed for debugging
                logger.debug(f"❌ Cannot reconcile {group[0].envelope_id[:30]} + {group[1].envelope_id[:30]}")

        # Check if aggregation is valid using the centralized can_aggregate function
        # This checks all compatibility rules in one place
        if len(group) == 2:
            from cassoulet.utils.envelope_utilities import can_aggregate
            if not can_aggregate(group[0], group[1]):
                logger.warning(
                    f"REJECTED AGGREGATION: Cannot aggregate {group[0].envelope_id[:30]} "
                    f"with {group[1].envelope_id[:30]} - incompatible envelopes"
                )
                # RETURN BOTH. Deciding two envelopes cannot be merged is a
                # decision to keep them apart, not a licence to discard one -
                # yet this returned group[0] alone while the caller marked every
                # member processed, so group[1] left the pipeline with no
                # lineage, no warning and no posting. A rejected merge is
                # precisely the case where both transactions are real and
                # distinct, which is the argument for keeping them, and the
                # rejection is already recorded on each.
                #
                # It fired zero times on the reference ledger, which is why the
                # envelope arithmetic came out exact. It was invisible rather
                # than harmless: for as long as the integrity check reported
                # five permanent CRITICALs, a genuine loss here would have been
                # lost among them.
                kept = []
                for envelope in group:
                    cleaned_metadata = dict(envelope.metadata) if envelope.metadata else {}
                    cleaned_metadata.pop('matched_with', None)
                    cleaned_metadata.pop('match_score', None)
                    cleaned_metadata.pop('match_confidence', None)
                    cleaned_metadata['aggregation_rejected'] = True
                    cleaned_metadata['rejection_reason'] = 'Failed can_aggregate check'
                    kept.append(enhance_envelope(
                        envelope,
                        reason="Aggregation rejected - incompatible envelopes",
                        metadata=cleaned_metadata
                    ))

                warnings.append(ProcessingWarning(
                    processor_name=self.processor_name,
                    severity='WARNING',
                    message=(
                        "Aggregation rejected - both envelopes kept: "
                        f"{group[0].envelope_id} and {group[1].envelope_id}"
                    ),
                    source_transaction=None,
                    details={
                        'envelope_ids': [e.envelope_id for e in group],
                        'reason': 'Failed can_aggregate check',
                    },
                ))
                return kept, warnings

        # Not reconciliation - do aggregation
        # Catch UnsafeMergeError and handle it gracefully per Steel Thread principles
        try:
            return self._aggregate_envelopes(group), warnings  # Returns List[Envelope]
        except UnsafeMergeError as e:
            # Create structured envelope details for the warning
            env_details = []
            for env in group:
                amount = env.inbound_units or env.outbound_units
                env_details.append({
                    'envelope_id': env.envelope_id,
                    'date': str(env.date),
                    'amount': str(amount) if amount else 'None',
                    'account': env.inbound_account or env.outbound_account,
                    'narration': env.narration[:50] if env.narration else None
                })

            logger.warning(f"UNSAFE MERGE: {str(e)}")
            logger.debug(f"  Envelopes being merged: {env_details}")
            logger.debug(f"  Primary envelope metadata: {group[0].metadata if group else 'None'}")

            # Create an UnsafeMergeWarning (not an exception - just a structured warning)
            warning = UnsafeMergeWarning(
                processor_name=self.processor_name,
                severity='WARNING',  # Changed from ERROR - this is expected behavior now
                message='',  # Will be built in __post_init__
                env1_id=e.env1_id,
                env2_id=e.env2_id,
                loss=e.loss,
                env1_type=e.env1_type,
                env2_type=e.env2_type,
                envelope_details=env_details,
                match_score=group[0].metadata.get('match_score') if group and group[0].metadata else None,
                match_confidence=group[0].metadata.get('match_confidence') if group and group[0].metadata else None,
                matched_patterns=group[0].metadata.get('potential_matches', [])[:3] if group and group[0].metadata else []
            )
            warnings.append(warning)

            # DON'T MERGE - "Fail up" by leaving envelopes unmerged
            # This is better than creating a broken merge
            logger.info(f"SKIPPING MERGE: Returning first envelope unmodified to preserve data integrity")

            # Clear the match metadata since we're not merging
            primary = group[0]
            cleaned_metadata = dict(primary.metadata) if primary.metadata else {}
            cleaned_metadata.pop('matched_with', None)
            cleaned_metadata.pop('match_score', None)
            cleaned_metadata.pop('match_confidence', None)
            cleaned_metadata['merge_rejected'] = True
            cleaned_metadata['merge_rejection_reason'] = f"Amount mismatch of {e.loss:.2f} - cannot safely merge {e.env1_type} with {e.env2_type}"

            # Return the primary envelope WITHOUT merging
            return [enhance_envelope(
                primary,
                reason="Merge rejected due to unsafe amount mismatch",
                metadata=cleaned_metadata
            )], warnings

    def _reconcile_envelopes(self, env1: Envelope, env2: Envelope) -> Envelope:
        """Reconcile a manual transaction with CSV import.

        This is for manual + CSV of the SAME transaction (reconciliation).
        We keep the manual transaction (source of truth) and mark it as reconciled.

        Args:
            env1: First envelope (order doesn't matter)
            env2: Second envelope (order doesn't matter)

        Returns:
            The manual transaction, marked as reconciled
        """
        logger.info(f"🔄 _reconcile_envelopes CALLED:")
        logger.info(f"     env1 ID: {env1.envelope_id}")
        logger.info(f"     env2 ID: {env2.envelope_id}")
        logger.info(f"     env1 source_type: {env1.source_type}")
        logger.info(f"     env2 source_type: {env2.source_type}")

        # Figure out which is manual and which is CSV
        result = get_manual_and_csv_envelopes(env1, env2)
        if not result:
            logger.error(
                f"RECONCILIATION VALIDATION FAILED: Expected manual+CSV pair "
                f"for {env1.envelope_id} + {env2.envelope_id}. "
                f"This should have been blocked at scoring level."
            )
            # Fallback: return first envelope
            return env1

        manual, csv = result

        # Defense in depth: Validate EXACT match for reconciliation
        # Check that they're actually compatible for reconciliation
        if not can_reconcile(manual, csv):
            logger.error(
                f"RECONCILIATION VALIDATION FAILED: Not compatible for reconciliation "
                f"for {manual.envelope_id} + {csv.envelope_id}. "
                f"This should have been blocked at scoring level."
            )

        # Check units (numeric values) match exactly
        if not units_match(manual, csv):
            logger.error(
                f"RECONCILIATION VALIDATION FAILED: Numeric values don't match exactly "
                f"for {manual.envelope_id} + {csv.envelope_id}. "
                f"This should have been blocked at scoring level."
            )

        # Check dates - allow reasonable differences for reconciliation
        # Manual entries might use different dates for record-keeping
        delay = transfer_delay(manual, csv)
        if delay is not None and abs(delay) > 30:
            # Only warn if dates are very far apart
            logger.warning(
                f"Large date difference ({delay} days) in reconciliation "
                f"for {manual.envelope_id} + {csv.envelope_id}. "
                f"Manual review recommended."
            )
        elif delay is not None and abs(delay) > 7:
            # Info level for moderate differences - this is normal
            logger.info(
                f"Date difference of {delay} days in reconciliation "
                f"for {manual.envelope_id} + {csv.envelope_id} - this is normal for transfers."
            )

        # Keep the manual transaction, mark as reconciled
        reconciled = enhance_envelope(
            manual,
            reason=f"Reconciled with {csv.source}",
            metadata={
                'reconciled_with': csv.envelope_id,
                'reconciliation_validated': True,
                'reconciliation_timestamp': datetime.now().isoformat(),
                'csv_envelope_source': csv.source,
                'csv_envelope_narration': csv.narration,
            }
        )

        logger.info(f"✅ RECONCILIATION COMPLETE:")
        logger.info(f"     KEPT manual envelope: {reconciled.envelope_id}")
        logger.info(f"     CONSUMED CSV envelope: {csv.envelope_id}")
        logger.info(f"     Result envelope ID: {reconciled.envelope_id}")

        return reconciled

    def _aggregate_envelopes(self, group: List[Envelope]) -> List[Envelope]:
        """Aggregate multiple partial legs into complete transaction.

        This is the original merge logic - combining bank + broker legs,
        applying transfer_leakage when appropriate.

        For transfers with different settlement dates, creates TWO transactions
        using an in-transit account to preserve accurate balances.

        Args:
            group: List of envelopes to aggregate

        Returns:
            List of merged envelopes (usually 1, but 2 for in-transit splits)
        """
        # Find primary (sovereign/manual wins)
        sovereign = find_sovereign(group)
        primary = sovereign or group[0]

        # Calculate date and amount discrepancies
        date_to_use = primary.date
        transfer_metadata = {}

        if len(group) == 2:
            # Perfect pair - can calculate precise discrepancies
            # Check date mismatch
            delay = transfer_delay(group[0], group[1])
            if delay is not None and delay != 0 and group[0].date != group[1].date:
                # Different settlement dates - create TWO transactions with in-transit
                logger.info(f"🚚 In-transit split: {delay} day gap between {group[0].date} and {group[1].date}")
                return self._create_in_transit_pair(group[0], group[1])

            # Check amount mismatch (fees/gains)
            loss = transfer_loss(group[0], group[1])
            if loss is not None and abs(loss) > 0.001:  # Tolerance for tiny rounding
                # Check if we can safely apply transfer_leakage
                can_apply_leakage = self._should_apply_transfer_leakage(group[0], group[1])

                if not can_apply_leakage:
                    # Raise UnsafeMergeError - will be caught by _merge_group
                    raise UnsafeMergeError(
                        env1_id=group[0].envelope_id,
                        env2_id=group[1].envelope_id,
                        loss=loss,
                        env1_type=str(group[0].transaction_type),
                        env2_type=str(group[1].transaction_type)
                    )

                # Safe to apply - add to metadata
                transfer_metadata['transfer_leakage'] = loss
                # Try to detect currency from envelopes using proper currency utilities
                currency = None

                # Check inbound type first
                if group[0].inbound_type and is_currency(group[0].inbound_type):
                    currency = group[0].inbound_type.upper()
                # Then check outbound type
                elif group[0].outbound_type and is_currency(group[0].outbound_type):
                    currency = group[0].outbound_type.upper()
                # Check second envelope's types
                elif group[1].inbound_type and is_currency(group[1].inbound_type):
                    currency = group[1].inbound_type.upper()
                elif group[1].outbound_type and is_currency(group[1].outbound_type):
                    currency = group[1].outbound_type.upper()
                # Default to GBP if we can't determine
                else:
                    currency = 'GBP'

                transfer_metadata['leakage_currency'] = currency

                if loss > 0:
                    transfer_metadata['leakage_reason'] = 'Transfer fee or rounding difference'
                    if loss > 10:
                        transfer_metadata['leakage_warning'] = f'Large transfer loss: {loss} {currency}'
                else:
                    transfer_metadata['leakage_reason'] = 'Transfer rounding gain'
                    if abs(loss) > 10:
                        transfer_metadata['leakage_warning'] = f'Unusual transfer gain: {abs(loss)} {currency}'

        elif len(group) > 2:
            # 3+ way merge - CRITICAL WARNING per Steel Thread requirements
            logger.warning(f"Multi-way merge of {len(group)} envelopes - date/amount reconciliation may be incomplete")
            transfer_metadata['multi_way_merge_warning'] = (
                f"Merging {len(group)} envelopes - cannot reliably detect transfer leakage or date discrepancies. "
                f"Manual review recommended."
            )

            # Use earliest date for conservative booking
            all_dates = [e.date for e in group if e.date]
            if all_dates:
                earliest = min(all_dates)
                latest = max(all_dates)
                if earliest != latest:
                    date_to_use = earliest
                    date_span = (latest - earliest).days
                    transfer_metadata['date_span_days'] = date_span
                    transfer_metadata['date_range'] = f"{earliest} to {latest}"
                    if date_span > 7:
                        transfer_metadata['date_span_warning'] = f"Large date span: {date_span} days across {len(group)} envelopes"

            # Track envelope IDs for audit trail
            transfer_metadata['multi_merge_ids'] = [e.envelope_id for e in group]

        # Build update fields for enhancement
        update_fields = {}

        # Update date if different from primary
        if date_to_use != primary.date:
            update_fields['date'] = date_to_use

        # Collect financial fields from non-primary envelopes
        for env in group:
            if env == primary:
                continue

            # Fill in missing outbound fields
            if env.outbound_units and not primary.outbound_units:
                update_fields['outbound_units'] = env.outbound_units
                update_fields['outbound_type'] = env.outbound_type
                update_fields['outbound_account'] = env.outbound_account

            # Fill in missing inbound fields
            if env.inbound_units and not primary.inbound_units:
                update_fields['inbound_units'] = env.inbound_units
                update_fields['inbound_type'] = env.inbound_type
                update_fields['inbound_account'] = env.inbound_account

            # Fill in missing unit_price
            if env.unit_price and not primary.unit_price:
                update_fields['unit_price'] = env.unit_price

        # Combine narrations if different
        narrations = [e.narration for e in group
                     if e.narration and e.narration != primary.narration]
        if narrations:
            update_fields['narration'] = f"{primary.narration} | {' | '.join(narrations)}"

        # Build merged metadata
        merged_metadata = dict(primary.metadata) if primary.metadata else {}
        merged_metadata.update({
            'merged_from': [e.envelope_id for e in group],
            'merge_count': len(group),
            'merge_timestamp': datetime.now().isoformat(),
            'merge_id': generate_merged_envelope_id(group),
            **transfer_metadata  # Include transfer analysis
        })
        update_fields['metadata'] = merged_metadata

        # Use enhance_envelope to preserve all fields and maintain audit trail
        if update_fields:
            # We have updates to apply
            merged = enhance_envelope(
                primary,
                reason=f"Merged {len(group)} envelopes with transfer analysis",
                **update_fields
            )
        else:
            # No updates needed, just add merge metadata
            merged = enhance_envelope(
                primary,
                reason=f"Marked as merged (no changes needed)",
                metadata=merged_metadata
            )

        # Classify transaction type based on merged envelope
        merged.transaction_type = classify_transaction_type(merged)

        return [merged]

    def _create_in_transit_pair(self, env1: Envelope, env2: Envelope) -> List[Envelope]:
        """Create two transactions for a transfer with different settlement dates.

        Uses Assets:Transfer:InTransit as an intermediate account to preserve
        accurate account balances when settlement dates differ.

        Args:
            env1: First envelope (sender or receiver)
            env2: Second envelope (sender or receiver)

        Returns:
            List of two envelopes (one for each date)
        """
        from cassoulet.utils.envelope_utilities import (
            envelope_compatibility_checks,
            get_transfer_amounts,
            enhance_envelope
        )

        # Determine flow direction
        compat = envelope_compatibility_checks(env1, env2)
        flow = compat.get('flow_pattern', '')

        # Get the transfer amount
        amounts = get_transfer_amounts(env1, env2)
        if amounts == NOT_APPLICABLE:
            logger.warning(f"Cannot create in-transit pair: no transfer amounts")
            # Fall back to single merged transaction
            return [env1]  # Return first envelope unmodified

        outbound_amt, inbound_amt = amounts
        amount = abs(outbound_amt) if outbound_amt else abs(inbound_amt)

        # Determine which is sender and receiver
        if 'env1_sends' in flow:
            sender_env, receiver_env = env1, env2
        elif 'env2_sends' in flow:
            sender_env, receiver_env = env2, env1
        else:
            logger.warning(f"Cannot determine flow direction: {flow}")
            return [env1]

        # Get currency (use outbound currency)
        currency = sender_env.outbound_type or receiver_env.inbound_type or 'GBP'

        # Transaction 1: Earlier date (receiver gets money, from InTransit)
        earlier_date = min(env1.date, env2.date)
        later_date = max(env1.date, env2.date)

        logger.info(f"  Creating in-transit pair:")
        logger.info(f"    Transaction 1 ({earlier_date}): {receiver_env.inbound_account} receives from InTransit")
        logger.info(f"    Transaction 2 ({later_date}): {sender_env.outbound_account} sends to InTransit")

        # Build transaction 1 (receiving side)
        txn1_metadata = dict(receiver_env.metadata) if receiver_env.metadata else {}
        txn1_metadata.update({
            'in_transit_pair': 'receiver',
            'in_transit_counterpart_date': str(later_date),
            'merged_from': [env1.envelope_id, env2.envelope_id],
            'merge_count': 2,
            'merge_timestamp': datetime.now().isoformat(),
            'settlement_delay_days': abs((later_date - earlier_date).days)
        })

        txn1 = enhance_envelope(
            receiver_env,
            reason="In-transit split - receiving side",
            outbound_account='Assets:Transfer:InTransit',
            outbound_units=-amount,
            outbound_type=currency,
            metadata=txn1_metadata
        )

        # Build transaction 2 (sending side)
        txn2_metadata = dict(sender_env.metadata) if sender_env.metadata else {}
        txn2_metadata.update({
            'in_transit_pair': 'sender',
            'in_transit_counterpart_date': str(earlier_date),
            'merged_from': [env1.envelope_id, env2.envelope_id],
            'merge_count': 2,
            'merge_timestamp': datetime.now().isoformat(),
            'settlement_delay_days': abs((later_date - earlier_date).days)
        })

        txn2 = enhance_envelope(
            sender_env,
            reason="In-transit split - sending side",
            inbound_account='Assets:Transfer:InTransit',
            inbound_units=amount,
            inbound_type=currency,
            metadata=txn2_metadata
        )

        return [txn1, txn2]