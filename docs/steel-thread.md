# Steel Thread Architecture

The steel thread architecture is cassoulet's answer to a fundamental problem with financial data pipelines: **silent failures.** When a processor drops a transaction, the system doesn't crash — it just produces wrong numbers. You might not notice for months, until a balance assertion fails or a tax return is wrong.

Cassoulet enforces three guarantees, verified through structured JSON audit trails.

## The Three Guarantees

### 1. Zero Silent Failures

Every decision made by every processor is logged. If a transaction is modified, skipped, or dropped, there's an explicit record of what happened and why.

```python
# Wrong: silently discard on failure
def process_sell(self, envelope):
    cost = self.lookup_cost(envelope)
    if cost is None:
        return None  # Transaction vanishes!

# Right: preserve and warn
def process_sell(self, envelope):
    cost = self.lookup_cost(envelope)
    if cost is None:
        envelope.warnings.append("Cost basis lookup failed — using empty {}")
        return envelope  # Transaction preserved, issue visible
```

This principle was discovered the hard way. An early version of the `SellTransactionProcessor` silently discarded sells when cost basis lookup failed. The result: zero-balance commodity accounts with no indication anything was wrong. The balance errors only appeared downstream, disconnected from their cause.

### 2. Complete Data Integrity

Every processor must account for every transaction it receives. Input count must equal output count, unless a transaction is explicitly merged or discarded with a reason.

```python
class EnvelopeProcessor:
    def process(self, envelopes):
        result = self._process_internal(envelopes)
        processed, warnings = result

        # Integrity check
        if len(processed) != len(envelopes):
            self.monitor.record_violation(
                severity="CRITICAL",
                message=f"Data loss: {len(envelopes)} in, {len(processed)} out"
            )

        return processed, warnings
```

### 3. Total Operational Transparency

The complete lifecycle of every transaction is available for inspection. Not just the final result, but every intermediate decision, every score, every match attempt.

## The 3-Tuple Contract

All processors return `(envelopes, warnings)`:

```python
class EnvelopeProcessor:
    def _process_internal(self, envelopes) -> Tuple[List[Envelope], List[Warning]]:
        """Subclasses implement this."""
        raise NotImplementedError
```

Warnings are typed with severity levels:

| Severity | Meaning | Appears In |
|----------|---------|------------|
| **CRITICAL** | Architecture violation, potential data loss | `_errors.log`, forensic report |
| **ERROR** | Business logic failure (cost basis not found) | `_errors.log`, forensic report |
| **WARNING** | May need attention (low confidence match) | Forensic report |
| **INFO** | Normal processing (successful match) | Forensic report |

## The Steel Thread Monitor

The `SteelThreadMonitor` provides runtime circuit-breaker functionality:

```python
monitor = SteelThreadMonitor(
    critical_threshold=5,     # Trip after 5 CRITICAL violations
    error_threshold=20,       # Trip after 20 ERROR violations
    enable_circuit_breaker=True
)
```

When thresholds are exceeded, the monitor raises `CriticalViolationError` and halts the pipeline. This prevents cascading failures from producing a ledger full of wrong data.

The monitor tracks:
- Violation counts by severity and processor
- Total transactions and postings processed
- Empty transaction count
- Data loss events

## Multi-Log Output

Every import run produces five log files plus structured JSON:

| File | Audience | Content |
|------|----------|---------|
| `importer_*.log` | Developer | Complete processing log |
| `*_summary.log` | User | Statistics and counts |
| `*_errors.log` | User | Bean-check errors + CRITICAL/ERROR warnings |
| `*_error_summary.log` | User | Error counts grouped by type |
| `*_forensic.md` | Investigator | Detailed root cause analysis |

### Structured JSON (Primary Debugging Tool)

The JSON files are the most powerful debugging resource:

**`all_envelopes_full.json`** — complete envelope lifecycle:
```json
{
  "envelope_id": "hsbc_checking_20240115_abc",
  "state": "reconciled",
  "date": "2024-01-15",
  "narration": "STARLING BANK",
  "outbound_units": "500.00",
  "outbound_account": "Assets:Bank:HSBC:Checking",

  "classification": "transfer",
  "classification_confidence": 0.92,

  "history": [
    {
      "state": "ingested",
      "component": "HSBCImporter",
      "action": "Created from CSV row",
      "details": {"source_file": "jack_hsbc_checking.csv", "line": 5}
    },
    {
      "state": "classified",
      "component": "TransferScorer",
      "action": "Scored as transfer candidate",
      "details": {"score": 125, "best_match": "starling_20240116_def"}
    }
  ],

  "warnings": []
}
```

**`canonical_transactions.json`** — final Beancount transactions as written.

**`error_summary.log`** — categorized error analysis:
```
ReductionError: 12 instances
  - Missing purchase for SELL (cost basis not found in history)
BalanceError: 3 instances
  - Assets:Broker:HL:ISA off by 0.01 GBP (precision mismatch)
```

## Error Categorization

The error reporter automatically classifies validation errors:

| Category | Meaning | Typical Cause |
|----------|---------|---------------|
| **ReductionError** | Sell without matching purchase | Missing transaction history |
| **BalanceError** | Balance assertion mismatch | Data quality, precision |
| **BookingError** | Invalid cost specification | Wrong cost syntax |
| **DuplicateError** | Duplicate transaction detected | Non-unique IDs |

## Debugging Recipes

### Find a specific envelope
```bash
jq '.[] | select(.envelope_id == "hsbc_checking_20240115_abc")' \
   logs/*/all_envelopes_full.json
```

### Find all envelopes with warnings
```bash
jq '.[] | select(.warnings | length > 0)' \
   logs/*/all_envelopes_full.json
```

### Count envelopes by state
```bash
jq '[.[] | .state] | group_by(.) | map({state: .[0], count: length})' \
   logs/*/all_envelopes_full.json
```

### Find low-confidence matches
```bash
jq '.[] | select(.classification_confidence < 0.70)' \
   logs/*/all_envelopes_full.json
```

### Trace data loss
```bash
# Compare input vs output counts per processor
jq '.[] | select(.metadata.input_count != .metadata.output_count) |
    {processor: .processor_name, lost: (.metadata.input_count - .metadata.output_count)}' \
   logs/*/all_envelopes_full.json
```

### Find all transactions for an account
```bash
jq --arg acc "Assets:Broker:HL:ISA" \
   '.[] | select(.outbound_account == $acc or .inbound_account == $acc)' \
   logs/*/all_envelopes_full.json
```

## Lessons Learned

### Silent data loss is the worst bug

The `SellTransactionProcessor` silently dropped transactions when cost basis lookup failed. 20 monthly transfers vanished with no errors. The only symptom was a £220 balance discrepancy discovered months later.

**Fix:** processors must never return `None`. Return the original envelope with a warning.

### Non-deterministic IDs corrupt entire systems

Python's `hash()` is non-deterministic across sessions. Eight files were using it for transaction IDs. The same transaction got different IDs each run, defeating deduplication — some transactions appeared twice, others disappeared.

**Fix:** SHA256-based deterministic IDs. An automated test scans for `hash()` usage to prevent regression.

### Validate final state, not intermediates

An early version validated output files before the year-file updater ran. The validator saw a ledger that didn't include the files just written, producing false errors.

**Fix:** validation runs only after all pipeline stages complete and the ledger is in its final state.

### Component audits, not just orchestration

Fixing data flow between pipeline stages is necessary but not sufficient. Each component's internal failure modes must be audited: what does it do with bad input? Incomplete data? Missing dependencies?

**Fix:** negative-path testing. For every critical component, test what happens when it receives data that triggers its error paths.
