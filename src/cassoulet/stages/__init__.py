"""
Cassoulet Pipeline Stages

Core components:
- Envelope: Transaction wrapper with processing history
- Three-Stage Reconciliation: Scorer/Matcher/Merger pipeline
"""

from .envelope import Envelope, EnvelopeState, HistoryEntry

__all__ = [
    'Envelope',
    'EnvelopeState',
    'HistoryEntry',
]
