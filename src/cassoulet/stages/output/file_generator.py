"""
Main file generator for pipeline output.

Generates the main importers.beancount file that includes all output files.
"""

import logging
import shutil
from pathlib import Path
from typing import List, Set
from datetime import datetime

logger = logging.getLogger(__name__)


class FileGenerator:
    """Generates the main Beancount include file."""
    
    def __init__(self, output_dir: str, template_dir: str = None, manual_dir: str = None):
        """Initialize the main file generator.

        Args:
            output_dir: Directory containing output files
            template_dir: Directory containing beancount templates
            manual_dir: Directory containing manual transaction files
        """
        if not output_dir:
            from cassoulet.base.exceptions import ConfigurationError
            raise ConfigurationError('output_dir', 'FileGenerator',
                                   'Output directory is required')

        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.template_dir = Path(template_dir) if template_dir else Path("entries/template")
        self.manual_dir = Path(manual_dir) if manual_dir else Path("entries/manual")

        # Copy necessary template files to output directory
        self._copy_template_files()
    
    def update_includes_with_balances(self, balance_files: List[str]) -> None:
        """Update includes.beancount to include balance files.
        
        Args:
            balance_files: List of balance file names to include
        """
        if not balance_files:
            return
        
        includes_path = self.output_dir / "includes.beancount"
        
        # Read existing content
        if includes_path.exists():
            with open(includes_path, 'r', encoding='utf-8') as f:
                content = f.read()
        else:
            content = "; Includes file\n\n"
        
        # Check if balance files are already included
        has_balance_section = "; Manual Balance Assertions" in content
        
        if not has_balance_section:
            # Add balance file includes at the end
            lines = content.rstrip().split('\n')
            lines.append("")
            lines.append("; Manual Balance Assertions")
            for balance_file in sorted(balance_files):
                lines.append(f'include "{balance_file}"')
            lines.append("")
            
            # Write back
            with open(includes_path, 'w', encoding='utf-8') as f:
                f.write('\n'.join(lines))
    
    def generate_main_file(
        self,
        transaction_files: List[str],
        balance_files: List[str] = None,
        include_orphans: bool = True
    ) -> str:
        """Generate the main importers.beancount file.
        
        Args:
            transaction_files: List of transaction file paths
            balance_files: Optional list of balance assertion file paths
            include_orphans: Whether to include the orphans file
            
        Returns:
            Path to the generated main file
        """
        filepath = self.output_dir / "main.beancount"
        balance_files = balance_files or []
        
        # Check if template exists
        template_path = self.template_dir / "main.beancount.template"
        
        with open(filepath, 'w', encoding='utf-8') as f:
            if template_path.exists():
                # Use template as base
                with open(template_path, 'r', encoding='utf-8') as template:
                    template_content = template.read()
                    # Remove the last line that includes importers.beancount
                    template_lines = template_content.strip().split('\n')
                    if template_lines[-1].startswith('include "importers.beancount"'):
                        template_lines = template_lines[:-1]
                    f.write('\n'.join(template_lines))
                    f.write('\n\n')
                    f.write("; === Generated Import Files ===\n")
                    f.write(f"; Generated: {datetime.now().isoformat()}\n\n")
            else:
                # Fallback: generate minimal header
                f.write("; Pipeline - Main Ledger File\n")
                f.write(f"; Generated: {datetime.now().isoformat()}\n")
                f.write("; This file includes all pipeline output files\n\n")
            
            # Include balance assertions first
            if balance_files:
                f.write("; Balance Assertions\n")
                for balance_file in sorted(balance_files):
                    relative_path = self._get_relative_path(balance_file)
                    f.write(f'include "{relative_path}"\n')
                f.write("\n")
            
            # Group transaction files by year
            files_by_year = self._group_files_by_year(transaction_files)
            
            # Write includes organized by year
            for year in sorted(files_by_year.keys()):
                f.write(f"; {year} Transactions\n")
                
                for file_path in sorted(files_by_year[year]):
                    relative_path = self._get_relative_path(file_path)
                    f.write(f'include "{relative_path}"\n')
                
                f.write("\n")
            
            # Include orphans file
            if include_orphans:
                orphans_file = self.output_dir / "txn_orphans.beancount"
                if orphans_file.exists():
                    f.write("; Orphaned Transactions\n")
                    relative_path = self._get_relative_path(str(orphans_file))
                    f.write(f'include "{relative_path}"\n')
        
        logger.info(f"Generated main file: {filepath}")
        return str(filepath)
    
    def _get_relative_path(self, file_path: str) -> str:
        """Get relative path from output directory.
        
        Args:
            file_path: Absolute or relative file path
            
        Returns:
            Relative path from output directory
        """
        path = Path(file_path)
        
        # If path is absolute, try to make it relative
        if path.is_absolute():
            try:
                return str(path.relative_to(self.output_dir))
            except ValueError:
                # Not under output_dir, return absolute
                return str(path)
        
        # If path starts with output_dir, strip it
        path_str = str(path)
        output_dir_str = str(self.output_dir)
        if path_str.startswith(output_dir_str + '/'):
            return path_str[len(output_dir_str) + 1:]
        elif path_str.startswith(output_dir_str):
            return path_str[len(output_dir_str):]
        
        # Return just the filename if it's in the output dir
        if (self.output_dir / path.name).exists():
            return path.name
        
        return str(path)
    
    def _group_files_by_year(
        self,
        file_paths: List[str]
    ) -> dict[int, List[str]]:
        """Group file paths by year.
        
        Args:
            file_paths: List of file paths
            
        Returns:
            Dictionary mapping year to list of file paths
        """
        grouped = {}
        
        for file_path in file_paths:
            # Extract year from filename (format: institution_YYYY.beancount)
            filename = Path(file_path).stem
            parts = filename.split('_')
            
            try:
                # Year is typically the last part
                year = int(parts[-1])
                if 2000 <= year <= 2100:  # Sanity check
                    if year not in grouped:
                        grouped[year] = []
                    grouped[year].append(file_path)
                else:
                    # Year not found, add to unknown
                    if 'unknown' not in grouped:
                        grouped['unknown'] = []
                    grouped['unknown'].append(file_path)
            except (ValueError, IndexError):
                # Can't extract year, add to unknown
                if 'unknown' not in grouped:
                    grouped['unknown'] = []
                grouped['unknown'].append(file_path)
        
        return grouped
    
    def _extract_institutions(self, file_paths: List[str]) -> Set[str]:
        """Extract institution names from file paths.
        
        Args:
            file_paths: List of file paths
            
        Returns:
            Set of institution names
        """
        institutions = set()
        
        for file_path in file_paths:
            # Format: institution_YYYY.beancount
            filename = Path(file_path).stem
            parts = filename.split('_')
            
            if len(parts) >= 2:
                # Institution is everything except the last part (year)
                institution = '_'.join(parts[:-1])
                institutions.add(institution)
        
        return institutions
    
    def _copy_template_files(self):
        """Copy necessary template files to output directory.
        
        Copies:
        - includes.beancount.template -> includes.beancount
        - manual_includes.beancount from entries/manual/
        - txn_orphans.beancount (create empty if doesn't exist)
        """
        template_dir = self.template_dir
        manual_dir = self.manual_dir
        
        # Copy includes.beancount template
        includes_source = template_dir / "includes.beancount.template"
        includes_dest = self.output_dir / "includes.beancount"
        if includes_source.exists():
            logger.info(f"Copying template: {includes_source} -> {includes_dest}")
            shutil.copy2(includes_source, includes_dest)
        
        # Copy manual_includes.beancount
        manual_includes_source = manual_dir / "manual_includes.beancount"
        manual_includes_dest = self.output_dir / "manual_includes.beancount"
        if manual_includes_source.exists():
            logger.info(f"Copying manual includes: {manual_includes_source} -> {manual_includes_dest}")
            shutil.copy2(manual_includes_source, manual_includes_dest)
        else:
            # Create empty placeholder if doesn't exist
            with open(manual_includes_dest, 'w') as f:
                f.write("; Manual includes placeholder\n")
                f.write("; Add manual transaction includes here\n")
        
        # Create empty orphans file if it doesn't exist
        orphans_file = self.output_dir / "txn_orphans.beancount"
        if not orphans_file.exists():
            with open(orphans_file, 'w') as f:
                f.write("; Orphaned Transactions\n")
                f.write("; Transactions that could not be matched or categorized\n")
                f.write(f"; Generated: {datetime.now().isoformat()}\n\n")