"""Build FactoredMarket.from_nodes(...) input from seeds-v1 market data.

Seeds-v1 shape (see ``data/seeds_takeoff.json``):
    {
      "version": "seeds-v1",
      "markets": {market_id: {...}, ...},
      "conditionalMarginals": {market_id: {cpt_key: {outcome: prob}}, ...}
    }

Node construction (independent-root vs. CPT-child, ``cpt_key`` parsing, etc.)
is delegated to the vendored ``build_network_nodes`` in
``venues.joint.inference.network_model`` — the same function the upstream
bayes-market server uses to build both the flat and factored market makers —
so this module stays a thin adapter rather than a second copy of that logic.
"""

from __future__ import annotations

from typing import Any, Mapping

from venues.joint.inference import build_network_nodes


def nodes_from_seeds(seeds: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Build ``FactoredMarket.from_nodes`` input from a seeds-v1 document."""
    markets: Mapping[str, Mapping[str, Any]] = seeds["markets"]
    conditional_marginals: Mapping[str, Mapping[str, Mapping[str, float]]] = (
        seeds.get("conditionalMarginals", {})
    )
    return build_network_nodes(markets, conditional_marginals)
