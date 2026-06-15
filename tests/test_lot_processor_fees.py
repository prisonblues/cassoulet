"""Tests for fee inclusion in cost basis (GitHub issue #9).

Verifies that separately-tracked fees/commission are included in the
per-unit cost basis for BUY transactions.
"""

from datetime import date
from decimal import Decimal

from cassoulet.stages.envelope_lot_processor import EnvelopeLotProcessor
from cassoulet.utils.envelope_utilities import create_envelope


def _make_buy_envelope(
    quantity,
    amount,
    commission=None,
    source_type="csv",
    commodity="VWRL",
    envelope_id="test_buy_001",
):
    """Create a BUY envelope with optional commission metadata."""
    metadata = {}
    if commission is not None:
        metadata["commission"] = str(commission)

    return create_envelope(
        reason="test",
        date=date(2024, 1, 15),
        narration="Buy shares",
        outbound_units=Decimal(str(amount)),
        outbound_type="GBP",
        outbound_account="Assets:Broker:Cash",
        inbound_units=Decimal(str(quantity)),
        inbound_type=commodity,
        inbound_account="Assets:Broker:Stocks",
        transaction_type="BUY",
        source_type=source_type,
        envelope_id=envelope_id,
        metadata=metadata,
    )


def test_buy_with_commission_includes_fee_in_cost_basis():
    """Cost basis should include commission: (principal + fee) / quantity."""
    # Buy 10 shares, principal £1000, commission £10
    # Expected cost per unit: (1000 + 10) / 10 = 101
    envelope = _make_buy_envelope(quantity=10, amount=1000, commission=10)

    processor = EnvelopeLotProcessor()
    results, warnings = processor.process_envelopes([envelope])

    assert len(results) == 1
    result = results[0]
    assert result.unit_price == Decimal("101")

    # Lot should also have the fee-inclusive cost
    lot = result.metadata["lot_created"]
    assert lot["cost_per_unit"] == Decimal("101")

    # outbound_units should be adjusted to include commission
    assert result.outbound_units == Decimal("1010")


def test_buy_without_commission_unchanged():
    """Without commission, cost basis is just principal / quantity."""
    envelope = _make_buy_envelope(quantity=10, amount=1000)

    processor = EnvelopeLotProcessor()
    results, warnings = processor.process_envelopes([envelope])

    assert len(results) == 1
    result = results[0]
    assert result.unit_price == Decimal("100")
    assert result.outbound_units == Decimal("1000")


def test_manual_envelope_commission_not_double_counted():
    """Manual (beancount) envelopes already include commission in outbound_units.

    Adding commission again would double-count, so it must be skipped.
    """
    # Manual envelope: outbound_units = 1010 (already includes £10 commission)
    envelope = _make_buy_envelope(
        quantity=10, amount=1010, commission=10, source_type="beancount"
    )

    processor = EnvelopeLotProcessor()
    results, warnings = processor.process_envelopes([envelope])

    assert len(results) == 1
    result = results[0]
    # Should be 1010/10 = 101, NOT (1010+10)/10 = 102
    assert result.unit_price == Decimal("101")
    assert result.outbound_units == Decimal("1010")


def test_buy_with_small_commission():
    """Typical real-world case: small commission relative to principal."""
    # Buy 25 shares at £49.50, principal £1237.50, commission £7.99
    envelope = _make_buy_envelope(quantity=25, amount="1237.50", commission="7.99")

    processor = EnvelopeLotProcessor()
    results, warnings = processor.process_envelopes([envelope])

    result = results[0]
    # (1237.50 + 7.99) / 25 = 1245.49 / 25 = 49.8196
    expected_cost = (Decimal("1237.50") + Decimal("7.99")) / Decimal("25")
    assert result.unit_price == expected_cost
    assert result.outbound_units == Decimal("1245.49")


def test_lot_registry_has_fee_inclusive_cost():
    """Lot registry should reflect the fee-inclusive cost per unit."""
    envelope = _make_buy_envelope(quantity=10, amount=1000, commission=10)

    processor = EnvelopeLotProcessor()
    processor.process_envelopes([envelope])

    registry = processor.get_lot_registry()
    lots = registry["lot_registry"]["Assets:Broker:Stocks:VWRL"]
    assert len(lots) == 1
    assert lots[0]["cost_per_unit"] == "101"
