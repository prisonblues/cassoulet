"""
Commodity Registry System for Investment Transaction Processing

This module provides a centralized mapping system between broker-specific
fund/stock descriptions and standardized commodity symbols used in Beancount.

Key Features:
- Maps various broker descriptions to standard symbols (e.g., VLS80, VUSEI)
- Supports fuzzy matching for slight variations in naming
- Extensible design for adding new brokers and commodities
- Automatic loading from commodities.beancount file
"""

import re
import logging
from typing import Dict, List, Optional, Set
from dataclasses import dataclass


@dataclass
class CommodityInfo:
    """Information about a commodity from commodities.beancount"""
    symbol: str
    name: str
    asset_class: str
    quote_currency: str
    exchange: Optional[str] = None  # Exchange for TradingView format (e.g., "NASDAQ", "LSE")


class CommodityRegistry:
    """
    Central registry for commodity symbol resolution across all importers.
    
    This class handles the mapping between broker-specific descriptions
    (like "Vanguard LifeStrategy 80% Equity Accumulation (GBP)") and
    standardized commodity symbols (like "VLS80").
    """
    
    def __init__(self, commodities_file: str = None):
        self.commodities: Dict[str, CommodityInfo] = {}
        self.alias_map: Dict[str, str] = {}  # description -> symbol
        self.prefix_patterns: Dict[str, tuple] = {}  # prefix -> (symbol, keywords)
        self._commodities_file = commodities_file
        self._load_commodity_definitions()
        self._build_alias_mappings()

    def _load_commodity_definitions(self):
        """Load commodity definitions from commodities.beancount"""
        commodities_path = self._commodities_file or 'commodities/commodities.beancount'
        try:
            from beancount.loader import load_file
            entries, errors, _ = load_file(commodities_path)

            for entry in entries:
                if hasattr(entry, 'currency') and hasattr(entry, 'meta'):
                    symbol = entry.currency
                    meta = entry.meta

                    self.commodities[symbol] = CommodityInfo(
                        symbol=symbol,
                        name=meta.get('name', ''),
                        asset_class=meta.get('asset_class', ''),
                        quote_currency=meta.get('quote_currency', 'GBP'),
                        exchange=meta.get('exchange')  # Load exchange metadata
                    )

            logging.info(f"Loaded {len(self.commodities)} commodity definitions")

        except Exception as e:
            logging.warning(f"Could not load commodities.beancount: {e}")
    
    def _build_alias_mappings(self):
        """Build prefix-based mappings with smart disambiguation"""
        
        # Prefix-based patterns: fund_prefix -> (symbol, disambiguation_keywords)
        prefix_patterns = {
            # Vanguard LifeStrategy funds (distinguish by percentage)
            'Vanguard LifeStrategy 80% Equity': ('VLS80', ['accumulation', 'acc']),
            'Vanguard LifeStrategy 60% Equity': ('VLS60', ['accumulation', 'acc']),
            'Vanguard LifeStrategy 100% Equity': ('VLS100', ['accumulation', 'acc']),
            'LifeStrategy 80% Equity': ('VLS80', ['accumulation', 'acc']),
            'LifeStrategy 60% Equity': ('VLS60', ['accumulation', 'acc']),
            'LifeStrategy 100% Equity': ('VLS100', ['accumulation', 'acc']),
            
            # Vanguard Index funds
            'Vanguard US Equity Index': ('VUSEI', ['accumulation', 'acc']),
            'Vanguard S&P 500': ('VUSA', ['ucits', 'etf']),
            
            # iShares funds (distinguish by region/type)
            'iShares Japan Equity Index': ('ISJE', ['accumulation', 'acc']),
            'iShares Corporate Bond Index': ('ISCB', ['accumulation', 'acc']),
            
            # Crypto/Commodities (unique names)
            'Coinshares XBT Provider': ('XBTE', []),
            'XBT Provider Bitcoin Tracker': ('XBTE', []),
            'WisdomTree Physical Gold': ('PHGP', []),
            'WisdomTree S&P 500 VIX': ('VIXL', []),
            
            # UK Fund families (use first few words for uniqueness)
            'AXA World Funds UK Equity': ('AXAFUK', ['accumulation', 'acc']),
            'FTF Martin Currie UK Mid Cap': ('FRUKMC', ['accumulation', 'acc']),
            'HSBC FTSE 250 Index': ('HSBC250', ['accumulation', 'acc']),
            'Invesco Tactical Bond': ('INVTB', ['accumulation', 'acc']),
            'Jupiter Global Value Equity': ('JUPGVE', ['accumulation', 'acc']),
            'WS Lindsell Train UK Equity': ('LFLTUK', ['accumulation', 'acc']),
            'Lindsell Train UK Equity': ('LFLTUK', ['accumulation', 'acc']),
            'Liontrust SF European Growth': ('LTSEG', ['accumulation', 'acc']),
            
            # AJ Bell specific fund descriptions
            'Liontrust Sust Fut UK Gr': ('LSFUKG', ['accumulation', 'acc']),
            'Liontrust Sustainable Future UK Growth': ('LSFUKG', ['accumulation', 'acc']),
            'abrdn-Global Innovation': ('ASIGE', ['equity', 'acc']),
            'Aberdeen Standard Global Innovation': ('ASIGE', ['equity', 'acc']),
            'Liontrust Global Technology': ('LGTACC', ['accumulation', 'acc']),
            'WisdomTree NASDAQ 100 3x Daily': ('QQQ3', ['leveraged']),
            'QQQ3': ('QQQ3', []),
            'VanEck Semiconductor ETF': ('SMGB', []),
            'SMGB': ('SMGB', []),
            'UBS ETF SMIM': ('SMGB', []),
            
            # Individual stocks (use company names - case insensitive prefix matching)
            'Activision': ('ATVI', []),
            'Advanced Micro': ('AMD', []),
            'MicroStrategy Inc': ('MSTR', []),
            'Strategy Class A': ('MSTR', []),  # AJ Bell abbreviation for MicroStrategy
            'Meta Platforms': ('META', []),
            'NVIDIA': ('NVDA', []),
            'Apple': ('AAPL', []),
            'Unity Software': ('UNITY', []),
            'Green Dot': ('GDOT', [])
        }
        
        # Store patterns for prefix matching
        self.prefix_patterns = {}
        for prefix, (symbol, keywords) in prefix_patterns.items():
            self.prefix_patterns[prefix.upper()] = (symbol, [kw.upper() for kw in keywords])
        
        # Also add the canonical names from commodities.beancount for exact matching
        for symbol, info in self.commodities.items():
            if info.name:
                self.alias_map[info.name.upper()] = symbol
        
        logging.info(f"Built {len(self.prefix_patterns)} prefix patterns and {len(self.alias_map)} exact mappings")
    
    def lookup_commodity(self, description: str) -> Optional[str]:
        """
        Look up commodity symbol from broker description using prefix matching.
        
        Args:
            description: Broker-specific description (e.g., from HL, II, etc.)
            
        Returns:
            Standard commodity symbol (e.g., 'VLS80') or None if not found
        """
        if not description:
            return None
        
        # Normalize the description
        desc_upper = description.strip().upper()
        
        # 1. Direct exact lookup first
        if desc_upper in self.alias_map:
            return self.alias_map[desc_upper]
        
        # 2. Prefix-based matching (new smart approach)
        prefix_match = self._prefix_match(desc_upper)
        if prefix_match:
            logging.debug(f"Prefix matched '{description}' -> {prefix_match}")
            return prefix_match
        
        # 3. Fallback to fuzzy matching for edge cases
        fuzzy_match = self._fuzzy_match(desc_upper)
        if fuzzy_match:
            logging.debug(f"Fuzzy matched '{description}' -> {fuzzy_match}")
            return fuzzy_match
        
        logging.warning(f"No commodity mapping found for: '{description}'")
        return None
    
    def _prefix_match(self, description: str) -> Optional[str]:
        """
        Smart prefix-based matching that handles broker descriptions with variable suffixes.
        
        Matches fund name prefixes and validates disambiguation keywords (Acc vs Dist, etc.)
        to handle descriptions like "Vanguard LifeStrategy 80% Equity Accumulation (GBP) 32.974 @ 454.89"
        """
        best_match = None
        best_score = 0
        
        for prefix, (symbol, disambiguation_keywords) in self.prefix_patterns.items():
            # Check if description starts with this prefix (or contains it prominently)
            if prefix in description:
                # Calculate match score based on how early the prefix appears
                match_pos = description.find(prefix)
                prefix_score = 1.0 - (match_pos / len(description)) if len(description) > 0 else 1.0
                
                # Validate disambiguation keywords if any are specified
                if disambiguation_keywords:
                    keyword_found = any(keyword in description for keyword in disambiguation_keywords)
                    if not keyword_found:
                        # If we need disambiguation but don't find keywords, skip this match
                        continue
                
                # Prefer longer, more specific prefixes and earlier matches
                total_score = prefix_score * len(prefix)
                
                if total_score > best_score:
                    best_score = total_score
                    best_match = symbol
        
        return best_match
    
    def _fuzzy_match(self, description: str) -> Optional[str]:
        """
        Attempt fuzzy matching for unmapped descriptions.
        
        This uses substring matching and common patterns to find the
        BEST match (highest overlap ratio) rather than first match above threshold.
        """
        desc = description.upper()
        desc_words = set(re.findall(r'\w+', desc))
        
        best_symbol = None
        best_ratio = 0.0
        
        # Find the best match among all aliases
        for alias_desc, symbol in self.alias_map.items():
            alias_words = set(re.findall(r'\w+', alias_desc))
            
            if desc_words and alias_words:
                overlap = len(desc_words & alias_words)
                min_words = min(len(desc_words), len(alias_words))
                overlap_ratio = overlap / min_words
                
                # Only consider matches with at least 70% overlap
                if overlap_ratio >= 0.7 and overlap_ratio > best_ratio:
                    best_ratio = overlap_ratio
                    best_symbol = symbol
        
        return best_symbol
    
    def get_commodity_info(self, symbol: str) -> Optional[CommodityInfo]:
        """Get full commodity information by symbol"""
        return self.commodities.get(symbol)
    
    def list_commodities(self) -> List[str]:
        """Get list of all known commodity symbols"""
        return list(self.commodities.keys())
    
    def add_manual_mapping(self, description: str, symbol: str):
        """Add a manual mapping for testing or one-off cases"""
        self.alias_map[description.upper()] = symbol
        logging.info(f"Added manual mapping: '{description}' -> {symbol}")


# Global registry instance
_registry = None


def get_commodity_registry(commodities_file: str = None) -> CommodityRegistry:
    """Get the global commodity registry instance (singleton).

    Args:
        commodities_file: Path to commodities.beancount. Only used on first call
            (when creating the singleton). Ignored on subsequent calls.
    """
    global _registry
    if _registry is None:
        _registry = CommodityRegistry(commodities_file=commodities_file)
    return _registry


def set_commodity_registry(registry: CommodityRegistry) -> None:
    """Inject a pre-configured registry (for use by external projects)."""
    global _registry
    _registry = registry


def reset_commodity_registry() -> None:
    """Reset the singleton (for testing)."""
    global _registry
    _registry = None