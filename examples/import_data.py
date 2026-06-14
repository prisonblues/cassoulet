#!/usr/bin/env python3
"""
Example import script for Cassoulet.

Demonstrates how to configure and run the import pipeline with
Jack Spriggins' sample data. Copy and adapt for your own accounts.

Usage:
    cd examples/
    uv run python import_data.py
"""

import sys
from pathlib import Path

from cassoulet.config.component_config import ComponentConfig, ProjectPaths
from cassoulet.stages.pipeline import UnifiedPipeline, PipelineConfig

# Import your project-specific configuration
from config.importers import IMPORTER_CONFIG
from config.expense_patterns import EXPENSE_PATTERNS


def main():
    # Set up paths relative to this script's directory
    examples_dir = Path(__file__).parent

    paths = ProjectPaths(
        accounts_file=str(examples_dir / "accounts.beancount"),
        commodities_file=str(examples_dir / "commodities.beancount"),
        raw_data_dir=str(examples_dir / "sample_data"),
        manual_dir=str(examples_dir / "entries" / "manual"),
        template_dir=str(examples_dir / "entries" / "template"),
        output_dir=str(examples_dir / "entries" / "output"),
    )

    component_config = ComponentConfig(paths=paths)

    pipeline_config = PipelineConfig(
        component_config=component_config,
        expense_patterns=EXPENSE_PATTERNS,
    )

    pipeline = UnifiedPipeline(pipeline_config)

    print(f"Importing from: {paths.raw_data_dir}")
    print(f"Output to:      {paths.output_dir}")
    print()

    pipeline.run()

    print("\nDone. Validate with:")
    print(f"  uv run bean-check {paths.output_dir}/main.beancount")


if __name__ == "__main__":
    main()
