"""
Envelope-Native Transfer Matcher

This is the envelope-native implementation of TransferMatcher that works
directly with Envelopes without creating Transaction objects.

Part of the deferred posting architecture (Issue #79).
"""

import logging
from typing import List, Dict, Any, Tuple, Optional, Set
from dataclasses import dataclass
from collections import defaultdict

from cassoulet.base.exceptions import ProcessingWarning
from cassoulet.stages.envelope import Envelope
from cassoulet.config.component_config import TransferMatchingConfig
from cassoulet.stages.envelope_processor import EnvelopeProcessor
from cassoulet.utils.envelope_utilities import (
    build_envelope_lookup,
    enhance_envelope,
    envelope_compatibility_checks,
)

logger = logging.getLogger(__name__)


@dataclass
class EnvelopeMatchDecision:
    """Represents a matching decision between envelopes."""
    env1_id: str
    env2_id: str
    score: float
    confidence: str
    source_pattern: str  # 'manual-csv', 'csv-csv', 'manual-manual'
    flow_pattern: str  # 'opposite_flows', 'same_flows', etc.
    comparison_mode: str  # 'transfer', 'reconciliation', 'subset'


class TransferMatcher(EnvelopeProcessor):
    """
    Envelope-native transfer matcher.

    Makes matching decisions based on scored transfer candidates,
    working directly with envelopes without creating transactions.
    """

    def __init__(
        self,
        config: Optional[TransferMatchingConfig] = None,
        debug: bool = False
    ):
        """
        Initialize the envelope-native transfer matcher.

        Args:
            config: Configuration object (uses default if None)
            debug: Enable debug logging
        """
        super().__init__(processor_name="TransferMatcher")

        # Use provided config or default
        self.config = config or TransferMatchingConfig()
        self.debug = debug

        self.stats = {
            'envelopes_processed': 0,
            'matches_made': 0,
            'high_confidence_matches': 0,
            'medium_confidence_matches': 0,
            'conflicts_resolved': 0,
            'conflicts_unresolved': 0,
            'manual_csv_matches': 0,
            'csv_csv_matches': 0,
            'love_triangles_detected': 0,
            'asymmetric_matches_blocked': 0,
            'leftover_envelopes': 0
        }

        # Track love triangles and leftovers for reporting
        self.love_triangles = []
        self.leftover_envelopes = []

    def _process_internal(
        self,
        envelopes: List[Envelope]
    ) -> Tuple[List[Envelope], List[ProcessingWarning]]:
        """
        Process scored envelopes to make matching decisions.

        This method works directly with envelopes, no Transaction objects.

        Args:
            envelopes: List of envelopes with scoring metadata

        Returns:
            Tuple of (matched_envelopes, warnings)
        """
        warnings = []

        # Build envelope lookup
        env_by_id = build_envelope_lookup(envelopes)

        # Track envelopes processed
        self.stats['envelopes_processed'] = len(envelopes)

        # Collect all potential matches from scored envelopes
        match_candidates = self._collect_match_candidates(envelopes, env_by_id)

        # Resolve conflicts and make decisions
        match_decisions, decision_warnings = self._make_match_decisions(match_candidates, env_by_id)
        warnings.extend(decision_warnings)

        # Apply match decisions to envelopes
        matched_envelopes = self._apply_match_decisions(
            envelopes, match_decisions, env_by_id
        )

        # Add processing metadata
        for env in matched_envelopes:
            env.metadata['reconciliation_stage'] = 'matched'
            env.add_history(
                state=env.state,
                component=self.processor_name,
                action="Transfer matching completed",
                details={'stats': self.stats.copy()}
            )

        # Log summary statistics
        logger.info(
            f"TransferMatcher: Processed {self.stats['envelopes_processed']} envelopes, "
            f"made {self.stats['matches_made']} matches"
        )

        # Report love triangles if any were detected
        if self.love_triangles:
            logger.warning(f"💔 DETECTED {len(self.love_triangles)} LOVE TRIANGLES")
            for i, triangle in enumerate(self.love_triangles, 1):
                logger.debug(
                    f"  Triangle #{i}: "
                    f"{triangle['env1']['id'][:8]} → {triangle['env1']['best_match'][:8] if triangle['env1']['best_match'] else 'None'}, "
                    f"{triangle['env2']['id'][:8]} → {triangle['env2']['best_match'][:8] if triangle['env2']['best_match'] else 'None'}, "
                    f"{triangle['env3']['id'][:8]} → {triangle['env3']['best_match'][:8] if triangle['env3']['best_match'] else 'None'}"
                )

        # Report leftover envelopes with potential matches
        if self.leftover_envelopes:
            logger.info(f"⚠️ {len(self.leftover_envelopes)} envelopes with candidates but no match")

            # Group by reason
            by_reason = {}
            for leftover in self.leftover_envelopes:
                reason = leftover['reason']
                if reason not in by_reason:
                    by_reason[reason] = []
                by_reason[reason].append(leftover)

            for reason, leftovers in by_reason.items():
                logger.debug(f"  {reason}: {len(leftovers)} envelopes")
                if self.debug:
                    for leftover in leftovers[:3]:  # Show first 3 of each type
                        logger.debug(
                            f"    - {leftover['envelope_id'][:8]}: '{leftover['narration'][:30]}' "
                            f"wanted {leftover['best_match_id'][:8] if leftover['best_match_id'] else 'None'} "
                            f"(score {leftover['best_match_score']:.1f})"
                        )
                    if len(leftovers) > 3:
                        logger.debug(f"    ... and {len(leftovers) - 3} more")

        # Add summary warning for leftover analysis
        if self.leftover_envelopes or self.love_triangles:
            warnings.append(ProcessingWarning(
                processor_name=self.processor_name,
                severity='WARNING',
                message=f"Matching issues detected: {len(self.love_triangles)} love triangles, {len(self.leftover_envelopes)} leftover envelopes",
                details={
                    'love_triangles': self.love_triangles,
                    'leftover_summary': {
                        reason: len([l for l in self.leftover_envelopes if l['reason'] == reason])
                        for reason in set(l['reason'] for l in self.leftover_envelopes)
                    } if self.leftover_envelopes else {}
                }
            ))

        return matched_envelopes, warnings


    def _collect_match_candidates(
        self,
        envelopes: List[Envelope],
        env_by_id: Dict[str, Envelope]
    ) -> Dict[str, List[Tuple[str, float, str, Dict]]]:
        """
        Collect all match candidates from scored envelopes.

        The TransferScorer has already looked at each envelope and found
        other envelopes that might be the other side of a transfer
        (e.g., money leaving one account and arriving in another).
        These are stored as 'potential_matches' with scores.

        Returns:
            Dictionary mapping envelope ID to list of (candidate_id, score, confidence, details)
        """
        candidates_by_env = defaultdict(list)

        for env in envelopes:
            env_id = env.envelope_id
            if not env_id:
                continue

            # Get candidates from TransferScorer metadata
            # These are potential transfer matches that the scorer found
            potential_matches = env.metadata.get('potential_matches', [])
            if potential_matches:
                for candidate in potential_matches:
                    candidate_id = candidate.get('envelope_id')
                    if candidate_id and candidate_id in env_by_id:
                        candidates_by_env[env_id].append((
                            candidate_id,
                            candidate['score'],
                            self._get_confidence_level(candidate['score']),
                            candidate
                        ))
            # Fallback to simplified metadata
            elif env.metadata.get('potential_match'):
                candidate_id = env.metadata['potential_match']
                if candidate_id in env_by_id:
                    score = float(env.metadata.get('transfer_score', 0))
                    confidence = self._get_confidence_level(score)
                    candidates_by_env[env_id].append((
                        candidate_id,
                        score,
                        confidence,
                        {'envelope_id': candidate_id, 'score': score, 'confidence': confidence}
                    ))

        return candidates_by_env

    def _get_confidence_level(self, score: float) -> str:
        """Determine confidence level from score.

        These thresholds align with our pattern-based scoring system:
        - High: Strong matches with multiple patterns
        - Medium: Good matches with some patterns
        - Low: Weak matches with few patterns
        """
        if score >= 100:  # Multiple strong patterns
            return 'high'
        elif score >= 70:  # At least a few good patterns
            return 'medium'
        else:
            return 'low'

    def _make_match_decisions(
        self,
        candidates_by_env: Dict[str, List],
        env_by_id: Dict[str, Envelope]
    ) -> Tuple[List[EnvelopeMatchDecision], List[ProcessingWarning]]:
        """
        Make matching decisions based on candidates and resolve conflicts.

        Returns:
            Tuple of (decisions, warnings)
        """
        warnings = []
        decisions = []

        # Track what's been matched to avoid double-matching
        matched_envelopes = set()

        # Sort all potential matches by score (highest first)
        all_matches = []
        processed_pairs = set()

        for env_id, candidates in candidates_by_env.items():
            if env_id not in env_by_id:
                continue

            for candidate_id, score, confidence, metadata in candidates:
                # Create a consistent pair identifier to avoid duplicates
                pair_id = tuple(sorted([env_id, candidate_id]))
                if pair_id in processed_pairs:
                    continue

                processed_pairs.add(pair_id)

                # Check bidirectional matching and compatibility
                reverse_candidates = candidates_by_env.get(candidate_id, [])
                has_reverse = any(c[0] == env_id for c in reverse_candidates)

                # Use compatibility check to validate the match makes sense
                env1 = env_by_id.get(env_id)
                env2 = env_by_id.get(candidate_id)
                if env1 and env2:
                    compat = envelope_compatibility_checks(env1, env2)
                    is_compatible = compat.get('compatible', False)
                else:
                    is_compatible = False

                # Allow match if: bidirectional OR (high confidence AND compatible)
                if has_reverse or (confidence == 'high' and is_compatible):
                    all_matches.append((score, env_id, candidate_id, confidence, metadata))
                else:
                    # Track asymmetric or incompatible match attempts
                    self.stats['asymmetric_matches_blocked'] += 1
                    if self.debug:
                        reason = "incompatible" if not is_compatible else "no reverse match"
                        logger.debug(
                            f"Blocked match: {env_id[:8]} -> {candidate_id[:8]} "
                            f"({reason}, confidence={confidence})"
                        )

        # Sort by score descending
        all_matches.sort(reverse=True)

        logger.info(f"Processing {len(all_matches)} potential matches...")

        # Process matches in order of confidence
        for score, env1_id, env2_id, confidence, metadata in all_matches:
            # Skip if either envelope is already matched
            if env1_id in matched_envelopes or env2_id in matched_envelopes:
                continue

            # CRITICAL: Enforce minimum score threshold
            # Scores below 70 are too weak to be reliable matches
            if score < 70:
                self.stats.setdefault('matches_below_threshold', 0)
                self.stats['matches_below_threshold'] += 1
                logger.info(
                    f"REJECTED: Match below threshold: {env1_id[:30]} <-> {env2_id[:30]} "
                    f"(score={score}, threshold=70)"
                )
                continue

            env1 = env_by_id[env1_id]
            env2 = env_by_id[env2_id]

            # Use our comprehensive compatibility check
            compat = envelope_compatibility_checks(env1, env2)

            # Extract meaningful relationship information
            source_pattern = compat.get('source_pattern', 'unknown')
            flow_pattern = compat.get('flow_pattern', 'unknown')
            comparison_mode = compat.get('comparison_mode', 'unknown')

            # Create match decision with meaningful context
            decision = EnvelopeMatchDecision(
                env1_id=env1_id,
                env2_id=env2_id,
                score=score,
                confidence=confidence,
                source_pattern=source_pattern,
                flow_pattern=flow_pattern,
                comparison_mode=comparison_mode
            )
            decisions.append(decision)

            # Mark both as matched
            matched_envelopes.add(env1_id)
            matched_envelopes.add(env2_id)

            # Update statistics
            self.stats['matches_made'] += 1
            if confidence == 'high':
                self.stats['high_confidence_matches'] += 1
            elif confidence == 'medium':
                self.stats['medium_confidence_matches'] += 1

            if source_pattern == 'manual-csv':
                self.stats['manual_csv_matches'] += 1
            elif source_pattern == 'csv-csv':
                self.stats['csv_csv_matches'] += 1

            if self.debug:
                logger.debug(
                    f"Match decision: {env1_id[:8]} <-> {env2_id[:8]} "
                    f"(score={score:.1f}, confidence={confidence}, "
                    f"source={source_pattern}, flow={flow_pattern}, mode={comparison_mode})"
                )

        # Detect love triangles and other issues
        self._detect_love_triangles(candidates_by_env, matched_envelopes, env_by_id)
        self._analyze_leftovers(candidates_by_env, matched_envelopes, env_by_id)

        # Log summary of matching results
        if self.stats.get('matches_below_threshold', 0) > 0:
            logger.info(
                f"Matching summary: {len(decisions)} matches made, "
                f"{self.stats.get('matches_below_threshold', 0)} rejected (score < 70)"
            )

        return decisions, warnings

    def _detect_love_triangles(
        self,
        candidates_by_env: Dict[str, List],
        matched_envelopes: Set[str],
        env_by_id: Dict[str, Envelope]
    ) -> None:
        """Detect love triangle situations where three envelopes all want to match each other."""
        # Find envelopes with mutual attraction that weren't matched
        for env_id in candidates_by_env:
            if env_id in matched_envelopes:
                continue

            candidates = candidates_by_env[env_id]
            if not candidates:
                continue

            # Get this envelope's best match
            best_match = max(candidates, key=lambda x: x[1]) if candidates else None
            if not best_match:
                continue

            best_match_id = best_match[0]

            # Check if best match also has candidates
            if best_match_id not in candidates_by_env:
                continue

            best_match_candidates = candidates_by_env[best_match_id]
            if not best_match_candidates:
                continue

            # Get best match's best match
            bm_best = max(best_match_candidates, key=lambda x: x[1])
            bm_best_id = bm_best[0]

            # Check for triangle: A->B, B->C, C->A
            if bm_best_id != env_id and bm_best_id in candidates_by_env:
                third_candidates = candidates_by_env[bm_best_id]
                if third_candidates:
                    third_best = max(third_candidates, key=lambda x: x[1])
                    if third_best[0] == env_id:
                        # Love triangle detected!
                        self.love_triangles.append({
                            'env1': {'id': env_id, 'best_match': best_match_id},
                            'env2': {'id': best_match_id, 'best_match': bm_best_id},
                            'env3': {'id': bm_best_id, 'best_match': env_id}
                        })
                        self.stats['love_triangles_detected'] += 1

    def _analyze_leftovers(
        self,
        candidates_by_env: Dict[str, List],
        matched_envelopes: Set[str],
        env_by_id: Dict[str, Envelope]
    ) -> None:
        """Analyze envelopes that had candidates but weren't matched."""
        for env_id in candidates_by_env:
            if env_id in matched_envelopes:
                continue

            candidates = candidates_by_env[env_id]
            if not candidates:
                continue

            env = env_by_id.get(env_id)
            if not env:
                continue

            # Get best candidate
            best_candidate = max(candidates, key=lambda x: x[1]) if candidates else None
            if not best_candidate:
                continue

            best_match_id = best_candidate[0]
            best_score = best_candidate[1]

            # Determine why it wasn't matched
            reason = 'unknown'
            if best_match_id in matched_envelopes:
                reason = 'best_match_taken'
            elif best_match_id not in candidates_by_env.get(best_match_id, {}) :
                reason = 'no_bidirectional_match'
            elif best_score < 70:  # Minimum score threshold
                reason = 'score_too_low'

            self.leftover_envelopes.append({
                'envelope_id': env_id,
                'narration': env.narration[:50] if env.narration else '',
                'best_match_id': best_match_id,
                'best_match_score': best_score,
                'reason': reason
            })
            self.stats['leftover_envelopes'] += 1

    def _apply_match_decisions(
        self,
        envelopes: List[Envelope],
        decisions: List[EnvelopeMatchDecision],
        env_by_id: Dict[str, Envelope]
    ) -> List[Envelope]:
        """
        Apply matching decisions to envelopes.

        Args:
            envelopes: Original envelopes
            decisions: Match decisions to apply
            env_by_id: Envelope lookup

        Returns:
            List of envelopes with match metadata applied
        """
        # Create a map of envelope ID to its match
        matches = {}
        for decision in decisions:
            matches[decision.env1_id] = {
                'matched_with': decision.env2_id,
                'match_score': decision.score,
                'match_confidence': decision.confidence,
                'source_pattern': decision.source_pattern,
                'flow_pattern': decision.flow_pattern,
                'comparison_mode': decision.comparison_mode
            }
            matches[decision.env2_id] = {
                'matched_with': decision.env1_id,
                'match_score': decision.score,
                'match_confidence': decision.confidence,
                'source_pattern': decision.source_pattern,
                'flow_pattern': decision.flow_pattern,
                'comparison_mode': decision.comparison_mode
            }

        # Apply match metadata to envelopes
        result = []
        for env in envelopes:
            env_id = env.envelope_id

            if env_id in matches:
                # This envelope is matched
                match_info = matches[env_id]

                # Create enhanced metadata with match information
                enhanced_metadata = {
                    **env.metadata,
                    'matched': True,
                    'matched_with': match_info['matched_with'],
                    'match_score': match_info['match_score'],
                    'match_confidence': match_info['match_confidence'],
                    'source_pattern': match_info['source_pattern'],
                    'flow_pattern': match_info['flow_pattern'],
                    'comparison_mode': match_info['comparison_mode'],
                    'ready_to_merge': True
                }

                # Add envelope role information
                matched_env = env_by_id.get(match_info['matched_with'])
                if matched_env:
                    compat = envelope_compatibility_checks(env, matched_env)
                    enhanced_metadata['env_role'] = compat.get('env1_role', 'unknown')
                    enhanced_metadata['matched_env_role'] = compat.get('env2_role', 'unknown')

                enhanced_env = enhance_envelope(
                    env,
                    reason="Apply transfer match decision",
                    metadata=enhanced_metadata
                )

                if self.debug:
                    logger.debug(
                        f"Applied match to {env_id[:8]}: matched with {match_info['matched_with'][:8]} "
                        f"(confidence={match_info['match_confidence']})"
                    )

                result.append(enhanced_env)
            else:
                # Not matched, clean up any stale match metadata
                cleaned_metadata = {k: v for k, v in env.metadata.items()
                                    if k not in ['matched_with', 'ready_to_merge',
                                                 'match_score', 'match_confidence',
                                                 'source_pattern', 'flow_pattern',
                                                 'comparison_mode', 'env_role',
                                                 'matched_env_role']}
                cleaned_metadata['matched'] = False

                enhanced_env = enhance_envelope(
                    env,
                    reason="Clear match metadata",
                    metadata=cleaned_metadata
                )

                result.append(enhanced_env)

        return result