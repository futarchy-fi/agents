"""Exact joint inference over the seeded market Bayes network.

The seeded markets form a small DAG (one variable per market, CPT rows in
CONDITIONAL_MARGINALS). This module materializes the full joint distribution
— tractable because the state space is tiny (2^16 for the seed network) —
and answers arbitrary conditional queries, including diagnostic
(child-to-parent) evidence that the per-market lookup artifacts cannot
express.
"""

from __future__ import annotations

from typing import Any, Mapping

MAX_JOINT_STATES = 1 << 22
_QUERY_CACHE_LIMIT = 4096


class NetworkModelError(ValueError):
    """Raised when the market data cannot form a valid DAG network model."""


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


class BayesNetworkModel:
    """Immutable joint model over one-variable-per-market nodes.

    nodes: list of dicts with keys
      - variable_id: str
      - outcomes: sequence of outcome ids (order defines digit order)
      - parents: sequence of parent variable ids (empty for roots)
      - rows: mapping frozenset[(parent_var, outcome)] -> {outcome_id: prob}
              (for roots: {frozenset(): prior})
    """

    def __init__(self, nodes: list[dict[str, Any]]) -> None:
        by_var = {str(n["variable_id"]): n for n in nodes}
        if len(by_var) != len(nodes):
            raise NetworkModelError("Duplicate variable ids in network nodes")

        # Kahn topological sort (deterministic: sorted tie-break).
        pending = {var: set(n["parents"]) & set(by_var) for var, n in by_var.items()}
        order: list[str] = []
        while pending:
            ready = sorted(var for var, deps in pending.items() if not deps)
            if not ready:
                raise NetworkModelError(
                    "Market network contains a cycle: " + ", ".join(sorted(pending))
                )
            for var in ready:
                order.append(var)
                del pending[var]
            for deps in pending.values():
                deps.difference_update(ready)

        self._order = order
        self._pos = {var: i for i, var in enumerate(order)}
        self._outcomes: dict[str, tuple[str, ...]] = {
            var: tuple(str(o) for o in by_var[var]["outcomes"]) for var in order
        }
        self._counts = [len(self._outcomes[var]) for var in order]

        total_states = 1
        for count in self._counts:
            total_states *= max(count, 1)
            if total_states > MAX_JOINT_STATES:
                raise NetworkModelError(
                    f"Joint state space exceeds bound ({MAX_JOINT_STATES})"
                )

        strides: list[int] = []
        acc = 1
        for count in self._counts:
            strides.append(acc)
            acc *= count
        self._strides = strides

        # Build the joint by extending one variable at a time in topological
        # order; parents always precede children, so their digits are already
        # encoded in the prefix index.
        probs = [1.0]
        for var in order:
            node = by_var[var]
            outcomes = self._outcomes[var]
            parent_vars = [p for p in node["parents"] if p in self._pos]
            parent_pos = [self._pos[p] for p in parent_vars]
            rows: Mapping[Any, Mapping[str, float]] = node["rows"]

            # Pre-resolve rows into per-parent-digit-combo probability lists.
            combo_rows: dict[tuple[int, ...], list[float]] = {}
            for combo_digits in _digit_combos([self._counts[p] for p in parent_pos]):
                key = frozenset(
                    (parent_vars[j], self._outcomes[parent_vars[j]][d])
                    for j, d in enumerate(combo_digits)
                )
                row = rows.get(key)
                if row is None:
                    raise NetworkModelError(
                        f"Missing CPT row for {var} given {sorted(key)}"
                    )
                values = [float(row.get(outcome, 0.0)) for outcome in outcomes]
                total = sum(values)
                if total <= 0.0:
                    raise NetworkModelError(f"CPT row for {var} sums to zero")
                combo_rows[combo_digits] = [v / total for v in values]

            stride = len(probs)
            new = [0.0] * (stride * len(outcomes))
            counts = self._counts
            strides_local = self._strides
            for idx, p in enumerate(probs):
                combo = tuple(
                    (idx // strides_local[pp]) % counts[pp] for pp in parent_pos
                )
                row_values = combo_rows[combo]
                for digit, value in enumerate(row_values):
                    new[idx + digit * stride] = p * value
            probs = new

        self._joint = probs
        self._cache: dict[Any, dict[str, float]] = {}

    def variables(self) -> tuple[str, ...]:
        return tuple(self._order)

    def export_state(
        self,
    ) -> tuple[tuple[str, ...], dict[str, tuple[str, ...]], list[float]]:
        """Expose (variable order, outcomes per variable, joint copy).

        Used to seed mutable consumers (the combinatorial LMSR market maker)
        from the compiled network without re-deriving the joint.
        """
        return tuple(self._order), dict(self._outcomes), list(self._joint)

    def has_variable(self, variable_id: str) -> bool:
        return variable_id in self._pos

    def marginal(
        self,
        variable_id: str,
        evidence: Mapping[str, str] | None = None,
    ) -> dict[str, float] | None:
        """Return P(variable | evidence), or None when unanswerable.

        Evidence on variables outside the network is ignored. Evidence on
        the queried variable itself yields the degenerate point mass.
        Returns None for unknown variables, unknown outcome ids, or
        zero-probability evidence.
        """
        if variable_id not in self._pos:
            return None
        outcomes = self._outcomes[variable_id]

        ev_digits: list[tuple[int, int]] = []
        for var, outcome in sorted((evidence or {}).items()):
            if var == variable_id:
                if outcome not in outcomes:
                    return None
                return {o: 1.0 if o == outcome else 0.0 for o in outcomes}
            if var not in self._pos:
                continue
            var_outcomes = self._outcomes[var]
            if outcome not in var_outcomes:
                return None
            ev_digits.append((self._pos[var], var_outcomes.index(outcome)))

        target_pos = self._pos[variable_id]
        cache_key = (target_pos, tuple(ev_digits))
        cached = self._cache.get(cache_key)
        if cached is not None:
            return dict(cached)

        strides = self._strides
        counts = self._counts
        target_stride = strides[target_pos]
        target_count = counts[target_pos]
        acc = [0.0] * target_count
        for idx, p in enumerate(self._joint):
            if p == 0.0:
                continue
            matched = True
            for pos_i, digit in ev_digits:
                if (idx // strides[pos_i]) % counts[pos_i] != digit:
                    matched = False
                    break
            if matched:
                acc[(idx // target_stride) % target_count] += p

        total = sum(acc)
        if total <= 0.0:
            return None
        result = {outcome: acc[i] / total for i, outcome in enumerate(outcomes)}
        if len(self._cache) >= _QUERY_CACHE_LIMIT:
            self._cache.clear()
        self._cache[cache_key] = dict(result)
        return result


def _digit_combos(counts: list[int]):
    """Yield every digit tuple for the given per-position counts."""
    if not counts:
        yield ()
        return
    combo = [0] * len(counts)
    while True:
        yield tuple(combo)
        for i in range(len(counts) - 1, -1, -1):
            combo[i] += 1
            if combo[i] < counts[i]:
                break
            combo[i] = 0
        else:
            return


def build_network_nodes(
    markets: Mapping[str, Mapping[str, Any]],
    conditional_marginals: Mapping[str, Mapping[str, Mapping[str, float]]],
) -> list[dict[str, Any]]:
    """Build CPT nodes from market records — the flat and factored makers' shared input.

    Every market becomes a node. Markets with a complete, well-formed CPT
    (all parent combinations present, parents resolvable to markets) get
    those parents; anything else — including markets never referenced by a
    CPT — is an independent root whose prior is its stored marginals.
    """
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
            if valid and parent_vars and all(
                p in outcomes_by_var and p != variable_id for p in parent_vars
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


def build_market_network(
    markets: Mapping[str, Mapping[str, Any]],
    conditional_marginals: Mapping[str, Mapping[str, Mapping[str, float]]],
) -> BayesNetworkModel:
    """Build the joint model from market records and per-market CPTs."""
    return BayesNetworkModel(build_network_nodes(markets, conditional_marginals))


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
