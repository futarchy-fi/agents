"""Factored (junction-tree) combinatorial LMSR market maker.

Same market semantics as JointMarket — one Hanson LMSR over the full joint
outcome space, trades as minimum-KL multiplicative reweightings, resolutions
as Bayesian conditioning — but the joint is never materialized. State is a
calibrated junction tree: one belief table per cluster and one per separator,
with joint = prod(cluster beliefs) / prod(separator beliefs).

Scaling: memory is O(n * d^(w+1)) and a trade or evidence query costs
O(component_size * d^(w+1)), where w is the tree width — so thousands of
markets are tractable as long as the belief structure stays sparse. The
width is a hard budget (max_width): trades conditioned on variables that
share no cluster force an exact re-triangulation with the trade scope as a
forced clique (every CPT family and every previously traded scope stays
covered, so no information is lost), and trades that would push the width
past the budget are rejected with JointMarketError.
"""

from __future__ import annotations

import heapq
import math
from typing import Any, Iterable, Mapping

from .joint_market import JointMarketError

_QUERY_CACHE_LIMIT = 4096


class _Cluster:
    __slots__ = (
        "vars",
        "counts",
        "strides",
        "size",
        "table",
        "parent",
        "children",
        "sep_vars",
        "sep_size",
        "sep_table",
        "self_to_sep",
        "parent_to_sep",
        "component",
    )

    def __init__(self, variables: tuple[str, ...], counts: list[int]) -> None:
        self.vars = variables
        self.counts = counts
        strides: list[int] = []
        acc = 1
        for count in counts:
            strides.append(acc)
            acc *= count
        self.strides = strides
        self.size = acc
        self.table = [1.0] * acc
        self.parent: int | None = None
        self.children: list[int] = []
        self.sep_vars: tuple[str, ...] = ()
        self.sep_size = 1
        self.sep_table: list[float] = [1.0]
        self.self_to_sep: list[int] = [0] * acc
        self.parent_to_sep: list[int] = []
        self.component = 0


def _projection_map(
    src_vars: tuple[str, ...],
    src_counts: list[int],
    src_strides: list[int],
    src_size: int,
    dst_vars: tuple[str, ...],
    dst_counts: list[int],
) -> list[int]:
    """Map every source-table index to the index of its dst-subset digits."""
    dst_strides: list[int] = []
    acc = 1
    for count in dst_counts:
        dst_strides.append(acc)
        acc *= count
    positions = [src_vars.index(v) for v in dst_vars]
    out = [0] * src_size
    for idx in range(src_size):
        s = 0
        for k, pos in enumerate(positions):
            s += ((idx // src_strides[pos]) % src_counts[pos]) * dst_strides[k]
        out[idx] = s
    return out


class FactoredMarket:
    """Junction-tree LMSR maker; drop-in interface match for JointMarket."""

    def __init__(
        self,
        order: tuple[str, ...],
        outcomes_by_variable: Mapping[str, tuple[str, ...]],
        families: list[tuple[str, ...]],
        liquidity: float,
        max_width: int,
    ) -> None:
        if liquidity <= 0:
            raise JointMarketError("liquidity must be positive")
        if max_width < 1:
            raise JointMarketError("max_width must be at least 1")
        self._order = tuple(order)
        self._outcomes = {v: tuple(outcomes_by_variable[v]) for v in self._order}
        self._counts = {v: len(self._outcomes[v]) for v in self._order}
        self._families = [tuple(sorted(f)) for f in families]
        self._trade_scopes: list[tuple[str, ...]] = []
        self.liquidity = float(liquidity)
        self.max_width = int(max_width)
        self._clusters: list[_Cluster] = []
        self._elim_cluster: dict[str, int] = {}
        self._cache: dict[Any, dict[str, float]] = {}

    # -- construction ---------------------------------------------------------

    @classmethod
    def from_nodes(
        cls, nodes: list[dict[str, Any]], liquidity: float, max_width: int
    ) -> "FactoredMarket":
        """Build and calibrate from CPT nodes (build_network_nodes output)."""
        by_var = {str(n["variable_id"]): n for n in nodes}
        if len(by_var) != len(nodes):
            raise JointMarketError("duplicate variable ids in network nodes")

        pending = {v: set(n["parents"]) & set(by_var) for v, n in by_var.items()}
        order: list[str] = []
        while pending:
            ready = sorted(v for v, deps in pending.items() if not deps)
            if not ready:
                raise JointMarketError(
                    "market network contains a cycle: " + ", ".join(sorted(pending))
                )
            for v in ready:
                order.append(v)
                del pending[v]
            for deps in pending.values():
                deps.difference_update(ready)

        outcomes = {
            v: tuple(str(o) for o in by_var[v]["outcomes"]) for v in order
        }
        families = []
        for v in order:
            parents = tuple(p for p in by_var[v]["parents"] if p in by_var)
            families.append(tuple(sorted((v, *parents))))

        market = cls(tuple(order), outcomes, families, liquidity, max_width)
        market._build_structure(market._forced_scopes())

        # Multiply every CPT factor into a covering cluster, then calibrate.
        for v, family in zip(order, market._families):
            factor = market._family_factor(by_var[v], v, family)
            ci = market._covering_cluster(family)
            if ci is None:  # families are forced scopes; cannot happen
                raise JointMarketError(f"no cluster covers family of {v}")
            cluster = market._clusters[ci]
            fmap = _projection_map(
                cluster.vars,
                cluster.counts,
                cluster.strides,
                cluster.size,
                family,
                [market._counts[u] for u in family],
            )
            for i in range(cluster.size):
                cluster.table[i] *= factor[fmap[i]]
        market._calibrate_full()
        return market

    def _family_factor(
        self, node: Mapping[str, Any], variable: str, family: tuple[str, ...]
    ) -> list[float]:
        """CPT as a table over the (sorted) family variables."""
        outcomes = self._outcomes[variable]
        rows: Mapping[Any, Mapping[str, float]] = node["rows"]
        counts = [self._counts[u] for u in family]
        strides: list[int] = []
        acc = 1
        for count in counts:
            strides.append(acc)
            acc *= count
        var_pos = family.index(variable)
        parent_vars = [u for u in family if u != variable]
        table = [0.0] * acc
        for idx in range(acc):
            digits = [(idx // strides[k]) % counts[k] for k in range(len(family))]
            key = frozenset(
                (u, self._outcomes[u][digits[family.index(u)]]) for u in parent_vars
            )
            row = rows.get(key)
            if row is None:
                raise JointMarketError(
                    f"missing CPT row for {variable} given {sorted(key)}"
                )
            values = [float(row.get(o, 0.0)) for o in outcomes]
            total = sum(values)
            if total <= 0.0:
                raise JointMarketError(f"CPT row for {variable} sums to zero")
            table[idx] = values[digits[var_pos]] / total
        return table

    def _forced_scopes(self) -> list[tuple[str, ...]]:
        return self._families + self._trade_scopes

    def _build_structure(self, scopes: list[tuple[str, ...]]) -> None:
        """Triangulate (min-fill) and build the rooted cluster tree in place.

        Raises JointMarketError without touching state when any cluster would
        exceed the width budget; callers may therefore use it speculatively.
        """
        max_states = 1 << (self.max_width + 1)
        adj: dict[str, set[str]] = {v: set() for v in self._order}
        for scope in scopes:
            for a in scope:
                for b in scope:
                    if a != b and a in adj and b in adj:
                        adj[a].add(b)

        remaining = set(self._order)
        keys: dict[str, tuple[int, int, str]] = {}

        def score(v: str) -> tuple[int, int, str]:
            ns = sorted(adj[v])
            fill = 0
            for i in range(len(ns)):
                for j in range(i + 1, len(ns)):
                    if ns[j] not in adj[ns[i]]:
                        fill += 1
            states = self._counts[v]
            for u in ns:
                states *= self._counts[u]
            return (fill, states, v)

        heap: list[tuple[tuple[int, int, str], str]] = []
        for v in self._order:
            keys[v] = score(v)
            heapq.heappush(heap, (keys[v], v))

        elim_order: list[str] = []
        raw_clusters: list[tuple[str, tuple[str, ...]]] = []
        while remaining:
            key, v = heapq.heappop(heap)
            if v not in remaining or keys[v] != key:
                continue
            ns = sorted(adj[v])
            cluster_vars = tuple(sorted([v, *ns]))
            states = 1
            for u in cluster_vars:
                states *= self._counts[u]
            if states > max_states:
                raise JointMarketError(
                    f"belief structure would exceed the treewidth budget "
                    f"(cluster over {len(cluster_vars)} variables, "
                    f"{states} states > {max_states}; max_width={self.max_width})"
                )
            raw_clusters.append((v, cluster_vars))
            elim_order.append(v)
            remaining.discard(v)
            dirty: set[str] = set()
            for i in range(len(ns)):
                for j in range(i + 1, len(ns)):
                    a, b = ns[i], ns[j]
                    if b not in adj[a]:
                        adj[a].add(b)
                        adj[b].add(a)
                        dirty.update(adj[a] & adj[b])
            for u in ns:
                adj[u].discard(v)
            del adj[v]
            dirty.update(ns)
            for u in dirty:
                if u in remaining:
                    keys[u] = score(u)
                    heapq.heappush(heap, (keys[u], u))

        elim_pos = {v: i for i, v in enumerate(elim_order)}
        clusters: list[_Cluster] = []
        elim_cluster: dict[str, int] = {}
        for v, cluster_vars in raw_clusters:
            cluster = _Cluster(cluster_vars, [self._counts[u] for u in cluster_vars])
            elim_cluster[v] = len(clusters)
            clusters.append(cluster)

        for idx, (v, cluster_vars) in enumerate(raw_clusters):
            rest = tuple(sorted(u for u in cluster_vars if u != v))
            cluster = clusters[idx]
            if not rest:
                continue
            u = min(rest, key=lambda r: elim_pos[r])
            parent_idx = elim_cluster[u]
            parent = clusters[parent_idx]
            cluster.parent = parent_idx
            parent.children.append(idx)
            cluster.sep_vars = rest
            sep_counts = [self._counts[r] for r in rest]
            sep_size = 1
            for c in sep_counts:
                sep_size *= c
            cluster.sep_size = sep_size
            cluster.sep_table = [1.0] * sep_size
            cluster.self_to_sep = _projection_map(
                cluster.vars, cluster.counts, cluster.strides, cluster.size,
                rest, sep_counts,
            )
            cluster.parent_to_sep = _projection_map(
                parent.vars, parent.counts, parent.strides, parent.size,
                rest, sep_counts,
            )

        # Connected components (for per-component normalization and queries).
        component = 0
        seen: set[int] = set()
        for idx in range(len(clusters)):
            if idx in seen:
                continue
            stack = [idx]
            while stack:
                c = stack.pop()
                if c in seen:
                    continue
                seen.add(c)
                clusters[c].component = component
                cluster = clusters[c]
                if cluster.parent is not None:
                    stack.append(cluster.parent)
                stack.extend(cluster.children)
            component += 1

        self._clusters = clusters
        self._elim_cluster = elim_cluster
        self._elim_order = elim_order
        self._cache.clear()

    def _covering_cluster(self, scope: Iterable[str]) -> int | None:
        """Smallest-work cluster containing every scope variable, or None."""
        scope = tuple(scope)
        if not scope:
            return None
        best: int | None = None
        for v in scope:
            ci = self._elim_cluster.get(v)
            if ci is None:
                return None
            cluster = self._clusters[ci]
            if all(u in cluster.vars for u in scope):
                if best is None or cluster.size < self._clusters[best].size:
                    best = ci
        if best is not None:
            return best
        # Fall back to a full scan (scope may live in an ancestor cluster).
        for ci, cluster in enumerate(self._clusters):
            if all(u in cluster.vars for u in scope):
                if best is None or cluster.size < self._clusters[best].size:
                    best = ci
        return best

    # -- message passing ------------------------------------------------------

    def _send(self, edge_child: int, upward: bool) -> None:
        """HUGIN absorption across the (child, parent) edge, in place."""
        child = self._clusters[edge_child]
        assert child.parent is not None
        parent = self._clusters[child.parent]
        if upward:
            src, dst = child, parent
            src_map, dst_map = child.self_to_sep, child.parent_to_sep
        else:
            src, dst = parent, child
            src_map, dst_map = child.parent_to_sep, child.self_to_sep
        message = [0.0] * child.sep_size
        for i, value in enumerate(src.table):
            message[src_map[i]] += value
        old = child.sep_table
        ratio = [
            (message[k] / old[k]) if old[k] > 0.0 else 0.0
            for k in range(child.sep_size)
        ]
        table = dst.table
        for i in range(dst.size):
            table[i] *= ratio[dst_map[i]]
        child.sep_table = message

    def _calibrate_full(self) -> None:
        order = [self._elim_cluster[v] for v in self._elim_order]
        for ci in order:
            if self._clusters[ci].parent is not None:
                self._send(ci, upward=True)
        for ci in reversed(order):
            if self._clusters[ci].parent is not None:
                self._send(ci, upward=False)
        self._cache.clear()

    def _distribute_from(self, start: int) -> None:
        """Re-calibrate the component after mutating cluster `start`."""
        visited = {start}
        frontier = [start]
        while frontier:
            nxt: list[int] = []
            for ci in frontier:
                cluster = self._clusters[ci]
                neighbors = list(cluster.children)
                if cluster.parent is not None:
                    neighbors.append(cluster.parent)
                for ni in neighbors:
                    if ni in visited:
                        continue
                    visited.add(ni)
                    if ni == cluster.parent:
                        self._send(ci, upward=True)
                    else:
                        self._send(ni, upward=False)
                    nxt.append(ni)
            frontier = nxt
        self._cache.clear()

    def _component_members(self, component: int) -> list[int]:
        return [
            ci for ci, c in enumerate(self._clusters) if c.component == component
        ]

    def _collect_with_evidence(
        self, target: int, ev_digits: list[tuple[str, int]]
    ) -> list[float]:
        """Belief over cluster `target` with evidence absorbed (unnormalized).

        Works on copies; self is untouched. Evidence variables must live in
        target's component. The returned table sums to P(evidence-in-component).
        """
        tables: dict[int, list[float]] = {}

        def tbl(ci: int) -> list[float]:
            table = tables.get(ci)
            if table is None:
                table = list(self._clusters[ci].table)
                tables[ci] = table
            return table

        for variable, digit in ev_digits:
            ci = self._elim_cluster[variable]
            cluster = self._clusters[ci]
            pos = cluster.vars.index(variable)
            stride, count = cluster.strides[pos], cluster.counts[pos]
            table = tbl(ci)
            for i in range(cluster.size):
                if (i // stride) % count != digit:
                    table[i] = 0.0

        # BFS from target; send messages inward in reverse BFS order.
        toward: dict[int, int] = {}
        bfs = [target]
        seen = {target}
        i = 0
        while i < len(bfs):
            ci = bfs[i]
            i += 1
            cluster = self._clusters[ci]
            neighbors = list(cluster.children)
            if cluster.parent is not None:
                neighbors.append(cluster.parent)
            for ni in neighbors:
                if ni not in seen:
                    seen.add(ni)
                    toward[ni] = ci
                    bfs.append(ni)
        for ci in reversed(bfs[1:]):
            dst = toward[ci]
            cluster = self._clusters[ci]
            # The edge between ci and dst is owned by whichever is the child.
            if dst == cluster.parent:
                edge = cluster
                src_map, dst_map = cluster.self_to_sep, cluster.parent_to_sep
            else:
                edge = self._clusters[dst]
                src_map, dst_map = edge.parent_to_sep, edge.self_to_sep
            message = [0.0] * edge.sep_size
            for i2, value in enumerate(tbl(ci)):
                message[src_map[i2]] += value
            old = edge.sep_table
            ratio = [
                (message[k] / old[k]) if old[k] > 0.0 else 0.0
                for k in range(edge.sep_size)
            ]
            dst_table = tbl(dst)
            for i2 in range(len(dst_table)):
                dst_table[i2] *= ratio[dst_map[i2]]
        return tbl(target)

    # -- queries --------------------------------------------------------------

    def has_variable(self, variable_id: str) -> bool:
        return variable_id in self._elim_cluster

    def variables(self) -> tuple[str, ...]:
        return self._order

    def marginal(
        self, variable_id: str, evidence: Mapping[str, str] | None = None
    ) -> dict[str, float] | None:
        """P(variable | evidence) under the current (traded) belief state."""
        if variable_id not in self._elim_cluster:
            return None
        outcomes = self._outcomes[variable_id]
        ev_digits: list[tuple[str, int]] = []
        for variable, outcome in sorted((evidence or {}).items()):
            if variable == variable_id:
                if outcome not in outcomes:
                    return None
                return {o: 1.0 if o == outcome else 0.0 for o in outcomes}
            if variable not in self._elim_cluster:
                continue
            var_outcomes = self._outcomes[variable]
            if outcome not in var_outcomes:
                return None
            ev_digits.append((variable, var_outcomes.index(outcome)))

        cache_key = (variable_id, tuple(ev_digits))
        cached = self._cache.get(cache_key)
        if cached is not None:
            return dict(cached)

        target_ci = self._elim_cluster[variable_id]
        target_component = self._clusters[target_ci].component

        if not ev_digits:
            belief = self._clusters[target_ci].table
        else:
            in_comp = [
                (v, d) for v, d in ev_digits
                if self._clusters[self._elim_cluster[v]].component
                == target_component
            ]
            out_comp = [(v, d) for v, d in ev_digits if (v, d) not in in_comp]
            # Evidence in other components cannot move this variable, but it
            # can be jointly impossible — in which case nothing is answerable.
            remaining = out_comp
            while remaining:
                comp = self._clusters[
                    self._elim_cluster[remaining[0][0]]
                ].component
                group = [
                    (v, d) for v, d in remaining
                    if self._clusters[self._elim_cluster[v]].component == comp
                ]
                remaining = [pair for pair in remaining if pair not in group]
                anchor = self._elim_cluster[group[0][0]]
                mass = sum(self._collect_with_evidence(anchor, group))
                if mass <= 0.0:
                    return None
            belief = self._collect_with_evidence(target_ci, in_comp)

        cluster = self._clusters[target_ci]
        pos = cluster.vars.index(variable_id)
        stride, count = cluster.strides[pos], cluster.counts[pos]
        acc = [0.0] * count
        for i, value in enumerate(belief):
            if value != 0.0:
                acc[(i // stride) % count] += value
        total = sum(acc)
        if total <= 0.0:
            return None
        result = {o: acc[k] / total for k, o in enumerate(outcomes)}
        if len(self._cache) >= _QUERY_CACHE_LIMIT:
            self._cache.clear()
        self._cache[cache_key] = dict(result)
        return result

    # -- trading --------------------------------------------------------------

    def trade_to_probability(
        self,
        variable_id: str,
        outcome_id: str,
        target: float,
        context: Mapping[str, str] | None = None,
    ) -> dict[str, float]:
        """Move P(variable=outcome | context) to target; return the LMSR fill.

        Identical semantics and closed forms to JointMarket: the update is a
        called-off bet (P(context) unchanged) and every other price in the
        network reprices coherently. Contexts that no current cluster covers
        trigger an exact re-triangulation; past the width budget they raise.
        """
        if variable_id not in self._elim_cluster:
            raise JointMarketError(f"unknown variable: {variable_id}")
        outcomes = self._outcomes[variable_id]
        if outcome_id not in outcomes:
            raise JointMarketError(f"unknown outcome: {outcome_id}")
        if not 0.0 < target < 1.0:
            raise JointMarketError("target probability must be strictly between 0 and 1")
        context = dict(context or {})
        if variable_id in context:
            raise JointMarketError("context must not include the traded variable")
        ctx_pairs: list[tuple[str, int]] = []
        for variable, outcome in sorted(context.items()):
            if variable not in self._elim_cluster:
                continue
            var_outcomes = self._outcomes[variable]
            if outcome not in var_outcomes:
                raise JointMarketError("context references an unknown outcome")
            ctx_pairs.append((variable, var_outcomes.index(outcome)))

        scope = tuple(sorted({variable_id, *(v for v, _ in ctx_pairs)}))
        ci = self._covering_cluster(scope)
        if ci is None:
            self._restructure(scope)
            ci = self._covering_cluster(scope)
            if ci is None:  # _restructure forces the scope; cannot happen
                raise JointMarketError("re-triangulation failed to cover the trade scope")
        self._record_trade_scope(scope)

        cluster = self._clusters[ci]
        ctx_digits = [
            (cluster.strides[cluster.vars.index(v)],
             cluster.counts[cluster.vars.index(v)], d)
            for v, d in ctx_pairs
        ]
        x_pos = cluster.vars.index(variable_id)
        x_stride, x_count = cluster.strides[x_pos], cluster.counts[x_pos]
        x_digit = outcomes.index(outcome_id)

        yes_mass = 0.0
        slice_mass = 0.0
        for i, value in enumerate(cluster.table):
            if value == 0.0:
                continue
            in_slice = True
            for stride, count, digit in ctx_digits:
                if (i // stride) % count != digit:
                    in_slice = False
                    break
            if not in_slice:
                continue
            slice_mass += value
            if (i // x_stride) % x_count == x_digit:
                yes_mass += value
        if slice_mass <= 0.0:
            raise JointMarketError("context has zero probability")
        p = yes_mass / slice_mass
        if not 0.0 < p < 1.0:
            raise JointMarketError("price is degenerate; the event is already settled")

        b = self.liquidity
        shares = b * math.log(target * (1 - p) / (p * (1 - target)))
        cost = b * math.log((1 - p) / (1 - target))

        yes_factor = target / p
        no_factor = (1 - target) / (1 - p)
        table = cluster.table
        for i, value in enumerate(table):
            if value == 0.0:
                continue
            in_slice = True
            for stride, count, digit in ctx_digits:
                if (i // stride) % count != digit:
                    in_slice = False
                    break
            if not in_slice:
                continue
            if (i // x_stride) % x_count == x_digit:
                table[i] = value * yes_factor
            else:
                table[i] = value * no_factor

        total = sum(table)
        if not math.isclose(total, 1.0, rel_tol=0, abs_tol=1e-6):
            cluster.table = [v / total for v in table]
        self._distribute_from(ci)

        return {
            "previousProbability": round(p, 6),
            "newProbability": round(target, 6),
            "shares": round(shares, 6),
            "cost": round(cost, 6),
            "liquidity": b,
        }

    def _record_trade_scope(self, scope: tuple[str, ...]) -> None:
        if len(scope) < 2:
            return
        scope_set = set(scope)
        for known in self._families + self._trade_scopes:
            if scope_set <= set(known):
                return
        self._trade_scopes.append(scope)

    def condition(self, variable_id: str, outcome_id: str) -> bool:
        """Bayesian-condition the belief state on a resolved outcome."""
        if variable_id not in self._elim_cluster:
            return False
        outcomes = self._outcomes[variable_id]
        if outcome_id not in outcomes:
            raise JointMarketError(f"unknown outcome: {outcome_id}")
        ci = self._elim_cluster[variable_id]
        cluster = self._clusters[ci]
        pos = cluster.vars.index(variable_id)
        stride, count = cluster.strides[pos], cluster.counts[pos]
        digit = outcomes.index(outcome_id)
        kept = 0.0
        table = cluster.table
        for i, value in enumerate(table):
            if (i // stride) % count == digit:
                kept += value
            else:
                table[i] = 0.0
        if kept <= 0.0:
            raise JointMarketError("resolution outcome has zero probability in the joint")
        cluster.table = [v / kept for v in table]
        self._distribute_from(ci)
        return True

    # -- restructuring --------------------------------------------------------

    def _restructure(self, new_scope: tuple[str, ...]) -> None:
        """Re-triangulate with `new_scope` forced into a cluster, exactly.

        Every CPT family and every past trade scope is re-forced, so the
        current belief state factorizes over the new tree and the projection
        is exact. Raises (state untouched) when the width budget is exceeded.
        """
        shadow = FactoredMarket(
            self._order, self._outcomes, self._families,
            self.liquidity, self.max_width,
        )
        shadow._trade_scopes = list(self._trade_scopes) + [new_scope]
        shadow._build_structure(shadow._forced_scopes())  # may raise; self intact

        for cluster in shadow._clusters:
            source_ci = self._covering_cluster(cluster.vars)
            if source_ci is not None:
                source = self._clusters[source_ci]
                smap = _projection_map(
                    source.vars, source.counts, source.strides, source.size,
                    cluster.vars, cluster.counts,
                )
                table = [0.0] * cluster.size
                for i, value in enumerate(source.table):
                    table[smap[i]] += value
                cluster.table = table
            else:
                cluster.table = self._enumerated_marginal(cluster)
        for cluster in shadow._clusters:
            if cluster.parent is None:
                continue
            message = [0.0] * cluster.sep_size
            for i, value in enumerate(cluster.table):
                message[cluster.self_to_sep[i]] += value
            cluster.sep_table = message

        self._clusters = shadow._clusters
        self._elim_cluster = shadow._elim_cluster
        self._elim_order = shadow._elim_order
        self._trade_scopes = shadow._trade_scopes
        self._cache.clear()

    def _enumerated_marginal(self, cluster: _Cluster) -> list[float]:
        """P(cluster vars) via per-assignment evidence masses on the old tree."""
        table = [0.0] * cluster.size
        for idx in range(cluster.size):
            digits = [
                (v, (idx // cluster.strides[k]) % cluster.counts[k])
                for k, v in enumerate(cluster.vars)
            ]
            mass = 1.0
            remaining = digits
            while remaining:
                comp = self._clusters[
                    self._elim_cluster[remaining[0][0]]
                ].component
                group = [
                    (v, d) for v, d in remaining
                    if self._clusters[self._elim_cluster[v]].component == comp
                ]
                remaining = [pair for pair in remaining if pair not in group]
                anchor = self._elim_cluster[group[0][0]]
                mass *= sum(self._collect_with_evidence(anchor, group))
                if mass == 0.0:
                    break
            table[idx] = mass
        return table

    # -- persistence ----------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Serializable state: structure, beliefs, separators, trade scopes."""
        return {
            "format": "factored-v1",
            "order": list(self._order),
            "outcomes": {v: list(self._outcomes[v]) for v in self._order},
            "liquidity": self.liquidity,
            "maxWidth": self.max_width,
            "families": [list(f) for f in self._families],
            "tradeScopes": [list(s) for s in self._trade_scopes],
            "elimOrder": list(self._elim_order),
            "clusters": [
                {
                    "vars": list(c.vars),
                    "parent": c.parent,
                    "table": list(c.table),
                    "sepTable": list(c.sep_table),
                }
                for c in self._clusters
            ],
        }

    @classmethod
    def from_snapshot(
        cls, data: Mapping[str, Any], max_width: int | None = None
    ) -> "FactoredMarket":
        if data.get("format") != "factored-v1":
            raise JointMarketError("not a factored-v1 snapshot")
        order = tuple(str(v) for v in data["order"])
        outcomes = {
            str(v): tuple(str(o) for o in outs)
            for v, outs in data["outcomes"].items()
        }
        families = [tuple(str(v) for v in f) for f in data["families"]]
        market = cls(
            order, outcomes, families,
            float(data["liquidity"]),
            int(max_width if max_width is not None else data["maxWidth"]),
        )
        market._trade_scopes = [
            tuple(str(v) for v in s) for s in data["tradeScopes"]
        ]
        market._build_structure(market._forced_scopes())
        # The snapshot's structure was produced by the same deterministic
        # triangulation of the same scopes; verify and load beliefs.
        stored = data["clusters"]
        if len(stored) != len(market._clusters) or [
            list(c.vars) for c in market._clusters
        ] != [list(s["vars"]) for s in stored]:
            raise JointMarketError("snapshot structure does not match rebuild")
        for cluster, entry in zip(market._clusters, stored):
            table = [float(x) for x in entry["table"]]
            sep = [float(x) for x in entry["sepTable"]]
            if len(table) != cluster.size or len(sep) != cluster.sep_size:
                raise JointMarketError("snapshot table sizes do not match")
            cluster.table = table
            cluster.sep_table = sep
        return market

    def absorb_flat(
        self,
        order: Iterable[str],
        outcomes: Mapping[str, Iterable[str]],
        probabilities: list[float],
    ) -> None:
        """Load beliefs from a flat joint (legacy JointMarket snapshot).

        Sets every cluster belief to the flat joint's marginal over its
        variables — the information projection onto the tree. Exact whenever
        the flat joint factorizes over the tree (true unless past trades
        entangled variables across clusters).
        """
        flat_order = tuple(str(v) for v in order)
        if set(flat_order) != set(self._order):
            raise JointMarketError("flat snapshot variable space does not match")
        flat_counts = [len(tuple(outcomes[v])) for v in flat_order]
        strides: list[int] = []
        acc = 1
        for count in flat_counts:
            strides.append(acc)
            acc *= count
        if len(probabilities) != acc:
            raise JointMarketError("flat snapshot size does not match")
        total = sum(probabilities)
        if total <= 0:
            raise JointMarketError("flat snapshot has zero mass")
        probs = [p / total for p in probabilities]
        pos = {v: k for k, v in enumerate(flat_order)}
        for v in self._order:
            if tuple(outcomes[v]) != self._outcomes[v]:
                raise JointMarketError("flat snapshot outcomes do not match")

        for cluster in self._clusters:
            positions = [pos[v] for v in cluster.vars]
            table = [0.0] * cluster.size
            for idx, value in enumerate(probs):
                if value == 0.0:
                    continue
                s = 0
                for k, fp in enumerate(positions):
                    s += ((idx // strides[fp]) % flat_counts[fp]) * cluster.strides[k]
                table[s] += value
            cluster.table = table
        for cluster in self._clusters:
            if cluster.parent is None:
                continue
            message = [0.0] * cluster.sep_size
            for i, value in enumerate(cluster.table):
                message[cluster.self_to_sep[i]] += value
            cluster.sep_table = message
        self._cache.clear()

    # -- diagnostics ----------------------------------------------------------

    def stats(self) -> dict[str, float]:
        def entropy(table: list[float]) -> float:
            return -sum(p * math.log(p) for p in table if p > 0.0)

        total_entropy = 0.0
        width = 0
        max_states = 0
        for cluster in self._clusters:
            total_entropy += entropy(cluster.table)
            width = max(width, len(cluster.vars) - 1)
            max_states = max(max_states, cluster.size)
            if cluster.parent is not None:
                total_entropy -= entropy(cluster.sep_table)
        log2_states = 0.0
        for v in self._order:
            log2_states += math.log2(self._counts[v])
        try:
            states = float(2.0 ** log2_states)
        except OverflowError:
            states = float("inf")
        return {
            "liquidity": self.liquidity,
            "states": states,
            "entropyNats": round(total_entropy, 6),
            "statesLog2": round(log2_states, 6),
            "cliqueCount": float(len(self._clusters)),
            "treewidth": float(width),
            "maxCliqueStates": float(max_states),
            "maxWidthBudget": float(self.max_width),
        }
