"""
Transfer Scorer - Pattern-based implementation

Scores potential transfer matches using declarative patterns.
This replaces the old algorithmic scorer with a cleaner, more maintainable approach.
"""

import logging
import multiprocessing as mp
import os
from typing import Dict, List, Tuple, Any, Optional
from datetime import date

from cassoulet.stages.envelope_processor import EnvelopeProcessor
from cassoulet.stages.envelope import Envelope
from cassoulet.base.exceptions import ProcessingWarning
from cassoulet.utils.pattern_matcher import PatternMatcher
from cassoulet.config.transfer_scoring_patterns import TRANSFER_SCORING_PATTERNS
from cassoulet.stages._compiled_patterns import compile_patterns
import cassoulet.utils.envelope_utilities as eu
from cassoulet.utils.envelope_utilities import (
    enhance_envelope,
    get_absolute_amount,
    match_pattern_pair,
    build_envelope_date_index,
    find_envelopes_in_date_window,
    is_transfer_eligible,
    _check_value_match,
    can_aggregate,
    can_reconcile,
)

from cassoulet.stages._fast_scoring_fallback import score_pair_fallback as _score_pair_impl

logger = logging.getLogger(__name__)

# Number of worker processes for parallel scoring
_NUM_WORKERS = max((os.cpu_count() or 1) * 3 // 4, 1)

# ── Module-level shared state for forked worker processes ──
# Set by _process_internal before forking; inherited via copy-on-write.
# Workers read these directly — no pickling overhead.
_shared_envelopes: Optional[List] = None
_shared_date_index: Optional[Dict] = None
_shared_compiled_groups: Optional[List] = None
_shared_pattern_functions: Optional[Dict] = None
_shared_field_names: Optional[List] = None


def _init_worker():
    """Per-worker initializer: create a fresh compat cache."""
    eu._compat_cache = {}


def _score_chunk(indices: List[int]) -> Dict[int, List[Dict]]:
    """Score a chunk of envelopes. Runs in a forked worker process.

    Args:
        indices: List of envelope indices to score.

    Returns:
        {envelope_index: candidates_list} for envelopes with candidates.
    """
    all_envelopes = _shared_envelopes
    date_index = _shared_date_index
    compiled_groups = _shared_compiled_groups
    pattern_functions = _shared_pattern_functions
    field_names = _shared_field_names

    results: Dict[int, List[Dict]] = {}

    for i in indices:
        envelope = all_envelopes[i]

        if not is_transfer_eligible(envelope):
            continue

        window_candidates = find_envelopes_in_date_window(
            envelope, all_envelopes, date_index=date_index, window_days=30
        )

        candidates = []
        for candidate in window_candidates:
            if candidate.metadata and candidate.metadata.get('matched_with'):
                continue
            if not is_transfer_eligible(candidate):
                continue

            # Early exit: pair must be aggregatable or reconcilable
            if not (can_aggregate(envelope, candidate)
                    or can_reconcile(envelope, candidate)):
                continue

            score, pattern_details = _score_pair_impl(
                envelope, candidate, compiled_groups, pattern_functions,
                field_names,
            )

            if score > 0:
                candidates.append({
                    'envelope_id': candidate.envelope_id,
                    'score': score,
                    'patterns_matched': [p['pattern_id'] for p in pattern_details],
                    'pattern_details': pattern_details,
                    'date': str(candidate.date),
                    'amount': str(get_absolute_amount(candidate)),
                    'type': candidate.outbound_type or candidate.inbound_type,
                })

        if candidates:
            candidates.sort(key=lambda x: x['score'], reverse=True)
            results[i] = candidates[:5]

    return results


class TransferScorer(EnvelopeProcessor):
    """
    Score potential transfer matches using patterns.

    This processor evaluates envelope pairs using declarative patterns
    to generate match scores. Higher scores indicate stronger matches.
    """

    def __init__(self, date_tolerance_days: int = 20, debug: bool = False):
        """Initialize with pattern configuration.

        Args:
            date_tolerance_days: Kept for compatibility (unused - patterns define their own)
            debug: Kept for compatibility (unused - controlled by logging level)
        """
        super().__init__(processor_name="TransferScorer")
        self.pattern_matcher = PatternMatcher(TRANSFER_SCORING_PATTERNS)
        # Keep parameters for compatibility but don't use them
        # Suppresses "not accessed" warnings
        _ = (date_tolerance_days, debug)

        # Pre-resolve pattern field names -> functions (avoids hasattr+getattr per call)
        self._pattern_functions: Dict[str, Any] = {}
        for pattern_config in TRANSFER_SCORING_PATTERNS.values():
            for pattern in pattern_config.get('patterns', []):
                for field in pattern:
                    if field not in self._pattern_functions:
                        func = getattr(eu, field, None)
                        if func is not None and callable(func):
                            self._pattern_functions[field] = func

        # Pre-compile patterns into typed structs for fast scoring
        self._compiled_groups, self._field_index_map = compile_patterns(
            TRANSFER_SCORING_PATTERNS
        )
        self._field_names = [''] * len(self._field_index_map)
        for name, idx in self._field_index_map.items():
            self._field_names[idx] = name


    def _process_internal(
        self,
        envelopes: List[Envelope]
    ) -> Tuple[List[Envelope], List]:
        """
        Score potential matches for each envelope.

        Uses multiprocessing to evaluate pairs in parallel across CPU cores.
        Adds 'potential_matches' metadata with scored candidates.

        Steel Thread compliant: Every envelope accounted for, all decisions logged.
        """
        global _shared_envelopes, _shared_date_index
        global _shared_compiled_groups, _shared_pattern_functions, _shared_field_names

        warnings = []
        metadata = {
            'envelopes_processed': 0,
            'matches_scored': 0,
            'patterns_applied': {},
        }

        # Build date index for efficient date-window searching
        logger.info(f"Building date index for {len(envelopes)} envelopes...")
        date_index = build_envelope_date_index(envelopes, transfer_eligible_only=True)
        eligible_count = sum(len(v) for v in date_index.values())
        logger.info(f"Date index built: {len(date_index)} unique dates, "
                     f"{eligible_count} transfer-eligible envelopes")

        # Separate scoring vs skipped envelopes
        scoring_indices = []
        skipped_indices = []
        for i, env in enumerate(envelopes):
            if env.metadata and env.metadata.get('matched_with'):
                skipped_indices.append(i)
            else:
                scoring_indices.append(i)

        # Log skipped envelopes
        for i in skipped_indices:
            env = envelopes[i]
            warnings.append(ProcessingWarning(
                processor_name="TransferScorer",
                severity='INFO',
                message=f"Envelope {env.envelope_id} already matched, skipping scoring",
                source_transaction=None,
                details={
                    'envelope_id': env.envelope_id,
                    'matched_with': env.metadata.get('matched_with'),
                }
            ))

        # ── Parallel scoring ──
        # Set module globals before forking (inherited via COW, no pickle cost)
        _shared_envelopes = envelopes
        _shared_date_index = date_index
        _shared_compiled_groups = self._compiled_groups
        _shared_pattern_functions = self._pattern_functions
        _shared_field_names = self._field_names

        # Split scoring indices into chunks for workers
        num_workers = min(_NUM_WORKERS, max(1, len(scoring_indices) // 100))
        chunk_size = (len(scoring_indices) + num_workers - 1) // num_workers
        chunks = [
            scoring_indices[i:i + chunk_size]
            for i in range(0, len(scoring_indices), chunk_size)
        ]

        logger.info(f"  Scoring {len(scoring_indices)} envelopes across "
                     f"{len(chunks)} workers...")

        # Use fork context explicitly (Linux COW, no data pickling)
        ctx = mp.get_context('fork')
        all_results: Dict[int, List[Dict]] = {}

        if num_workers <= 1:
            # Single-process fast path (avoids fork overhead for small datasets)
            eu._compat_cache = {}
            all_results = _score_chunk(scoring_indices)
            eu._compat_cache = None
        else:
            with ctx.Pool(
                processes=num_workers, initializer=_init_worker
            ) as pool:
                chunk_results = pool.map(_score_chunk, chunks)
            for chunk_result in chunk_results:
                all_results.update(chunk_result)

        # Clean up module globals
        _shared_envelopes = None
        _shared_date_index = None
        _shared_compiled_groups = None
        _shared_pattern_functions = None
        _shared_field_names = None

        # ── Merge results back into envelopes ──
        skipped_set = set(skipped_indices)
        processed_envelopes = []
        for i, envelope in enumerate(envelopes):
            candidates = all_results.get(i)
            if candidates:
                enhanced_metadata = dict(envelope.metadata) if envelope.metadata else {}
                enhanced_metadata['potential_matches'] = candidates
                envelope = enhance_envelope(
                    envelope,
                    reason="Added transfer scoring results",
                    metadata=enhanced_metadata
                )
                metadata['matches_scored'] += len(candidates)

                warnings.append(ProcessingWarning(
                    processor_name="TransferScorer",
                    severity='INFO',
                    message=f"Scored {len(candidates)} potential matches for {envelope.envelope_id}",
                    source_transaction=None,
                    details={
                        'envelope_id': envelope.envelope_id,
                        'candidate_count': len(candidates),
                        'top_score': candidates[0]['score'] if candidates else 0,
                        'top_patterns': candidates[0]['patterns_matched'] if candidates else []
                    }
                ))
            elif i not in skipped_set:
                warnings.append(ProcessingWarning(
                    processor_name="TransferScorer",
                    severity='INFO',
                    message=f"No potential matches found for {envelope.envelope_id}",
                    source_transaction=None,
                    details={'envelope_id': envelope.envelope_id}
                ))

            processed_envelopes.append(envelope)
            if i not in skipped_set:
                metadata['envelopes_processed'] += 1

        # Log summary
        logger.info(
            f"TransferScorer complete: {len(envelopes)} envelopes, "
            f"{len(scoring_indices)} scored, {len(skipped_indices)} skipped, "
            f"{metadata['matches_scored']} potential matches found"
        )

        warnings.append(ProcessingWarning(
            processor_name="TransferScorer",
            severity='INFO',
            message=(f"Scoring complete: {len(envelopes)} in -> "
                     f"{len(processed_envelopes)} out, "
                     f"{metadata['matches_scored']} matches scored"),
            source_transaction=None,
            details=metadata
        ))

        return processed_envelopes, warnings

    def _score_envelope_matches(
        self,
        envelope: Envelope,
        all_envelopes: List[Envelope],
        date_index: Dict[date, List[Envelope]] = None
    ) -> List[Dict]:
        """
        Score all potential matches for an envelope.

        Args:
            envelope: Envelope to find matches for
            all_envelopes: All envelopes
            date_index: Pre-built date index for efficiency

        Returns:
            List of candidate matches with scores, sorted by score descending
        """
        candidates = []

        # Early exit if envelope is not transfer-eligible
        if not is_transfer_eligible(envelope):
            return []

        # Use date window to get candidates efficiently
        window_candidates = find_envelopes_in_date_window(
            envelope,
            all_envelopes,
            date_index=date_index,
            window_days=30  # Standard 30-day window for transfers
        )

        # Log periodically to show activity
        if len(window_candidates) > 100:
            logger.debug(f"Scoring {len(window_candidates)} candidates for {envelope.envelope_id[:8]}")

        for candidate in window_candidates:
            # Skip if already matched
            if candidate.metadata and candidate.metadata.get('matched_with'):
                continue

            # Skip if candidate is not transfer-eligible
            if not is_transfer_eligible(candidate):
                continue

            # Score using patterns
            score, pattern_details = self._score_pair(envelope, candidate)

            # Only keep positive scores
            if score > 0:
                candidates.append({
                    'envelope_id': candidate.envelope_id,
                    'score': score,
                    'patterns_matched': [p['pattern_id'] for p in pattern_details],
                    'pattern_details': pattern_details,
                    'date': str(candidate.date),
                    'amount': str(get_absolute_amount(candidate)),
                    'type': candidate.outbound_type or candidate.inbound_type,
                })

        # Sort by score descending
        candidates.sort(key=lambda x: x['score'], reverse=True)

        # Keep only top 5
        return candidates[:5]


    _MISSING = object()  # sentinel for "function raised an exception"

    def _match_pattern(
        self, env1: Envelope, env2: Envelope, pattern: Dict, func_cache: Dict
    ) -> bool:
        """Pattern matching with per-pair function result caching."""
        for field, expected in pattern.items():
            # Check per-pair cache first
            if field in func_cache:
                result = func_cache[field]
            else:
                func = self._pattern_functions.get(field)
                if func is None:
                    func_cache[field] = self._MISSING
                    return False
                try:
                    result = func(env1, env2)
                except Exception:
                    func_cache[field] = self._MISSING
                    return False
                func_cache[field] = result
            if result is self._MISSING:
                return False
            if not _check_value_match(result, expected):
                return False
        return True

    def _score_pair(self, env1: Envelope, env2: Envelope) -> Tuple[float, List[Dict[str, Any]]]:
        """
        Score an envelope pair using pre-compiled patterns.

        Returns:
            (total_score, list_of_matched_pattern_details)
        """
        # Early exit: pair must be aggregatable or reconcilable
        # (envelope_compatibility_checks results cached via eu._compat_cache)
        if not (can_aggregate(env1, env2) or can_reconcile(env1, env2)):
            return 0, []

        return _score_pair_impl(
            env1, env2,
            self._compiled_groups,
            self._pattern_functions,
            self._field_names,
        )
