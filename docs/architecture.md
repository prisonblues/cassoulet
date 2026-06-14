# Architecture

Cassoulet is built around a single insight: **don't create Beancount postings until you have complete information.** Most import tools create `Transaction` objects immediately from CSV rows, then try to fix them later. This causes wrong accounts, missing cost basis, and silent data loss. Cassoulet wraps every transaction in an `Envelope` and defers all posting creation to the final output stage.

## The Envelope

Every transaction flows through the pipeline inside an `Envelope` dataclass:

```
CSV Row → Envelope(INGESTED) → CLASSIFIED → GROUPED → RECONCILED → ENHANCED → WRITTEN
```

An envelope carries:
- **Financial data** as directional flow (inbound/outbound units, types, accounts)
- **Processing history** — a complete audit trail of every decision made
- **Warnings** — accumulated throughout the pipeline, never swallowed
- **Metadata** — source file, line number, confidence scores, match details
- **State** — where in the lifecycle this envelope currently sits

```python
@dataclass
class Envelope:
    # Transaction data (NO Beancount objects until the very end)
    date: date
    narration: str
    payee: Optional[str] = None

    # Financial data — the inbound/outbound pattern
    outbound_units: Optional[Decimal] = None
    outbound_type: Optional[str] = None      # Currency or commodity
    outbound_account: Optional[str] = None
    inbound_units: Optional[Decimal] = None
    inbound_type: Optional[str] = None
    inbound_account: Optional[str] = None
    unit_price: Optional[Decimal] = None     # For BUY/SELL

    # Additional postings for 3+ posting transactions
    additional_postings: List[Dict[str, Any]] = field(default_factory=list)

    # Complete lifecycle tracking
    state: EnvelopeState = EnvelopeState.INGESTED
    history: List[HistoryEntry] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
```

## The Inbound/Outbound Pattern

Instead of guessing which Beancount accounts and postings to create, envelopes store financial data as directional flow. This representation is universal:

| Transaction Type | Outbound | Inbound |
|-----------------|----------|---------|
| **BUY** | Cash (GBP) | Commodity (shares) |
| **SELL** | Commodity (shares) | Cash (GBP) |
| **Transfer** | From account (cash) | To account (cash) |
| **Income** | — | Cash |
| **Expense** | Cash | — |

```python
# A stock purchase
envelope.outbound_units = Decimal("1500.00")
envelope.outbound_type = "GBP"
envelope.outbound_account = "Assets:Broker:HL:ISA"
envelope.inbound_units = Decimal("10.5")
envelope.inbound_type = "VWRL"
envelope.inbound_account = "Assets:Broker:HL:ISA"
envelope.unit_price = Decimal("142.86")
```

The key benefit: the system knows the full financial picture before deciding how to create postings. No guessing, no fixing later.

## Deferred Posting

**No Beancount `Transaction` or `Posting` objects exist until the final output stage.**

This is the core architectural decision. Early versions created postings immediately from CSV rows, which caused:

1. **Wrong accounts** — the system guessed transfer destinations before knowing them from matching
2. **Missing cost basis** — sell transactions couldn't reference purchases from earlier in the same import
3. **Premature decisions** — classification committed to a transaction structure before seeing the full picture

The fix: defer everything. The pipeline processes envelopes through scoring, matching, and merging with complete information. Only at the very end does `PostingWriter` create Beancount transactions, once, correctly.

```
Phase 1: CSV → Envelope (no Beancount objects)
Phase 2: Score potential transfer matches
Phase 3: Match and merge envelopes
Phase 4: Enhance with cost basis, metadata
Phase 5: PostingWriter creates Beancount Transaction (the only time)
```

## Pipeline Phases

### Phase 1: Ingestion

Each institution importer reads CSV rows and creates envelopes with financial data. No classification, no matching, just raw observation.

At this stage, the system applies "no-match" patterns — rules that identify transactions which should never be matched as transfers (HMRC tax relief, employer pension contributions, government benefits). These are flagged early because they look like transfers but aren't.

### Phase 2: Transfer Scoring

The `TransferScorer` evaluates every eligible envelope pair within a date window, computing a composite match score across multiple dimensions:

- **Amount compatibility** — do the amounts match?
- **Date proximity** — how close are the dates?
- **Account compatibility** — do the accounts make sense as a transfer pair?
- **Narration clues** — do the descriptions reference each other?

Scoring is parallelized across CPU cores. Each pair gets a detailed score breakdown stored in envelope metadata for full transparency.

### Phase 3: Matching and Merging

The `TransferMatcher` takes scored candidates and makes matching decisions, resolving conflicts when multiple envelopes compete for the same match. The `TransferMerger` then combines matched pairs using the correct strategy:

- **Reconciliation** for two views of the same transaction (manual entry + CSV)
- **Aggregation** for two partial legs (bank A debit + bank B credit)

See [Envelope Matching](envelope-matching.md) for the full details.

### Phase 4: Enhancement

Post-matching processors enrich envelopes with:
- Cost basis lookup for sell transactions
- Expense categorization from pattern matching
- Lot tracking for investment transactions
- Corporate action processing (stock splits)

### Phase 5: Output

`PostingWriter` creates Beancount `Transaction` objects from envelopes and writes them to year-organized output files. This is the only point where Beancount objects are created.

## Additional Postings

Real-world transactions sometimes have 3+ postings (commissions, fees, tax withholding). The envelope's binary inbound/outbound pattern handles the main flow, while an `additional_postings` array carries the extras:

```python
# Stock purchase with commission
envelope.outbound_units = Decimal("1500.00")    # Cash
envelope.outbound_type = "GBP"
envelope.inbound_units = Decimal("10.5")        # Shares
envelope.inbound_type = "VWRL"

envelope.additional_postings = [
    {"units": Decimal("11.95"), "type": "GBP", "account": "Expenses:Commissions"}
]
```

The pipeline processes the main flow normally. Additional postings pass through untouched and are reconstructed by `PostingWriter` at the end.

## Configuration

Account metadata drives importer configuration — a single source of truth:

```beancount
2007-01-01 open Assets:Bank:HSBC:Checking   GBP
  institution:        "HSBC UK"
  importer:           "HSBCImporter"
  file_identifier:    "jack_hsbc_checking.csv"
```

The `ProjectPaths` dataclass centralizes all file paths:

```python
paths = ProjectPaths(
    accounts_file="accounts/accounts.beancount",
    commodities_file="commodities/commodities.beancount",
    raw_data_dir="entries/raw",
    output_dir="entries/output",
)
```

No framework defaults baked in — your project provides all paths.

## Extension Points

- **Custom processors** — add business-specific logic via `PipelineConfig.custom_processors`
- **Expense patterns** — inject categorization rules via `PipelineConfig.expense_patterns`
- **No-match patterns** — define transactions that should never be transfer-matched
- **Transfer scoring patterns** — declarative rules for match quality assessment

See the README for examples of each.
