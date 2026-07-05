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

import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from core.risk_engine import RiskEngine
from venues.joint.inference import FactoredMarket, build_network_nodes

TREASURY_SEED = Decimal("1000000")


def nodes_from_seeds(seeds: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Build ``FactoredMarket.from_nodes`` input from a seeds-v1 document."""
    markets: Mapping[str, Mapping[str, Any]] = seeds["markets"]
    conditional_marginals: Mapping[str, Mapping[str, Mapping[str, float]]] = (
        seeds.get("conditionalMarginals", {})
    )
    return build_network_nodes(markets, conditional_marginals)


class VenueError(Exception):
    """Base class for JointVenue errors."""


class UnknownMarket(VenueError):
    """Raised when a market id has no corresponding seed record."""


class UnknownVariable(VenueError):
    """Raised when a variable id is not part of the joint model."""


class JointVenue:
    """Venue B: a factored joint (Bayes-network) prediction market.

    Loads a seeds-v1 document, builds the calibrated ``FactoredMarket``
    inference engine from it, and exposes a read surface over the live
    (traded) marginals plus the seed metadata for each market.
    """

    def __init__(
        self,
        risk_engine: RiskEngine,
        seeds_path: str | Path | dict,
        liquidity: Decimal = Decimal("50"),
        max_width: int = 8,
    ) -> None:
        self._risk_engine = risk_engine
        seeds = self._load_seeds(seeds_path)

        self._markets: dict[str, dict[str, Any]] = dict(seeds["markets"])
        self._var_to_market: dict[str, str] = {
            str(record["variableId"]): market_id
            for market_id, record in self._markets.items()
        }

        nodes = nodes_from_seeds(seeds)
        self._fm = FactoredMarket.from_nodes(
            nodes, liquidity=float(liquidity), max_width=max_width
        )

        account = risk_engine.create_account()
        risk_engine.mint(account.id, TREASURY_SEED)
        self.treasury_account_id: int = account.id

    @staticmethod
    def _load_seeds(seeds_path: str | Path | dict) -> dict:
        if isinstance(seeds_path, dict):
            return seeds_path
        return json.loads(Path(seeds_path).read_text())

    # -- read surface ---------------------------------------------------

    def market_ids(self) -> list[str]:
        """Market ids in seed (insertion) order."""
        return list(self._markets.keys())

    def get_market(self, market_id: str) -> dict[str, Any]:
        """Seed metadata for ``market_id`` merged with live marginals."""
        record = self._markets.get(market_id)
        if record is None:
            raise UnknownMarket(market_id)
        variable_id = str(record["variableId"])
        marginals = self._fm.marginal(variable_id)
        return {**record, "marginals": marginals}

    def marginal(
        self, variable_id: str, context: dict[str, str] | None = None
    ) -> dict[str, float]:
        """P(variable | context) under the current (traded) belief state."""
        result = self._fm.marginal(variable_id, context)
        if result is None:
            raise UnknownVariable(variable_id)
        return result

    # -- internal bookkeeping --------------------------------------------

    def _vb_lock_market_id(self, variable_id: str) -> int:
        """Stable int id for RiskEngine lock bookkeeping.

        1_000_000 + the index of the market (owning ``variable_id``) in
        seed order.
        """
        market_id = self._var_to_market[variable_id]
        return 1_000_000 + self.market_ids().index(market_id)
