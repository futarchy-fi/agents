"""Cache invalidation manager for junction-tree compile snapshots."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class InvalidationResult:
    """Result of a cache invalidation check for a single market."""

    needs_recompile: bool
    clear_conditional_marginals: bool
    previous_hash: str | None
    current_hash: str


class CacheInvalidationManager:
    """Track per-market source_state_hash to detect no-op mutations and skip redundant recompilation.

    The manager compares the new source_state_hash against the previously stored
    hash for each market.  When they match the compile is a no-op (cache hit).
    When they differ the caller must recompile (cache miss) and should clear
    stale conditional marginals for the affected market.
    """

    def __init__(self) -> None:
        """Initialize an empty hash store and zero hit/miss counters."""
        self._hashes: dict[str, str] = {}
        self._cache_hits: int = 0
        self._cache_misses: int = 0

    # -- public API ----------------------------------------------------------

    def check(self, market_id: str, new_source_state_hash: str) -> InvalidationResult:
        """Compare *new_source_state_hash* against the stored hash for *market_id*.

        Returns an :class:`InvalidationResult` indicating whether recompilation
        is needed and whether conditional marginals should be cleared.

        :param market_id: Unique identifier for the market being checked.
        :param new_source_state_hash: Hash of the current market source state.
        :returns: An :class:`InvalidationResult` with recompilation and
            marginal-clearing flags.
        """
        previous = self._hashes.get(market_id)
        if previous is not None and previous == new_source_state_hash:
            self._cache_hits += 1
            return InvalidationResult(
                needs_recompile=False,
                clear_conditional_marginals=False,
                previous_hash=previous,
                current_hash=new_source_state_hash,
            )

        self._cache_misses += 1
        self._hashes[market_id] = new_source_state_hash
        return InvalidationResult(
            needs_recompile=True,
            clear_conditional_marginals=True,
            previous_hash=previous,
            current_hash=new_source_state_hash,
        )

    @property
    def cache_hits(self) -> int:
        """Return the number of cache hits since last reset."""
        return self._cache_hits

    @property
    def cache_misses(self) -> int:
        """Return the number of cache misses since last reset."""
        return self._cache_misses

    def reset(self) -> None:
        """Clear all stored hashes and counters."""
        self._hashes.clear()
        self._cache_hits = 0
        self._cache_misses = 0
