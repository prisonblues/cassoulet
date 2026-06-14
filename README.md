# Cassoulet

An opinionated, envelope-based import pipeline for [Beancount](https://beancount.github.io/docs/), the open-source double-entry accounting system.

Takes CSV exports from UK banks and brokers, wraps every transaction in an auditable envelope, runs them through a multi-phase reconciliation pipeline, and outputs clean Beancount ledger files. Zero silent failures.

## Why "Cassoulet"?

Because it takes in mixed beans and makes them work together.

## Why This Exists

Beancount is a powerful double-entry accounting engine, but importing real-world financial data into it is a hard problem that existing tools don't solve well.

**The gap:** Banks and brokers each export CSVs in different formats, describing the same money movements from different perspectives. A £500 transfer shows up as a debit in your HSBC CSV and a credit in your Starling CSV, with different dates, different descriptions, and no shared identifier. Stock purchases appear in transaction files and cash files separately. In-specie transfers between brokers take weeks and create entries that look nothing alike on each side.

**What typically goes wrong:**
- Transfers between your own accounts get double-counted as both an expense and income
- Cost basis for investments gets lost when you sell shares bought in a previous year
- Stock splits require manually calculating 30-digit decimal lot adjustments for every holding
- Silent failures mean you don't notice problems until a tax return is wrong

**What cassoulet does differently:**
- Wraps every transaction in an **envelope** that tracks its complete lifecycle — every decision is recorded, nothing fails silently
- **Defers all Beancount posting creation** until the final stage, when the system has complete information (no more "wrong account" bugs)
- Uses **fuzzy scoring** to match transfers across institutions, handling date mismatches, reverse settlement, and account disambiguation
- Solves **corporate actions declaratively** — stock splits are 3 lines of metadata, not 20+ lines of manual lot calculations
- Provides **structured JSON audit trails** that you can query with `jq` to debug any issue

This represents several years of production use, iterating through real edge cases with real UK financial institutions. The [documentation](#documentation) covers the hard-won lessons in detail.

## Features

- **Envelope Architecture** — every transaction is wrapped in an envelope with complete lifecycle tracking, from ingestion through reconciliation to output
- **Deferred Posting** — no Beancount postings are created until the final output stage, eliminating "wrong account" bugs in transfers
- **Three-Stage Reconciliation** — scoring, matching, and merging with full transparency at each step
- **Transfer Detection** — fuzzy scoring across date, amount, and account dimensions to match transfers between institutions
- **Investment-Aware** — proper cost basis tracking, FIFO lot management, stock splits, dividends, in-specie transfers
- **Corporate Actions** — declarative stock split directives that Beancount can't handle natively
- **Steel Thread Guarantees** — zero silent failures, complete data integrity verification, total operational transparency
- **Multi-Log Output** — structured JSON envelope data, error summaries, forensic reports for root cause analysis

## Supported Institutions

- HSBC (current accounts, credit cards)
- Hargreaves Lansdown (ISA, SIPP)
- AJ Bell (ISA, SIPP)
- Interactive Investor
- Vanguard
- Monzo
- Starling
- Santander

Adding a new institution means writing a single importer class that maps CSV columns to envelope fields. The pipeline, matching, and output stages are shared.

## Quick Start

### Installation

```bash
# Clone the repo
git clone https://github.com/yourusername/cassoulet.git
cd cassoulet

# Install with uv
uv sync

# Or install as editable dependency in your own project
uv pip install -e /path/to/cassoulet
```

### Project Setup

Cassoulet expects your project to have:

```
your-project/
├── accounts/
│   └── accounts.beancount      # Chart of accounts with importer metadata
├── commodities/
│   └── commodities.beancount   # Investment instruments and currencies
├── entries/
│   ├── raw/                    # CSV files from banks/brokers
│   ├── manual/                 # Manual transaction files
│   └── output/                 # Generated ledger files
│       └── main.beancount      # Global orchestrator
└── import_data.py              # Your import script
```

See the `examples/` directory for a complete working setup using the fictional **Jack Spriggins** and his beanstalk-funded portfolio.

### Configuration

Account metadata drives importer configuration — no separate config files needed:

```beancount
2007-01-01 open Assets:Bank:HSBC:Checking   GBP
  institution:        "HSBC UK"
  importer:           "HSBCImporter"
  file_identifier:    "jack_hsbc_checking.csv"
```

### Running an Import

```python
from cassoulet.stages.pipeline import UnifiedPipeline, PipelineConfig
from cassoulet.config.component_config import ComponentConfig, ProjectPaths

config = ComponentConfig(
    paths=ProjectPaths(
        accounts_file="accounts/accounts.beancount",
        commodities_file="commodities/commodities.beancount",
        raw_data_dir="entries/raw",
        output_dir="entries/output",
    )
)

pipeline_config = PipelineConfig(
    component_config=config,
    expense_patterns=my_expense_patterns,  # Your categorization rules
)

pipeline = UnifiedPipeline(pipeline_config)
pipeline.run()
```

### Viewing Your Data

Once imported, your ledger is a standard Beancount file. You can:

**Validate it:**
```bash
uv run bean-check entries/output/main.beancount
```

**Browse it in [Fava](https://beancount.github.io/fava/)**, Beancount's web interface — interactive balance sheets, income statements, portfolio views, and drill-down into any account or transaction:
```bash
uv run fava entries/output/main.beancount
# Opens at http://localhost:5000
```

**Query it** with Beancount's SQL-like query language:
```bash
# Show all holdings with cost basis
uv run bean-query entries/output/main.beancount \
  "SELECT account, currency, sum(position) WHERE account ~ 'Broker' GROUP BY account, currency"
```

## Architecture

### The Envelope

Every transaction flows through the pipeline inside an `Envelope`:

```
CSV Row → Envelope(INGESTED) → CLASSIFIED → GROUPED → RECONCILED → ENHANCED → WRITTEN
```

The envelope carries:
- **Financial data** as directional flow (`inbound_units`/`outbound_units`)
- **Processing history** — complete audit trail of every decision
- **Warnings** — accumulated throughout the pipeline
- **Metadata** — source file, line number, confidence scores

### Inbound/Outbound Pattern

Instead of creating Beancount postings early, envelopes store financial data as directional flow:

```python
envelope.outbound_units = Decimal("1500.00")   # Cash going out
envelope.outbound_type = "GBP"
envelope.inbound_units = Decimal("10.5")       # Shares coming in
envelope.inbound_type = "VLS100"
```

This works universally for buys, sells, transfers, income, and expenses.

### Reconciliation vs Aggregation

The system distinguishes two fundamentally different merge operations:
- **Reconciliation**: Two views of the same transaction (manual entry + CSV match) — they share an account
- **Aggregation**: Merging partial transactions (bank A debit + bank B credit = complete transfer) — no shared account

Using the wrong strategy causes double-counting or missing transfers. See [Envelope Matching](docs/envelope-matching.md) for the full story.

### Debugging

Every import run generates structured JSON in `logs/`:

```bash
# Find envelopes with warnings
jq '.[] | select(.warnings | length > 0)' logs/*/all_envelopes_full.json

# Trace a specific envelope through the pipeline
jq '.[] | select(.envelope_id == "hsbc_checking_20240115_abc")' logs/*/all_envelopes_full.json

# Count envelopes by state
jq '[.[] | .state] | group_by(.) | map({state: .[0], count: length})' logs/*/all_envelopes_full.json
```

See the [Debugging Guide](docs/debugging.md) for comprehensive recipes.

## Extending Cassoulet

### Custom Processors

Add business-specific logic via custom processors:

```python
from cassoulet.stages.envelope_processor import EnvelopeProcessor

class MyCompanyProcessor(EnvelopeProcessor):
    def __init__(self):
        super().__init__(processor_name="my_company")

    def _process_internal(self, envelopes):
        # Your logic here
        return processed_envelopes, warnings

# Pass to pipeline
pipeline_config = PipelineConfig(
    custom_processors=[MyCompanyProcessor()],
)
```

### Expense Patterns

Inject your own categorization rules:

```python
expense_patterns = {
    "Groceries": {
        "account": "Expenses:Food:Groceries",
        "patterns": ["TESCO", "SAINSBURY", "WAITROSE", "ALDI", "LIDL"],
    },
    "Transport": {
        "account": "Expenses:Transport",
        "patterns": ["TFL", "TRAINLINE", "UBER"],
    },
}
```

## Documentation

Cassoulet's documentation covers the architecture and the hard-won lessons behind it. These aren't theoretical designs — they're solutions to real problems discovered through production use with real UK financial institutions.

### Core Architecture

- **[Architecture](docs/architecture.md)** — the envelope lifecycle, deferred posting, pipeline phases, and the inbound/outbound pattern that makes everything work

- **[Envelope Matching](docs/envelope-matching.md)** — how scoring-based reconciliation replaces brittle hash matching, the critical distinction between reconciliation and aggregation, and the declarative pattern system

### Investment Handling

- **[Cost Basis](docs/cost-basis.md)** — solving cross-year FIFO booking, the seven approaches tested for in-specie transfers (only one works), and why cost basis lookup must happen mid-pipeline

- **[Corporate Actions](docs/corporate-actions.md)** — declarative stock splits that Beancount can't do natively, turning 20+ lines of manual lot calculations into 3 lines of metadata

### Operations

- **[Transfer Detection](docs/transfer-detection.md)** — fuzzy scoring across date, amount, and account dimensions; ISA/SIPP disambiguation; reverse settlement handling; external transfer classification

- **[Steel Thread](docs/steel-thread.md)** — the zero-silent-failures architecture, circuit breakers, multi-log output, and real lessons from silent data loss bugs

- **[Debugging](docs/debugging.md)** — using structured JSON audit trails and `jq` for root cause analysis, with recipes for every common scenario

## Related Projects

- **[Beancount](https://beancount.github.io/docs/)** — the double-entry accounting engine that cassoulet builds on. Start here if you're new to plaintext accounting.
- **[Fava](https://beancount.github.io/fava/)** — web-based UI for Beancount ledgers. Interactive balance sheets, income statements, and portfolio views.
- **[Beancount docs](https://beancount.github.io/docs/beancount_language_syntax.html)** — the language syntax reference for `.beancount` files.

## License

MIT
