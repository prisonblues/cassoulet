"""
Currency configuration and utilities.

Defines known currencies and provides utilities to distinguish
between currencies and commodities.
"""

import logging
from typing import Set
from pathlib import Path

logger = logging.getLogger(__name__)

# Known fiat currencies
CURRENCIES: Set[str] = {
    'GBP',  # British Pound
    'USD',  # US Dollar
    'EUR',  # Euro
    'CHF',  # Swiss Franc
    'JPY',  # Japanese Yen
    'AUD',  # Australian Dollar
    'CAD',  # Canadian Dollar
    'NZD',  # New Zealand Dollar
    'HKD',  # Hong Kong Dollar
    'SGD',  # Singapore Dollar
    'SEK',  # Swedish Krona
    'NOK',  # Norwegian Krone
    'DKK',  # Danish Krone
    'PLN',  # Polish Zloty
    'CZK',  # Czech Koruna
    'HUF',  # Hungarian Forint
    'RON',  # Romanian Leu
    'BGN',  # Bulgarian Lev
    'HRK',  # Croatian Kuna
    'RUB',  # Russian Ruble
    'TRY',  # Turkish Lira
    'CNY',  # Chinese Yuan
    'INR',  # Indian Rupee
    'KRW',  # South Korean Won
    'MXN',  # Mexican Peso
    'BRL',  # Brazilian Real
    'ARS',  # Argentine Peso
    'CLP',  # Chilean Peso
    'COP',  # Colombian Peso
    'PEN',  # Peruvian Sol
    'UYU',  # Uruguayan Peso
}

# Currency symbols mapping (for display and parsing)
CURRENCY_SYMBOLS = {
    'GBP': '£',
    'USD': '$',
    'EUR': '€',
    'JPY': '¥',
    'CNY': '¥',  # Same as JPY
    'INR': '₹',
    'KRW': '₩',
    'CHF': 'CHF',  # Usually written as CHF
    'SEK': 'kr',
    'NOK': 'kr',
    'DKK': 'kr',
    'AUD': 'A$',
    'CAD': 'C$',
    'NZD': 'NZ$',
    'HKD': 'HK$',
    'SGD': 'S$',
    'MXN': '$',  # Same as USD
    'BRL': 'R$',
    'ARS': '$',  # Same as USD
    'CLP': '$',  # Same as USD
    'COP': '$',  # Same as USD
}

# Reverse mapping: symbol to possible currency codes
SYMBOL_TO_CURRENCIES = {}
for code, symbol in CURRENCY_SYMBOLS.items():
    if symbol not in SYMBOL_TO_CURRENCIES:
        SYMBOL_TO_CURRENCIES[symbol] = []
    SYMBOL_TO_CURRENCIES[symbol].append(code)

# Cache for commodity registry
_commodity_registry = None


def get_commodity_registry():
    """Get or create the commodity registry singleton."""
    global _commodity_registry
    if _commodity_registry is None:
        try:
            from cassoulet.utils.commodity_registry import CommodityRegistry
            _commodity_registry = CommodityRegistry()
        except Exception as e:
            logger.error(f"Failed to load commodity registry: {e}")
            _commodity_registry = False  # Mark as failed
    return _commodity_registry


def is_currency(symbol: str) -> bool:
    """
    Check if a symbol is a known currency.
    
    Args:
        symbol: The symbol to check
        
    Returns:
        True if the symbol is a known currency
    """
    if not symbol:
        return False
    return symbol.upper() in CURRENCIES


def is_commodity(symbol: str) -> bool:
    """
    Check if a symbol is a commodity (not a currency).
    
    Also warns if the commodity is not in the known commodities list.
    
    Args:
        symbol: The symbol to check
        
    Returns:
        True if the symbol is not a currency (assumed to be commodity)
    """
    if not symbol:
        return False
    
    # If it's a currency, it's not a commodity
    if is_currency(symbol):
        return False
    
    # Check if it's a known commodity using the registry
    registry = get_commodity_registry()
    if registry and registry is not False:
        # Check if it's in the registry
        if symbol not in registry.commodities:
            logger.warning(f"Unknown commodity: {symbol} (not in commodity registry)")
    
    return True