# Corporate Actions

Beancount has no native support for stock splits, mergers, spin-offs, or other corporate actions that transform existing lots. Users must manually calculate exact cost basis adjustments for every lot — a tedious, error-prone process that requires 30-digit decimal precision.

Cassoulet solves this with declarative corporate action directives that automatically transform lots in the registry.

## The Problem

A 10-for-1 stock split in Beancount requires manually specifying every lot transformation:

```beancount
2024-08-08 * "MSTR 10-for-1 stock split"
  Assets:Broker:HL:SIPP  -23.000000 MSTR {625.3443478260869565217391304 GBP, 2021-11-11}
  Assets:Broker:HL:SIPP  230.000000 MSTR {62.53443478260869565217391304 GBP, 2021-11-11}
  Assets:Broker:HL:SIPP  -23.000000 MSTR {406.2273913043478260869565217 GBP, 2022-01-05}
  Assets:Broker:HL:SIPP  230.000000 MSTR {40.62273913043478260869565217 GBP, 2022-01-05}
```

For each lot you must:
1. Look up the original purchase date and exact cost per unit
2. Calculate the new cost per unit (divide by split ratio) to full precision
3. Write a sell posting at the old cost and a buy posting at the new cost
4. Preserve the original acquisition date for tax purposes
5. Repeat for every lot in the account

With 10 lots this is 20 postings of manual calculation. Get a single digit wrong and subsequent FIFO sales break silently.

## The Solution: Declarative Directives

### Format A: Intent-Based (Recommended)

Specify what happened, let the system figure out the postings:

```beancount
2024-08-08 * "MSTR 10-for-1 stock split"
  corporate-action: "stock-split"
  split-commodity: "MSTR"
  split-account: "Assets:Broker:HL:SIPP"
  split-ratio: "10:1"
```

Three lines of metadata. The system:
1. Queries the lot registry for all MSTR lots in the account
2. For each lot: divides cost per unit by 10, multiplies quantity by 10
3. Preserves original acquisition dates
4. Generates correct postings with full decimal precision
5. Updates the lot registry so subsequent FIFO sales work

### Format B: Simplified Postings

If you prefer to show the gross quantities:

```beancount
2024-08-08 * "MSTR 10-for-1 stock split"
  Assets:Broker:HL:SIPP  -46 MSTR {}
  Assets:Broker:HL:SIPP  460 MSTR {}
```

The system detects this is a corporate action (same commodity, same account, quantities consistent with a split ratio), infers the ratio, and expands it into lot-level postings.

## How It Works

### The StockSplit Dataclass

```python
@dataclass
class StockSplit:
    date: date
    commodity: str
    account: str
    ratio: Decimal          # e.g., 10 for 10:1

    @classmethod
    def from_metadata(cls, txn, file_path=None):
        """Create from Format A (intent-based directive)."""
        ratio_str = txn.meta['split-ratio']      # "10:1"
        numerator, denominator = ratio_str.split(':')
        ratio = Decimal(numerator) / Decimal(denominator)
        return cls(date=txn.date, commodity=meta['split-commodity'], ...)

    @classmethod
    def from_postings(cls, txn, file_path=None):
        """Create from Format B (simplified postings)."""
        total_positive = sum(p.units.number for p in txn.postings if p.units.number > 0)
        total_negative = sum(abs(p.units.number) for p in txn.postings if p.units.number < 0)
        ratio = total_positive / total_negative
        return cls(date=txn.date, ...)
```

### Chronological Processing

The `EnvelopeLotProcessor` interleaves corporate actions with regular transactions in date order:

```python
def _process_internal(self, envelopes):
    sorted_envelopes = sorted(envelopes, key=lambda e: e.date)

    # Merge envelopes and corporate actions chronologically
    while envelope_idx < len(sorted_envelopes) or action_idx < len(self.corporate_actions):
        # Process whichever comes first by date
        if should_process_envelope:
            # BUY creates lots, SELL consumes lots (FIFO)
            self._process_envelope(envelope)
        else:
            # Stock split transforms existing lots
            self._apply_corporate_action(action)
```

This ensures that:
- Lots purchased before the split are correctly transformed
- Lots purchased after the split use post-split prices
- The split is applied at exactly the right point in time

### Lot Transformation

```python
def _apply_split(self, split: StockSplit):
    key = (split.account, split.commodity)
    lots = self.lots.get(key, [])

    for lot in lots:
        old_quantity = lot['quantity']
        old_cost = lot['cost_per_unit']

        lot['quantity'] = old_quantity * split.ratio
        lot['cost_per_unit'] = old_cost / split.ratio
        # Acquisition date is preserved — critical for tax purposes
```

### Generated Output

The `CorporateActionResult` generates posting data with full precision:

```python
def generate_posting_data(self):
    postings = []
    for old_lot, new_lot in self.lots_transformed:
        # Remove old shares at original cost
        postings.append({
            'account': self.action.account,
            'units': -old_lot['quantity'],
            'cost_per_unit': old_lot['cost_per_unit'],
            'cost_date': old_lot['date']       # Original acquisition date
        })
        # Add new shares at adjusted cost
        postings.append({
            'account': self.action.account,
            'units': new_lot['quantity'],
            'cost_per_unit': new_lot['cost_per_unit'],
            'cost_date': new_lot['date']       # Same acquisition date
        })
    return postings
```

## Why This Matters

1. **Precision** — the system computes cost basis to full `Decimal` precision. No rounding errors that silently compound through subsequent sales.

2. **Correctness** — acquisition dates are preserved through the split. A lot bought in 2021 and split in 2024 still shows a 2021 acquisition date for capital gains purposes.

3. **FIFO compatibility** — subsequent sell transactions using empty cost `{}` correctly match the post-split lots with their adjusted cost basis.

4. **Maintainability** — adding a stock split is 3 lines of metadata, not 20+ lines of manual lot calculations. When you get another split, you add another 3-line directive.

5. **Auditable** — the lot transformation history is tracked, showing exactly how each lot was modified by the corporate action.

## Supported Actions

Currently implemented:
- **Stock splits** (forward splits like 10:1, 2:1)
- **Reverse splits** (consolidations like 1:10)
- **Fractional splits** (e.g., 3:2)

The architecture supports future corporate action types (mergers, spin-offs, rights issues) through the same pattern: declarative intent → lot registry query → automatic transformation → posting generation.

## Pipeline Integration

Corporate actions are not standalone — they're woven into the envelope pipeline:

1. Manual entries with `corporate-action: "stock-split"` metadata are detected during ingestion
2. They're extracted from the envelope stream and passed to `EnvelopeLotProcessor`
3. The lot processor interleaves them chronologically with BUY/SELL envelopes
4. Transformed lots are available for subsequent SELL transactions' FIFO matching
5. Generated postings are written to the output files

The key guarantee: **the lot registry is always consistent.** A sell that happens one day after a split will see the post-split lots, not the pre-split ones.
