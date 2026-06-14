"""
Progress Tracking Module

Tracks and logs import progress across runs, maintaining a persistent log
of error counts and import statistics for trend analysis.
"""

import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from beancount import loader


class ProgressTracker:
    """Tracks import progress and maintains progress log."""
    
    def __init__(self, log_dir: str = "logs"):
        """Initialize progress tracker.
        
        Args:
            log_dir: Directory to store progress log
        """
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(exist_ok=True)
        self.progress_file = self.log_dir / "progress.log"
        
    def analyze_errors(self, ledger_path: str, dropped_count: int = 0) -> Dict[str, int]:
        """Analyze errors from bean-check validation.
        
        Args:
            ledger_path: Path to the ledger file to validate
            dropped_count: Number of CSV entries dropped during import
            
        Returns:
            Dictionary of error counts by category
        """
        error_counts = {
            'total': 0,
            'csv': dropped_count,
            'reduction': 0,
            'booking': 0,
            'balance': 0,
            'transaction_balance': 0,
            'validation': 0,
            'commodity': 0,
            'account': 0,
            'other': 0,
            'duplicates': 0
        }
        
        # If ledger doesn't exist, return early with just CSV errors
        if not Path(ledger_path).exists():
            error_counts['total'] = dropped_count
            return error_counts
        
        try:
            # Load the ledger file to get validation errors
            entries, errors, _ = loader.load_file(str(Path(ledger_path).absolute()))
            
            # Categorize errors
            for error in errors:
                error_str = str(error)
                error_type = type(error).__name__
                
                # Check for duplicate warnings (these are filtered out)
                if self._is_duplicate_warning(error):
                    error_counts['duplicates'] += 1
                # Reduction/Inventory errors
                elif 'ReductionError' in error_str or 'No position matches' in error_str:
                    error_counts['reduction'] += 1
                # Booking errors (including AmbiguousMatchError)
                elif 'Booking' in error_str or 'AmbiguousMatchError' in error_type:
                    error_counts['booking'] += 1
                # Commodity errors
                elif 'Invalid commodity' in error_str or 'Unknown commodity' in error_str:
                    error_counts['commodity'] += 1
                # Account errors
                elif 'Invalid reference to unknown account' in error_str:
                    error_counts['account'] += 1
                # Balance errors
                elif 'BalanceError' in error_str or 'Balance failed' in error_str:
                    error_counts['balance'] += 1
                # Transaction balance errors
                elif 'Transaction does not balance' in error_str:
                    error_counts['transaction_balance'] += 1
                # Validation errors (ValidationError type)
                elif 'ValidationError' in error_type:
                    error_counts['validation'] += 1
                # LoadError and other errors
                else:
                    error_counts['other'] += 1
            
            # Calculate total (excluding duplicate warnings as they're filtered)
            error_counts['total'] = (
                len(errors) - error_counts['duplicates'] + dropped_count
            )
            
        except Exception as e:
            print(f"  ⚠️  Error analyzing ledger: {e}")
            error_counts['total'] = dropped_count
            
        return error_counts
    
    def _is_duplicate_warning(self, error) -> bool:
        """Check if an error is an expected duplicate warning.
        
        Args:
            error: The error to check
            
        Returns:
            True if this is an expected duplicate warning
        """
        error_str = str(error)
        return 'Duplicate' in error_str and 'filename' in error_str.lower()
    
    def update_progress_log(self, error_counts: Dict[str, int]) -> None:
        """Append a row to progress log with current error counts.
        
        Args:
            error_counts: Dictionary with error category counts
        """
        # Check if file exists to determine if we need headers
        file_exists = self.progress_file.exists()
        
        with open(self.progress_file, 'a', encoding='utf-8') as f:
            if not file_exists:
                # Write headers
                f.write("Timestamp                | Total | CSV | Reduction | Booking | Balance | Txn Balance | Validation | Commodity | Account | Other | Duplicates\n")
                f.write("-------------------------|-------|-----|-----------|---------|---------|-------------|------------|-----------|---------|-------|------------\n")
            
            # Write data row
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            total = error_counts.get('total', 0)
            csv_errors = error_counts.get('csv', 0)
            reduction = error_counts.get('reduction', 0)
            booking = error_counts.get('booking', 0)
            balance = error_counts.get('balance', 0)
            txn_balance = error_counts.get('transaction_balance', 0)
            validation = error_counts.get('validation', 0)
            commodity = error_counts.get('commodity', 0)
            account = error_counts.get('account', 0)
            other = error_counts.get('other', 0)
            duplicates = error_counts.get('duplicates', 0)
            
            f.write(f"{timestamp:24} | {total:5d} | {csv_errors:3d} | {reduction:9d} | {booking:7d} | {balance:7d} | {txn_balance:11d} | {validation:10d} | {commodity:9d} | {account:7d} | {other:5d} | {duplicates:10d}\n")
    
    def log_import_summary(
        self,
        error_counts: Dict[str, int],
        envelope_count: int,
        transaction_count: int,
        manual_count: int,
        csv_count: int,
        reconciliation_stats: Optional[Dict] = None
    ) -> None:
        """Log a summary of the import run.
        
        Args:
            error_counts: Dictionary of error counts by category
            envelope_count: Total number of envelopes processed
            transaction_count: Total number of transactions output
            manual_count: Number of manual transactions
            csv_count: Number of CSV transactions
            reconciliation_stats: Optional reconciliation statistics
        """
        print(f"\n📊 Progress Log Update:")
        print(f"  Total Errors: {error_counts['total']}")
        print(f"  Balance Errors: {error_counts['balance']}")
        print(f"  Reduction Errors: {error_counts['reduction']}")
        print(f"  Transaction Balance: {error_counts['transaction_balance']}")
        
        # Update the progress log file
        self.update_progress_log(error_counts)
        print(f"  ✅ Updated: {self.progress_file}")
        
        # Show trend if we have history
        self._show_trend(error_counts)
    
    def _show_trend(self, current_counts: Dict[str, int]) -> None:
        """Show trend compared to previous run.
        
        Args:
            current_counts: Current error counts
        """
        if not self.progress_file.exists():
            return
            
        # Read all lines to get previous run
        with open(self.progress_file, 'r') as f:
            lines = f.readlines()
            
        # Skip header lines and get data lines
        data_lines = [l for l in lines if '|' in l and not l.startswith('-') and not l.startswith('Timestamp')]
        if len(data_lines) < 1:  # Need at least one previous run
            return
            
        # Parse the last data line (most recent previous run)
        prev_line = data_lines[-1] if data_lines else None
        if not prev_line:
            return
            
        try:
            parts = prev_line.split('|')
            prev_total = int(parts[1].strip())
            prev_balance = int(parts[5].strip())
            
            # Calculate changes
            total_change = current_counts['total'] - prev_total
            balance_change = current_counts['balance'] - prev_balance
            
            # Show trend
            if total_change != 0 or balance_change != 0:
                print("\n  📈 Trend from previous run:")
                if total_change < 0:
                    print(f"    ✅ Total errors: {prev_total} → {current_counts['total']} (↓{abs(total_change)})")
                elif total_change > 0:
                    print(f"    ❌ Total errors: {prev_total} → {current_counts['total']} (↑{total_change})")
                else:
                    print(f"    ➡️  Total errors: {current_counts['total']} (unchanged)")
                    
                if balance_change < 0:
                    print(f"    ✅ Balance errors: {prev_balance} → {current_counts['balance']} (↓{abs(balance_change)})")
                elif balance_change > 0:
                    print(f"    ❌ Balance errors: {prev_balance} → {current_counts['balance']} (↑{balance_change})")
                else:
                    print(f"    ➡️  Balance errors: {current_counts['balance']} (unchanged)")
                    
        except (ValueError, IndexError):
            # Can't parse previous line, skip trend
            pass