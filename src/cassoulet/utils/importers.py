"""
Utility functions for importer management.

Provides common functionality for working with importers and their configurations,
used by both the main import process and specialized processors like balance gap detection.
"""

from typing import Dict, List, Any


def get_configs_by_base_class(importer_configs: List[Dict[str, Any]],
                              base_class: str) -> List[Dict[str, Any]]:
    """
    Get all configurations for importers with a specific base class.

    This checks the actual class hierarchy without instantiation.

    Args:
        importer_configs: List of importer configuration dicts
        base_class: The base class name to filter by (e.g., 'BankImporter', 'BrokerImporter')

    Returns:
        List of configuration dicts
    """
    import importlib
    matching_configs = []

    for config in importer_configs:
        try:
            importer_class_name = config['importer_class']

            # Derive module path from class name (same logic as create_importer_from_config)
            if 'GeneralImporter' in importer_class_name:
                institution = importer_class_name.replace('GeneralImporter', '').lower()
                module_name = f"{institution}_general_importer"
            else:
                institution = importer_class_name.replace('Importer', '').lower()
                module_name = f"{institution}_importer"

            module_path = f"cassoulet.importers.{institution}.{module_name}"

            # Import the module and get the class
            module = importlib.import_module(module_path)
            ImporterClass = getattr(module, importer_class_name)

            # Check the class hierarchy without instantiation
            for cls in ImporterClass.__mro__:
                if cls.__name__ == base_class:
                    matching_configs.append(config)
                    break
        except (ImportError, AttributeError):
            # Skip if we can't import the class
            pass

    return matching_configs


def create_importer_from_config(config: Dict[str, Any]):
    """
    Create a single importer instance from a configuration dict.

    Args:
        config: Configuration dict with keys: importer_class, account, file_identifier, etc.

    Returns:
        Instantiated importer or None if creation fails
    """
    import importlib
    import logging

    try:
        importer_class_name = config['importer_class']

        # Derive module path from class name using convention
        institution = importer_class_name.replace('Importer', '').lower()
        module_name = f"{institution}_importer"

        module_path = f"cassoulet.importers.{institution}.{module_name}"

        # Import and instantiate
        module = importlib.import_module(module_path)
        ImporterClass = getattr(module, importer_class_name)

        # Extract configuration
        account = config['account']
        file_identifier = config['file_identifier']
        importer_config = {
            k: v for k, v in config.items()
            if k not in ['importer_class', 'account', 'file_identifier', 'base_class']
        }

        # Create instance
        importer = ImporterClass(
            account=account,
            file_identifier=file_identifier,
            config=importer_config
        )

        return importer

    except Exception as e:
        logging.error(f"Failed to create importer {config.get('importer_class')}: {e}")
        return None
