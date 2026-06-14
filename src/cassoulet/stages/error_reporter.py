"""
Error Reporting System

Provides focused error reporting similar to the existing pipeline:
- error_report.log: CRITICAL/ERROR warnings plus bean-check validation errors
- error_summary.log: Categorized error analysis with counts
- forensic.md: Detailed forensic analysis when critical issues occur
"""

import logging
import os
import orjson
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional
from collections import defaultdict
from dataclasses import dataclass, field

from beancount import loader
from beancount.core.data import Transaction

from .envelope import Envelope
from cassoulet.base.exceptions import ProcessingWarning


logger = logging.getLogger(__name__)


@dataclass
class ErrorReport:
    """Container for error reporting data."""
    critical_count: int = 0
    error_count: int = 0
    warning_count: int = 0
    info_count: int = 0
    validation_errors: List[Any] = field(default_factory=list)
    critical_warnings: List[ProcessingWarning] = field(default_factory=list)
    error_warnings: List[ProcessingWarning] = field(default_factory=list)
    error_categories: Dict[str, List[Any]] = field(default_factory=dict)
    
    @property
    def has_critical_issues(self) -> bool:
        """Check if there are critical issues requiring forensic analysis."""
        return self.critical_count > 0 or self.error_count > 0 or len(self.validation_errors) > 0


class ErrorReporter:
    """
    Error reporting system for pipeline.
    
    Generates:
    1. error_report.log - CRITICAL/ERROR warnings plus validation errors
    2. error_summary.log - Categorized error analysis
    3. forensic.md - Detailed forensic report for critical issues
    """
    
    def __init__(self, log_dir: str):
        """
        Initialize error reporter.
        
        Args:
            log_dir: Directory to write error reports (should be timestamped subdir)
        """
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        
    def generate_reports(self,
                        envelopes: List[Envelope],
                        canonical_transactions: List[Transaction],
                        warnings: List[ProcessingWarning],
                        ledger_path: str,
                        pipeline_stats: Dict[str, Any],
                        steel_thread_monitor=None,
                        lot_lineage_data: Optional[Dict[str, Any]] = None) -> ErrorReport:
        """
        Generate error reports based on pipeline results.
        
        Args:
            envelopes: All transaction envelopes (for forensic analysis)
            canonical_transactions: Final canonical transactions
            warnings: All warnings generated during processing
            ledger_path: Path to main ledger file for validation
            pipeline_stats: Statistics from pipeline execution
            
        Returns:
            ErrorReport with analysis results
        """
        # Analyze warnings by severity
        report = self._analyze_warnings(warnings)
        
        # Also collect warnings from envelopes themselves
        for env in envelopes:
            if hasattr(env, 'warnings') and env.warnings:
                for warning in env.warnings:
                    if hasattr(warning, 'severity'):
                        if warning.severity == 'CRITICAL':
                            report.critical_count += 1
                            report.critical_warnings.append(warning)
                        elif warning.severity == 'ERROR':
                            report.error_count += 1
                            report.error_warnings.append(warning)
                        elif warning.severity == 'WARNING':
                            report.warning_count += 1
        
        # Run bean-check validation
        validation_errors = self._run_bean_check(ledger_path)
        report.validation_errors = validation_errors
        
        # Categorize validation errors
        report.error_categories = self._categorize_errors(validation_errors)
        
        # ALWAYS write complete envelope state when there are issues
        if report.has_critical_issues or report.warning_count > 0:
            self._write_envelope_states(envelopes)
        
        # ALWAYS write the canonical transactions (the actual ledger data)
        self._write_canonical_transactions(canonical_transactions)
        
        # Write lot lineage data if provided
        if lot_lineage_data:
            self._write_lot_lineage(lot_lineage_data)
        else:
            logger.debug("No lot lineage data provided to ErrorReporter")
        
        # Write error_report.log (CRITICAL/ERROR only + validation)
        self._write_errors_log(report)
        
        # Write error_summary.log (categorized analysis)
        self._write_error_summary(report, pipeline_stats)
        
        # Write forensic.md if critical issues
        if report.has_critical_issues:
            self._write_forensic_report(report, envelopes, canonical_transactions, pipeline_stats, steel_thread_monitor)
            
        return report
    
    def _analyze_warnings(self, warnings: List[ProcessingWarning]) -> ErrorReport:
        """Analyze warnings by severity."""
        report = ErrorReport()
        
        for warning in warnings:
            if hasattr(warning, 'severity'):
                severity = warning.severity
                if severity == 'CRITICAL':
                    report.critical_count += 1
                    report.critical_warnings.append(warning)
                elif severity == 'ERROR':
                    report.error_count += 1
                    report.error_warnings.append(warning)
                elif severity == 'WARNING':
                    report.warning_count += 1
                elif severity == 'INFO':
                    report.info_count += 1
            else:
                # String warnings - check for severity keywords
                warning_str = str(warning)
                if 'CRITICAL' in warning_str:
                    report.critical_count += 1
                    report.critical_warnings.append(warning)
                elif 'ERROR' in warning_str:
                    report.error_count += 1
                    report.error_warnings.append(warning)
                else:
                    report.warning_count += 1
                    
        return report
    
    def _run_bean_check(self, ledger_path: str) -> List[Any]:
        """Run bean-check validation."""
        if not os.path.exists(ledger_path):
            logger.warning(f"Ledger file not found: {ledger_path}")
            return []
            
        try:
            entries, errors, options = loader.load_file(ledger_path)
            return errors
        except Exception as e:
            logger.error(f"Failed to load ledger: {e}")
            return []
    
    def _categorize_errors(self, validation_errors: List[Any]) -> Dict[str, List[Any]]:
        """Categorize validation errors by type."""
        categories = defaultdict(list)
        
        for error in validation_errors:
            error_type = type(error).__name__
            categories[error_type].append(error)
            
        return dict(categories)
    
    def _write_envelope_states(self, envelopes: List[Envelope]):
        """Write complete state of all envelopes, especially those with issues."""
        # Separate envelopes by whether they have issues
        envelopes_with_issues = []
        envelopes_without_issues = []

        for env in envelopes:
            has_issues = False

            # Check for warnings
            if hasattr(env, 'warnings') and env.warnings:
                has_issues = True

            # Check if discarded
            if env.state == 'discarded' or env.is_discarded():
                has_issues = True

            # Check for ERROR/CRITICAL in history
            for entry in env.history:
                if 'ERROR' in str(entry.action) or 'CRITICAL' in str(entry.action):
                    has_issues = True
                    break

            if has_issues:
                envelopes_with_issues.append(env)
            else:
                envelopes_without_issues.append(env)

        # Write envelopes with issues
        if envelopes_with_issues:
            issues_path = self.log_dir / "envelopes_with_issues.json"
            envelope_data = []
            for env in envelopes_with_issues:
                data = env.to_dict()
                # Add full transaction details
                data['transaction_full'] = {
                    'date': str(env.transaction.date),
                    'flag': env.transaction.flag,
                    'payee': env.transaction.payee,
                    'narration': env.transaction.narration,
                    'tags': list(env.transaction.tags) if env.transaction.tags else [],
                    'links': list(env.transaction.links) if env.transaction.links else [],
                    'meta': dict(env.transaction.meta) if env.transaction.meta else {},
                    'postings': [
                        {
                            'account': p.account,
                            'units': str(p.units) if p.units else None,
                            'cost': str(p.cost) if p.cost else None,
                            'price': str(p.price) if p.price else None,
                            'flag': p.flag,
                            'meta': dict(p.meta) if p.meta else {}
                        }
                        for p in env.transaction.postings
                    ]
                }
                # Add warning details
                if env.warnings:
                    data['warnings_detailed'] = []
                    for w in env.warnings:
                        if hasattr(w, '__dict__'):
                            data['warnings_detailed'].append(w.__dict__)
                        else:
                            data['warnings_detailed'].append(str(w))
                envelope_data.append(data)

            with open(issues_path, 'wb') as f:
                f.write(orjson.dumps(envelope_data, default=str))
            logger.info(f"Wrote {len(envelopes_with_issues)} envelopes with issues to {issues_path}")

        # Also write ALL envelopes for complete picture
        envelopes_with_issues_set = set(id(e) for e in envelopes_with_issues)
        all_path = self.log_dir / "all_envelopes_full.json"
        all_data = []
        for env in envelopes:
            data = env.to_dict()
            # Add issue flag
            data['has_issues'] = id(env) in envelopes_with_issues_set
            # Add full transaction data from envelope (deferred posting architecture)
            data['transaction_full'] = {
                'date': str(env.date),
                'flag': env.flag,
                'payee': env.payee,
                'narration': env.narration,
                'tags': list(env.beancount_tags) if env.beancount_tags else [],
                'links': list(env.links) if env.links else [],
                'meta': dict(env.metadata) if env.metadata else {},
                'postings': []  # No postings in deferred posting architecture until PostingWriter
            }
            all_data.append(data)

        with open(all_path, 'wb') as f:
            f.write(orjson.dumps(all_data, default=str))
        logger.info(f"Wrote complete state of {len(envelopes)} envelopes to {all_path}")
    
    def _write_errors_log(self, report: ErrorReport):
        """Write error_report.log with CRITICAL/ERROR warnings and validation errors."""
        errors_path = self.log_dir / "error_report.log"
        
        with open(errors_path, 'w', encoding='utf-8') as f:
            f.write("=" * 80 + "\n")
            f.write("PIPELINE ERROR LOG\n")
            f.write("=" * 80 + "\n")
            f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"Critical: {report.critical_count}, Errors: {report.error_count}\n")
            f.write(f"Validation Errors: {len(report.validation_errors)}\n")
            f.write("=" * 80 + "\n\n")
            
            # Write CRITICAL warnings
            if report.critical_warnings:
                f.write("CRITICAL WARNINGS\n")
                f.write("-" * 40 + "\n")
                for i, warning in enumerate(report.critical_warnings, 1):
                    f.write(f"\nCritical #{i}:\n")
                    if hasattr(warning, 'message'):
                        f.write(f"  Message: {warning.message}\n")
                    if hasattr(warning, 'processor_name'):
                        f.write(f"  Component: {warning.processor_name}\n")
                    if hasattr(warning, 'details'):
                        f.write(f"  Details: {self._truncate_large_details(warning.details)}\n")
                    if not hasattr(warning, 'message'):
                        f.write(f"  {warning}\n")
                f.write("\n")
            
            # Write ERROR warnings
            if report.error_warnings:
                f.write("ERROR WARNINGS\n")
                f.write("-" * 40 + "\n")
                for i, warning in enumerate(report.error_warnings, 1):
                    f.write(f"\nError #{i}:\n")
                    if hasattr(warning, 'message'):
                        f.write(f"  Message: {warning.message}\n")
                    if hasattr(warning, 'processor_name'):
                        f.write(f"  Component: {warning.processor_name}\n")
                    if hasattr(warning, 'details'):
                        f.write(f"  Details: {self._truncate_large_details(warning.details)}\n")
                    if not hasattr(warning, 'message'):
                        f.write(f"  {warning}\n")
                f.write("\n")
            
            # Write validation errors from bean-check
            if report.validation_errors:
                f.write("BEAN-CHECK VALIDATION ERRORS\n")
                f.write("-" * 40 + "\n")
                for i, error in enumerate(report.validation_errors, 1):
                    try:
                        f.write(f"\nError #{i}:\n{self._safe_serialize_error(error)}\n")
                    except Exception as e:
                        logger.error(f"Failed to write error #{i}: {e}")
                        f.write(f"  [ERROR: Could not format this error - {type(error).__name__}]")
                f.write("\n")
            
            if not report.has_critical_issues:
                f.write("✅ No critical issues found\n")
                
        logger.info(f"Wrote error log to {errors_path}")
    
    def _truncate_large_details(self, details: Any, max_list_items: int = 5) -> str:
        """
        Truncate large data structures in error details for readability.

        Args:
            details: Details object to potentially truncate
            max_list_items: Maximum number of list items to show before truncating

        Returns:
            String representation with large structures truncated
        """
        if not isinstance(details, dict):
            return str(details)

        # Create a copy to modify
        truncated = {}

        for key, value in details.items():
            if isinstance(value, list) and len(value) > max_list_items:
                # For large lists, show first few items and indicate truncation
                truncated[key] = (
                    f"[{len(value)} items - showing first {max_list_items}] "
                    f"{value[:max_list_items]}"
                )
                # Add helpful message for merge_audit specifically
                if key == 'merge_audit':
                    truncated[key] += f"\n       See forensic.md for complete merge audit (full list of {len(value)} merged transactions)"
            else:
                truncated[key] = value

        return str(truncated)

    def _safe_serialize_error(self, error: Any) -> str:
        """
        Safely serialize a beancount error object to a string, avoiding direct
        calls to str(error) which may be corrupted.
        """
        if error is None:
            return "None"

        # Basic sanitization function for strings
        def sanitize_string(s):
            if isinstance(s, str):
                return s.encode('utf-8', 'replace').decode('utf-8')
            return s

        # Recursively sanitize dictionary values
        def sanitize_dict(d):
            if not isinstance(d, dict):
                return d
            return {sanitize_string(k): sanitize_string(v) for k, v in d.items()}

        parts = [f"  Type: {type(error).__name__}"]

        # Safely access and serialize 'source'
        source = getattr(error, 'source', None)
        if source:
            # Extract envelope_id for easier debugging
            envelope_id = source.get('envelope_id') if isinstance(source, dict) else None
            if envelope_id:
                parts.append(f"  🔍 Envelope ID: {envelope_id}")  # Highlight for easier searching
            parts.append(f"  Source: {sanitize_dict(source)}")

        # Safely access and serialize 'message'
        message = getattr(error, 'message', None)
        if message:
            parts.append(f"  Message: {sanitize_string(message)}")

        # Safely access and serialize 'entry'
        entry = getattr(error, 'entry', None)
        if entry and isinstance(entry, Transaction):
            # Format the transaction safely without calling its potentially corrupt __str__ or __repr__
            formatted_entry = self._format_transaction(entry)
            parts.append(f"  Entry:\n---\n{formatted_entry}\n---")
        elif entry:
            # Fallback for non-Transaction entries
            entry_repr = repr(entry)
            parts.append(f"  Entry: {sanitize_string(entry_repr)}")
            
        return "\n".join(parts)

    
    def _write_error_summary(self, report: ErrorReport, pipeline_stats: Dict[str, Any]):
        """Write error_summary.log with categorized analysis."""
        summary_path = self.log_dir / "error_summary.log"
        
        with open(summary_path, 'w', encoding='utf-8') as f:
            f.write("=" * 80 + "\n")
            f.write("PIPELINE ERROR SUMMARY & CATEGORIZATION\n")
            f.write("=" * 80 + "\n")
            f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("=" * 80 + "\n\n")
            
            # Overall statistics
            f.write("OVERALL STATISTICS\n")
            f.write("-" * 40 + "\n")
            f.write(f"Pipeline Warnings by Severity:\n")
            f.write(f"  CRITICAL: {report.critical_count}\n")
            f.write(f"  ERROR:    {report.error_count}\n")
            f.write(f"  WARNING:  {report.warning_count}\n")
            f.write(f"  INFO:     {report.info_count}\n")
            f.write(f"\n")
            f.write(f"Bean-check Validation Errors: {len(report.validation_errors)}\n")
            f.write("\n")
            
            # Validation error categories
            if report.error_categories:
                f.write("VALIDATION ERROR CATEGORIES\n")
                f.write("-" * 40 + "\n")
                
                # Sort by count
                sorted_categories = sorted(
                    report.error_categories.items(),
                    key=lambda x: len(x[1]),
                    reverse=True
                )
                
                for error_type, errors in sorted_categories:
                    f.write(f"\n{error_type}: {len(errors)} errors\n")
                    
                    # Show first few examples
                    for error in errors[:3]:
                        error_str = self._safe_serialize_error(error)
                        # Sanitize for single-line summary
                        error_str_single_line = error_str.replace('\n', ' ').replace('\r', '')
                        if len(error_str_single_line) > 100:
                            error_str_single_line = error_str_single_line[:97] + "..."
                        f.write(f"  - {error_str_single_line}\n")
                    
                    if len(errors) > 3:
                        f.write(f"  ... and {len(errors) - 3} more\n")
                f.write("\n")
            
            # Problem accounts (extract from errors)
            problem_accounts = self._extract_problem_accounts(report.validation_errors)
            if problem_accounts:
                f.write("PROBLEM ACCOUNTS\n")
                f.write("-" * 40 + "\n")
                for account, count in sorted(problem_accounts.items(), key=lambda x: x[1], reverse=True)[:10]:
                    f.write(f"  {account}: {count} errors\n")
                f.write("\n")
            
            # Stage-specific issues
            if pipeline_stats:
                f.write("PIPELINE STAGE ANALYSIS\n")
                f.write("-" * 40 + "\n")
                
                if 'stage1' in pipeline_stats:
                    stage1 = pipeline_stats['stage1']
                    f.write(f"Stage 1 (Ingestion):\n")
                    f.write(f"  Envelopes created: {stage1.get('envelopes_created', 0)}\n")
                    f.write(f"  Files processed: {stage1.get('files_processed', 0)}\n")
                    
                if 'stage2' in pipeline_stats:
                    stage2 = pipeline_stats['stage2']
                    f.write(f"\nStage 2 (Reconciliation):\n")
                    f.write(f"  Sets created: {stage2.get('reconciliation_sets_created', 0)}\n")
                    f.write(f"  Duplicates found: {stage2.get('duplicates_found', 0)}\n")
                    f.write(f"  Conflicts resolved: {stage2.get('conflicts_resolved', 0)}\n")
                    
                if 'stage3' in pipeline_stats:
                    stage3 = pipeline_stats['stage3']
                    f.write(f"\nStage 3 (Enhancement):\n")
                    f.write(f"  Transactions enhanced: {stage3.get('transactions_enhanced', 0)}\n")
                    f.write(f"  Warnings generated: {stage3.get('warnings_generated', 0)}\n")
                    
                f.write("\n")
            
            # Recommendations
            f.write("RECOMMENDED ACTIONS\n")
            f.write("-" * 40 + "\n")
            
            if report.critical_count > 0:
                f.write("1. Review CRITICAL warnings in error_report.log immediately\n")
                f.write("2. Check forensic.md for detailed analysis\n")
            
            if 'BalanceError' in report.error_categories:
                f.write("3. Review balance assertions - possible missing transactions\n")
                
            if 'ReductionError' in report.error_categories:
                f.write("4. Check for missing purchase transactions (cost basis issues)\n")
                
            if not report.has_critical_issues:
                f.write("✅ No critical issues - pipeline ran successfully\n")
                
        logger.info(f"Wrote error summary to {summary_path}")
    
    def _write_forensic_report(self, report: ErrorReport, 
                               envelopes: List[Envelope],
                               canonical_transactions: List[Transaction],
                               pipeline_stats: Dict[str, Any],
                               steel_thread_monitor=None):
        """Write detailed forensic.md report for critical issues.
        
        Args:
            report: ErrorReport with analysis
            envelopes: Transaction envelopes
            canonical_transactions: Final transactions
            pipeline_stats: Pipeline statistics
            steel_thread_monitor: Optional SteelThreadMonitor instance
        """
        forensic_path = self.log_dir / "forensic.md"
        
        with open(forensic_path, 'w', encoding='utf-8') as f:
            # Header
            f.write("# Pipeline Forensic Report\n\n")
            f.write(f"**Generated**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"**Log Directory**: {self.log_dir}\n\n")
            
            # Executive Summary
            f.write("## Executive Summary\n\n")
            f.write(f"**Status**: {'❌ FAILED' if report.has_critical_issues else '✅ SUCCESS'}\n\n")
            
            f.write("| Metric | Count |\n")
            f.write("|--------|-------|\n")
            f.write(f"| Input Envelopes | {len(envelopes)} |\n")
            f.write(f"| Canonical Transactions | {len(canonical_transactions)} |\n")
            f.write(f"| CRITICAL Warnings | {report.critical_count} |\n")
            f.write(f"| ERROR Warnings | {report.error_count} |\n")
            f.write(f"| Validation Errors | {len(report.validation_errors)} |\n")
            f.write("\n")
            
            # Critical Warnings Detail
            if report.critical_warnings:
                f.write("## Critical Warnings\n\n")
                f.write("These require immediate attention:\n\n")
                
                for i, warning in enumerate(report.critical_warnings, 1):
                    f.write(f"### Critical Warning #{i}\n\n")
                    if hasattr(warning, '__dict__'):
                        for key, value in warning.__dict__.items():
                            f.write(f"- **{key}**: {value}\n")
                    else:
                        f.write(f"{warning}\n")
                    f.write("\n")
            
            # Discarded Envelopes Analysis
            discarded = [env for env in envelopes if env.is_discarded()]
            if discarded:
                f.write("## Discarded Envelopes\n\n")
                f.write(f"Found {len(discarded)} discarded envelopes during reconciliation:\n\n")
                
                for env in discarded[:5]:  # Show first 5
                    f.write(f"### {env.source} - {env.transaction.narration}\n\n")
                    f.write(f"- **Date**: {env.transaction.date}\n")
                    f.write(f"- **State**: {env.state}\n")
                    f.write(f"- **Reason**: {self.get_discard_reason(env)}\n")
                    f.write("\n")
                    
                if len(discarded) > 5:
                    f.write(f"*... and {len(discarded) - 5} more discarded envelopes*\n\n")
            
            # Validation Error Analysis
            if report.validation_errors:
                f.write("## Validation Errors\n\n")
                
                # Group by type
                for error_type, errors in report.error_categories.items():
                    f.write(f"### {error_type} ({len(errors)} errors)\n\n")
                    
                    # Show first few
                    for error in errors[:3]:
                        f.write(f"```\n{self._safe_serialize_error(error)}\n```\n\n")
                    
                    if len(errors) > 3:
                        f.write(f"*... and {len(errors) - 3} more {error_type} errors*\n\n")
            
            # Processing History for Failed Transactions
            if report.has_critical_issues and envelopes:
                f.write("## Processing History\n\n")
                f.write("Detailed history for transactions with issues:\n\n")
                
                # Find envelopes with warnings or errors
                problem_envelopes = [
                    env for env in envelopes 
                    if any('ERROR' in str(e) or 'CRITICAL' in str(e) 
                          for e in env.history)
                ]
                
                for env in problem_envelopes[:5]:
                    f.write(f"### {env.envelope_id}: {env.narration if hasattr(env, 'narration') else 'No narration'}\n\n")
                    f.write(env.get_history_summary())
                    f.write("\n")
                    
                if len(problem_envelopes) > 5:
                    f.write(f"*... and {len(problem_envelopes) - 5} more envelopes with issues*\n\n")
            
            # Steel Thread Architecture Report
            if steel_thread_monitor and steel_thread_monitor.has_violations():
                f.write("## Steel Thread Architecture Analysis\n\n")
                f.write("**⚠️ Steel Thread violations detected**\n\n")
                
                # Get the forensic report from the monitor
                steel_thread_report = steel_thread_monitor.generate_forensic_report()
                f.write(steel_thread_report)
                f.write("\n\n")
            elif steel_thread_monitor:
                f.write("## Steel Thread Architecture Analysis\n\n")
                f.write("✅ **No Steel Thread violations detected**\n\n")
                f.write("All processors are compliant with the Steel Thread Architecture:\n")
                f.write("- All processors return proper 3-tuple (transactions, warnings, metadata)\n")
                f.write("- No data loss detected during processing\n")
                f.write("- All contract violations properly handled\n\n")
            
            # Recommendations
            f.write("## Recommendations\n\n")
            
            if report.critical_count > 0:
                f.write("1. **Address CRITICAL warnings first** - These indicate architectural failures\n")
                
            if 'BalanceError' in report.error_categories:
                f.write("2. **Check balance assertions** - Missing transactions or incorrect amounts\n")
                
            if 'ReductionError' in report.error_categories:
                f.write("3. **Review cost basis** - Missing purchase transactions for sales\n")
                
            if discarded:
                f.write("4. **Review discarded envelopes** - Check reconciliation logic\n")
                
            # --- Forensic Summary ---
            f.write("\n## Files Generated\n\n")
            f.write("The following files contain additional details:\n\n")
            f.write("- `error_report.log` - CRITICAL/ERROR warnings and validation errors\n")
            f.write("- `error_summary.log` - Categorized error analysis\n")
            f.write("- `reconciliation_report.txt` - Reconciliation details\n")
            f.write("- `canonical_transactions.json` - Final envelope data\n")
            f.write("- `discarded_envelopes.txt` - Details of discarded transactions\n")
            
        logger.info(f"Wrote forensic report to {forensic_path}")
    
    def _extract_problem_accounts(self, validation_errors: List[Any]) -> Dict[str, int]:
        """Extract accounts mentioned in validation errors."""
        accounts = defaultdict(int)
        
        for error in validation_errors:
            error_str = self._safe_serialize_error(error)
            # Look for account patterns (Assets:*, Liabilities:*, etc.)
            import re
            pattern = r'(Assets|Liabilities|Equity|Income|Expenses):[^\\s,)]+' 
            matches = re.findall(pattern, error_str)
            for match in matches:
                # This is a basic extraction; might need refinement
                # For now, we just use the full matched string
                accounts[match] += 1
                
        return dict(accounts)
    
    def get_discard_reason(self, envelope: Envelope) -> str:
        """Extract discard reason from envelope history."""
        for entry in reversed(envelope.history):
            if 'Discarded' in entry.action:
                # Simple extraction, assuming reason is in the action string
                return entry.action.split(':', 1)[-1].strip()
        return "No reason found"

    def _write_canonical_transactions(self, transactions: List[Transaction]):
        """Write the final canonical transactions to a beancount file."""
        from .envelope import Envelope

        # --- JSON Output ---
        json_path = self.log_dir / "canonical_transactions.json"

        # Get envelopes from metadata
        envelopes_map = {}
        for txn in transactions:
            if 'envelope_id' in txn.meta:
                envelope_id = txn.meta['envelope_id']
                if envelope_id not in envelopes_map and 'envelope' in txn.meta:
                    try:
                        envelope = txn.meta['envelope']
                        if isinstance(envelope, Envelope):
                             envelopes_map[envelope_id] = envelope.to_dict()
                    except Exception:
                        pass

        with open(json_path, 'wb') as f:
            f.write(orjson.dumps(list(envelopes_map.values()), default=str))

        # --- Beancount Output ---
        beancount_path = self.log_dir / "canonical_transactions.beancount"
        
        # Sort transactions by date
        transactions.sort(key=lambda x: x.date)
        
        with open(beancount_path, 'w', encoding='utf-8') as f:
            for txn in transactions:
                f.write(self._format_transaction(txn))
                f.write("\n")
                
        logger.info(f"Wrote {len(transactions)} canonical transactions to {beancount_path}")

    def _format_transaction(self, txn: Transaction) -> str:
        """Format a beancount transaction to a string."""
        
        lines = []
        
        # Date and details - use safe access
        date_str = self._safe_format_value(txn.date)
        flag = self._safe_format_value(txn.flag)
        payee = self._safe_format_value(getattr(txn, 'payee', ''))
        narration = self._safe_format_value(getattr(txn, 'narration', ''))
        lines.append(f'{date_str} {flag} "{payee}" "{narration}"')
        
        # Metadata - safely format
        meta = getattr(txn, 'meta', {})
        if meta and isinstance(meta, dict):
            for key, value in sorted(meta.items()):
                # Skip envelope object
                if key == 'envelope':
                    continue
                safe_key = self._safe_format_value(key)
                safe_value = self._safe_format_value(value)
                lines.append(f"  {safe_key}: \"{safe_value}\"")
        
        # Postings - safely format
        postings = getattr(txn, 'postings', [])
        if postings:
            for post in postings:
                account = self._safe_format_value(getattr(post, 'account', 'Unknown'))
                units = self._safe_format_value(getattr(post, 'units', None))
                cost = self._safe_format_value(getattr(post, 'cost', None))
                price = self._safe_format_value(getattr(post, 'price', None))
                
                # Format units specially if it looks like an Amount
                units_str = ""
                if units and units != "None":
                    units_str = units
                
                # Format cost specially if present
                cost_str = ""
                if cost and cost != "None":
                    cost_str = f" {{{cost}}}"
                    
                # Format price specially if present
                price_str = ""
                if price and price != "None":
                    price_str = f" @ {price}"

                line = f"  {account:<40} {units_str:>30}{cost_str}{price_str}"
                lines.append(line)
            
        return "\n".join(lines)
    
    def _safe_format_value(self, value: Any, max_depth: int = 5) -> str:
        """
        Recursively and safely format any value to a string.
        
        This is a generic, blind formatter that doesn't assume anything about
        the structure or field names. It handles all Python types and special
        beancount types safely.
        
        Args:
            value: Any value to format
            max_depth: Maximum recursion depth to prevent infinite loops
            
        Returns:
            A safe string representation of the value
        """
        # Prevent infinite recursion
        if max_depth <= 0:
            return "..."
        
        # Handle None
        if value is None:
            return "None"
        
        # Handle basic types that are already safe
        if isinstance(value, (str, int, float, bool)):
            return str(value)
        
        # Handle bytes - decode safely
        if isinstance(value, bytes):
            return value.decode('utf-8', 'replace')
        
        # Handle datetime types
        if hasattr(value, 'strftime'):
            try:
                return value.strftime('%Y-%m-%d')
            except:
                return str(value)
        
        # Handle Decimal
        if hasattr(value, '__class__') and value.__class__.__name__ == 'Decimal':
            return str(value)
        
        # Handle beancount's MISSING type
        if hasattr(value, '__class__') and value.__class__.__name__ == 'MISSING':
            return "MISSING"
        
        # Handle namedtuple-like objects (including beancount Amount, Cost, etc.)
        if hasattr(value, '_fields'):
            try:
                # Format as "TypeName(field1=value1, field2=value2)"
                class_name = value.__class__.__name__
                field_strs = []
                for field in value._fields:
                    field_value = getattr(value, field, None)
                    safe_value = self._safe_format_value(field_value, max_depth - 1)
                    # Special handling for Amount objects - format more concisely
                    if class_name == 'Amount' and field == 'number':
                        field_strs.append(safe_value)
                    elif class_name == 'Amount' and field == 'currency':
                        field_strs.append(safe_value)
                    else:
                        field_strs.append(f"{field}={safe_value}")
                
                # Special compact formatting for Amount objects
                if class_name == 'Amount' and len(field_strs) == 2:
                    return f"{field_strs[0]} {field_strs[1]}"
                return f"{class_name}({', '.join(field_strs)})"
            except Exception as e:
                # Fallback to str
                try:
                    return str(value)
                except:
                    return f"<{value.__class__.__name__} object>"
        
        # Handle dictionaries
        if isinstance(value, dict):
            try:
                items = []
                for k, v in value.items():
                    safe_key = self._safe_format_value(k, max_depth - 1)
                    safe_val = self._safe_format_value(v, max_depth - 1)
                    items.append(f"{safe_key}: {safe_val}")
                return "{" + ", ".join(items) + "}"
            except:
                return "{...}"
        
        # Handle lists and tuples
        if isinstance(value, (list, tuple)):
            try:
                items = [self._safe_format_value(item, max_depth - 1) for item in value[:10]]  # Limit to first 10
                if len(value) > 10:
                    items.append("...")
                bracket = "[" if isinstance(value, list) else "("
                close = "]" if isinstance(value, list) else ")"
                return bracket + ", ".join(items) + close
            except:
                return "[...]" if isinstance(value, list) else "(...)"
        
        # Handle objects with useful __dict__
        if hasattr(value, '__dict__') and value.__dict__:
            try:
                class_name = value.__class__.__name__
                # Don't recurse into complex objects, just show the type
                return f"<{class_name} object>"
            except:
                return "<object>"
        
        # Final fallback - try str() but catch any exceptions
        try:
            result = str(value)
            # Sanitize the string to remove any problematic characters
            return result.encode('utf-8', 'replace').decode('utf-8')
        except:
            try:
                return f"<{value.__class__.__name__} object>"
            except:
                return "<unprintable object>"
    
    def _safe_format_cost(self, cost: Any) -> str:
        """Safely format a beancount Cost or CostSpec object to a string."""
        # Just use our generic safe formatter - it handles everything safely
        return self._safe_format_value(cost)
    
    def _write_lot_lineage(self, lot_lineage_data: Dict[str, Any]):
        """Write lot lineage data to a JSON file for analysis."""
        path = self.log_dir / "lot_registry.json"
        with open(path, 'wb') as f:
            f.write(orjson.dumps(lot_lineage_data, default=str))
        logger.info(f"Wrote lot registry data to {path}")