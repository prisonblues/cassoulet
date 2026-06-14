# Debugging Guide

Cassoulet generates structured JSON files on every import run. These are your primary debugging tools — more powerful than log files because they're queryable with `jq`.

## Output Files

Every run creates a timestamped directory under `logs/`:

```
logs/20240115_143022/
├── all_envelopes_full.json       # Complete envelope lifecycle data
├── canonical_transactions.json    # Final Beancount transactions
├── error_summary.log             # Categorized error analysis
├── importer_20240115_143022.log           # Full processing log
├── importer_20240115_143022_summary.log   # Statistics only
├── importer_20240115_143022_errors.log    # Validation errors
└── importer_20240115_143022_forensic.md   # Root cause analysis
```

## Envelope JSON Structure

Each entry in `all_envelopes_full.json` is a complete envelope lifecycle:

```json
{
  "envelope_id": "hsbc_checking_20240115_abc123",
  "source": "jack_hsbc_checking.csv",
  "source_type": "csv",
  "state": "written",

  "date": "2024-01-15",
  "narration": "STARLING BANK",
  "outbound_units": "500.00",
  "outbound_type": "GBP",
  "outbound_account": "Assets:Bank:HSBC:Checking",

  "classification": "transfer",
  "classification_confidence": 0.92,

  "reconciliation_set_id": "recon_xyz789",
  "metadata": {
    "matched_with": "starling_20240116_def456",
    "match_score": 125,
    "transfer_candidates": [
      {
        "candidate_id": "starling_20240116_def456",
        "score": 125,
        "patterns_matched": [
          {"name": "Exact Amount Match", "score": 50},
          {"name": "Next Day Match", "score": 35}
        ]
      }
    ]
  },

  "history": [
    {
      "timestamp": "2024-01-15T14:30:01",
      "state": "ingested",
      "component": "HSBCImporter",
      "action": "Created from CSV row",
      "details": {"source_file": "jack_hsbc_checking.csv", "line": 5}
    },
    {
      "timestamp": "2024-01-15T14:30:02",
      "state": "classified",
      "component": "TransferScorer",
      "action": "Scored as transfer candidate"
    },
    {
      "timestamp": "2024-01-15T14:30:03",
      "state": "reconciled",
      "component": "TransferMerger",
      "action": "Aggregated with starling_20240116_def456"
    }
  ],

  "warnings": []
}
```

## Common Debugging Scenarios

### Balance Error: Account Is Off By a Known Amount

A `BalanceError` means the computed balance doesn't match an assertion. Start by finding all transactions for the account:

```bash
# Step 1: Find all envelopes touching the account
account="Assets:Broker:HL:ISA"
jq --arg acc "$account" \
   '.[] | select(.outbound_account == $acc or .inbound_account == $acc) |
    {id: .envelope_id, date: .date, narration: .narration,
     out: .outbound_units, in: .inbound_units, state: .state}' \
   logs/*/all_envelopes_full.json

# Step 2: Look for orphaned or low-confidence matches
jq --arg acc "$account" \
   '.[] | select(
     (.outbound_account == $acc or .inbound_account == $acc) and
     (.metadata.is_orphaned == true or
      (.classification_confidence != null and .classification_confidence < 0.70))
   )' logs/*/all_envelopes_full.json

# Step 3: Check for modified transactions (cost basis changes, account corrections)
jq --arg acc "$account" \
   '.[] | select(.outbound_account == $acc or .inbound_account == $acc) |
    select(.history | length > 3) |
    {id: .envelope_id, history_length: (.history | length)}' \
   logs/*/all_envelopes_full.json
```

**Common causes:**
- Missing transaction (check the source CSV)
- Wrong account (check transfer disambiguation)
- Precision mismatch (PDF vs CSV data source)
- External transfer not classified (money appeared from outside tracked accounts)

### Balance Is Exactly 2x Expected

This is the signature of a **double import** — the same transaction exists in two output files. Usually a manual entry that wasn't consumed during reconciliation:

```bash
# Find transactions that appear in multiple files
jq '[.[] | {id: .envelope_id, source: .source}] |
    group_by(.id) | map(select(length > 1))' \
   logs/*/all_envelopes_full.json
```

**Root cause:** Manual transactions that don't match CSV (stock splits, corrections) get written to both the year file and the orphaned file. Fix by adding `skip-transfer-detection: "true"` to standalone manual entries.

### Transfer Matched to Wrong Account

When money goes to the wrong broker account (e.g., SIPP instead of ISA):

```bash
# Find the transfer and see its scoring details
jq '.[] | select(.narration | test("AJ BELL"; "i")) |
    {id: .envelope_id, matched_with: .metadata.matched_with,
     score: .metadata.match_score,
     candidates: .metadata.transfer_candidates}' \
   logs/*/all_envelopes_full.json
```

Look at the `transfer_candidates` array. Each candidate shows its score breakdown. If the wrong candidate won, check which patterns gave it higher scores. You may need ISA/SIPP disambiguation keywords in your narrations, or a scoring pattern adjustment.

### Transaction Disappeared

A transaction exists in the CSV but doesn't appear in the output:

```bash
# Search by narration
jq '.[] | select(.narration | test("MISSING PAYEE"; "i"))' \
   logs/*/all_envelopes_full.json

# Search by date range and amount
jq '.[] | select(.date >= "2024-01-10" and .date <= "2024-01-20" and
    (.outbound_units == "500.00" or .inbound_units == "500.00"))' \
   logs/*/all_envelopes_full.json
```

If it appears in the JSON but not the output, check its `state` — it may have been discarded during reconciliation or merged into another envelope. The `history` array shows what happened.

If it doesn't appear in the JSON at all, the importer didn't extract it. Check the CSV file format and the importer's parsing logic.

### Cost Basis Error (No Position Matches)

A `ReductionError` means Beancount can't find a matching lot for a sell transaction:

```bash
# Find sells with cost basis warnings
jq '.[] | select(.transaction_type == "SELL") |
    select(.warnings | length > 0) |
    {id: .envelope_id, date: .date, narration: .narration,
     warnings: .warnings}' \
   logs/*/all_envelopes_full.json
```

**Common causes:**
- Purchase is in a different year file not visible to Beancount
- Stock split not applied before the sell (lot quantities don't match post-split)
- In-specie transfer lost the cost basis

### Unexpected Matches

To find all matches and check for false positives:

```bash
# All matched envelopes with their scores
jq '.[] | select(.metadata.matched_with != null) |
    {id: .envelope_id, matched_with: .metadata.matched_with,
     score: .metadata.match_score, date: .date,
     narration: .narration[:50]}' \
   logs/*/all_envelopes_full.json

# Low-confidence matches (likely false positives)
jq '.[] | select(.classification_confidence != null and
    .classification_confidence < 0.70 and
    .metadata.matched_with != null)' \
   logs/*/all_envelopes_full.json
```

## Quick Reference

### Count envelopes by state
```bash
jq '[.[] | .state] | group_by(.) | map({state: .[0], count: length})' \
   logs/*/all_envelopes_full.json
```

### Count by source file
```bash
jq '[.[] | .source] | group_by(.) | map({source: .[0], count: length})' \
   logs/*/all_envelopes_full.json
```

### Find all warnings
```bash
jq '.[] | select(.warnings | length > 0) |
    {id: .envelope_id, warnings: .warnings}' \
   logs/*/all_envelopes_full.json
```

### Find envelopes with specific metadata
```bash
jq '.[] | select(.metadata.transfer_type == "external_capital")' \
   logs/*/all_envelopes_full.json
```

### Trace a single envelope through the pipeline
```bash
jq '.[] | select(.envelope_id == "hsbc_checking_20240115_abc") | .history' \
   logs/*/all_envelopes_full.json
```

### Validate data integrity (no transactions lost)
```bash
# Compare source counts vs output counts
jq '{
  total: length,
  written: [.[] | select(.state == "written")] | length,
  discarded: [.[] | select(.state == "discarded")] | length,
  other: [.[] | select(.state != "written" and .state != "discarded")] | length
}' logs/*/all_envelopes_full.json
```

## When JSON Isn't Enough

For issues that need source-level debugging:

```bash
# Full import log
less logs/*/importer_*.log

# Errors only
cat logs/*/importer_*_errors.log

# Error patterns
cat logs/*/importer_*_error_summary.log

# Enable debug mode for verbose output
uv run python scripts/import_data.py --debug
```
