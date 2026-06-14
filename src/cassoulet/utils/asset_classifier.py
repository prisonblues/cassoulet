"""
Central Asset Classification System

This module provides a centralized, scoring-based system for determining whether
a posting represents cash or commodity assets. It uses account metadata as the
primary authority and supplements with commodity registry and pattern analysis.

Key Design Principles:
1. Account type is dominant - Bank accounts can ONLY hold cash (definitive)
2. Brokerage accounts can hold both - rely on secondary factors
3. Commodity registry lookup provides strong signals
4. Standard currency check provides cash signals
5. Transaction patterns provide disambiguation hints

This replaces the broken `'commodity' in str(p.units)` logic that was causing
securities purchases to be misclassified as cash transfers.
"""

import logging
from typing import Dict, Optional, Tuple
from dataclasses import dataclass

from beancount.core.data import Transaction, Posting
from beancount.loader import load_file

from cassoulet.utils.commodity_registry import get_commodity_registry


@dataclass
class AccountInfo:
    """Account metadata loaded from accounts.beancount"""
    account: str
    account_type: str  # Bank, Brokerage, Asset, Liability, etc.
    primary_currency: Optional[str]  # Main currency for cash holdings
    institution: Optional[str]


class AssetClassifier:
    """
    Central asset classification system using account-type-dominant scoring.
    
    Classification Logic:
    1. Bank accounts: Always cash (100.0 confidence, unbeatable)
    2. Brokerage accounts: Use secondary factors to determine cash vs commodity
    3. Other accounts: Strong presumption of cash unless commodity signals present
    """
    
    def __init__(self, accounts_file: str = None, commodity_registry=None):
        self.commodity_registry = commodity_registry or get_commodity_registry()
        self._accounts_file = accounts_file
        self.account_metadata: Dict[str, AccountInfo] = {}
        self._load_account_metadata()
        
        # Standard currencies that indicate cash holdings
        self.standard_currencies = {'GBP', 'USD', 'EUR', 'CAD', 'AUD', 'CHF', 'JPY'}
        
        # Account type scoring (definitive for certain types)
        self.account_type_scores = {
            'Bank': 100.0,        # Definitive - cash only
            'Brokerage': 0.0,     # Neutral - depends on other factors  
            'Asset': 80.0,        # Strong cash presumption
            'Liability': 80.0,    # Strong cash presumption
        }
        
        # Secondary factor weights (for non-definitive account types)
        self.commodity_registry_weight = 60.0   # Strong indicator
        self.standard_currency_weight = 40.0    # Moderate indicator  
        self.transaction_pattern_weight = 20.0  # Helpful for disambiguation
    
    def _load_account_metadata(self):
        """Load account definitions from accounts.beancount"""
        accounts_path = self._accounts_file or 'accounts/accounts.beancount'
        try:
            entries, errors, _ = load_file(accounts_path)
            
            for entry in entries:
                if hasattr(entry, 'account') and hasattr(entry, 'meta'):
                    account = entry.account
                    meta = entry.meta
                    
                    # Extract account type (renamed from fbar_type)
                    account_type = meta.get('type') or meta.get('fbar_type', 'Unknown')
                    
                    # Extract primary currency from account opening or metadata
                    primary_currency = None
                    if hasattr(entry, 'currencies') and entry.currencies:
                        primary_currency = list(entry.currencies)[0]
                    else:
                        primary_currency = meta.get('primary_currency')
                    
                    self.account_metadata[account] = AccountInfo(
                        account=account,
                        account_type=account_type,
                        primary_currency=primary_currency,
                        institution=meta.get('institution')
                    )
            
            logging.info(f"Loaded {len(self.account_metadata)} account definitions for asset classification")
            
        except Exception as e:
            logging.warning(f"Could not load accounts.beancount for asset classification: {e}")
    
    def classify_posting(self, posting: Posting, account: str, txn: Transaction) -> Tuple[str, float, dict]:
        """
        Classify a posting as CASH or COMMODITY with confidence and debug info.
        
        Args:
            posting: The Beancount posting to classify
            account: Account name for the posting
            txn: The full transaction for context
            
        Returns:
            Tuple of (classification, confidence, debug_metadata)
            - classification: 'CASH' | 'COMMODITY'
            - confidence: 0.0-100.0
            - debug_metadata: dict with scoring breakdown
        """
        
        if not posting.units:
            return ('CASH', 0.0, {'error': 'No units in posting'})
        
        currency = posting.units.currency
        debug_metadata = {
            'account': account,
            'currency': currency,
            'amount': str(posting.units.number)
        }
        
        scores = {}
        
        # PRIMARY FACTOR: Account Type (Can be definitive)
        account_info = self.account_metadata.get(account)
        if account_info:
            account_type = account_info.account_type
            debug_metadata['account_type'] = account_type
            debug_metadata['institution'] = account_info.institution
            debug_metadata['primary_currency'] = account_info.primary_currency
            
            if account_type == 'Bank':
                # Banks can ONLY hold cash - this is definitive and unbeatable
                debug_metadata['classification_reason'] = 'Bank account - cash only'
                debug_metadata['definitive'] = True
                return ('CASH', 100.0, debug_metadata)
            
            # For non-Bank accounts, account type provides baseline score
            account_score = self.account_type_scores.get(account_type, 0.0)
            scores['account_type'] = account_score
            
        else:
            # No account metadata - use heuristics
            debug_metadata['account_type'] = 'Unknown'
            scores['account_type'] = 0.0
        
        # SECONDARY FACTORS: For Brokerage/Mixed accounts
        
        # 1. Commodity Registry Check
        commodity_score = 0.0
        if currency in self.commodity_registry.list_commodities():
            commodity_score = self.commodity_registry_weight
            debug_metadata['commodity_registry_match'] = True
        else:
            debug_metadata['commodity_registry_match'] = False
        scores['commodity_registry'] = commodity_score
        
        # 2. Standard Currency Check  
        standard_currency_score = 0.0
        if currency in self.standard_currencies:
            standard_currency_score = self.standard_currency_weight
            debug_metadata['is_standard_currency'] = True
        else:
            debug_metadata['is_standard_currency'] = False
        scores['standard_currency'] = standard_currency_score
        
        # 3. Transaction Pattern Analysis
        pattern_score = self._score_transaction_patterns(txn)
        scores['transaction_patterns'] = pattern_score
        debug_metadata['transaction_keywords'] = self._extract_pattern_keywords(txn)
        
        # Calculate totals
        commodity_total = commodity_score + max(0, pattern_score)
        cash_total = scores.get('account_type', 0.0) + standard_currency_score + max(0, -pattern_score)
        
        debug_metadata['scores'] = scores
        debug_metadata['commodity_total'] = commodity_total
        debug_metadata['cash_total'] = cash_total
        debug_metadata['definitive'] = False
        
        # Determine classification
        if commodity_total > cash_total:
            classification = 'COMMODITY'
            confidence = min(commodity_total, 100.0)
            debug_metadata['classification_reason'] = 'Commodity registry match or patterns'
        else:
            classification = 'CASH'
            confidence = min(cash_total, 100.0)
            debug_metadata['classification_reason'] = 'Standard currency, account type, or cash patterns'
        
        return (classification, confidence, debug_metadata)
    
    def _score_transaction_patterns(self, txn: Transaction) -> float:
        """
        Score transaction patterns for cash vs commodity hints.
        
        Returns:
            Positive score for commodity patterns, negative for cash patterns
        """
        narration = (txn.narration or '').lower()
        payee = (txn.payee or '').lower()
        combined_text = f"{narration} {payee}"
        
        # Strong commodity indicators (positive score)
        commodity_keywords = ['transfer in', 'transfer out', 'in-specie', 'dividend', 'interest']
        commodity_score = sum(self.transaction_pattern_weight for keyword in commodity_keywords 
                            if keyword in combined_text)
        
        # Strong cash indicators (negative score to reduce commodity total)
        cash_keywords = ['payment', 'deposit', 'withdrawal', 'fee', 'charge']
        cash_score = sum(-self.transaction_pattern_weight for keyword in cash_keywords 
                        if keyword in combined_text)
        
        return commodity_score + cash_score
    
    def _extract_pattern_keywords(self, txn: Transaction) -> list:
        """Extract pattern keywords found in transaction for debugging"""
        narration = (txn.narration or '').lower()
        payee = (txn.payee or '').lower()
        combined_text = f"{narration} {payee}"
        
        all_keywords = ['transfer in', 'transfer out', 'in-specie', 'dividend', 'interest',
                       'payment', 'deposit', 'withdrawal', 'fee', 'charge']
        
        found_keywords = [keyword for keyword in all_keywords if keyword in combined_text]
        return found_keywords
    
    def classify_transaction(self, txn: Transaction) -> dict:
        """
        Classify all postings in a transaction and determine overall transaction type.
        
        Returns:
            dict with classification results for each posting and overall decision
        """
        results = {
            'postings': [],
            'has_commodities': False,
            'has_cash_only': True,
            'decision': 'UNKNOWN',
            'decision_reason': ''
        }
        
        for i, posting in enumerate(txn.postings):
            if posting.units:
                classification, confidence, debug_metadata = self.classify_posting(
                    posting, posting.account, txn
                )
                
                posting_result = {
                    'posting_index': i,
                    'account': posting.account,
                    'classification': classification,
                    'confidence': confidence,
                    'debug_metadata': debug_metadata
                }
                results['postings'].append(posting_result)
                
                if classification == 'COMMODITY':
                    results['has_commodities'] = True
                    results['has_cash_only'] = False
        
        # Determine overall decision
        if results['has_commodities']:
            results['decision'] = 'EXCLUDE_FROM_CASH_TRANSFERS'
            results['decision_reason'] = 'Contains commodity posting(s)'
        else:
            results['decision'] = 'CASH_TRANSFER_CANDIDATE'
            results['decision_reason'] = 'All postings are cash'
        
        return results


# Global classifier instance
_classifier = None

def get_asset_classifier() -> AssetClassifier:
    """Get the global asset classifier instance (singleton)"""
    global _classifier
    if _classifier is None:
        _classifier = AssetClassifier()
    return _classifier