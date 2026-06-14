"""
Cassoulet - An opinionated, envelope-based import pipeline for Beancount.

Takes CSV exports from banks and brokers, wraps every transaction in an
auditable envelope, runs them through a multi-phase reconciliation pipeline,
and outputs clean Beancount ledger files. Zero silent failures.
"""

__version__ = "0.1.0"
