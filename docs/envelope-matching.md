# Envelope Matching

Transfer matching is the hardest problem in automated import. A £500 outflow from HSBC could be a bill payment, a transfer to Starling, or an ISA subscription — and the system must figure out which, using incomplete information from multiple CSV files that describe the same money movement differently.

Cassoulet uses a **scoring-based approach** with three distinct stages: Score, Match, Merge.

## Why Not Hash-Based Matching?

Early versions used hash-based matching: compute a transfer ID from amount + date, look for an identical hash on the other side. This failed in practice because:

1. **Dates don't align** — bank A debits on Monday, bank B credits on Wednesday. Sometimes the credit arrives *before* the debit (reverse settlement).
2. **Amounts don't match exactly** — fees, rounding, or currency conversion create small differences.
3. **No disambiguation** — when two accounts at the same broker receive transfers of the same amount, a hash can't distinguish which is which.
4. **Binary decisions** — a hash either matches or doesn't. There's no "this is a 90% match, that's a 60% match."

## The Scoring Architecture

### Stage 1: TransferScorer

The scorer evaluates every eligible envelope pair within a configurable date window (default: 30 days). For each pair, it computes scores across multiple dimensions using declarative patterns:

```python
TRANSFER_SCORING_PATTERNS = {
    # Anti-patterns (kill bad matches)
    'amounts_incompatible': {
        'patterns': [{'amount_match_quality': '0-39'}],
        'metadata': {'score': -500}
    },

    # Date proximity (separate rules for cash vs commodity transfers)
    'same_day_match': {
        'patterns': [{'date_gap': '0'}],
        'metadata': {'score': 40}
    },
    'next_day_match': {
        'patterns': [{'date_gap': '1'}],
        'metadata': {'score': 35}
    },

    # Amount precision
    'exact_amount_match': {
        'patterns': [{'amount_match_quality': '95-100'}],
        'metadata': {'score': 50}
    },

    # Account compatibility
    'known_transfer_pair': {
        'patterns': [{'accounts_are_known_pair': True}],
        'metadata': {'score': 30}
    },
}
```

**Design principles:**

- **Non-overlapping ranges** prevent unintended score compounding. Date gap `0` and date gap `1` are separate rules, not `<=1`.
- **Anti-patterns use large negative scores** to kill obviously bad matches. An amount mismatch of -500 overwhelms any positive signals.
- **Separate date tolerances** for cash transfers (0-4 days typical) versus commodity/in-specie transfers (5-30 days normal for broker-to-broker).
- **Every matched pattern** is recorded in envelope metadata for full transparency.

### Stage 2: TransferMatcher

The matcher takes scored candidates and makes decisions:

1. **Sort by score** — highest-scoring candidate wins
2. **Resolve conflicts** — when envelope A's best match is envelope B, but B's best match is envelope C (a "love triangle"), the matcher picks the globally optimal assignment
3. **Apply confidence thresholds** — matches below a minimum score are rejected
4. **Block asymmetric matches** — both sides must consider each other viable candidates

```python
@dataclass
class EnvelopeMatchDecision:
    env1_id: str
    env2_id: str
    score: float
    confidence: str           # 'high', 'medium'
    source_pattern: str       # 'manual-csv', 'csv-csv'
    comparison_mode: str      # 'transfer', 'reconciliation'
```

### Stage 3: TransferMerger

The merger combines matched pairs using the correct strategy, determined by a simple test: **do the envelopes share an account?**

## Reconciliation vs Aggregation

This is a critical distinction that most import tools get wrong.

### Reconciliation: Two Views of the Same Transaction

A user enters a manual transaction for a broker purchase. Later, the CSV import finds the same transaction. These are two descriptions of the same event — they share at least one account.

```
Manual:  2024-01-15 "Buy VWRL"
         Assets:Broker:HL:ISA  10 VWRL {150.00 GBP}
         Assets:Broker:HL:ISA  -1500.00 GBP

CSV:     2024-01-15 "B549475583" "Vanguard FTSE All-World"
         outbound: 1500.00 GBP from Assets:Broker:HL:ISA
         inbound: 10 VWRL to Assets:Broker:HL:ISA
```

**Reconciliation strategy:**
- Manual structure is preserved (it has richer data)
- CSV metadata is added (transaction reference, source file)
- No new postings created — just metadata enrichment
- Amounts must match exactly (no transfer leakage)

### Aggregation: Two Partial Legs Become One Transaction

HSBC debits £500 on Monday. Starling credits £500 on Tuesday. These are two halves of the same transfer, from different institutions with no shared account.

```
HSBC CSV:     2024-01-15 "STARLING BANK"
              outbound: 500.00 GBP from Assets:Bank:HSBC:Checking

Starling CSV: 2024-01-16 "J SPRIGGINS"
              inbound: 500.00 GBP to Assets:Bank:Starling:Personal
```

**Aggregation strategy:**
- Create a single transaction with both legs
- Use the earlier date (or the "sovereign" — the side that initiated the transfer)
- If amounts differ slightly, add a transfer leakage posting for the difference
- Mark as cross-institution transfer for dual file placement

### The Discriminator

The `shared_flow_account()` utility is the key test:

```python
def shared_flow_account(env1, env2):
    """Return the account shared between two envelopes, or None."""
    accounts_1 = {env1.outbound_account, env1.inbound_account} - {None}
    accounts_2 = {env2.outbound_account, env2.inbound_account} - {None}
    shared = accounts_1 & accounts_2
    return shared.pop() if shared else None
```

If they share an account → reconciliation. If they don't → aggregation. Using the wrong strategy causes double-counting (treating reconciliation as aggregation) or missing transfers (treating aggregation as reconciliation).

## No-Match Patterns

Some transactions look like transfers but aren't. Without explicit handling, the scorer will try to match them:

- HMRC tax relief paid into a SIPP (looks like an incoming transfer)
- Employer pension contributions
- Government benefits (DWP, child benefit)
- Platform fee rebates
- Internal bank movements (Monzo pots, Starling spaces)

No-match patterns flag these at import time:

```python
NO_MATCH_PATTERNS = {
    'sipp_tax_relief': {
        'name': 'SIPP Tax Relief',
        'reason': 'Government tax relief, not a transfer between your accounts',
        'patterns': [
            {'narration': 'tax relief', 'account': '*SIPP*', 'amount': 'positive'},
            {'narration': 'HMRC*', 'account': '*SIPP*', 'amount': 'positive'},
        ],
        'metadata': {
            'transfer-eligible': False,
            'dont-match-reason': 'HMRC tax relief payment',
        }
    },
}
```

Flagged envelopes are skipped entirely by the transfer scorer — no scoring computation, no false matches.

## ISA/SIPP Disambiguation

When a bank sends money to a broker that offers both ISA and SIPP accounts, the system must determine which account received the funds. The bank's CSV says "AJ BELL" — it doesn't say which wrapper.

The scorer uses narration analysis for disambiguation:

```python
isa_keywords = ['isa', 'subscription', 'debit card subscription']
sipp_keywords = ['sipp', 'pension', 'contribution']
```

If the HSBC narration says "AJ BELL ISA SUBSCRIPTION", the match score for the ISA account gets a boost. If it says "AJ BELL SIPP", the SIPP account wins. Without these clues, the system relies on other scoring dimensions and flags low-confidence matches for review.

## Bidirectional Date Tolerance

Real-world transfers don't always follow the expected direction. Normally, the debit appears first and the credit follows. But reverse settlement happens:

```
July 30: AJ Bell ISA receives £10,000  (credit first!)
Aug  2:  HSBC sends £10,000 to AJ Bell (debit follows)
```

The scoring patterns handle this naturally — `date_gap` is absolute, and separate patterns exist for different gap sizes. Cash transfers get strict tolerances (>10 days kills the match), while in-specie commodity transfers get generous ones (broker-to-broker takes weeks).

## Transparency

Every scoring decision is preserved in the envelope's metadata:

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
    }
  ]
}
```

This makes debugging straightforward: if a match is wrong, you can see exactly which patterns fired and why. If a match was missed, you can see what scored too low.
