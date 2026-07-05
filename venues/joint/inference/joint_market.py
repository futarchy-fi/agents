"""Combinatorial LMSR market maker over the network's joint state space.

Hanson (2003): a single logarithmic market scoring rule over the full joint
outcome space. The maker's state IS a probability distribution over joint
states; trading an event E to a target probability applies the minimum-KL
multiplicative reweighting that fixes P(E), and the trader's cost/shares
follow the standard LMSR closed forms:

    shares = b * ln( t(1-p) / (p(1-t)) )      (shares of E bought; <0 = sold)
    cost   = b * ln( (1-p) / (1-t) )          (negative = trader is paid)

with p the pre-trade price, t the target, b the liquidity parameter.
Conditional trades (E given context C) are called-off bets: only states in
C are reweighted (P(C) is unchanged), and p, t are conditional prices.
Resolving a variable is Bayesian conditioning: incompatible states go to
zero and the rest renormalize — every other market reprices coherently.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from .network_model import BayesNetworkModel

_QUERY_CACHE_LIMIT = 4096


class JointMarketError(ValueError):
    """Raised for untradeable requests (unknown ids, degenerate prices)."""


class JointMarket:
    """Mutable joint distribution + LMSR accounting over network variables."""

    def __init__(
        self,
        order: tuple[str, ...],
        outcomes_by_variable: Mapping[str, tuple[str, ...]],
        probabilities: list[float],
        liquidity: float,
    ) -> None:
        if liquidity <= 0:
            raise JointMarketError("liquidity must be positive")
        self._order = tuple(order)
        self._outcomes = {v: tuple(outcomes_by_variable[v]) for v in self._order}
        self._pos = {v: i for i, v in enumerate(self._order)}
        self._counts = [len(self._outcomes[v]) for v in self._order]

        strides: list[int] = []
        acc = 1
        for count in self._counts:
            strides.append(acc)
            acc *= count
        self._strides = strides
        if len(probabilities) != acc:
            raise JointMarketError("joint size does not match variable space")

        total = sum(probabilities)
        if total <= 0:
            raise JointMarketError("joint distribution has zero mass")
        self._probs = [p / total for p in probabilities]
        self.liquidity = float(liquidity)
        self._cache: dict[Any, dict[str, float]] = {}

    @classmethod
    def from_network(cls, model: BayesNetworkModel, liquidity: float) -> "JointMarket":
        order, outcomes, probs = model.export_state()
        return cls(order, outcomes, probs, liquidity)

    # -- queries ------------------------------------------------------------

    def has_variable(self, variable_id: str) -> bool:
        return variable_id in self._pos

    def variables(self) -> tuple[str, ...]:
        return self._order

    def _digit(self, index: int, position: int) -> int:
        return (index // self._strides[position]) % self._counts[position]

    def _evidence_digits(
        self, evidence: Mapping[str, str] | None, *, exclude: str | None = None
    ) -> list[tuple[int, int]] | None:
        digits: list[tuple[int, int]] = []
        for variable, outcome in sorted((evidence or {}).items()):
            if variable == exclude or variable not in self._pos:
                continue
            var_outcomes = self._outcomes[variable]
            if outcome not in var_outcomes:
                return None
            digits.append((self._pos[variable], var_outcomes.index(outcome)))
        return digits

    def marginal(
        self, variable_id: str, evidence: Mapping[str, str] | None = None
    ) -> dict[str, float] | None:
        """P(variable | evidence) under the current (traded) joint."""
        if variable_id not in self._pos:
            return None
        outcomes = self._outcomes[variable_id]
        for variable, outcome in (evidence or {}).items():
            if variable == variable_id:
                if outcome not in outcomes:
                    return None
                return {o: 1.0 if o == outcome else 0.0 for o in outcomes}
        digits = self._evidence_digits(evidence, exclude=variable_id)
        if digits is None:
            return None

        target_pos = self._pos[variable_id]
        cache_key = (target_pos, tuple(digits))
        cached = self._cache.get(cache_key)
        if cached is not None:
            return dict(cached)

        acc = [0.0] * len(outcomes)
        target_stride = self._strides[target_pos]
        target_count = self._counts[target_pos]
        for index, p in enumerate(self._probs):
            if p == 0.0:
                continue
            matched = True
            for position, digit in digits:
                if self._digit(index, position) != digit:
                    matched = False
                    break
            if matched:
                acc[(index // target_stride) % target_count] += p
        total = sum(acc)
        if total <= 0.0:
            return None
        result = {o: acc[i] / total for i, o in enumerate(outcomes)}
        if len(self._cache) >= _QUERY_CACHE_LIMIT:
            self._cache.clear()
        self._cache[cache_key] = dict(result)
        return result

    # -- trading ------------------------------------------------------------

    def trade_to_probability(
        self,
        variable_id: str,
        outcome_id: str,
        target: float,
        context: Mapping[str, str] | None = None,
    ) -> dict[str, float]:
        """Move P(variable=outcome | context) to target; return LMSR fill.

        Applies the minimum-KL reweighting within the context slice, so
        P(context) is untouched (called-off bet) and every other price in
        the network reprices coherently through the joint.
        """
        if variable_id not in self._pos:
            raise JointMarketError(f"unknown variable: {variable_id}")
        outcomes = self._outcomes[variable_id]
        if outcome_id not in outcomes:
            raise JointMarketError(f"unknown outcome: {outcome_id}")
        if not 0.0 < target < 1.0:
            raise JointMarketError("target probability must be strictly between 0 and 1")
        context = dict(context or {})
        if variable_id in context:
            raise JointMarketError("context must not include the traded variable")
        digits = self._evidence_digits(context, exclude=None)
        if digits is None:
            raise JointMarketError("context references an unknown outcome")

        current = self.marginal(variable_id, context)
        if current is None:
            raise JointMarketError("context has zero probability")
        p = current[outcome_id]
        if not 0.0 < p < 1.0:
            raise JointMarketError("price is degenerate; the event is already settled")

        b = self.liquidity
        shares = b * math.log(target * (1 - p) / (p * (1 - target)))
        cost = b * math.log((1 - p) / (1 - target))

        yes_factor = target / p
        no_factor = (1 - target) / (1 - p)
        target_pos = self._pos[variable_id]
        target_digit = outcomes.index(outcome_id)
        for index, value in enumerate(self._probs):
            if value == 0.0:
                continue
            in_slice = True
            for position, digit in digits:
                if self._digit(index, position) != digit:
                    in_slice = False
                    break
            if not in_slice:
                continue
            if self._digit(index, target_pos) == target_digit:
                self._probs[index] = value * yes_factor
            else:
                self._probs[index] = value * no_factor

        total = sum(self._probs)
        if not math.isclose(total, 1.0, rel_tol=0, abs_tol=1e-6):
            self._probs = [v / total for v in self._probs]
        self._cache.clear()

        return {
            "previousProbability": round(p, 6),
            "newProbability": round(target, 6),
            "shares": round(shares, 6),
            "cost": round(cost, 6),
            "liquidity": b,
        }

    def condition(self, variable_id: str, outcome_id: str) -> bool:
        """Bayesian-condition the joint on a resolved outcome."""
        if variable_id not in self._pos:
            return False
        outcomes = self._outcomes[variable_id]
        if outcome_id not in outcomes:
            raise JointMarketError(f"unknown outcome: {outcome_id}")
        position = self._pos[variable_id]
        digit = outcomes.index(outcome_id)
        kept = 0.0
        for index, value in enumerate(self._probs):
            if self._digit(index, position) == digit:
                kept += value
            else:
                self._probs[index] = 0.0
        if kept <= 0.0:
            raise JointMarketError("resolution outcome has zero probability in the joint")
        self._probs = [v / kept for v in self._probs]
        self._cache.clear()
        return True

    # -- persistence ----------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Serializable state: variable space, joint, liquidity."""
        return {
            "order": list(self._order),
            "outcomes": {v: list(self._outcomes[v]) for v in self._order},
            "probabilities": list(self._probs),
            "liquidity": self.liquidity,
        }

    @classmethod
    def from_snapshot(cls, data: Mapping[str, Any]) -> "JointMarket":
        return cls(
            tuple(str(v) for v in data["order"]),
            {str(v): tuple(str(o) for o in outs) for v, outs in data["outcomes"].items()},
            [float(p) for p in data["probabilities"]],
            float(data["liquidity"]),
        )

    # -- diagnostics ----------------------------------------------------------

    def stats(self) -> dict[str, float]:
        entropy = -sum(p * math.log(p) for p in self._probs if p > 0.0)
        return {
            "liquidity": self.liquidity,
            "states": float(len(self._probs)),
            "entropyNats": round(entropy, 6),
        }
