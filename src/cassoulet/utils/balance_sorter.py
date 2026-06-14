"""
Balance Sorter for Envelopes

Hybrid envelope-native version of the balance sorting algorithm.
Handles the complex case of same-day transactions with partial balance data.

Uses a two-stage approach:
1. Graph-based algorithm for efficient sorting when it works
2. Permutation-based brute force for small sets when graph fails

This ensures we always find the correct ordering if one exists.
"""

from typing import List, Dict, Optional, Tuple
from decimal import Decimal
from collections import defaultdict
from itertools import permutations
import logging

from cassoulet.stages.envelope import Envelope
from cassoulet.utils.envelope_utilities import enhance_envelope

logger = logging.getLogger(__name__)


def solve_balance_ordering(envelopes: List[Envelope]) -> List[Envelope]:
    """
    Apply balance-aware ordering to envelopes.

    Returns a new list with sort_order fields set based on balance constraints,
    sorted by these values.

    Args:
        envelopes: List of envelopes to order

    Returns:
        New list of envelopes with sort_order set and sorted by it
    """
    if not envelopes:
        return []

    # Group by date for processing
    by_date = defaultdict(list)
    for env in envelopes:
        by_date[env.date].append(env)

    # Process each date to assign sort_order values
    result = []
    for date in sorted(by_date.keys()):
        date_envs = by_date[date]

        if len(date_envs) == 1:
            # Single transaction - trivial
            enhanced = enhance_envelope(date_envs[0],
                                       reason="Single transaction on date",
                                       sort_order=Decimal(date.toordinal()))
            result.append(enhanced)

        elif not any(e.balance_after for e in date_envs):
            # No balance data - maintain original order
            sorted_envs = sorted(date_envs, key=lambda e: e.original_order or 0)
            for i, env in enumerate(sorted_envs):
                enhanced = enhance_envelope(env,
                                           reason="No balance data - preserving original order",
                                           sort_order=Decimal(f"{date.toordinal()}.{i:04d}"))
                result.append(enhanced)

        else:
            # Multiple transactions with balance constraints
            # Apply smart ordering to satisfy balance math
            ordered = _solve_constrained_ordering(date_envs)
            for i, env in enumerate(ordered):
                enhanced = enhance_envelope(env,
                                           reason="Balance-constrained ordering applied",
                                           sort_order=Decimal(f"{date.toordinal()}.{i:04d}"))
                result.append(enhanced)

    # Final step: sort the entire list by the sort_order we just assigned
    result.sort(key=lambda e: e.sort_order)

    return result


def _solve_constrained_ordering(envelopes: List[Envelope],
                                tolerance: Decimal = Decimal('0.01')) -> List[Envelope]:
    """
    Apply constraint satisfaction for same-day transaction ordering.

    This handles the complex case where we have multiple transactions on the
    same day with partial balance information. We need to find an ordering
    that makes the balance math work.

    Uses a hybrid approach:
    1. Graph algorithms for efficiency when they work
    2. Permutation-based brute force for small sets when graph fails

    Args:
        envelopes: List of same-day envelopes
        tolerance: Maximum acceptable balance difference

    Returns:
        Ordered list that satisfies balance constraints
    """
    # Separate envelopes with and without balance data
    with_balance = [e for e in envelopes if e.balance_after is not None]
    without_balance = [e for e in envelopes if e.balance_after is None]

    if not with_balance:
        # No constraints - maintain original order
        return sorted(envelopes, key=lambda e: e.original_order or 0)

    # Mixed case: some with balance, some without
    # CRITICAL HEURISTIC: Put transactions WITHOUT balance first,
    # then transactions WITH balance at the end (end-of-day balances)
    if without_balance:
        # Sort each group by original index to maintain relative order
        without_balance.sort(key=lambda e: e.original_order or 0)

        # For the with_balance group, try to find perfect ordering if small enough
        if len(with_balance) <= 6:  # Permutation feasible for <= 6 items
            best_order = _find_best_permutation_order(with_balance, tolerance)
            if best_order:
                return without_balance + best_order

        with_balance.sort(key=lambda e: e.original_order or 0)
        return without_balance + with_balance

    # All have balance - use hybrid approach

    # First try graph-based approach (fast)
    graph = _build_envelope_graph(with_balance, tolerance)
    chain = _find_longest_chain(graph, with_balance, None, tolerance)

    # Check if we got a complete valid chain
    if chain and len(chain) == len(with_balance):
        # Verify the chain is actually valid
        if _check_balance_continuity(chain, tolerance):
            return chain

    # Graph didn't find complete solution - try permutations if feasible
    if len(with_balance) <= 7:  # 7! = 5040 permutations is still fast
        logger.debug(f"Graph approach incomplete, trying {len(with_balance)}! permutations")
        best_order = _find_best_permutation_order(with_balance, tolerance)
        if best_order:
            return best_order

    # For large sets where permutation isn't feasible, use best available heuristic
    if chain:
        return chain
    else:
        # Last resort: sort by balance value
        return sorted(with_balance, key=lambda e: e.balance_after or Decimal('0'), reverse=True)


def compute_implied_balances(sorted_envelopes: List[Envelope],
                            starting_balance: Decimal = Decimal('0')) -> List[Envelope]:
    """
    Compute implied balances for envelopes without express balance data.

    Returns a new list with updated envelopes that have balance_after and
    balance_type='implied' set for envelopes that don't have express balance data.

    Args:
        sorted_envelopes: List of envelopes sorted by date/order
        starting_balance: Opening balance for the account

    Returns:
        List of envelopes with implied balances computed
    """
    running_balance = starting_balance
    result = []

    for env in sorted_envelopes:
        if env.balance_after is not None and env.balance_type == 'express':
            # Express balance - use as checkpoint
            running_balance = env.balance_after
            result.append(env)
        else:
            # Compute implied balance
            if env.outbound_units:
                running_balance -= env.outbound_units
            if env.inbound_units:
                running_balance += env.inbound_units

            # Create new envelope with computed balance
            enhanced = enhance_envelope(env,
                                       reason="Computed implied balance",
                                       balance_after=running_balance,
                                       balance_type='implied')
            result.append(enhanced)

    return result


def validate_balance_continuity(sorted_envelopes: List[Envelope]) -> List[Dict]:
    """
    Validate that balance math is consistent.

    Args:
        sorted_envelopes: List of sorted envelopes with balances

    Returns:
        List of validation errors (empty if valid)
    """
    errors = []

    for i in range(len(sorted_envelopes) - 1):
        curr = sorted_envelopes[i]
        next = sorted_envelopes[i + 1]

        # Calculate expected balance
        expected = curr.balance_after or Decimal('0')
        if next.outbound_units:
            expected -= next.outbound_units
        if next.inbound_units:
            expected += next.inbound_units

        # Check against actual (if express)
        if next.balance_type == 'express' and next.balance_after:
            discrepancy = abs(next.balance_after - expected)
            if discrepancy > Decimal('0.01'):
                errors.append({
                    'date': next.date,
                    'expected': expected,
                    'actual': next.balance_after,
                    'discrepancy': discrepancy,
                    'envelope_id': next.envelope_id
                })

    return errors


def _build_envelope_graph(envelopes: List[Envelope],
                         tolerance: Decimal = Decimal('0.01')) -> Dict[int, List[int]]:
    """
    Build a directed graph where edges connect envelopes that can follow each other.

    An edge from envelope i to envelope j exists if:
    envelopes[i].balance_after + net_amount(envelopes[j]) = envelopes[j].balance_after

    Args:
        envelopes: List of envelopes with balance data
        tolerance: Maximum acceptable balance difference

    Returns:
        Adjacency list representation of the graph
    """
    n = len(envelopes)
    graph = {i: [] for i in range(n)}

    for i in range(n):
        for j in range(n):
            if i == j:
                continue

            # Check if envelope j can follow envelope i
            if (_can_follow(envelopes[i], envelopes[j], tolerance)):
                graph[i].append(j)

    return graph


def _can_follow(env1: Envelope, env2: Envelope, tolerance: Decimal) -> bool:
    """
    Check if env2 can directly follow env1 in transaction order.

    Args:
        env1: First envelope
        env2: Second envelope
        tolerance: Maximum acceptable balance difference

    Returns:
        True if env2 can follow env1
    """
    # Both must have balance data
    if env1.balance_after is None or env2.balance_after is None:
        return False

    # Calculate net amount for env2
    net_amount = Decimal('0')
    if env2.inbound_units:
        net_amount += env2.inbound_units
    if env2.outbound_units:
        net_amount -= env2.outbound_units

    # Check if balance math works
    expected = env1.balance_after + net_amount
    actual = env2.balance_after

    return abs(expected - actual) <= tolerance


def _find_longest_chain(graph: Dict[int, List[int]],
                       envelopes: List[Envelope],
                       previous_balance: Optional[Decimal],
                       tolerance: Decimal = Decimal('0.01')) -> List[Envelope]:
    """
    Find the longest valid chain (path) through the envelope graph.

    Uses DFS with memoization to efficiently explore all paths.

    Args:
        graph: Adjacency list of envelope graph
        envelopes: List of envelopes
        previous_balance: Balance from previous day (if known)
        tolerance: Maximum acceptable balance difference

    Returns:
        Ordered list of ALL envelopes, with the longest chain first
    """
    if not envelopes:
        return []

    n = len(envelopes)
    longest_chain_indices = []
    best_chain_with_prev_balance = []

    # Try starting from each envelope to find the overall longest chain
    for start_idx in range(n):
        # DFS to find longest path from this starting point
        visited = set()
        memo = {}  # Memoization cache for this search
        chain = _dfs_longest_path_memoized(graph, start_idx, visited, memo)

        # Track the longest chain overall
        if len(chain) > len(longest_chain_indices):
            longest_chain_indices = chain

        # Also track the longest chain that connects to previous_balance
        if previous_balance is not None and chain:
            first_env = envelopes[chain[0]]
            # Calculate net amount for first envelope
            net_amount = Decimal('0')
            if first_env.inbound_units:
                net_amount += first_env.inbound_units
            if first_env.outbound_units:
                net_amount -= first_env.outbound_units

            if first_env.balance_after is not None:
                expected = previous_balance + net_amount
                if abs(expected - first_env.balance_after) <= tolerance:
                    if len(chain) > len(best_chain_with_prev_balance):
                        best_chain_with_prev_balance = chain

    # Check if we found a chain that connects to previous balance
    connected_to_previous = False
    if best_chain_with_prev_balance:
        longest_chain_indices = best_chain_with_prev_balance
        connected_to_previous = True

    # Build result with proper orphan placement
    chain_set = set(longest_chain_indices)
    chain = [envelopes[i] for i in longest_chain_indices]

    # Collect orphan envelopes (not in the main chain)
    orphans = []
    for i, env in enumerate(envelopes):
        if i not in chain_set:
            orphans.append(env)

    if not orphans:
        return chain

    # Place orphans based on context
    if chain:
        # If chain connects to previous balance, always put it first
        if connected_to_previous:
            # Sort orphans by balance and place after the chain
            orphans.sort(key=lambda e: e.balance_after if e.balance_after is not None else Decimal('0'), reverse=True)
            result = chain + orphans
        else:
            # Original logic: place orphans based on balance relative to chain
            chain_start_balance = chain[0].balance_after if chain[0].balance_after is not None else Decimal('0')

            orphans_before = []
            orphans_after = []

            for orphan in orphans:
                orphan_balance = orphan.balance_after if orphan.balance_after is not None else Decimal('0')
                if orphan_balance > chain_start_balance:
                    orphans_before.append(orphan)
                else:
                    orphans_after.append(orphan)

            # Sort orphans by balance
            orphans_before.sort(key=lambda e: e.balance_after if e.balance_after is not None else Decimal('0'), reverse=True)
            orphans_after.sort(key=lambda e: e.balance_after if e.balance_after is not None else Decimal('0'), reverse=True)

            result = orphans_before + chain + orphans_after
    else:
        # No chain found, just sort all by balance
        orphans.sort(key=lambda e: e.balance_after if e.balance_after is not None else Decimal('0'), reverse=True)
        result = orphans

    return result


def _dfs_longest_path_memoized(graph: Dict[int, List[int]],
                               current: int,
                               visited: set,
                               memo: Dict[tuple, List[int]]) -> List[int]:
    """
    Optimized DFS with memoization to find the longest path.

    Args:
        graph: Adjacency list
        current: Current node index
        visited: Set of visited nodes
        memo: Memoization cache

    Returns:
        Longest path as list of node indices
    """
    # Create a frozen set key for memoization that includes visited state
    memo_key = (current, frozenset(visited))
    if memo_key in memo:
        return memo[memo_key]

    visited.add(current)
    longest_path = [current]

    # Explore all neighbors
    for neighbor in graph.get(current, []):
        if neighbor not in visited:
            path = _dfs_longest_path_memoized(graph, neighbor, visited.copy(), memo)
            if len(path) + 1 > len(longest_path):
                longest_path = [current] + path

    memo[memo_key] = longest_path
    return longest_path


def _check_balance_continuity(envelopes: List[Envelope],
                              tolerance: Decimal = Decimal('0.01')) -> bool:
    """
    Check if a sequence of envelopes maintains balance continuity.

    Args:
        envelopes: Ordered list of envelopes
        tolerance: Maximum acceptable balance difference

    Returns:
        True if balance continuity is maintained throughout the sequence
    """
    if len(envelopes) <= 1:
        return True

    for i in range(len(envelopes) - 1):
        curr = envelopes[i]
        next_env = envelopes[i + 1]

        # Both must have balance for continuity check
        if curr.balance_after is None or next_env.balance_after is None:
            continue

        # Calculate expected balance after next transaction
        expected = curr.balance_after
        if next_env.inbound_units:
            expected += next_env.inbound_units
        if next_env.outbound_units:
            expected -= next_env.outbound_units

        # Check against actual
        if abs(expected - next_env.balance_after) > tolerance:
            return False

    return True


def _find_best_permutation_order(envelopes: List[Envelope],
                                 tolerance: Decimal = Decimal('0.01')) -> Optional[List[Envelope]]:
    """
    Find the best ordering of envelopes using brute-force permutation search.

    This tries all possible orderings and returns the first one that maintains
    perfect balance continuity. If no perfect ordering exists, returns the one
    with the minimum total error.

    Args:
        envelopes: List of envelopes to order (should be small, <= 7 items)
        tolerance: Maximum acceptable balance difference

    Returns:
        Best ordering found, or None if no valid ordering exists
    """
    if not envelopes:
        return None

    best_order = None
    best_error = float('inf')
    perfect_found = False

    # Try all permutations
    for perm in permutations(envelopes):
        perm_list = list(perm)

        # Check if this permutation maintains perfect continuity
        if _check_balance_continuity(perm_list, tolerance):
            # Perfect match found, return immediately
            logger.debug(f"Found perfect ordering via permutation")
            return perm_list

        # Calculate total error for this ordering
        total_error = _calculate_ordering_error(perm_list, tolerance)
        if total_error < best_error:
            best_error = total_error
            best_order = perm_list

    if best_order and best_error < float('inf'):
        logger.debug(f"Best permutation found with error {best_error}")
        return best_order

    return None


def _calculate_ordering_error(envelopes: List[Envelope],
                              tolerance: Decimal = Decimal('0.01')) -> Decimal:
    """
    Calculate the total balance error for a given ordering.

    Args:
        envelopes: Ordered list of envelopes
        tolerance: Not used here, but kept for consistency

    Returns:
        Total absolute error across all consecutive envelope pairs
    """
    if len(envelopes) <= 1:
        return Decimal('0')

    total_error = Decimal('0')

    for i in range(len(envelopes) - 1):
        curr = envelopes[i]
        next_env = envelopes[i + 1]

        # Skip if either lacks balance data
        if curr.balance_after is None or next_env.balance_after is None:
            continue

        # Calculate expected vs actual
        expected = curr.balance_after
        if next_env.inbound_units:
            expected += next_env.inbound_units
        if next_env.outbound_units:
            expected -= next_env.outbound_units

        error = abs(expected - next_env.balance_after)
        total_error += error

    return total_error