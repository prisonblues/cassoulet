# Cost Basis Tracking

Cost basis is the original purchase price of a security, used to calculate capital gains when sold. Getting it right is essential for tax reporting. Getting it wrong means silent errors that compound through every subsequent transaction.

Cassoulet handles several cost basis challenges that Beancount's native lot tracking can't solve on its own.

## The Cross-Year Problem

Beancount uses FIFO (First In, First Out) booking with empty cost syntax `{}` to automatically match sells against the oldest purchase:

```beancount
; 2019 Purchase
2019-03-25 * "Buy VWRL"
  Assets:Broker:HL:ISA  10.0000 VWRL {150.00 GBP}
  Assets:Broker:HL:ISA  -1500.00 GBP

; 2020 Sale — empty {} triggers FIFO booking
2020-02-10 * "Sell VWRL"
  Assets:Broker:HL:ISA  -5.0000 VWRL {}
  Assets:Broker:HL:ISA   800.00 GBP
```

This works when both transactions are in the same file or visible in the same include chain. But when files are organized by year, Beancount can't see the 2019 purchase when processing the 2020 sale.

**What goes wrong:**
1. Importer correctly creates the sell with empty `{}`
2. Import script loads the 2020 file for processing
3. Beancount can't find the 2019 purchase (different year file, not included)
4. Beancount creates a phantom lot using the *sale price* as cost basis
5. File is rewritten with wrong explicit costs

**The solution:** Cassoulet's `SellTransactionProcessor` looks up original purchase costs from the full ledger *before* the file is rewritten:

```python
# Query the complete ledger for cost basis
original_cost = cost_basis_lookup.get_cost(
    account="Assets:Broker:HL:ISA",
    commodity="VWRL",
    quantity=Decimal("5.0000"),
    as_of_date=sale_date
)

# Create sell with explicit matching cost instead of empty {}
envelope.metadata['cost_per_unit'] = original_cost.cost_per_unit
envelope.metadata['cost_date'] = original_cost.acquisition_date
```

## In-Specie Transfers

In-specie transfers move securities between broker accounts without selling them. The cost basis must be preserved exactly — this is not a taxable event.

### Beancount's Limitation

Beancount cannot process multiple empty cost specifications in the same transaction:

```beancount
; This FAILS — Beancount can't handle {} on both sides
2021-08-06 * "In-specie transfer"
  Assets:Broker:Destination   100 VWRL {}
  Assets:Broker:Source       -100 VWRL {}
```

We tested seven different approaches:

| Approach | Result |
|----------|--------|
| Both empty `{}` | Fails — Beancount limitation |
| Source `{}` + destination no cost | Doesn't balance |
| No cost specifications | Loses all cost basis |
| Market price costs | Wrong cost basis, creates tax implications |
| Split buy/sell | Looks like a taxable disposal |
| Three-legged intermediate | Phantom cash flows |
| **Explicit matching costs** | **Works correctly** |

### The Only Correct Solution

```beancount
2021-08-06 * "In-specie transfer"
  Assets:Broker:Destination   100.000 VWRL {150.00 GBP, 2019-03-25}
  Assets:Broker:Source       -100.000 VWRL {150.00 GBP, 2019-03-25}
```

Both postings use the exact original cost and acquisition date. This:
- Preserves cost basis perfectly
- Doesn't look like a taxable event (no cash involved)
- Works with subsequent FIFO sales
- Maintains a clear audit trail

### Automated Cost Lookup

Cassoulet automates this by looking up the original purchase lots:

```python
def get_lifo_lots(self, account, commodity, quantity, as_of_date):
    """Query holdings BEFORE transfer date for accurate lot information."""
    # Critical: filter by date to see holdings as they were
    date_filter = f' AND date <= "{as_of_date}"'
    query = f'SELECT date, position, account WHERE account = "{account}" ...'
```

For transfers that span multiple lots, the system selects lots using LIFO ordering and handles partial lots:

```python
# Transfer 75 units — requires LIFO selection across lots:
# Holdings: 30@£200 (newest) + 40@£180 + 30@£150 (oldest)
# LIFO result: 30@£200 + 40@£180 + 5@£150 (partial lot)
for lot in lifo_lots:
    needed = min(lot.quantity, remaining_quantity)
    if needed > 0:
        result_lots.append(LotInfo(quantity=needed, cost=lot.unit_cost))
        remaining_quantity -= needed
```

### Fuzzy Date Matching

Transfer-out and transfer-in dates rarely match exactly. Broker A records the debit on August 6, Broker B records the credit on August 18. The system uses a ±14 day window:

```python
min_date = transfer_date - timedelta(days=14)
max_date = transfer_date + timedelta(days=14)
```

## Why Cost Basis Must Happen Mid-Pipeline

A natural question: why not process all transactions first, then do a simple cost-basis lookup at the end?

Because cost basis is not a final detail — it's **critical metadata that other pipeline stages need:**

1. **Transfer reconciliation** needs cost basis to confirm that `Transfer Out 100 VWRL` from Broker A is the same event as `Transfer In 100 VWRL` at Broker B. Matching costs = strong match signal.

2. **The cost basis itself** requires looking at historical purchases. Those might be in the committed ledger (on disk) or from earlier in the current import run (in memory).

3. **Therefore:** cost basis lookup must happen *before* transfer reconciliation, creating a "mixed state" of on-disk and in-memory data.

This mixed state is an architectural necessity, not a hack. The bugs we encountered (context contamination, phantom lots) were caused by incorrectly managing the state, not by the state itself.

## The Lot Registry

The `EnvelopeLotProcessor` maintains an in-memory lot registry:

```python
# Registry: (account, commodity) → list of lots
lots: Dict[Tuple[str, str], List[dict]]

# Each lot tracks:
{
    'date': date(2019, 3, 25),       # Acquisition date
    'quantity': Decimal('10.0000'),    # Current quantity
    'cost_per_unit': Decimal('150.00'), # Per-unit cost
    'envelope_id': 'hl_isa_20190325_abc'
}
```

The registry processes events chronologically:
- **BUY** creates a new lot
- **SELL** consumes lots in FIFO order
- **Stock split** transforms all lots (see [Corporate Actions](corporate-actions.md))
- **In-specie transfer** moves lots between accounts

Every lot movement is tracked in `lot_movements` for debugging.

## Precision

Financial calculations use Python's `Decimal` throughout — never floating point. Display formatting uses `Precision.MAXIMUM` to preserve source precision:

```python
formatter = dcontext.build(precision=Precision.MAXIMUM)
```

When multiple data sources exist with different precision levels, always trust the most precise:
- **CSV exports** — actual book records with full precision
- **PDF statements** — display-formatted with rounding
- **API data** — usually matches or exceeds CSV precision

A real case: AJ Bell PDF statements only show whole pounds (no pennies), while CSV files contain exact penny amounts. Using PDFs for balance assertions caused 12 failures that looked like complex architectural issues but were simply precision mismatches.
