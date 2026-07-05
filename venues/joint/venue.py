"""Build FactoredMarket.from_nodes(...) input from seeds-v1 market data.

This mirrors ``build_network_nodes`` in the upstream bayes-market server
(``backend/inference/network_model.py``): every seeded market becomes an
independent-root node keyed by its own marginals, unless it carries a
complete conditional-marginals table (CPT) over some set of parent
variables, in which case it becomes a CPT-child node instead.

Seeds-v1 shape (see ``data/seeds_takeoff.json``):
    {
      "version": "seeds-v1",
      "markets": {market_id: {...}, ...},
      "conditionalMarginals": {market_id: {cpt_key: {outcome: prob}}, ...}
    }

``cpt_key`` is a pipe-joined, sorted list of parent assignments, e.g.
``"a=yes|b=no"``.
"""

from __future__ import annotations

from typing import Any, Mapping


def parse_cpt_key(context_key: str) -> tuple[tuple[str, str], ...] | None:
    """Parse "a=1|b=2" into ((var, outcome), ...) pairs; None if malformed."""
    if not context_key:
        return None
    pairs: list[tuple[str, str]] = []
    for part in context_key.split("|"):
        variable_id, separator, outcome_id = part.partition("=")
        if not separator or not variable_id or not outcome_id:
            return None
        pairs.append((variable_id, outcome_id))
    return tuple(pairs)


def _root_node(
    variable_id: str,
    outcomes_by_var: Mapping[str, tuple[str, ...]],
    marginals_by_var: Mapping[str, Mapping[str, float]],
) -> dict[str, Any]:
    outcomes = outcomes_by_var[variable_id]
    marginals = marginals_by_var.get(variable_id, {})
    prior = {o: float(marginals.get(o, 0.0)) for o in outcomes}
    total = sum(prior.values())
    if total <= 0.0:
        prior = {o: 1.0 / len(outcomes) for o in outcomes}
    else:
        prior = {o: v / total for o, v in prior.items()}
    return {
        "variable_id": variable_id,
        "outcomes": outcomes,
        "parents": (),
        "rows": {frozenset(): prior},
    }


def nodes_from_seeds(seeds: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Build ``FactoredMarket.from_nodes`` input from a seeds-v1 document."""
    markets: Mapping[str, Mapping[str, Any]] = seeds["markets"]
    conditional_marginals: Mapping[str, Mapping[str, Mapping[str, float]]] = (
        seeds.get("conditionalMarginals", {})
    )

    outcomes_by_var: dict[str, tuple[str, ...]] = {}
    marginals_by_var: dict[str, Mapping[str, float]] = {}
    for market in markets.values():
        variable_id = str(market.get("variableId") or "")
        if not variable_id:
            continue
        outcomes_by_var[variable_id] = tuple(
            str(o["id"]) for o in market.get("outcomes", [])
        )
        marginals_by_var[variable_id] = market.get("marginals", {})

    nodes: list[dict[str, Any]] = []
    for market in markets.values():
        variable_id = str(market.get("variableId") or "")
        if not variable_id or variable_id not in outcomes_by_var:
            continue
        market_id = str(market.get("id"))
        node = _root_node(variable_id, outcomes_by_var, marginals_by_var)

        raw_rows = conditional_marginals.get(market_id)
        if raw_rows:
            parsed: dict[frozenset[tuple[str, str]], Mapping[str, float]] = {}
            parent_vars: set[str] = set()
            valid = True
            for key, row in raw_rows.items():
                pairs = parse_cpt_key(str(key))
                if pairs is None:
                    valid = False
                    break
                parsed[frozenset(pairs)] = row
                parent_vars.update(var for var, _ in pairs)
            if (
                valid
                and parent_vars
                and all(
                    p in outcomes_by_var and p != variable_id for p in parent_vars
                )
            ):
                parents = sorted(parent_vars)
                expected = 1
                for p in parents:
                    expected *= len(outcomes_by_var[p])
                if len(parsed) == expected:
                    node = {
                        "variable_id": variable_id,
                        "outcomes": outcomes_by_var[variable_id],
                        "parents": tuple(parents),
                        "rows": parsed,
                    }
        nodes.append(node)

    return nodes
