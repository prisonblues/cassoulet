"""
Configuration loading with dynamic environment variable support and institution overrides.

This module handles loading configuration from various sources:
1. Default configuration (built-in)
2. Environment variables (dynamic discovery)
3. Institution-specific overrides
4. Runtime overrides
"""

import os
import logging
from typing import Dict, Any, Optional, get_origin, get_args
from dataclasses import fields, is_dataclass, replace, Field
from decimal import Decimal

from cassoulet.config.component_config import ComponentConfig, DEFAULT_CONFIG
from cassoulet.config.institution_configs import INSTITUTION_CONFIGS


logger = logging.getLogger(__name__)


class ConfigLoader:
    """Handles configuration loading with multiple override layers."""
    
    ENV_PREFIX = "PIPELINE_"
    
    @classmethod
    def load_config(cls, 
                   base_config: Optional[ComponentConfig] = None,
                   institution: Optional[str] = None,
                   overrides: Optional[Dict[str, Any]] = None) -> ComponentConfig:
        """
        Load configuration with environment and institution-specific overrides.
        
        The configuration is built in layers:
        1. Start with base config or DEFAULT_CONFIG
        2. Apply environment variable overrides (automatic discovery)
        3. Apply institution-specific overrides
        4. Apply any runtime overrides
        5. Validate the final configuration
        
        Args:
            base_config: Base configuration to start from (defaults to DEFAULT_CONFIG)
            institution: Institution name for specific overrides
            overrides: Runtime overrides to apply
            
        Returns:
            Loaded and validated configuration
            
        Raises:
            ValueError: If configuration validation fails
        """
        # Start with base or default
        config = base_config or DEFAULT_CONFIG
        
        # Layer 1: Apply environment variable overrides (dynamic discovery)
        config = cls._apply_env_overrides_dynamic(config)
        
        # Layer 2: Apply institution-specific overrides
        if institution:
            config = cls._apply_institution_overrides(config, institution)
            logger.info(f"Applied configuration overrides for institution: {institution}")
        
        # Layer 3: Apply runtime overrides
        if overrides:
            config = cls._update_nested_config(config, overrides)
            logger.debug(f"Applied runtime overrides: {overrides}")
        
        # Validate final configuration
        errors = config.validate()
        if errors:
            error_msg = "Configuration validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
            raise ValueError(error_msg)
        
        return config
    
    @classmethod
    def _apply_env_overrides_dynamic(cls, config: ComponentConfig) -> ComponentConfig:
        """
        Dynamically apply environment variable overrides using dataclass introspection.
        
        This method automatically maps environment variables to config attributes
        without requiring manual updates for each new configuration option.
        
        Environment variable format: PIPELINE_<COMPONENT>_<SETTING>
        Examples:
            PIPELINE_SCORING_HIGH_CONFIDENCE=85.0
            PIPELINE_MATCHING_DATE_TOLERANCE_DAYS=10
            PIPELINE_CIRCUIT_BREAKER_FAILURE_THRESHOLD=20
            
        For nested dictionaries, use double underscore:
            PIPELINE_SCORING_SCORING_WEIGHTS__AMOUNT=60.0
        """
        updates = cls._collect_env_overrides(config, cls.ENV_PREFIX)
        
        if updates:
            config = cls._update_nested_config(config, updates)
            logger.info(f"Applied {len(updates)} environment variable overrides")
        
        return config
    
    @classmethod
    def _collect_env_overrides(cls, obj: Any, prefix: str, path: str = "") -> Dict[str, Any]:
        """
        Recursively collect environment variable overrides for a dataclass.
        
        Args:
            obj: Dataclass instance to inspect
            prefix: Environment variable prefix
            path: Current path in the nested structure
            
        Returns:
            Dictionary of updates to apply
        """
        if not is_dataclass(obj):
            return {}
        
        updates = {}
        
        for field in fields(obj):
            field_value = getattr(obj, field.name)
            field_path = f"{path}_{field.name.upper()}" if path else field.name.upper()
            
            if is_dataclass(field_value):
                # Recursively handle nested dataclass
                nested_updates = cls._collect_env_overrides(field_value, prefix, field_path)
                if nested_updates:
                    updates[field.name] = nested_updates
            else:
                # Check for direct field override
                env_var_name = f"{prefix}{field_path}"
                env_value = os.getenv(env_var_name)
                
                if env_value is not None:
                    # Convert to appropriate type
                    converted_value = cls._convert_env_value(env_value, field.type, field.name)
                    if converted_value is not None:
                        updates[field.name] = converted_value
                        logger.debug(f"Found env override: {env_var_name}={env_value}")
                
                # Special handling for dictionaries - check for sub-keys
                if isinstance(field_value, dict):
                    dict_updates = cls._collect_dict_env_overrides(
                        field_value, prefix, field_path, field.type
                    )
                    if dict_updates:
                        # Merge with existing dict
                        if field.name in updates:
                            updates[field.name].update(dict_updates)
                        else:
                            updates[field.name] = {**field_value, **dict_updates}
        
        return updates
    
    @classmethod
    def _collect_dict_env_overrides(cls, 
                                   dict_value: Dict,
                                   prefix: str,
                                   field_path: str,
                                   field_type: Any) -> Dict[str, Any]:
        """
        Collect environment variable overrides for dictionary fields.
        
        For dictionaries, we support setting individual keys using double underscore:
        PIPELINE_SCORING_SCORING_WEIGHTS__AMOUNT=60.0
        """
        updates = {}
        
        # Check for individual key overrides
        for key in dict_value.keys():
            # Handle tuple keys (like in account_quantity_tolerances)
            if isinstance(key, tuple):
                # Skip tuple keys - they can't be set via env vars
                continue
            
            # Convert key to string and uppercase for env var
            key_str = str(key).upper()
            env_var_name = f"{prefix}{field_path}__{key_str}"
            env_value = os.getenv(env_var_name)
            
            if env_value is not None:
                # Determine the value type from the original dict
                original_value = dict_value.get(key)
                if original_value is not None:
                    target_type = type(original_value)
                    converted_value = cls._convert_env_value(env_value, target_type, key)
                    if converted_value is not None:
                        updates[key] = converted_value
                        logger.debug(f"Found dict env override: {env_var_name}={env_value}")
        
        return updates
    
    @classmethod
    def _convert_env_value(cls, value: str, target_type: Any, field_name: str = "") -> Any:
        """
        Convert environment variable string to the appropriate type.
        
        Args:
            value: String value from environment variable
            target_type: Target type to convert to
            field_name: Name of the field (for error messages)
            
        Returns:
            Converted value or None if conversion fails
        """
        try:
            # Handle Optional types
            origin = get_origin(target_type)
            if origin in (Optional, type(Optional[int])):
                args = get_args(target_type)
                if args:
                    target_type = args[0]
            
            # Handle Union types (just try the first non-None type)
            if origin is type(Dict[str, float]):
                origin = dict
            
            # Convert based on type
            if target_type == bool:
                return value.lower() in ('true', '1', 'yes', 'on')
            elif target_type == int:
                return int(value)
            elif target_type == float:
                return float(value)
            elif target_type == str:
                return value
            elif target_type == Decimal:
                return Decimal(value)
            elif origin == dict or target_type == dict:
                # For dict types, parse as JSON
                import json
                return json.loads(value)
            elif origin == tuple:
                # For tuples, parse as comma-separated values
                args = get_args(target_type)
                if len(args) == 2 and args[0] == float and args[1] == float:
                    parts = value.split(',')
                    if len(parts) == 2:
                        return (float(parts[0].strip()), float(parts[1].strip()))
            else:
                # Unknown type, return as string
                logger.warning(f"Unknown type {target_type} for field {field_name}, returning as string")
                return value
                
        except (ValueError, TypeError) as e:
            logger.warning(f"Failed to convert '{value}' to {target_type} for field {field_name}: {e}")
            return None
    
    @classmethod
    def _apply_institution_overrides(cls, 
                                    config: ComponentConfig, 
                                    institution: str) -> ComponentConfig:
        """
        Apply institution-specific configuration overrides.
        
        Args:
            config: Current configuration
            institution: Name of the institution
            
        Returns:
            Configuration with institution overrides applied
        """
        if institution in INSTITUTION_CONFIGS:
            overrides = INSTITUTION_CONFIGS[institution]
            config = cls._update_nested_config(config, overrides)
            logger.debug(f"Applied overrides for institution '{institution}': {overrides}")
        else:
            logger.debug(f"No specific configuration for institution '{institution}'")
        
        return config
    
    @classmethod
    def _update_nested_config(cls, config: Any, updates: Dict[str, Any]) -> Any:
        """
        Recursively update nested dataclass configuration.
        
        Args:
            config: Configuration object (dataclass)
            updates: Dictionary of updates to apply
            
        Returns:
            Updated configuration
        """
        if not is_dataclass(config):
            return config
        
        changes = {}
        for field in fields(config):
            if field.name in updates:
                field_value = getattr(config, field.name)
                update_value = updates[field.name]
                
                if is_dataclass(field_value) and isinstance(update_value, dict):
                    # Recursive update for nested dataclass
                    changes[field.name] = cls._update_nested_config(field_value, update_value)
                elif isinstance(field_value, dict) and isinstance(update_value, dict):
                    # Merge dictionaries
                    changes[field.name] = {**field_value, **update_value}
                else:
                    # Direct value update
                    changes[field.name] = update_value
        
        return replace(config, **changes) if changes else config
    
# Convenience function
def load_config(institution: Optional[str] = None, **overrides) -> ComponentConfig:
    """
    Load pipeline configuration with optional institution and overrides.
    
    Args:
        institution: Optional institution name for specific config
        **overrides: Keyword arguments for runtime overrides
        
    Returns:
        Loaded and validated ComponentConfig
    """
    return ConfigLoader.load_config(
        institution=institution,
        overrides=overrides if overrides else None
    )