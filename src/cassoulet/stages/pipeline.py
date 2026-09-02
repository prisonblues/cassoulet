"""
Base Pipeline - Core Transaction Processing Framework

This is the foundational pipeline that orchestrates the complete transaction
processing workflow. It provides a clean, extensible architecture for:

1. INGESTION: Load and parse raw transaction data
2. RECONCILIATION: Match and merge related transactions  
3. ENHANCEMENT: Apply business logic and enrichments
4. OUTPUT: Write processed transactions to disk

Key Design Principles:
- Clear separation of concerns
- Extensible through inheritance
- Comprehensive error handling
- Full audit trail and logging
"""

import logging
import orjson
import os
from concurrent.futures import ThreadPoolExecutor, Future
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional, Any, Tuple
from dataclasses import dataclass, field
from collections import defaultdict
from decimal import Decimal

from beancount.core.data import Transaction

from .envelope import Envelope, EnvelopeState
# ReconciliationEngine replaced with Scorer/Matcher/Merger
from .transfer_scorer import TransferScorer
from .transfer_matcher import TransferMatcher
from .transfer_merger import TransferMerger
from cassoulet.utils.accounts import AccountMetadataRegistry
from cassoulet.utils.envelope_utilities import apply_patterns_to_envelopes
from cassoulet.config.component_config import ComponentConfig
from cassoulet.config.config_loader import ConfigLoader
from cassoulet.base.exceptions import ProcessingWarning
from .error_reporter import ErrorReporter
# Legacy transaction-based processors removed - using envelope-native versions
# Legacy processors removed - using envelope-native versions
from cassoulet.utils.logger import get_smart_logger
from cassoulet.utils.steel_thread_monitor import SteelThreadMonitor
from .progress_tracker import ProgressTracker


logger = logging.getLogger(__name__)


def _write_envelopes_to_file(envelopes, log_dir, filename):
    """Write envelope dicts to JSON file. Runs in background thread."""
    filepath = Path(log_dir) / filename
    data = [env.to_dict() for env in envelopes]
    with open(filepath, 'wb') as f:
        f.write(orjson.dumps(data, default=str))
    logger.info(f"  Wrote {len(data)} envelopes to {filename}")


@dataclass
class PipelineConfig:
    """Complete configuration contract for the pipeline execution.
    
    This is the complete interface between calling code and the pipeline.
    All external inputs and configuration must be provided through this class.
    """
    # Input data (complete contract - no external data needed)
    file_paths: List[str] = field(default_factory=list)  # CSV files to process
    manual_transaction_paths: List[str] = field(default_factory=list)  # Manual .beancount files
    importers: List[Any] = field(default_factory=list)  # Configured importers
    
    # Input/Output paths - all will be set from ComponentConfig if not provided
    input_dir: Optional[str] = None  # Will be set from ComponentConfig.input.raw_data_dir
    output_dir: Optional[str] = None  # Will be set from ComponentConfig.output.output_dir
    ledger_path: Optional[str] = None  # Will be derived from output_dir
    
    # Component configuration (new)
    component_config: Optional[ComponentConfig] = None
    institution: Optional[str] = None  # For institution-specific config
    
    # User-provided patterns (if None, loaded from built-in config modules)
    expense_patterns: Optional[Dict] = None      # expense categorization patterns
    payment_patterns: Optional[Dict] = None      # payee → account patterns
    no_match_patterns: Optional[list] = None     # patterns to exclude from transfer matching

    # User-provided custom processors (run after expense categorization)
    custom_processors: List[Any] = field(default_factory=list)

    # Feature flags
    enable_debug_mode: bool = False
    enable_tradingview_export: bool = True  # Export to TradingView format

    # Balance continuity configuration
    balance_tolerance: Decimal = Decimal('0.01')  # Maximum acceptable balance difference
    auto_remediate_gaps: bool = True  # Auto-create missing data entries
    missing_data_account_prefix: str = "Income:MissingData"  # Account for gaps
        
    # Timestamp for this run (set automatically if not provided)
    timestamp: Optional[str] = None


@dataclass
class PipelineResult:
    """Result from running the pipeline."""
    envelopes: List[Envelope]
    canonical_transactions: List[Transaction]
    statistics: Dict[str, Any]
    reconciliation_report: Optional[str] = None
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


class Pipeline:
    """
    Pipeline implementation providing core transaction processing workflow.
    
    This class orchestrates the complete pipeline:
    - INGESTION: Load raw data into transaction envelopes
    - RECONCILIATION: Score, match, and merge transfers
    - BALANCE CONTINUITY: Check for gaps and optionally remediate
    - OUTPUT: Write to disk with validation
    """
    
    def __init__(self, config: Optional[PipelineConfig] = None):
        self.config = config or PipelineConfig()
        self.smart_logger = get_smart_logger()

        # Set timestamp if not provided
        if not self.config.timestamp:
            self.config.timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        # Log directory setup - create timestamped subdirectory (moved up for logging setup)
        self.log_dir = os.path.join("logs", self.config.timestamp)
        os.makedirs(self.log_dir, exist_ok=True)

        # Setup logging to files
        self._setup_logging()

        # Load component configuration with institution-specific overrides
        if self.config.component_config:
            self.component_config = self.config.component_config
        else:
            # Load default config with any institution overrides
            self.component_config = ConfigLoader.load_config(
                institution=self.config.institution
            )

        # Set input/output paths from ComponentConfig if not explicitly provided
        if self.config.input_dir is None:
            self.config.input_dir = self.component_config.input.raw_data_dir
        if self.config.output_dir is None:
            self.config.output_dir = self.component_config.output.output_dir
        if self.config.ledger_path is None:
            self.config.ledger_path = os.path.join(self.config.output_dir, "main.beancount")
        # Set manual transaction paths if not provided
        if not self.config.manual_transaction_paths:
            self.config.manual_transaction_paths = self.component_config.input.get_manual_transaction_paths()
        
        # Components - now with configuration
        # Replaced ReconciliationEngine with three-stage reconciliation
        self.transfer_scorer = TransferScorer(
            date_tolerance_days=self.component_config.scoring.date_tolerance_days,
            debug=False  # Use False for now
        )
        self.transfer_matcher = TransferMatcher(
            config=self.component_config.matching
        )
        self.transfer_merger = TransferMerger(
            config=self.component_config.merging
        )
        self.institution_extractor = AccountMetadataRegistry(
            accounts_file=self.component_config.get_accounts_file()
        )
        
        # STEEL THREAD: Runtime monitoring
        self.steel_thread_monitor = SteelThreadMonitor(
            critical_threshold=5,
            error_threshold=20,
            enable_circuit_breaker=self.config.enable_circuit_breaker if hasattr(self.config, 'enable_circuit_breaker') else True
        )
        
        # Validate all processors are compliant
        self._validate_processor_compliance()
        
        # Statistics
        self.stats = {
            'ingestion': {},
            'reconciliation': {},
            'enhancement': {},
            'total_time': 0
        }
        
        # Error reporter
        self.error_reporter = ErrorReporter(self.log_dir)
        
        # Progress tracker - uses top-level logs directory for cross-run tracking
        self.progress_tracker = ProgressTracker("logs")
        
        # Track all warnings for error reporting
        self.all_warnings = []

        # Background thread pool for JSON writes (orjson releases GIL)
        self._write_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="json-writer")
        self._write_futures: List[Future] = []
    
    def _setup_logging(self):
        """Setup file logging handlers for the import process."""
        # Create log file paths
        log_file = os.path.join(self.log_dir, f"importer_{self.config.timestamp}.log")
        summary_log_file = os.path.join(self.log_dir, f"importer_{self.config.timestamp}_summary.log")
        errors_log_file = os.path.join(self.log_dir, f"importer_{self.config.timestamp}_errors.log")
        
        # Also create errors.log in the timestamped directory (comprehensive error report)
        comprehensive_errors_log = os.path.join(self.log_dir, "errors.log")
        
        # Configure root logger for full logging
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.DEBUG if self.config.enable_debug_mode else logging.INFO)
        
        # Remove any existing handlers to avoid duplicates
        for handler in root_logger.handlers[:]:
            if isinstance(handler, logging.FileHandler):
                root_logger.removeHandler(handler)
        
        # Full log file handler (everything)
        full_handler = logging.FileHandler(log_file, encoding='utf-8')
        full_handler.setLevel(logging.DEBUG if self.config.enable_debug_mode else logging.INFO)
        full_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        full_handler.setFormatter(full_formatter)
        root_logger.addHandler(full_handler)
        
        # Summary log handler (WARNING and above)
        summary_handler = logging.FileHandler(summary_log_file, encoding='utf-8')
        summary_handler.setLevel(logging.WARNING)
        summary_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        summary_handler.setFormatter(summary_formatter)
        root_logger.addHandler(summary_handler)
        
        # Errors-only log handler (ERROR and above)
        errors_handler = logging.FileHandler(errors_log_file, encoding='utf-8')
        errors_handler.setLevel(logging.ERROR)
        errors_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        errors_handler.setFormatter(errors_formatter)
        root_logger.addHandler(errors_handler)
        
        # Add comprehensive errors.log handler in timestamped directory
        comprehensive_errors_handler = logging.FileHandler(comprehensive_errors_log, mode='w', encoding='utf-8')
        comprehensive_errors_handler.setLevel(logging.ERROR)
        comprehensive_errors_formatter = logging.Formatter('%(asctime)s [%(name)s] %(levelname)s: %(message)s')
        comprehensive_errors_handler.setFormatter(comprehensive_errors_formatter)
        root_logger.addHandler(comprehensive_errors_handler)
        
        # Write header for errors.log
        with open(comprehensive_errors_log, 'w', encoding='utf-8') as f:
            f.write(f"{'='*80}\n")
            f.write(f"Import Error Log\n")
            f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"{'='*80}\n\n")
        
        logger.info(f"Logging initialized - output to {self.log_dir}")
    
    def _validate_processor_compliance(self):
        """
        Validate that all child processors inherit from BaseProcessor.
        This is a key requirement for Steel Thread compliance.
        """
        # Check for EnvelopeProcessor compliance
        from cassoulet.stages.envelope_processor import EnvelopeProcessor
        
        processors_to_check = [
            ('TransferScorer', self.transfer_scorer),
            ('TransferMatcher', self.transfer_matcher),
            ('TransferMerger', self.transfer_merger),
        ]
        
        non_compliant = []
        for name, processor in processors_to_check:
            if not isinstance(processor, EnvelopeProcessor):
                non_compliant.append(name)
        
        if non_compliant:
            raise ValueError(
                f"Steel Thread Violation: The following processors do not inherit from BaseProcessor: "
                f"{', '.join(non_compliant)}. All processors must be Steel Thread compliant."
            )
        
        logger.info(f"✅ All {len(processors_to_check)} core processors are Steel Thread compliant")
    
    def run(self) -> PipelineResult:
        """
        Run the complete pipeline.
        
        This is the single orchestrator for the entire import process.
        All data and configuration is taken from self.config.
        
        Returns:
            PipelineResult with all outputs
        """
        start_time = datetime.now()
        logger.info("")
        logger.info("="*80)
        logger.info(f"{'PIPELINE STARTING':^80}")
        logger.info("="*80)
        
        # Clean output directory first
        self._clean_output_directory()
        
        # Balance gap detection now happens within bank importers during extraction
        # Each bank importer handles its own balance processing via _process_balances()
        
        
        
        # INGESTION PHASE: Load all data
        logger.info("\n" + "=" * 60)
        logger.info("")
        logger.info(f"{'='*23} INGESTION PHASE {'='*24}")
        self.smart_logger.start_phase("Ingestion")
        
        # Load manual transactions first
        manual_envelopes = []
        corporate_actions = []
        if self.config.manual_transaction_paths:
            logger.info("  Loading manual transactions...")
            from cassoulet.stages.beancount_reader import BeancountReader
            reader = BeancountReader()
            for manual_path in self.config.manual_transaction_paths:
                if Path(manual_path).exists():
                    envelopes, actions = reader.load_transactions(manual_path)
                    logger.info(f"    Loaded {len(envelopes)} envelopes and {len(actions)} corporate actions from {Path(manual_path).name}")
                    manual_envelopes.extend(envelopes)
                    corporate_actions.extend(actions)
            logger.info(f"  Total manual transactions: {len(manual_envelopes)}")
            logger.info(f"  Total corporate actions: {len(corporate_actions)}")

        # Store corporate actions for lot processor
        self.corporate_actions = corporate_actions
        
        # Process CSV files
        csv_envelopes = []
        if self.config.file_paths and self.config.importers:
            logger.info("  Processing CSV files...")
            csv_envelopes = self._process_importers(self.config.file_paths, self.config.importers)
            logger.info(f"  Created {len(csv_envelopes)} CSV envelopes")
        
        # Combine all envelopes
        envelopes = manual_envelopes + csv_envelopes
        # In deferred posting architecture, we don't create transactions here
        # PostingGenerator will create them at the end of the pipeline
        self.manual_envelopes = manual_envelopes
        
        self.stats['ingestion'] = {
            'envelopes_created': len(envelopes),
            'manual_transactions': len(manual_envelopes),
            'csv_transactions': len(csv_envelopes)
        }
        
        logger.info(f"Ingestion complete: {len(envelopes)} total envelopes")
        self.smart_logger.end_phase()

        # CRITICAL FIX: Check and disambiguate duplicate envelope IDs
        # This prevents the massive duplication bug (e.g., 73 copies of one envelope)
        # Must happen immediately after ingestion before any processing
        logger.info("")
        logger.info(f"{'='*18} ID DISAMBIGUATION PHASE {'='*18}")
        logger.info("  → Checking for duplicate envelope IDs...")

        from cassoulet.utils.deterministic_id import check_and_disambiguate_ids
        fixed_envelopes, id_warnings = check_and_disambiguate_ids(envelopes)

        # Log warnings about duplicates
        duplicate_count = 0
        for warning in id_warnings:
            if warning['severity'] == 'ERROR':
                logger.error(f"  {warning['message']}")
                if 'count' in warning.get('details', {}):
                    duplicate_count += warning['details']['count'] - 1
            elif warning['severity'] == 'WARNING':
                logger.warning(f"  {warning['message']}")
            else:
                logger.info(f"  {warning['message']}")

        if duplicate_count > 0:
            logger.warning(f"  ⚠️  Fixed {duplicate_count} duplicate envelope IDs")

        # Store envelopes for processing through the pipeline
        # In the deferred posting architecture, we work with envelopes throughout
        pipeline_envelopes = fixed_envelopes.copy()

        # Initialize warnings list for the entire pipeline
        self.all_warnings = []
        self.all_warnings.extend([
            ProcessingWarning(
                processor_name="IDDisambiguation",
                severity=w['severity'],
                message=w['message'],
                source_transaction=None,
                details=w.get('details', {})
            ) for w in id_warnings
        ])

        # Apply NO_MATCH_PATTERNS to mark transfer eligibility
        logger.info("  → Applying no-match patterns...")
        if self.config.no_match_patterns is not None:
            no_match = self.config.no_match_patterns
        else:
            from cassoulet.config.no_match_patterns import NO_MATCH_PATTERNS
            no_match = NO_MATCH_PATTERNS
        pipeline_envelopes = apply_patterns_to_envelopes(pipeline_envelopes, no_match)
        logger.info(f"  Marked transfer eligibility for {len(pipeline_envelopes)} envelopes")

        reconciled_envelopes = pipeline_envelopes
        canonical_envelopes = pipeline_envelopes
        score_envelopes = None
        match_envelopes = None
        merge_envelopes = None

        # RECONCILIATION PHASE: Score, match, and merge transfers
        logger.info("\n" + "=" * 60)
        logger.info("")
        logger.info(f"{'='*20} RECONCILIATION PHASE {'='*19}")
        self.smart_logger.start_phase("Reconciliation")
        
        # In the deferred posting architecture, we work with envelopes
        envelopes_to_reconcile = pipeline_envelopes
        
        # Score potential transfers (envelope-native as of Chunk 6)
        logger.info("  → Scoring potential transfers...")
        scored_envelopes, scoring_warnings = self.transfer_scorer.process_envelopes(envelopes_to_reconcile)
        self.all_warnings.extend(scoring_warnings)
        
        # Score envelopes are already in envelope form
        score_envelopes = scored_envelopes
        logger.info(f"Scored {len(score_envelopes)} envelopes")
        
        # Write score_envelopes.json immediately
        self._write_stage_envelopes(score_envelopes, "score_envelopes.json")
        
        # Match transfers (envelope-native as of Chunk 8)
        logger.info("  → Matching transfers...")
        matched_envelopes, matching_warnings = self.transfer_matcher.process_envelopes(score_envelopes)
        self.all_warnings.extend(matching_warnings)
        
        # Match envelopes are already in envelope form
        match_envelopes = matched_envelopes
        
        # Write match_envelopes.json immediately
        self._write_stage_envelopes(match_envelopes, "match_envelopes.json")
        
        # Merge matched transfers (envelope-native as of Chunk 10)
        logger.info("  → Merging matched transfers...")
        merged_envelopes, merging_warnings = self.transfer_merger.process_envelopes(match_envelopes)
        self.all_warnings.extend(merging_warnings)
        
        # Merge envelopes are already in envelope form
        merge_envelopes = merged_envelopes
        canonical_envelopes = merged_envelopes
        
        # Write merge_envelopes.json immediately
        self._write_stage_envelopes(merge_envelopes, "merge_envelopes.json")
        
        # Store reconciled envelopes
        reconciled_envelopes = merged_envelopes
        
        self.stats['reconciliation'] = {
            'scoring': {'envelopes_scored': len(score_envelopes)},
            'matching': {'envelopes_matched': len(match_envelopes)},
            'merging': {'envelopes_merged': len(merge_envelopes)},
            'envelope_preservation': {
                'score_envelopes': len(score_envelopes) if score_envelopes else 0,
                'match_envelopes': len(match_envelopes) if match_envelopes else 0,
                'merge_envelopes': len(merge_envelopes) if merge_envelopes else 0,
                'canonical_envelopes': len(canonical_envelopes)
            }
        }
        
        logger.info(f"Reconciliation complete: {len(envelopes)} → {len(canonical_envelopes)} envelopes")
        logger.info(f"  Preserved: {len(score_envelopes)} score, {len(match_envelopes)} match, {len(merge_envelopes)} merge envelopes")
        self.smart_logger.end_phase()
        
        # We now have reconciled envelopes with correct accounts
        # These will be processed for lot tracking

        # CLASSIFICATION PHASE: Apply pattern-based classification
        logger.info("\n" + "=" * 60)
        logger.info("")
        logger.info(f"{'='*20} CLASSIFICATION PHASE {'='*19}")
        logger.info("  → Applying classification patterns...")
        from cassoulet.config.envelope_classifier_patterns import ENVELOPE_CLASSIFIER_PATTERNS
        canonical_envelopes = apply_patterns_to_envelopes(canonical_envelopes, ENVELOPE_CLASSIFIER_PATTERNS)
        logger.info(f"  Classified {len(canonical_envelopes)} envelopes")

        # Write classified_envelopes.json for debugging
        self._write_stage_envelopes(canonical_envelopes, "classified_envelopes.json")

        # EXPENSE CATEGORIZATION: Assign expense accounts and categories
        logger.info("\n💰 Categorizing expenses...")
        from cassoulet.stages.expense_categorization_processor import ExpenseCategorizationProcessor

        expense_processor = ExpenseCategorizationProcessor(
            patterns=self.config.expense_patterns
        )
        categorized_envelopes, categorization_warnings = expense_processor.process_envelopes(canonical_envelopes)
        self.all_warnings.extend(categorization_warnings)
        canonical_envelopes = categorized_envelopes

        # Store expense processor stats
        self.stats['expense_categorization'] = expense_processor.get_stats()

        # REVERSALS AND REFUNDS: money coming back on the same account it left
        # from. Must run AFTER categorisation, because the whole point is to
        # inherit the account the ORIGINAL was booked to.
        logger.info("\n↩️  Resolving reversals and refunds...")
        from cassoulet.stages.reversal_refund_processor import ReversalRefundProcessor

        reversal_processor = ReversalRefundProcessor()
        canonical_envelopes, reversal_warnings = reversal_processor.process_envelopes(
            canonical_envelopes
        )
        self.all_warnings.extend(reversal_warnings)
        self.stats['reversal_refund'] = reversal_processor.get_stats()

        # CUSTOM PROCESSORS: Run user-provided processors (e.g. company accounting)
        for processor in self.config.custom_processors:
            processor_name = getattr(processor, 'processor_name', type(processor).__name__)
            logger.info(f"\n🔧 Running custom processor: {processor_name}...")
            processed_envelopes, proc_warnings = processor.process_envelopes(canonical_envelopes)
            self.all_warnings.extend(proc_warnings)
            canonical_envelopes = processed_envelopes
            if hasattr(processor, 'get_stats'):
                self.stats[processor_name] = processor.get_stats()

        # INVESTMENT CLASSIFICATION: Identify BUY/SELL/DIVIDEND transactions
        logger.info("\n📊 Classifying investment transactions...")
        from cassoulet.stages.investment_classifier import InvestmentClassifier

        investment_classifier = InvestmentClassifier()
        classified_envelopes, classification_warnings = investment_classifier.process_envelopes(canonical_envelopes)
        self.all_warnings.extend(classification_warnings)
        canonical_envelopes = classified_envelopes

        # LOT TRACKING PHASE: Envelope-native lot tracking
        logger.info("\n📦 Lot Tracking (Envelope-Native)...")
        from cassoulet.stages.envelope_lot_processor import EnvelopeLotProcessor

        # Pass corporate actions to lot processor
        lot_processor = EnvelopeLotProcessor(corporate_actions=self.corporate_actions)

        # Use classified envelopes (which have gone through investment classification)
        lot_tracking_envelopes = canonical_envelopes

        # Process all transactions chronologically (including stock splits)
        lot_tracking_envelopes, lot_warnings = lot_processor.process_envelopes(lot_tracking_envelopes)
        self.all_warnings.extend(lot_warnings)

        # Store lot processor for any downstream use
        self.lot_processor = lot_processor

        # Log summary
        total_lots = sum(len(lots) for lots in lot_processor.lots.values())
        logger.info(f"  Tracking {total_lots} active lots across {len(lot_processor.lots)} account/commodity pairs")
        if lot_processor.processed_actions:
            logger.info(f"  Processed {len(lot_processor.processed_actions)} corporate actions (stock splits)")

        # Final envelopes after lot processing
        final_envelopes = lot_tracking_envelopes
        logger.info(f"Final envelopes ready: {len(final_envelopes)} envelopes")

        # Debug: Check first envelope to see if it has transaction data
        if final_envelopes:
            first = final_envelopes[0]
            logger.debug(f"First envelope check: id={first.envelope_id[:8]}, date={first.date}, narration={first.narration[:30] if first.narration else 'None'}")

        # ENVELOPE-NATIVE OUTPUT: Create transactions and write output in one pass
        logger.info("\n✅ Creating transactions and writing output files (ENVELOPE-NATIVE)...")
        from cassoulet.stages.output.transaction_writer import TransactionWriter

        # Copy manual balance assertion files BEFORE TransactionWriter runs
        # so they're available when main.beancount is generated
        self._copy_and_register_manual_balances()

        output_writer = TransactionWriter(self.config.output_dir)
        processed_envelopes, writer_warnings = output_writer._process_internal(final_envelopes)
        self.all_warnings.extend(writer_warnings)

        # Write stock split transactions from processed corporate actions
        if lot_processor.processed_actions:
            logger.info(f"\n📝 Writing {len(lot_processor.processed_actions)} stock split transactions...")
            split_warnings = output_writer.write_stock_splits(lot_processor.processed_actions)
            self.all_warnings.extend(split_warnings)

        # Get the created transactions for error reporting
        final_transactions = output_writer.get_created_transactions()

        for warning in writer_warnings:
            if warning.severity in ['CRITICAL', 'ERROR']:
                logger.warning(f"TransactionWriter: {warning.message}")

        logger.info(f"Created {len(final_transactions)} final transactions")
        logger.info(f"  Wrote {output_writer.stats['files_written']} output files")
        logger.info(f"  {output_writer.stats['dual_placed']} dual-placed transfers")
        logger.info(f"  {output_writer.stats['balance_assertions']} balance assertions")

        # Generate reports
        reconciliation_report = None
        # Reconciliation report now comes from three-stage stats
        reconciliation_report = self._generate_reconciliation_report()
        self._write_reconciliation_report(reconciliation_report)
        
        # Write debug artifacts
        if self.config.enable_debug_mode:
            self._write_debug_artifacts(pipeline_envelopes, enhanced_envelopes)
        
        # Run validation if requested
        validation_errors = []
        logger.info("\n📊 Running bean-check validation...")
        validation_errors = self._run_validation()
        if validation_errors:
            logger.warning(f"⚠️  Found {len(validation_errors)} validation errors")
            # Write comprehensive error report to errors.log
            self._write_comprehensive_error_report(validation_errors)
        else:
            logger.info("✅ Validation passed - no errors")
        
        # Ensure all background JSON writes are complete before error reporting
        self._wait_for_writes()

        # Generate error reports
        logger.info("\n📝 Generating error reports...")
        ledger_path = os.path.join(self.config.output_dir, "main.beancount")

        # Get lot registry data if available
        lot_lineage_data = {}
        if hasattr(self, 'lot_processor') and self.lot_processor:
            lot_lineage_data = self.lot_processor.get_lot_registry()
            logger.info(f"  Including lot registry with {lot_lineage_data.get('stats', {}).get('total_lots', 0)} lots")

        error_report = self.error_reporter.generate_reports(
            envelopes=final_envelopes,
            canonical_transactions=final_transactions,
            warnings=self.all_warnings,
            ledger_path=ledger_path,
            pipeline_stats=self.stats,
            steel_thread_monitor=getattr(self, 'steel_thread_monitor', None),
            lot_lineage_data=lot_lineage_data
        )
        
        # Update progress log
        if hasattr(self, 'progress_tracker'):
            # Analyze errors from bean-check and other sources
            error_counts = self.progress_tracker.analyze_errors(
                ledger_path=ledger_path,
                dropped_count=0  # Could track dropped CSV entries if needed
            )
            
            # Log import summary with statistics (this also updates the progress log)
            self.progress_tracker.log_import_summary(
                error_counts=error_counts,
                envelope_count=len(final_envelopes),
                transaction_count=len(final_transactions),
                manual_count=0,  # Could track manual transaction count if needed
                csv_count=len(self.config.file_paths) if self.config.file_paths else 0,
                reconciliation_stats=self.stats.get('reconciliation', {})
            )
            
            logger.info(f"📊 Updated progress log: {self.progress_tracker.progress_file}")
        
        # STEEL THREAD: Generate forensic analysis
        logger.info("\n🔍 Generating Steel Thread forensic analysis...")
        forensic_report = self.steel_thread_monitor.generate_forensic_report()
        
        # Write forensic report to file
        forensic_path = os.path.join(self.log_dir, f"steel_thread_forensic_{self.config.timestamp}.md")
        with open(forensic_path, 'w') as f:
            f.write(forensic_report)
        logger.info(f"   Steel Thread forensic report written to: {forensic_path}")
        
        # Check for Steel Thread violations
        if self.steel_thread_monitor.violation_counts.get('CRITICAL', 0) > 0:
            logger.error(f"❌ STEEL THREAD VIOLATIONS: {self.steel_thread_monitor.violation_counts['CRITICAL']} CRITICAL violations detected!")
            logger.error(f"   Data loss events: {self.steel_thread_monitor.data_loss_events}")
            logger.error(f"   Empty transactions: {self.steel_thread_monitor.empty_transaction_count}")
        
        if error_report.has_critical_issues:
            logger.error(f"❌ Found {error_report.critical_count} CRITICAL and {error_report.error_count} ERROR issues")
            logger.error(f"   See {self.log_dir}/forensic.md for detailed analysis")
        else:
            logger.info("✅ No critical issues found")

        # TradingView Export (if enabled) - AFTER validation
        # This ensures we export from the final beancount files which include
        # stock splits and all other transformations
        if self.config.enable_tradingview_export:
            logger.info("\n📊 Exporting to TradingView format...")
            from cassoulet.stages.output.tradingview_exporter import TradingViewExporter

            tv_exporter = TradingViewExporter()
            # TODO: Update to read from beancount files instead of envelopes
            # For now, still using envelopes (known limitation with stock splits)
            tv_exporter.process_envelopes(final_envelopes)
            tv_files = tv_exporter.write_csv_files()
            tv_stats = tv_exporter.get_statistics()

            logger.info(f"  Exported {tv_stats['total_exported']} transactions to TradingView format")
            logger.info(f"  Created {len(tv_files)} CSV files for {len(tv_stats['owners'])} owners")
            logger.info(f"  Skipped {tv_stats['skipped']} transactions (dividends, interest, etc.)")

            # Store stats for summary
            self.stats['tradingview_export'] = tv_stats

        # Calculate total time
        self.stats['total_time'] = (datetime.now() - start_time).total_seconds()
        self.stats['validation_errors'] = len(validation_errors)

        # Log final statistics
        self._log_final_stats()
        
        logger.info("\n" + "=" * 80)
        logger.info("")
        logger.info("="*80)
        logger.info(f"{'PIPELINE COMPLETE':^80}")
        logger.info("="*80)

        # Ensure all background writes finished before returning
        self._wait_for_writes()
        self._write_pool.shutdown(wait=False)

        # HEALTH SUMMARY - the machine-readable answer to "did this actually
        # work". Callers previously had no way to ask: PipelineResult.warnings
        # was never populated, so a processor could die, the run could report
        # success, and a materially wrong ledger got written with nobody the
        # wiser. A CRITICAL here means a whole stage failed and returned its
        # input unchanged - not a degraded ledger, a wrong one.
        severity_counts: Dict[str, int] = {}
        failed_processors = []
        for w in self.all_warnings:
            severity = getattr(w, 'severity', None)
            if not severity:
                continue
            severity_counts[severity] = severity_counts.get(severity, 0) + 1
            if severity == 'CRITICAL':
                name = getattr(w, 'processor_name', 'unknown')
                message = getattr(w, 'message', '')
                # EVERY critical trips the alarm. An exception means the stage
                # died and returned its input untouched; the Steel Thread
                # envelope-count violations are less understood but are flagged
                # CRITICAL by a system whose stated guarantee is zero silent
                # failures, so they get the same treatment until someone either
                # fixes them or argues the severity down. Five of them fired on
                # every run of the reference ledger and nobody had ever seen
                # one, which is exactly how a sixth that matters gets missed.
                died = message.startswith('Processor failed with exception')
                failed_processors.append({
                    'processor': name,
                    'message': message,
                    'kind': 'died' if died else 'contract',
                })

        self.stats['health'] = {
            'critical': severity_counts.get('CRITICAL', 0),
            'errors': severity_counts.get('ERROR', 0),
            'warnings': severity_counts.get('WARNING', 0),
            'failed_processors': failed_processors,
            'healthy': not failed_processors,
        }
        if failed_processors:
            logger.critical(
                "PIPELINE DEGRADED - %d critical failure(s). Any stage that DIED "
                "returned its input unchanged, making the written ledger wrong "
                "rather than merely incomplete: %s",
                len(failed_processors),
                "; ".join(f"{f['processor']}: {f['message']}" for f in failed_processors),
            )

        return PipelineResult(
            envelopes=final_envelopes,  # Use final_envelopes, not enhanced_envelopes
            canonical_transactions=final_transactions,
            statistics=self.stats,
            reconciliation_report=reconciliation_report,
            warnings=[str(getattr(w, 'message', w)) for w in self.all_warnings],
        )
    
    def _clean_output_directory(self) -> None:
        """Clean the output directory completely before starting import."""
        import shutil
        output_path = Path(self.config.output_dir)
        
        if not output_path.exists():
            logger.info(f"Output directory does not exist, will be created: {output_path}")
            output_path.mkdir(parents=True, exist_ok=True)
            return
        
        logger.info(f"Cleaning output directory ({output_path}) completely...")
        shutil.rmtree(output_path)
        logger.info("Output directory cleaned - ready for fresh import")
    
    def _process_importers(self, file_paths: List[str], importers: List) -> List[Envelope]:
        """
        Process all configured importers against their specified files.

        Replaces the entire ingestion_layer with a simple, direct approach.
        """
        from pathlib import Path

        all_envelopes = []

        # Create lookup of available files by basename
        available_files = {}
        for file_path in file_paths:
            basename = Path(file_path).name
            available_files[basename] = file_path

        # Log AJ Bell files specifically for debugging
        ajbell_in_available = [k for k in available_files.keys() if 'ajb' in k.lower() or 'ajbell' in k.lower()]
        if ajbell_in_available:
            logger.info(f"    AJ Bell files in available_files: {ajbell_in_available}")

        logger.info(f"    Found {len(available_files)} CSV files")
        logger.info(f"    Processing {len(importers)} configured importers")

        # Log AJ Bell importers specifically
        ajbell_importers = [imp for imp in importers if 'AJBELL' in getattr(imp, 'account', '')]
        if ajbell_importers:
            logger.info(f"    Found {len(ajbell_importers)} AJ Bell importers:")
            for imp in ajbell_importers:
                logger.info(f"      - {imp.account}: expecting files {imp.file_identifier}")

        # Process each importer with its configured files
        for importer in importers:
            # Ensure importer has output_dir in its config for balance assertion writing
            if hasattr(importer, 'config') and isinstance(importer.config, dict):
                importer.config['output_dir'] = self.config.output_dir

            file_id = importer.file_identifier

            # Dispatch based on file_identifier type:
            # - dict: Multi-file importer, call extract(file_dict: Dict[str, str])
            # - str: Single-file importer, call extract(file_path: str)
            if isinstance(file_id, dict):
                # Multi-file importer (e.g., AJ Bell, Vanguard)
                # extract() signature: extract(file_dict: Dict[str, str]) -> List[Envelope]
                file_dict = {}
                missing = []
                found = []

                for file_type, filename in file_id.items():
                    if filename in available_files:
                        file_dict[file_type] = str(available_files[filename])
                        found.append(f"{file_type}:{filename}")
                    else:
                        missing.append(f"{file_type}:{filename}")

                if found:
                    logger.info(f"    {importer.account} found files: {', '.join(found)}")

                if missing:
                    logger.warning(f"    {importer.account} missing files: {', '.join(missing)}")
                    continue

                # Call extract() once with the complete file dict
                try:
                    logger.info(f"    Processing {importer.account} with {len(file_dict)} files...")
                    envelopes = importer.extract(file_dict)

                    if envelopes:
                        all_envelopes.extend(envelopes)
                        logger.info(f"    {importer.account}: extracted {len(envelopes)} transactions")
                except Exception as e:
                    logger.error(f"    {importer.account} failed: {e}")

            else:
                # Single-file importer (e.g., banks, HL, II)
                # extract() signature: extract(file_path: str) -> List[Envelope]
                if file_id in available_files:
                    file_path = str(available_files[file_id])
                else:
                    logger.debug(f"    {importer.account} expects '{file_id}' - not found in current batch")
                    continue

                try:
                    logger.info(f"    Processing {importer.account} with 1 file...")
                    envelopes = importer.extract(file_path)

                    if envelopes:
                        all_envelopes.extend(envelopes)
                        logger.info(f"    {importer.account}: extracted {len(envelopes)} transactions")
                except Exception as e:
                    logger.error(f"    {importer.account} failed: {e}")
        
        return all_envelopes
            
    def _copy_manual_balance_files(self) -> List[str]:
        """Copy manual balance assertion files from entries/manual/balances to year subdirectories.

        Returns:
            List of relative paths to copied balance files
        """
        import shutil
        import re
        from cassoulet.config.component_config import get_files_from_directory

        balance_files = []
        manual_balances_dir = self.component_config.input.get_manual_balances_dir()

        # Steel Thread compliant: Use utility that logs all decisions
        balance_file_paths = get_files_from_directory(
            directory=manual_balances_dir,
            pattern="*_balances.beancount",
            purpose="manual balance assertions",
            logger=logger
        )

        if not balance_file_paths:
            return balance_files

        logger.info(f"Processing {len(balance_file_paths)} manual balance files for copying")

        for balance_file_path_str in balance_file_paths:
            balance_file_path = Path(balance_file_path_str)
            filename = balance_file_path.name
            # Extract year from filename (e.g., "2019_balances.beancount" -> "2019")
            year_match = re.match(r'^(\d{4})_balances\.beancount$', filename)
            if not year_match:
                logger.warning(f"Skipping file with unexpected name format: {filename}")
                continue

            year = year_match.group(1)

            # Create year directory if needed
            year_dir = Path(self.config.output_dir) / year
            year_dir.mkdir(parents=True, exist_ok=True)

            # Copy to year directory with new name
            new_filename = f"manual_balance_assertions_{year}.beancount"
            dest_path = year_dir / new_filename
            shutil.copy2(balance_file_path, dest_path)

            # Add relative path for includes
            relative_path = f"{year}/{new_filename}"
            balance_files.append(relative_path)
            logger.debug(f"  Copied {filename} to {dest_path}")

        return balance_files
    
    def _copy_and_register_manual_balances(self) -> None:
        """Copy manual balance assertion files and update includes.

        Note: Gap remediations are now handled by TransactionWriter
        when it encounters remediation envelopes.
        """

        # Copy manual balance assertion files
        manual_balance_files = self._copy_manual_balance_files()
        if manual_balance_files:
            logger.info(f"  Copied {len(manual_balance_files)} manual balance files")

        # Find balance assertion files created by TransactionWriter
        balance_assertion_files = []
        from pathlib import Path
        output_path = Path(self.config.output_dir)
        for bal_file in output_path.glob("**/bank_balance_assertions_*.beancount"):
            balance_assertion_files.append(str(bal_file))

        # Update includes.beancount with manual balance files
        if manual_balance_files:
            self._update_includes_with_files(manual_balance_files)
            logger.info(f"  Updated includes.beancount with {len(manual_balance_files)} balance files")
    
    def _generate_reconciliation_report(self) -> str:
        """Generate reconciliation report from pipeline stats."""
        if 'reconciliation' not in self.stats:
            return "No reconciliation performed"
        
        reconciliation_stats = self.stats['reconciliation']
        lines = []
        lines.append("=" * 60)
        lines.append("RECONCILIATION REPORT")
        lines.append("=" * 60)
        lines.append(f"Generated: {datetime.now().isoformat()}")
        lines.append("")
        
        # Scoring stats
        lines.append("SCORING PHASE:")
        scoring = reconciliation_stats.get('scoring', {})
        lines.append(f"  Transactions processed: {scoring.get('transactions_processed', 0)}")
        lines.append(f"  Potential matches found: {scoring.get('potential_matches_found', 0)}")
        lines.append(f"  High confidence: {scoring.get('high_confidence_matches', 0)}")
        lines.append(f"  Medium confidence: {scoring.get('medium_confidence_matches', 0)}")
        lines.append(f"  Low confidence: {scoring.get('low_confidence_matches', 0)}")
        lines.append("")
        
        # Matching stats
        lines.append("MATCHING PHASE:")
        matching = reconciliation_stats.get('matching', {})
        lines.append(f"  Transactions processed: {matching.get('transactions_processed', 0)}")
        lines.append(f"  Matches made: {matching.get('matches_made', 0)}")
        lines.append(f"  High confidence matches: {matching.get('high_confidence_matches', 0)}")
        lines.append(f"  Medium confidence matches: {matching.get('medium_confidence_matches', 0)}")
        lines.append(f"  Conflicts resolved: {matching.get('conflicts_resolved', 0)}")
        lines.append(f"  Manual-CSV matches: {matching.get('manual_csv_matches', 0)}")
        lines.append(f"  CSV-CSV matches: {matching.get('csv_csv_matches', 0)}")
        lines.append("")
        
        # Merging stats
        lines.append("MERGING PHASE:")
        merging = reconciliation_stats.get('merging', {})
        lines.append(f"  Transactions processed: {merging.get('transactions_processed', 0)}")
        lines.append(f"  Pairs merged: {merging.get('pairs_merged', 0)}")
        lines.append(f"  Unmatched preserved: {merging.get('unmatched_preserved', 0)}")
        lines.append(f"  Date conflicts: {merging.get('date_conflicts', 0)}")
        lines.append(f"  Amount conflicts: {merging.get('amount_conflicts', 0)}")
        lines.append(f"  Manual overrides: {merging.get('manual_overrides', 0)}")
        lines.append(f"  Metadata preserved: {merging.get('metadata_preserved', 0)}")
        
        return "\n".join(lines)
    
    def _write_reconciliation_report(self, report: str) -> None:
        """Write reconciliation report to file."""
        report_path = Path(self.log_dir) / "reconciliation_report.txt"

        with open(report_path, 'w') as f:
            f.write(report)

        logger.info(f"Wrote reconciliation report to {report_path}")

    def _update_includes_with_files(self, file_paths: List[str]) -> None:
        """Update includes.beancount to include the specified files.

        Args:
            file_paths: List of file paths to add to includes
        """
        includes_path = Path(self.config.output_dir) / "includes.beancount"

        if not includes_path.exists():
            logger.warning(f"includes.beancount not found at {includes_path}")
            return

        # Read current content
        with open(includes_path, 'r') as f:
            lines = f.readlines()

        # Check which files are already included
        new_files = []
        for filepath in file_paths:
            filename = Path(filepath).name
            include_line = f'include "{filename}"'

            # Check if already included
            already_included = any(include_line in line for line in lines)
            if not already_included:
                new_files.append(filename)

        if not new_files:
            return  # All files already included

        # Find insertion point (after core definitions)
        insert_index = len(lines)  # Default to end
        for i, line in enumerate(lines):
            if 'Core system definitions' in line:
                # Find the end of core definitions section
                for j in range(i+1, len(lines)):
                    if lines[j].strip() and not lines[j].strip().startswith(';') and not lines[j].strip().startswith('include'):
                        insert_index = j
                        break
                    elif j == len(lines) - 1:
                        insert_index = j + 1
                break

        # Add new includes
        if insert_index > 0 and lines[insert_index-1].strip():
            lines.insert(insert_index, '\n')
            insert_index += 1

        lines.insert(insert_index, '; Balance assertions and remediations\n')
        for filename in sorted(new_files):
            lines.insert(insert_index + 1, f'include "{filename}"\n')
            insert_index += 1

        # Write updated content
        with open(includes_path, 'w') as f:
            f.writelines(lines)

        logger.debug(f"    Added {len(new_files)} files to includes.beancount")

    def _write_stage_envelopes(self, envelopes: List[Envelope], filename: str) -> None:
        """Submit stage envelope JSON write to background thread.

        Envelopes are immutable (replaced, not mutated) by subsequent pipeline
        stages, so passing them to a background thread is safe. orjson releases
        the GIL during encoding, enabling true parallelism with pipeline work.
        """
        if not envelopes:
            return

        envs = list(envelopes)  # shallow copy of list references
        log_dir = self.log_dir
        future = self._write_pool.submit(
            _write_envelopes_to_file, envs, log_dir, filename
        )
        self._write_futures.append(future)

    def _wait_for_writes(self) -> None:
        """Wait for all background JSON writes to complete."""
        for future in self._write_futures:
            future.result()  # raises if write failed
        self._write_futures.clear()
    
    def _create_score_envelopes(self, 
                               original_envelopes: List[Envelope],
                               scored_txns: List[Transaction],
                               warnings: List[ProcessingWarning]) -> List[Envelope]:
        """Create score envelopes with full scoring results."""
        score_envelopes = []
        
        # Map warnings to transactions
        warning_map = defaultdict(list)
        for warning in warnings:
            if hasattr(warning, 'source_transaction') and warning.source_transaction:
                txn_id = warning.source_transaction.meta.get('transaction_id') if warning.source_transaction.meta else None
                if txn_id:
                    warning_map[txn_id].append(warning)
        
        for txn in scored_txns:
            # Find original envelope by matching envelope_id in transaction metadata
            txn_envelope_id = txn.meta.get('envelope_id') if txn.meta else None
            orig_env = next((e for e in original_envelopes 
                            if e.envelope_id == txn_envelope_id), None)
            
            if orig_env:
                # Create enhanced envelope with scoring results
                env_dict = orig_env.to_dict()
                env_dict['state'] = 'scored'
                
                # Add scoring results from transaction metadata
                if txn.meta and 'scoring_results' in txn.meta:
                    env_dict['scoring_results'] = txn.meta['scoring_results']
                elif txn.meta and 'transfer_candidates' in txn.meta:
                    # Convert candidates to scoring results format
                    env_dict['scoring_results'] = []
                    for candidate in txn.meta['transfer_candidates']:
                        env_dict['scoring_results'].append({
                            'candidate_id': candidate.get('transaction_id', 'unknown'),
                            'candidate_source_id': candidate.get('source_id', ''),
                            'score': candidate.get('score', 0),
                            'score_components': candidate.get('score_components', {}),
                            'candidate_summary': {
                                'date': str(candidate.get('date', '')),
                                'narration': candidate.get('narration', ''),
                                'amount': candidate.get('amount', ''),
                                'account': candidate.get('account', '')
                            }
                        })
                
                # Add any scoring warnings
                txn_id = txn.meta.get('transaction_id') if txn.meta else None
                if txn_id and txn_id in warning_map:
                    env_dict['scoring_warnings'] = [
                        {'severity': w.severity, 'message': w.message} 
                        for w in warning_map[txn_id]
                    ]
                
                # Copy original envelope and add scoring data
                score_env = orig_env  # Use the original envelope
                score_env.state = EnvelopeState.RECONCILED
                
                # Add only the scoring-specific data to metadata, not the entire dict
                if 'scoring_results' in env_dict:
                    score_env.metadata['scoring_results'] = env_dict['scoring_results']
                if 'scoring_warnings' in env_dict:
                    score_env.metadata['scoring_warnings'] = env_dict['scoring_warnings']
                    
                score_envelopes.append(score_env)
            else:
                # Transaction without original envelope - should not happen but handle it
                txn_id = txn.meta.get('transaction_id') if txn.meta else 'unknown'
                logger.warning(f"Transaction without original envelope: {txn_id}")
                # Skip orphan transactions - we can't create envelopes from them anymore
                continue
        
        return score_envelopes
    
    def _create_match_envelopes(self,
                               score_envelopes: List[Envelope],
                               matched_txns: List[Transaction],
                               warnings: List[ProcessingWarning]) -> List[Envelope]:
        """Create match envelopes with match decisions and validation."""
        match_envelopes = []
        
        # Build map of matched transactions
        match_map = {}
        for txn in matched_txns:
            if txn.meta and 'matched_with' in txn.meta:
                txn_id = txn.meta.get('transaction_id')
                if txn_id:
                    match_map[txn_id] = txn.meta['matched_with']
        
        # Build map of transactions by envelope_id
        txn_by_envelope = {}
        for txn in matched_txns:
            if txn.meta and 'envelope_id' in txn.meta:
                txn_by_envelope[txn.meta['envelope_id']] = txn
        
        for score_env in score_envelopes:
            # Get the transaction for this envelope
            txn = txn_by_envelope.get(score_env.envelope_id)
            if not txn:
                logger.warning(f"No transaction found for envelope {score_env.envelope_id}")
                continue
            
            txn_id = txn.meta.get('transaction_id') if txn.meta else None
            
            # Create match envelope with decision
            env_dict = score_env._metadata if hasattr(score_env, '_metadata') else score_env.to_dict()
            env_dict['state'] = 'matched'
            
            if txn_id and txn_id in match_map:
                # Add match decision
                matched_with = match_map[txn_id]
                env_dict['match_decision'] = {
                    'matched_with': matched_with,
                    'match_confidence': txn.meta.get('match_confidence', 'unknown'),
                    'match_score': txn.meta.get('match_score', 0),
                    'match_type': txn.meta.get('match_type', 'transfer')
                }
                
                # Add validation results if available
                if 'validation_results' in txn.meta:
                    env_dict['match_decision']['validation_results'] = txn.meta['validation_results']
                
                # Find and include the matched transaction details
                other_txn = next((t for t in matched_txns 
                                 if t.meta and t.meta.get('transaction_id') == matched_with), None)
                if other_txn:
                    env_dict['other_transaction_full'] = {
                        'transaction_id': other_txn.meta.get('transaction_id'),
                        'source_id': other_txn.meta.get('source-id', ''),
                        'date': str(other_txn.date),
                        'narration': other_txn.narration,
                        'postings': [{
                            'account': p.account,
                            'units': str(p.units) if p.units else None
                        } for p in other_txn.postings] if other_txn.postings else []
                    }
            else:
                # Unmatched transaction
                env_dict['match_decision'] = {
                    'matched_with': None,
                    'match_type': 'unmatched',
                    'reason': 'No suitable match found'
                }
            
            # Use the existing envelope and update its state
            match_env = score_env  # Reuse the existing envelope
            match_env.state = EnvelopeState.RECONCILED
            
            # Add match decision to metadata
            if 'match_decision' in env_dict:
                match_env.metadata['match_decision'] = env_dict['match_decision']
            if 'other_transaction_full' in env_dict:
                match_env.metadata['other_transaction_full'] = env_dict['other_transaction_full']
                
            match_envelopes.append(match_env)
        
        return match_envelopes
    
    def _create_merge_envelopes(self,
                               match_envelopes: List[Envelope],
                               merged_txns: List[Transaction],
                               warnings: List[ProcessingWarning],
                               original_envelopes: List[Envelope]) -> Tuple[List[Envelope], List[Envelope]]:
        """Create merge envelopes showing merge results while preserving originals."""
        merge_envelopes = []
        canonical_envelopes = []
        processed_ids = set()
        
        # Process merged transactions
        for txn in merged_txns:
            if txn.meta:
                # Check if this is a merged transaction
                if 'merged_from' in txn.meta:
                    merged_from = txn.meta['merged_from']
                    
                    # Create merge envelope
                    merge_env_dict = {
                        'envelope_id': f"merged_{txn.meta.get('transaction_id', 'unknown')[:8]}",
                        'state': 'merged',
                        'merged_from': merged_from,
                        'merge_result': {
                            'primary_transaction_id': txn.meta.get('transaction_id'),
                            'date': str(txn.date),
                            'narration': txn.narration,
                            'postings': [{
                                'account': p.account,
                                'amount': str(p.units) if p.units else None,
                                'source': p.meta.get('source', 'unknown') if p.meta else 'unknown'
                            } for p in txn.postings] if txn.postings else []
                        }
                    }
                    
                    # Add merge conflicts if any
                    if 'merge_conflicts' in txn.meta:
                        merge_env_dict['merge_result']['merge_conflicts'] = txn.meta['merge_conflicts']
                    
                    # Include original envelopes
                    merge_env_dict['original_envelopes'] = {}
                    for orig_id in merged_from:
                        # Find envelope by checking metadata instead of transaction
                        orig_env = next((e for e in match_envelopes 
                                        if e.metadata.get('transaction_id') == orig_id), None)
                        if orig_env:
                            merge_env_dict['original_envelopes'][orig_id] = orig_env.to_dict()
                        processed_ids.add(orig_id)
                    
                    # Create merge envelope from transaction data
                    merge_env = Envelope.create(
                        date_val=txn.date,
                        narration=txn.narration,
                        source=self.institution_extractor.extract_institution(txn) or "unknown",
                        payee=txn.payee,
                        flag=txn.flag
                    )
                    merge_env.envelope_id = merge_env_dict['envelope_id']
                    merge_env.state = EnvelopeState.RECONCILED
                    merge_env.metadata.update(merge_env_dict)
                    merge_envelopes.append(merge_env)
                    
                    # Also add to canonical
                    canonical_envelopes.append(merge_env)
                else:
                    # Non-merged transaction - preserve as-is
                    txn_id = txn.meta.get('transaction_id')
                    if txn_id and txn_id not in processed_ids:
                        # Find original envelope by checking metadata
                        orig_env = next((e for e in match_envelopes 
                                        if e.metadata.get('transaction_id') == txn_id), None)
                        if orig_env:
                            merge_envelopes.append(orig_env)
                            canonical_envelopes.append(orig_env)
                        processed_ids.add(txn_id)
        
        # Include ALL original envelopes that weren't merged (to preserve all transactions)
        for env in match_envelopes:
            # Check metadata directly instead of accessing transaction
            txn_id = env.metadata.get('transaction_id')
            if txn_id and txn_id not in processed_ids:
                merge_envelopes.append(env)
                canonical_envelopes.append(env)
        
        return merge_envelopes, canonical_envelopes
    
    def _write_debug_artifacts(self, all_envelopes: List[Envelope],
                              canonical_envelopes: List[Envelope]) -> None:
        """Write debug artifacts for analysis."""
        
        # Note: The three-stage envelope files are now written during reconciliation
        # This method now only writes supplementary debug files
        
        # Write canonical envelopes (final result)
        canonical_path = Path(self.log_dir) / "canonical_envelopes.json"
        with open(canonical_path, 'wb') as f:
            f.write(orjson.dumps(
                [env.to_dict() for env in canonical_envelopes],
                default=str,
            ))
        logger.info(f"Wrote canonical envelopes to {canonical_path}")
        
        # Write processing history for discarded envelopes
        discarded = [env for env in all_envelopes if env.is_discarded()]
        if discarded:
            discarded_path = Path(self.log_dir) / "discarded_envelopes.txt"
            with open(discarded_path, 'w') as f:
                for env in discarded:
                    f.write(f"\n{'=' * 60}\n")
                    f.write(f"DISCARDED: {env.source} - {env.narration}\n")
                    f.write(f"Date: {env.date}\n")
                    f.write(env.get_history_summary())
                    f.write("\n")
            logger.info(f"Wrote {len(discarded)} discarded envelopes to {discarded_path}")
    
    def _write_comprehensive_error_report(self, validation_errors: List) -> None:
        """Write a comprehensive error report to errors.log with grouping and full metadata."""
        from collections import defaultdict
        
        errors_log_path = os.path.join(self.log_dir, "errors.log")
        
        # Group errors by type
        error_groups = defaultdict(list)
        for error in validation_errors:
            error_type = type(error).__name__
            error_groups[error_type].append(error)
        
        # Write comprehensive report
        with open(errors_log_path, 'a', encoding='utf-8') as f:
            f.write("\n" + "=" * 80 + "\n")
            f.write("BEAN-CHECK VALIDATION ERRORS\n")
            f.write("=" * 80 + "\n\n")
            f.write(f"Total errors: {len(validation_errors)}\n")
            f.write(f"Error types: {len(error_groups)}\n\n")
            
            # Write errors grouped by type
            for error_type, errors in sorted(error_groups.items()):
                f.write(f"\n{'-' * 70}\n")
                f.write(f"{error_type}: {len(errors)} errors\n")
                f.write(f"{'-' * 70}\n\n")
                
                for i, error in enumerate(errors, 1):
                    f.write(f"Error #{i}:\n")
                    
                    # Extract file and line info if available
                    if hasattr(error, 'source'):
                        source = error.source
                        if isinstance(source, dict):
                            f.write(f"  File: {source.get('filename', 'unknown')}\n")
                            f.write(f"  Line: {source.get('lineno', 'unknown')}\n")
                    
                    # Write error message
                    error_str = str(error)
                    # Basic length check to prevent massive logs
                    if len(error_str) > 10000:
                        error_str = error_str[:10000] + "\n... [TRUNCATED - excessive length]"
                    f.write(f"  Message: {error_str}\n")
                    
                    # Add entry details if available
                    if hasattr(error, 'entry') and error.entry:
                        entry = error.entry
                        f.write(f"  Transaction:\n")
                        f.write(f"    Date: {entry.date}\n")
                        f.write(f"    Narration: {entry.narration}\n")
                        if hasattr(entry, 'postings') and entry.postings:
                            f.write(f"    Postings:\n")
                            for posting in entry.postings[:3]:  # Show first 3 postings
                                account = posting.account
                                units = posting.units if posting.units else "None"
                                f.write(f"      - {account}: {units}\n")
                            if len(entry.postings) > 3:
                                f.write(f"      ... and {len(entry.postings) - 3} more postings\n")
                    
                    f.write("\n")
            
            # Write summary
            f.write("\n" + "=" * 80 + "\n")
            f.write("ERROR SUMMARY BY TYPE\n")
            f.write("=" * 80 + "\n")
            for error_type, errors in sorted(error_groups.items(), key=lambda x: len(x[1]), reverse=True):
                f.write(f"  {error_type}: {len(errors)}\n")
            f.write("\n")
        
        logger.info(f"Wrote comprehensive error report to {errors_log_path}")
    
    def _log_final_stats(self) -> None:
        """Log final pipeline statistics."""
        logger.info("\n" + "=" * 60)
        logger.info("")
        logger.info(f"{'='*20} PIPELINE STATISTICS {'='*20}")
        
        # Ingestion
        logger.info("Ingestion Phase:")
        for key, value in self.stats['ingestion'].items():
            logger.info(f"  {key}: {value}")
        
        # Reconciliation
        if 'reconciliation' in self.stats and self.stats['reconciliation']:
            logger.info("\nReconciliation Phase:")
            
            # Scoring stats
            if 'scoring' in self.stats['reconciliation']:
                logger.info("  Scoring:")
                for key, value in self.stats['reconciliation']['scoring'].items():
                    logger.info(f"    {key}: {value}")
            
            # Matching stats
            if 'matching' in self.stats['reconciliation']:
                logger.info("  Matching:")
                for key, value in self.stats['reconciliation']['matching'].items():
                    logger.info(f"    {key}: {value}")
            
            # Merging stats
            if 'merging' in self.stats['reconciliation']:
                logger.info("  Merging:")
                for key, value in self.stats['reconciliation']['merging'].items():
                    logger.info(f"    {key}: {value}")
        
        # Enhancement
        if 'enhancement' in self.stats and self.stats['enhancement']:
            logger.info("\nEnhancement Phase:")
            for key, value in self.stats['enhancement'].items():
                logger.info(f"  {key}: {value}")

        # Expense Categorization
        if 'expense_categorization' in self.stats and self.stats['expense_categorization']:
            logger.info("\nExpense Categorization:")
            exp_stats = self.stats['expense_categorization']
            logger.info(f"  Categorized: {exp_stats.get('categorized', 0)}")
            logger.info(f"  Uncategorized: {exp_stats.get('uncategorized', 0)}")
            logger.info(f"  Skipped transfers: {exp_stats.get('skipped_transfers', 0)}")
            if exp_stats.get('categories_assigned'):
                logger.info("  Categories:")
                for cat, count in sorted(exp_stats['categories_assigned'].items()):
                    logger.info(f"    {cat}: {count}")

        # Validation results
        if 'validation_errors' in self.stats:
            if self.stats['validation_errors'] == 0:
                logger.info("\n✅ Validation: PASSED")
            else:
                logger.warning(f"\n⚠️  Validation: {self.stats['validation_errors']} errors")
        
        logger.info("")
        logger.info(f"Total time: {self.stats['total_time']:.2f} seconds")
        logger.info("="*60)
    
    def _run_validation(self) -> List[str]:
        """
        Run bean-check validation on the output.
        
        Returns:
            List of validation error messages
        """
        from beancount.loader import load_file
        
        # Load the main file which includes everything
        main_file = Path(self.config.output_dir) / "main.beancount"
        
        if not main_file.exists():
            from cassoulet.base.exceptions import PathNotFoundWarning
            warning = PathNotFoundWarning(str(main_file), "bean-check validation")
            logger.warning(str(warning))
            return [str(warning)]
        
        # Load and check for errors
        _, errors, _ = load_file(str(main_file))
        
        if errors:
            logger.warning(f"Validation found {len(errors)} errors:")
            # Log first few errors
            for error in errors[:5]:
                logger.warning(f"  - {error}")
            if len(errors) > 5:
                logger.warning(f"  ... and {len(errors) - 5} more")
        
        # Return error messages
        return [str(error) for error in errors]
    