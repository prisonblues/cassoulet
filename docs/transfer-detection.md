# Transfer Detection

Transfer detection identifies when money moves between your own accounts. A debit from HSBC and a credit to Starling two days later, for the same amount, describing the same person — that's a transfer, not two independent transactions.

Getting this right is critical. False positives create phantom money flows. False negatives leave orphaned half-transactions that break balance assertions.

## Two Types of Transfer

### Cash Transfers

Money moves between bank and/or broker accounts:

```
HSBC:     -500.00 GBP  "STARLING BANK"       (Jan 15)
Starling: +500.00 GBP  "J SPRIGGINS"         (Jan 16)
```

Characteristics:
- Same currency (always)
- Same or very close amounts
- Short date gap (0-4 days typical)
- Narrations reference the other institution or account holder

### Commodity (In-Specie) Transfers

Securities move between broker accounts without being sold:

```
HL ISA:   -100 VWRL    "Transfer Out"         (Aug 6)
AJ Bell:  +100 VWRL    "Transfer In - VWRL"   (Aug 18)
```

Characteristics:
- Same commodity and quantity
- Longer date gaps (5-30 days is normal for broker-to-broker)
- Must preserve original cost basis through the transfer
- Cannot use Beancount's `{}` syntax on both sides (see [Cost Basis](cost-basis.md))

## The Scoring Pipeline

### 1. Eligibility Filtering

Before scoring, ineligible envelopes are excluded:

**No-match patterns** flag transactions that look like transfers but aren't:
- HMRC tax relief into a SIPP
- Employer pension contributions
- Government benefits (DWP, child benefit)
- Internal bank movements (pot transfers)

**Manual overrides** allow explicit control:
- `skip-transfer-detection: "true"` — never match this envelope
- `match-target: "envelope_id"` — force a specific match
- `override-target: "account"` — force a specific destination account

### 2. Candidate Selection

For each eligible envelope, the scorer finds candidates within a date window:

```python
window_candidates = find_envelopes_in_date_window(
    envelope, all_envelopes, window_days=30
)
```

Candidates must pass basic compatibility checks before detailed scoring:
- They can be aggregated (no shared account) or reconciled (shared account)
- They haven't already been matched
- They're transfer-eligible

### 3. Pattern-Based Scoring

Each candidate pair is scored against declarative patterns:

```python
TRANSFER_SCORING_PATTERNS = {
    # === Anti-Patterns (kill bad matches) ===
    'amounts_incompatible': {
        'patterns': [{'amount_match_quality': '0-39'}],
        'metadata': {'score': -500}
    },
    'excessive_date_gap_cash': {
        'patterns': [{'date_gap': '>10', 'is_potential_in_specie_transfer': False}],
        'metadata': {'score': -1000}
    },

    # === Positive Signals ===
    'exact_amount_match': {
        'patterns': [{'amount_match_quality': '95-100'}],
        'metadata': {'score': 50}
    },
    'same_day_match': {
        'patterns': [{'date_gap': '0'}],
        'metadata': {'score': 40}
    },
    'known_transfer_pair': {
        'patterns': [{'accounts_are_known_pair': True}],
        'metadata': {'score': 30}
    },
}
```

**Pattern design rules:**
- Use non-overlapping ranges to prevent unintended score compounding
- Anti-patterns use large negative scores that overwhelm positive signals
- Cash and commodity transfers have separate date gap rules
- Higher scores = stronger match confidence

### 4. Parallel Execution

Scoring is CPU-intensive (every eligible envelope against every candidate in its date window). The scorer parallelizes across cores:

```python
_NUM_WORKERS = max((os.cpu_count() or 1) * 3 // 4, 1)
```

Module-level shared state is inherited by forked workers via copy-on-write — no pickling overhead.

## Edge Cases Solved

### Reverse Settlement

Sometimes the credit arrives before the debit:

```
July 30: AJ Bell receives £10,000  (credit first!)
Aug  2:  HSBC sends £10,000        (debit follows)
```

The scorer handles this naturally — `date_gap` is the absolute difference between dates. Separate patterns penalize large gaps without requiring a specific direction.

### ISA/SIPP Disambiguation

When a bank sends money to a broker offering multiple wrappers (ISA and SIPP), the CSV just says "AJ BELL" — which account?

The scorer analyzes narrations for clues:

| Narration Contains | Inferred Account |
|-------------------|-----------------|
| "ISA", "subscription" | ISA |
| "SIPP", "pension", "contribution" | SIPP |

These clues boost the match score for the corresponding account. Without clues, the system relies on other dimensions and flags low-confidence matches.

### Amount Tolerance

Cash transfers should match exactly — fee tolerance causes false matches. A real case: £10,000 to AJ Bell was incorrectly matched with £9,998 to Coinbase because 2% tolerance allowed it. The correct £10,000 match was ignored.

**Solution:** exact amount matching for cash transfers. The scoring pattern gives maximum points for 95-100% match quality and kills anything below 40%.

### Love Triangles

When envelope A's best match is B, but B's best match is C, the matcher must resolve globally:

```
A (£500 HSBC debit)  → best match: B (£500 Starling credit)
B (£500 Starling credit) → best match: C (£500 Monzo debit)
```

The matcher evaluates all competing claims and assigns matches to maximize total score.

## External Transfer Detection

Incoming transfers from outside your tracked accounts (pension rollovers, investment transfers from other providers) have no matching debit side. Rather than leaving them as orphaned half-transactions that create balance errors, the system classifies them as external capital transfers:

```beancount
2019-03-08 * "External Capital Transfer | Sipp Transfer (BACS)"
  Assets:Broker:HL:SIPP              140001.49 GBP
  Equity:Capital:External:HL:SIPP   -140001.49 GBP
```

This eliminates persistent balance errors while providing a clear audit trail for capital contributions.

## Scoring Transparency

Every decision is recorded. After scoring, each envelope's metadata contains:

```json
{
  "transfer_candidates": [
    {
      "candidate_id": "starling_20240116_abc",
      "score": 125,
      "patterns_matched": [
        {"name": "Exact Amount Match", "score": 50},
        {"name": "Next Day Match", "score": 35},
        {"name": "Known Transfer Pair", "score": 30},
        {"name": "Narration Reference", "score": 10}
      ]
    },
    {
      "candidate_id": "monzo_20240117_def",
      "score": 45,
      "patterns_matched": [
        {"name": "Close Amount Match", "score": 30},
        {"name": "Two Day Gap", "score": 15}
      ]
    }
  ]
}
```

If a match is wrong, you can see exactly which patterns fired. If a match was missed, you can see what scored too low and adjust the patterns.
