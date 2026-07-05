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
from venues.joint.inference import FactoredMarket, JointMarketError, build_network_nodes
from venues.joint.msr import payout_for_edit, stake_for_edit

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


class InsufficientCredits(VenueError):
    """Raised when an account lacks the available balance to cover a stake."""


class WidthBudgetExceeded(VenueError):
    """Raised when a probability edit is rejected by the junction-tree width budget."""


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
        self._liquidity: Decimal = liquidity
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

        self._orders: list[dict[str, Any]] = []
        self._orders_by_var: dict[str, list[dict[str, Any]]] = {}
        self._order_seq: int = 0

        self._resolutions: dict[str, str] = {}
        self._voided: set[str] = set()

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

    # -- staked probability edits -----------------------------------------

    def _before(
        self, variable_id: str, outcome_id: str, context: dict[str, str] | None
    ) -> float:
        """P(variable_id = outcome_id | context), raising VenueError on a bad outcome."""
        marginals = self.marginal(variable_id, context)  # raises UnknownVariable
        try:
            return marginals[outcome_id]
        except KeyError:
            raise VenueError(f"unknown outcome: {outcome_id}") from None

    def preview_edit(
        self,
        account_id: int,
        variable_id: str,
        outcome_id: str,
        target: float,
        context: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Quote the stake for an edit without touching any state."""
        before = self._before(variable_id, outcome_id, context)
        stake = stake_for_edit(self._liquidity, before, target)
        return {
            "stake": stake,
            "before": before,
            "after": target,
            "b": self._liquidity,
        }

    def place_edit(
        self,
        account_id: int,
        variable_id: str,
        outcome_id: str,
        target: float,
        context: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Freeze the worst-case stake and move P(variable_id = outcome_id) to target.

        Order of operations (money-safety property):
        1. Resolve ``before`` from the live marginal.
        2. Compute the stake and reject with ``InsufficientCredits`` *before*
           touching any balances if the account can't cover it.
        3. Lock the stake (always: the risk engine accepts a zero-amount
           lock, so a free edit still gets an order + lockId rather than a
           None sentinel — see the task report for why).
        4. Re-triangulate/trade against the factored market; on
           ``JointMarketError`` (width-budget or similar), release the lock
           just created and re-raise as ``WidthBudgetExceeded`` so the
           account and the joint are left exactly as before the call.
        5. Record and return the order.
        """
        before = self._before(variable_id, outcome_id, context)
        stake = stake_for_edit(self._liquidity, before, target)

        if stake > 0 and not self._risk_engine.check_available(account_id, stake):
            raise InsufficientCredits(
                f"account {account_id}: need {stake} available to stake this edit"
            )

        lock, _tx = self._risk_engine.lock(
            account_id, self._vb_lock_market_id(variable_id), stake, "msr_stake"
        )

        try:
            fill = self._fm.trade_to_probability(variable_id, outcome_id, target, context)
        except JointMarketError as err:
            self._risk_engine.release_lock(lock.lock_id)
            raise WidthBudgetExceeded(str(err)) from err

        self._order_seq += 1
        order_context = dict(context or {})
        order = {
            "orderId": f"vb_{self._order_seq}",
            "accountId": account_id,
            "variableId": variable_id,
            "outcomeId": outcome_id,
            "target": target,
            "context": order_context,
            "before": before,
            "after": target,
            "stake": str(stake),
            "lockId": lock.lock_id,
            "status": "open",
            "fill": fill,
            "remainingContext": dict(order_context),
        }
        self._orders.append(order)
        self._orders_by_var.setdefault(variable_id, []).append(order)
        return order

    # -- settlement -------------------------------------------------------

    def _settlement_market(self, variable_id: str) -> tuple[str, dict[str, Any]]:
        """(market_id, record) for a variable that may still resolve/void."""
        market_id = self._var_to_market.get(variable_id)
        if market_id is None:
            raise UnknownVariable(variable_id)
        if variable_id in self._resolutions:
            raise VenueError(f"variable already resolved: {variable_id}")
        if variable_id in self._voided:
            raise VenueError(f"variable already voided: {variable_id}")
        return market_id, self._markets[market_id]

    def _call_off(self, order: dict[str, Any]) -> None:
        """Return the full frozen stake and retire the order."""
        self._risk_engine.release_lock(order["lockId"])
        order["status"] = "called_off"

    def resolve_variable(self, variable_id: str, outcome_id: str) -> dict[str, Any]:
        """Resolve ``variable_id`` to ``outcome_id`` and settle affected orders.

        Conditions the joint on the outcome, then walks every open /
        awaiting_context order once, in placement order:

        - contradicted context -> called off (full stake back);
        - satisfied context key -> consumed, order stays in play;
        - order on the resolved variable -> win/loss recorded;
        - recorded win/loss + empty remaining context -> settled at the
          log-score payout against the treasury;
        - recorded win/loss + pending context -> awaiting_context (settles
          when a later resolution empties the context).
        """
        market_id, record = self._settlement_market(variable_id)
        if outcome_id not in {o["id"] for o in record["outcomes"]}:
            raise VenueError(f"unknown outcome: {outcome_id}")

        self._fm.condition(variable_id, outcome_id)
        self._resolutions[variable_id] = outcome_id
        # Copy-on-write: seed records may be shared with the caller's dict.
        self._markets[market_id] = {
            **record, "status": "resolved", "resolvedOutcome": outcome_id,
        }

        settled: list[str] = []
        called_off: list[str] = []
        awaiting: list[str] = []
        treasury_delta = Decimal("0")

        for order in self._orders:
            if order["status"] not in ("open", "awaiting_context"):
                continue

            remaining = order["remainingContext"]
            if variable_id in remaining:
                if remaining[variable_id] != outcome_id:
                    self._call_off(order)
                    called_off.append(order["orderId"])
                    continue
                del remaining[variable_id]

            if order["variableId"] == variable_id:
                order["resolvedWon"] = outcome_id == order["outcomeId"]

            if "resolvedWon" in order and not remaining:
                payout = payout_for_edit(
                    self._liquidity, order["before"], order["target"],
                    order["resolvedWon"],
                )
                self._risk_engine.release_lock(order["lockId"])
                if payout > 0:
                    self._risk_engine.transfer_available(
                        self.treasury_account_id, order["accountId"], payout,
                        market_id=self._vb_lock_market_id(order["variableId"]),
                        reason="msr_settlement",
                    )
                    treasury_delta -= payout
                elif payout < 0:
                    # Covered by construction: stake >= -payout was frozen
                    # for this order and released just above.
                    self._risk_engine.transfer_available(
                        order["accountId"], self.treasury_account_id, -payout,
                        market_id=self._vb_lock_market_id(order["variableId"]),
                        reason="msr_settlement",
                    )
                    treasury_delta += -payout
                order["status"] = "settled"
                order["payout"] = str(payout)
                settled.append(order["orderId"])
            elif "resolvedWon" in order:
                order["status"] = "awaiting_context"
                awaiting.append(order["orderId"])

        return {
            "settled": settled,
            "calledOff": called_off,
            "awaiting": awaiting,
            "treasuryDelta": str(treasury_delta),
        }

    def void_variable(self, variable_id: str) -> dict[str, Any]:
        """Void ``variable_id``: call off every bet it could have decided.

        Does NOT condition the joint. Calls off every open/awaiting order
        placed on the variable or conditioned on it (a context that can
        never be decided), returning each full stake.
        """
        market_id, record = self._settlement_market(variable_id)

        self._voided.add(variable_id)
        self._markets[market_id] = {**record, "status": "void"}

        called_off: list[str] = []
        for order in self._orders:
            if order["status"] not in ("open", "awaiting_context"):
                continue
            if (
                order["variableId"] == variable_id
                or variable_id in order["remainingContext"]
            ):
                self._call_off(order)
                called_off.append(order["orderId"])

        return {"calledOff": called_off}

    # -- internal bookkeeping --------------------------------------------

    def _vb_lock_market_id(self, variable_id: str) -> int:
        """Stable int id for RiskEngine lock bookkeeping.

        1_000_000 + the index of the market (owning ``variable_id``) in
        seed order.
        """
        market_id = self._var_to_market[variable_id]
        return 1_000_000 + self.market_ids().index(market_id)
