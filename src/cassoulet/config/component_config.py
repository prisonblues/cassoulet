"""
Comprehensive pipeline configuration with all configurable parameters.

This module provides a single source of truth for all pipeline configuration
values, eliminating hardcoded constants throughout the codebase.
All configuration values are centralized here with comprehensive documentation.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, Tuple, Optional, List, Any
from pathlib import Path
import logging

# Import custom exceptions for Steel Thread compliance
from cassoulet.base.exceptions import (
    PathNotFoundWarning,
    PathNotDirectoryError,
    DirectoryCreationError,
    NoFilesFoundInfo
)


# Steel Thread compliant path utilities
def get_files_from_directory(
    directory: str,
    pattern: str = "*.beancount",
    purpose: str = "files",
    logger: Optional[logging.Logger] = None
) -> List[str]:
    """
    Steel Thread compliant directory file listing.

    Principle #1: Zero Silent Failures - Always log when directory doesn't exist
    or when no files are found. Uses custom exceptions for consistency.

    Args:
        directory: Directory path to search
        pattern: Glob pattern for files (default: "*.beancount")
        purpose: Description of what files are for (for logging)
        logger: Logger instance (creates one if not provided)

    Returns:
        List of file paths matching pattern, sorted

    Raises:
        PathNotFoundWarning: Logged when directory doesn't exist
        PathNotDirectoryError: Logged when path exists but isn't a directory
        NoFilesFoundInfo: Logged when no files match pattern
    """
    if logger is None:
        logger = logging.getLogger(__name__)

    dir_path = Path(directory)

    if not dir_path.exists():
        # Steel Thread: Document missing directory using custom exception
        warning = PathNotFoundWarning(str(directory), purpose)
        logger.warning(str(warning))
        return []

    if not dir_path.is_dir():
        # Steel Thread: Document non-directory path using custom exception
        error = PathNotDirectoryError(str(directory), purpose)
        logger.error(str(error))
        return []

    files = sorted([str(p) for p in dir_path.glob(pattern)])

    if not files:
        # Steel Thread: Document empty result using custom exception
        info = NoFilesFoundInfo(str(directory), pattern, purpose)
        logger.info(str(info))
    else:
        # When purpose is "manual transactions", we're counting files not transactions
        if purpose == "manual transactions":
            logger.info(f"Found {len(files)} manual transaction files in {directory}")
        else:
            logger.info(f"Found {len(files)} {purpose} in {directory}")

    return files


def ensure_directory_exists(
    directory: str,
    purpose: str = "output",
    create: bool = True,
    logger: Optional[logging.Logger] = None
) -> bool:
    """
    Steel Thread compliant directory creation/verification.

    Principle #1: Zero Silent Failures - Always log directory operations.
    Uses custom exceptions for consistency.

    Args:
        directory: Directory path to ensure exists
        purpose: Description of directory purpose (for logging)
        create: Whether to create if missing
        logger: Logger instance

    Returns:
        True if directory exists (or was created), False otherwise

    Raises:
        PathNotDirectoryError: Logged when path exists but isn't a directory
        DirectoryCreationError: Logged when directory creation fails
        PathNotFoundWarning: Logged when directory doesn't exist and create=False
    """
    if logger is None:
        logger = logging.getLogger(__name__)

    dir_path = Path(directory)

    if dir_path.exists():
        if not dir_path.is_dir():
            error = PathNotDirectoryError(str(directory), purpose)
            logger.error(str(error))
            return False
        logger.debug(f"{purpose} directory exists: {directory}")
        return True

    if create:
        try:
            dir_path.mkdir(parents=True, exist_ok=True)
            logger.info(f"Created {purpose} directory: {directory}")
            return True
        except Exception as e:
            error = DirectoryCreationError(str(directory), purpose, e)
            logger.error(str(error))
            return False
    else:
        warning = PathNotFoundWarning(str(directory), purpose)
        logger.warning(str(warning))
        return False


@dataclass
class TransferScoringConfig:
    """Configuration for transfer scoring logic."""
    
    # Scoring weights (should sum to 100)
    scoring_weights: Dict[str, float] = field(default_factory=lambda: {
        'amount': 50.0,    # Weight for exact amount matching
        'date': 30.0,      # Weight for date proximity
        'account': 10.0,   # Weight for account compatibility
        'narration': 10.0  # Weight for description similarity
    })
    
    # Confidence thresholds
    high_confidence: float = 80.0    # Transactions above this are highly likely matches
    medium_confidence: float = 60.0  # Transactions above this need review
    low_confidence: float = 40.0     # Minimum score to consider
    
    # Amount limits by transaction type (min, max in base currency)
    amount_limits: Dict[str, Tuple[float, float]] = field(default_factory=lambda: {
        'transfer': (0.01, 1_000_000),      # Standard transfers
        'investment': (1.00, 10_000_000),   # Investment transactions
        'dividend': (0.01, 100_000),        # Dividend payments
        'fee': (0.01, 10_000),              # Fee charges
        'interest': (0.01, 10_000),         # Interest payments
        'default': (0.01, 10_000_000)       # Fallback limits
    })
    
    # Matching parameters
    date_tolerance_days: int = 7
    amount_tolerance: Decimal = Decimal('0.01')
    min_score: float = 40.0  # Minimum score for potential matches
    
    # Narration similarity
    narration_similarity_threshold: float = 0.5
    
    # Amount matching multipliers
    equal_amount_multiplier: float = 0.8  # Multiplier for equal vs opposite amounts
    
    # External capital detection
    external_capital_threshold: float = 10000.0  # £10k threshold for external transfers
    
    # Match Prevention Heuristics (Issue #111)
    # Smart amount difference scoring
    amount_increase_penalty: float = -100.0  # More received than sent (impossible)
    max_absolute_difference: float = 20.00   # £20 max difference allowed
    max_percentage_difference: float = 0.30  # 30% max loss allowed
    perfect_match_bonus: float = 20.0        # Bonus for exact amount match
    
    # Same account prevention
    same_account_skip: bool = True           # Skip scoring same-account pairs entirely
    
    # Three-way CSV blocking
    three_way_csv_block: bool = True         # Hard block 3-way CSV matches
    
    # Pending transaction handling
    pending_single_max_score: float = 70.0   # Max score when one transaction is pending
    pending_both_max_score: float = 50.0     # Max score when both are pending
    
    # Enable/disable specific heuristics
    enable_direction_check: bool = True      # Check transactions have opposite directions
    enable_currency_mismatch_check: bool = True  # Block currency/commodity mismatches
    enable_fee_blocking: bool = True         # Block fee transactions from matching
    enable_narration_patterns: bool = True   # Check for non-transfer narration patterns
    enable_circular_detection: bool = True   # Detect circular transfer patterns
    
    def validate(self) -> List[str]:
        """Validate scoring configuration."""
        errors = []
        
        # Check weights sum to 100
        weight_sum = sum(self.scoring_weights.values())
        if abs(weight_sum - 100.0) > 0.01:
            errors.append(f"Scoring weights sum to {weight_sum}, should be 100")
        
        # Check threshold ordering
        if self.high_confidence <= self.medium_confidence:
            errors.append("High confidence must be > medium confidence")
        if self.medium_confidence <= self.low_confidence:
            errors.append("Medium confidence must be > low confidence")
        
        # Check amount limits
        for name, (min_amt, max_amt) in self.amount_limits.items():
            if min_amt >= max_amt:
                errors.append(f"Invalid amount limits for {name}: min >= max")
        
        return errors


@dataclass
class TransferMatchingConfig:
    """Configuration for the TransferMatcher."""
    
    # Date tolerance for matching transfers (in days)
    date_tolerance_days: int = 7
    
    # Amount tolerance for matching (in base currency units)
    amount_tolerance: Decimal = Decimal('0.01')
    
    # Minimum score to consider as potential match
    min_match_score: float = 40.0
    
    # Auto-matching thresholds
    auto_match_high: float = 85.0     # Auto-accept matches above this
    auto_match_medium: float = 60.0   # Flag for review above this
    conflict_threshold: float = 15.0  # Score difference to resolve conflicts
    
    # Matching rules
    require_opposite_amounts: bool = True
    
    perfect_match_threshold: float = 99.0  # NEW: For two-pass matching
    
    def validate(self) -> List[str]:
        """Validate matching configuration."""
        errors = []
        
        if self.auto_match_high <= self.auto_match_medium:
            errors.append("Auto-match high must be > auto-match medium")
        
        if self.min_match_score >= self.auto_match_medium:
            errors.append("Min match score must be < auto-match medium")
        
        return errors


@dataclass
class TransferMergingConfig:
    """Configuration for transfer merging logic."""
    
    # Date tolerance for merging (in days)
    date_tolerance_days: int = 3
    
    # Amount tolerance for merging
    amount_tolerance: Decimal = Decimal('0.01')
    
    # Balance validation tolerance
    balance_tolerance: Decimal = Decimal('0.01')
    
    # Quantity tolerances
    quantity_tolerance: Decimal = Decimal('0.0001')
    
    # Account-specific quantity tolerances (user-provided overrides)
    # Key: (account, commodity), Value: tolerance
    account_quantity_tolerances: Dict[Tuple[str, str], Decimal] = field(default_factory=dict)
    
    # Metadata scoring weights
    metadata_weights: Dict[str, int] = field(default_factory=lambda: {
        'document': 10,          # Points for document metadata
        'paper_id': 10,         # Points for paper/paperless ID
        'paperless_id': 10,     # Alternative key for paperless ID
        'importer_id': 5,       # Points for importer ID
        'base': 1               # Points per metadata field
    })
    
    # Preference for CSV vs manual
    prefer_csv: bool = True
    
    def validate(self) -> List[str]:
        """Validate merging configuration."""
        errors = []
        
        if self.date_tolerance_days < 0:
            errors.append("Date tolerance must be >= 0")
        
        if self.amount_tolerance < 0:
            errors.append("Amount tolerance must be >= 0")
        
        return errors

@dataclass
class ClassificationConfig:
    """Configuration for transaction classification."""
    
    # Classification confidence scores
    standalone_confidence: float = 1.0   # Confidence for standalone transactions
    match_expected_confidence: float = 0.9  # Confidence for expected matches
    unclassified_confidence: float = 0.5    # Default unclassified confidence
    
    # Classification thresholds
    high_confidence_threshold: float = 0.8  # High confidence classification
    default_confidence: float = 0.5         # Default confidence value
    
    def validate(self) -> List[str]:
        """Validate classification configuration."""
        errors = []
        
        if self.high_confidence_threshold <= self.default_confidence:
            errors.append("High confidence threshold must be > default confidence")
        
        return errors


@dataclass
class ValidationConfig:
    """Configuration for validation rules."""
    
    # Year range validation
    min_valid_year: int = 2000
    max_valid_year: int = 2100
    
    # Default query parameters
    default_date_tolerance: int = 7
    default_amount_tolerance: Decimal = Decimal('0.01')
    
    def validate(self) -> List[str]:
        """Validate validation configuration."""
        errors = []
        
        if self.min_valid_year >= self.max_valid_year:
            errors.append("Min valid year must be < max valid year")
        
        return errors


@dataclass
class ProjectPaths:
    """All file/directory paths the pipeline needs to find user data.

    Cassoulet (the framework) defines NO defaults for data paths — the user
    must provide them.  This repo's backward-compatible defaults live in
    InputConfig / OutputConfig below.
    """
    accounts_file: Optional[str] = None         # chart of accounts (.beancount)
    commodities_file: Optional[str] = None      # commodity definitions (.beancount)
    raw_data_dir: Optional[str] = None          # CSV files from institutions
    manual_dir: Optional[str] = None            # manual transactions directory
    template_dir: Optional[str] = None          # beancount templates
    output_dir: Optional[str] = None            # where to write output

    def get_manual_transaction_paths(self) -> List[str]:
        """Get all *.beancount files from manual directory."""
        if not self.manual_dir:
            return []
        return get_files_from_directory(
            directory=self.manual_dir,
            pattern="*.beancount",
            purpose="manual transactions"
        )

    def get_manual_balances_dir(self, subdir: str = "balances") -> str:
        """Get full path to manual balances directory."""
        return f"{self.manual_dir}/{subdir}" if self.manual_dir else ""


@dataclass
class InputConfig:
    """Configuration for input paths.

    Provides backward-compatible defaults that map to this project's layout.
    """

    # Primary input directories
    raw_data_dir: str = "entries/raw"           # CSV files from institutions
    manual_dir: str = "entries/manual"          # Manual transactions and overrides
    template_dir: str = "entries/template"      # Templates for new files

    # Subdirectories
    manual_balances_subdir: str = "balances"    # Under manual_dir

    def get_manual_transaction_paths(self) -> List[str]:
        """Get all *.beancount files from manual directory.

        Returns:
            List of all .beancount file paths in manual_dir.

        Note:
            Steel Thread Compliant: Uses get_files_from_directory utility
            which logs all decisions per Principle #1.
        """
        return get_files_from_directory(
            directory=self.manual_dir,
            pattern="*.beancount",
            purpose="manual transactions"
        )

    def get_manual_balances_dir(self) -> str:
        """Get full path to manual balances directory."""
        return f"{self.manual_dir}/{self.manual_balances_subdir}"

    def validate(self) -> List[str]:
        """Validate input configuration."""
        return []  # Paths will be validated at runtime


@dataclass
class OutputConfig:
    """Configuration for output writing."""

    output_dir: str = "entries/output"

    # File organization
    group_by_year: bool = True
    group_by_institution: bool = True

    # Include options
    include_orphans: bool = True
    generate_main_file: bool = True
    generate_year_files: bool = True

    # File naming patterns
    institution_file_pattern: str = "{institution}_{year}.beancount"
    balance_file_pattern: str = "bank_balances_{year}.beancount"
    main_file_name: str = "importers.beancount"

    def validate(self) -> List[str]:
        """Validate output configuration."""
        return []  # No specific validation needed


@dataclass
class LoggingConfig:
    """Configuration for logging."""
    
    log_level: str = "INFO"
    log_format: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    
    # Separate log levels for components
    component_levels: Dict[str, str] = field(default_factory=lambda: {
        'TransferScorer': 'INFO',
        'TransferMatcher': 'INFO',
        'TransferMerger': 'INFO',
        'ManualTransactionLoader': 'INFO',
        'OutputWriter': 'INFO'
    })
    
    def validate(self) -> List[str]:
        """Validate logging configuration."""
        valid_levels = {'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'}
        errors = []
        
        if self.log_level not in valid_levels:
            errors.append(f"Invalid log level: {self.log_level}")
        
        for component, level in self.component_levels.items():
            if level not in valid_levels:
                errors.append(f"Invalid log level for {component}: {level}")
        
        return errors


@dataclass
class AccountConfig:
    """Configuration for account names used throughout the system."""

    # Virtual tracking accounts
    # This account is used in the "Virtual Tracking Pattern" for commission handling.
    # When a manual BUY transaction includes commission, we generate two transactions:
    # 1. Main purchase with total cost (for correct tax basis)
    # 2. Virtual commission tracking with net-zero P&L impact
    # The virtual transaction uses this contra-income account to balance against
    # the commission expense, allowing commission tracking without affecting P&L.
    capitalized_commissions_income: str = "Income:Investment:CapitalisedCommissions"

    # Default expense accounts
    transfer_leakage_expense: str = "Expenses:TransferLeakage"


@dataclass
class ComponentConfig:
    """Component configurations for the pipeline."""

    # Project paths (user-provided — no framework defaults for data files)
    paths: ProjectPaths = field(default_factory=ProjectPaths)

    # Component configurations
    scoring: TransferScoringConfig = field(default_factory=TransferScoringConfig)
    matching: TransferMatchingConfig = field(default_factory=TransferMatchingConfig)
    merging: TransferMergingConfig = field(default_factory=TransferMergingConfig)
    classification: ClassificationConfig = field(default_factory=ClassificationConfig)
    validation: ValidationConfig = field(default_factory=ValidationConfig)
    input: InputConfig = field(default_factory=InputConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    accounts: AccountConfig = field(default_factory=AccountConfig)
    
    # Global settings
    enable_debug_logging: bool = False
    enable_performance_tracking: bool = False
    dry_run: bool = False
    
    # Feature flags
    enable_transfer_detection: bool = True
    enable_commodity_transfers: bool = True
    enable_sell_processing: bool = True
    
    # Paths (legacy — prefer project_paths above)
    data_dir: Optional[Path] = None
    accounts_file: Optional[Path] = None
    ledger_path: Optional[str] = None  # Path to main ledger file for historical context
    
    # Runtime settings
    timestamp: Optional[str] = None
    
    def get_accounts_file(self) -> str:
        """Resolve the effective accounts file path.

        Priority: project_paths.accounts_file > legacy accounts_file > default.
        """
        if self.paths.accounts_file:
            return self.paths.accounts_file
        if self.accounts_file:
            return str(self.accounts_file)
        return "accounts/accounts.beancount"

    def get_commodities_file(self) -> str:
        """Resolve the effective commodities file path."""
        if self.paths.commodities_file:
            return self.paths.commodities_file
        return "commodities/commodities.beancount"

    def get_template_dir(self) -> str:
        """Resolve the effective template directory."""
        if self.paths.template_dir:
            return self.paths.template_dir
        return self.input.template_dir

    def get_manual_dir(self) -> str:
        """Resolve the effective manual transactions directory."""
        if self.paths.manual_dir:
            return self.paths.manual_dir
        return self.input.manual_dir

    def validate(self) -> List[str]:
        """Validate configuration consistency across all components."""
        errors = []
        
        # Validate each component
        errors.extend(self.scoring.validate())
        errors.extend(self.matching.validate())
        errors.extend(self.merging.validate())
        errors.extend(self.classification.validate())
        errors.extend(self.validation.validate())
        errors.extend(self.output.validate())
        errors.extend(self.logging.validate())
        
        # Cross-component validation
        errors.extend(self._validate_cross_component())
        
        return errors
    
    def _validate_cross_component(self) -> List[str]:
        """Validate consistency across components."""
        errors = []
        
        # Matching thresholds should align with scoring thresholds
        if self.matching.auto_match_high < self.scoring.high_confidence:
            errors.append(
                f"Auto-match high ({self.matching.auto_match_high}) should be >= "
                f"scoring high confidence ({self.scoring.high_confidence})"
            )
        
        # Date tolerances should be consistent
        if self.merging.date_tolerance_days > self.matching.date_tolerance_days:
            errors.append(
                "Merging date tolerance should not exceed matching date tolerance"
            )
        
        # Amount tolerances should be consistent
        if self.merging.amount_tolerance > self.matching.amount_tolerance:
            errors.append(
                "Merging amount tolerance should not exceed matching amount tolerance"
            )
        
        return errors
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert configuration to dictionary for serialization."""
        return {
            'scoring': {
                'scoring_weights': self.scoring.scoring_weights,
                'high_confidence': self.scoring.high_confidence,
                'medium_confidence': self.scoring.medium_confidence,
                'low_confidence': self.scoring.low_confidence,
                'amount_limits': self.scoring.amount_limits,
                'date_tolerance_days': self.scoring.date_tolerance_days,
                'amount_tolerance': str(self.scoring.amount_tolerance),
                'min_score': self.scoring.min_score,
            },
            'matching': {
                'date_tolerance_days': self.matching.date_tolerance_days,
                'amount_tolerance': str(self.matching.amount_tolerance),
                'min_match_score': self.matching.min_match_score,
                'auto_match_high': self.matching.auto_match_high,
                'auto_match_medium': self.matching.auto_match_medium,
                'conflict_threshold': self.matching.conflict_threshold,
                'perfect_match_threshold': self.matching.perfect_match_threshold,
            },
            'merging': {
                'date_tolerance_days': self.merging.date_tolerance_days,
                'amount_tolerance': str(self.merging.amount_tolerance),
                'balance_tolerance': str(self.merging.balance_tolerance),
                'quantity_tolerance': str(self.merging.quantity_tolerance),
                'metadata_weights': self.merging.metadata_weights,
            },
            'classification': {
                'standalone_confidence': self.classification.standalone_confidence,
                'match_expected_confidence': self.classification.match_expected_confidence,
                'unclassified_confidence': self.classification.unclassified_confidence,
                'high_confidence_threshold': self.classification.high_confidence_threshold,
            },
            'validation': {
                'min_valid_year': self.validation.min_valid_year,
                'max_valid_year': self.validation.max_valid_year,
                'default_date_tolerance': self.validation.default_date_tolerance,
                'default_amount_tolerance': str(self.validation.default_amount_tolerance),
            },
            'output': {
                'output_dir': self.output.output_dir,
                'group_by_year': self.output.group_by_year,
                'group_by_institution': self.output.group_by_institution,
            },
            'logging': {
                'log_level': self.logging.log_level,
                'component_levels': self.logging.component_levels,
            },
            'enable_debug_logging': self.enable_debug_logging,
            'enable_performance_tracking': self.enable_performance_tracking,
            'dry_run': self.dry_run,
        }


# Default configuration instance
DEFAULT_CONFIG = ComponentConfig()


# Convenience function for validation
def validate_config(config: ComponentConfig) -> None:
    """
    Validate a configuration and raise an exception if invalid.
    
    Args:
        config: Configuration to validate
        
    Raises:
        ValueError: If configuration is invalid
    """
    errors = config.validate()
    if errors:
        raise ValueError(f"Configuration validation failed:\n" + "\n".join(f"  - {e}" for e in errors))