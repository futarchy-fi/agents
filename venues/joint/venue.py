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
import logging
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from core.risk_engine import RiskEngine
from venues.joint.inference import FactoredMarket, JointMarketError, build_network_nodes
from venues.joint.msr import payout_for_edit, stake_for_edit

TREASURY_SEED = Decimal("1000000")

logger = logging.getLogger(__name__)


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


class InsufficientTreasury(VenueError):
    """Raised when the treasury can't cover a resolution's winning payouts.

    Guards against a partially-applied settlement: the check runs before any
    state is mutated, so an under-funded treasury fails cleanly instead of
    aborting mid-walk with orders half-settled and the joint already
    conditioned. Unreachable in normal operation (the treasury is seeded far
    above any realistic aggregate payout) — a defensive backstop against a
    drained/misconfigured treasury or an accounting bug."""


class WidthBudgetExceeded(VenueError):
    """Raised when a probability edit is rejected by the junction-tree width budget."""


class MarketClosed(VenueError):
    """Raised when an edit targets, or is contexted on, a resolved or voided variable.

    Rejecting these up front (before any lock is taken) is what keeps a bet
    from freezing funds forever: a market that can never resolve can never
    settle, so an edit against it must never be allowed to reach the risk
    engine.
    """


class ContextContradicted(VenueError):
    """Raised when a context is inconsistent with the current belief state.

    Two cases share this exception: (1) a context key at edit time whose
    requested outcome contradicts that variable's already-recorded
    resolution, and (2) a query/trade whose context makes the relevant
    probability mass zero under the joint (e.g. it conflicts with a
    resolution already conditioned into the factored market). Both are a
    logically-impossible context, not an "unknown variable".
    """


class InvalidOutcome(VenueError):
    """Raised when an outcome value is not among a variable's real outcomes.

    Covers two call sites: (1) a context key at edit time whose value isn't
    one of the outcomes of the variable that key names, and (2) the
    resolved-outcome argument to ``resolve_variable`` when it isn't one of
    the target variable's outcomes. Both are "bad outcome id", distinct
    from ``UnknownVariable`` (the variable/key itself doesn't exist) and
    from ``ContextContradicted`` (the variable and outcome are both real,
    but contradict an already-recorded resolution).
    """


class InvalidTarget(VenueError):
    """Raised when a target (or the current price) is degenerate.

    Covers msr's own validation (`before`/`target` not strictly inside
    (0, 1)) as well as a `trade_to_probability` price that's already
    pinned to 0 or 1 under the given context (the event is already
    effectively settled there).
    """


class TradeRejected(VenueError):
    """Catch-all for a rejected trade_to_probability call that is neither a
    width-budget failure nor a degenerate-price failure."""


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
        *,
        _bootstrap_treasury: bool = True,
        _treasury_account_id: int | None = None,
    ) -> None:
        """Build a venue from a seeds-v1 document.

        ``_bootstrap_treasury`` / ``_treasury_account_id`` are a private
        escape hatch for ``from_snapshot``: when restoring from a persisted
        exchange snapshot, the treasury account already exists (it was
        restored along with every other RE account) and must NOT be
        re-minted. Regular callers never pass these.
        """
        self._risk_engine = risk_engine
        self._liquidity: Decimal = liquidity
        self._max_width: int = max_width
        self._seeds_source: str = (
            "<inline>" if isinstance(seeds_path, dict) else str(seeds_path)
        )
        seeds = self._load_seeds(seeds_path)

        self._markets: dict[str, dict[str, Any]] = dict(seeds["markets"])
        self._var_to_market: dict[str, str] = {
            str(record["variableId"]): market_id
            for market_id, record in self._markets.items()
        }

        # Markets never change post-construction in Plan A, so both of
        # these are computed once here and never rebuilt: market_ids()
        # returns this same list object (O(1)), and _vb_lock_market_id
        # becomes an O(1) dict lookup instead of a market_ids() rebuild +
        # linear .index() search on every call.
        self._market_ids_list: list[str] = list(self._markets.keys())
        _market_index = {
            market_id: i for i, market_id in enumerate(self._market_ids_list)
        }
        self._lock_ids: dict[str, int] = {
            variable_id: 1_000_000 + _market_index[market_id]
            for variable_id, market_id in self._var_to_market.items()
        }

        nodes = nodes_from_seeds(seeds)
        self._fm = FactoredMarket.from_nodes(
            nodes, liquidity=float(liquidity), max_width=max_width
        )

        # Parents-for-API, derived once here from the same CPT-parsed
        # ``nodes`` the inference engine itself was built from (see
        # ``build_network_nodes`` / ``parse_cpt_key`` in
        # ``venues.joint.inference.network_model``) rather than re-parsing
        # ``conditionalMarginals`` a second time, or trusting a hand-authored
        # "parents" field a seed record might (or might not) carry — a
        # market with a malformed/incomplete CPT falls back to an
        # independent root there (``parents: ()``), and this table stays
        # consistent with that fallback by construction. Read by
        # ``get_market`` below.
        self._parents_by_variable: dict[str, list[str]] = {
            str(node["variable_id"]): list(node["parents"]) for node in nodes
        }

        if _bootstrap_treasury:
            account = risk_engine.create_account()
            risk_engine.mint(account.id, TREASURY_SEED)
            self.treasury_account_id: int = account.id
        else:
            if _treasury_account_id is None:
                raise VenueError(
                    "_treasury_account_id is required when "
                    "_bootstrap_treasury=False"
                )
            self.treasury_account_id = _treasury_account_id

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
        """Market ids in seed (insertion) order.

        Returns the same cached list object every call (O(1)) — markets
        never change post-construction in Plan A, so there's nothing to
        rebuild. Callers must not mutate the result.
        """
        return self._market_ids_list

    def get_market(self, market_id: str) -> dict[str, Any]:
        """Seed metadata for ``market_id`` merged with live marginals and parents.

        ``parents`` here always overrides any "parents" key the raw seed
        record itself might carry — see ``self._parents_by_variable`` above
        for why the CPT-derived table is the one source of truth.
        """
        record = self._markets.get(market_id)
        if record is None:
            raise UnknownMarket(market_id)
        variable_id = str(record["variableId"])
        marginals = self._fm.marginal(variable_id)
        parents = self._parents_by_variable.get(variable_id, [])
        return {**record, "marginals": marginals, "parents": parents}

    def marginal(
        self, variable_id: str, context: dict[str, str] | None = None
    ) -> dict[str, float]:
        """P(variable | context) under the current (traded) belief state.

        Raises ``UnknownVariable`` when ``variable_id`` itself isn't part
        of the joint model, or ``ContextContradicted`` when the variable
        IS known but the supplied context has zero probability under the
        current belief state (e.g. it conflicts with a resolution already
        conditioned into the joint). These used to be conflated into a
        single ``UnknownVariable`` — wrong, since an unknown variable and
        an unsatisfiable context are different failure modes with
        different remedies for the caller.
        """
        if not self._fm.has_variable(variable_id):
            raise UnknownVariable(variable_id)
        result = self._fm.marginal(variable_id, context)
        if result is None:
            raise ContextContradicted(
                f"context yields zero probability for {variable_id}"
            )
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

    def _check_lifecycle(
        self, variable_id: str, context: dict[str, str]
    ) -> dict[str, str]:
        """Reject edits against a dead market; return the still-open context.

        - ``variable_id`` voided or resolved -> ``MarketClosed``: an edit on
          a market that can never resolve can never settle, so it must
          never reach the risk engine (a bet against it would freeze funds
          forever otherwise).
        - a context KEY that names no known variable -> ``UnknownVariable``:
          this is the fund-freeze hole this guard exists to close. A typo'd
          or made-up context key used to be silently ignored by
          ``fm.marginal``/``fm.trade_to_probability`` (they just skip
          evidence variables they don't recognize), so the edit would place
          successfully with a ``remainingContext`` entry that names no real
          variable — no future ``resolve_variable`` call could EVER match
          that key, so the order (and its frozen stake) would sit in
          ``awaiting_context`` forever. Rejecting it here, before any lock
          or fm call, is what prevents that.
        - a context OUTCOME not among the named variable's real outcomes ->
          ``InvalidOutcome``: same rejection-before-any-money-path
          reasoning, for a value that will never match any resolution
          either.
        - a context key that's voided -> ``MarketClosed`` (same reasoning:
          that leg of the context can never be decided).
        - a context key resolved to an outcome that contradicts the
          requested value -> ``ContextContradicted``.
        - a context key resolved to the SAME value as requested: dropped
          from the *returned* dict (the order's future ``remainingContext``
          bookkeeping never needs to look at it again), but the caller's
          ``context`` dict is left untouched — see ``place_edit`` /
          ``preview_edit`` for why passing it through unstripped to
          ``_before`` / ``fm.trade_to_probability`` is the right call.

        Every rejection here happens before any risk-engine or fm call, so
        no lock, order, or marginal is ever touched on a rejection path.
        """
        if variable_id in self._voided:
            raise MarketClosed(f"variable is voided: {variable_id}")
        if variable_id in self._resolutions:
            raise MarketClosed(f"variable is resolved: {variable_id}")

        remaining: dict[str, str] = {}
        for key, value in context.items():
            market_id = self._var_to_market.get(key)
            if market_id is None:
                raise UnknownVariable(f"unknown context variable: {key}")
            outcomes = {o["id"] for o in self._markets[market_id]["outcomes"]}
            if value not in outcomes:
                raise InvalidOutcome(
                    f"context {key}={value!r} is not a valid outcome for {key}"
                )
            if key in self._voided:
                raise MarketClosed(f"context variable is voided: {key}")
            if key in self._resolutions:
                if self._resolutions[key] != value:
                    raise ContextContradicted(
                        f"context {key}={value!r} contradicts resolved "
                        f"outcome {self._resolutions[key]!r}"
                    )
                continue
            remaining[key] = value
        return remaining

    def _stake_for_edit_or_raise(self, before: float, target: float) -> Decimal:
        """``stake_for_edit``, wrapping msr's degenerate-probability ``ValueError``."""
        try:
            return stake_for_edit(self._liquidity, before, target)
        except ValueError as err:
            raise InvalidTarget(str(err)) from err

    @staticmethod
    def _trade_error(err: JointMarketError) -> VenueError:
        """Classify a ``JointMarketError`` raised at the trade_to_probability boundary.

        Only a genuine width-budget failure (the message names the
        treewidth budget — see ``factored_market.py``'s ``_build_structure``)
        becomes ``WidthBudgetExceeded``; a degenerate/already-settled price
        becomes ``InvalidTarget``; anything else is a general
        ``TradeRejected``.
        """
        message = str(err)
        if "width" in message:
            return WidthBudgetExceeded(message)
        if "degenerate" in message:
            return InvalidTarget(message)
        return TradeRejected(message)

    def preview_edit(
        self,
        account_id: int,
        variable_id: str,
        outcome_id: str,
        target: float,
        context: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Quote the stake for an edit without touching any state."""
        context = dict(context or {})
        self._check_lifecycle(variable_id, context)
        before = self._before(variable_id, outcome_id, context)
        stake = self._stake_for_edit_or_raise(before, target)
        return {
            "stake": str(stake),
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
        1. Lifecycle guard: reject outright (``MarketClosed`` /
           ``ContextContradicted``) if the variable or any context key is
           already voided or resolved-contradicting, before touching
           anything.
        2. Resolve ``before`` from the live marginal.
        3. Compute the stake (wrapping a degenerate before/target as
           ``InvalidTarget``) and reject with ``InsufficientCredits``
           *before* touching any balances if the account can't cover it.
        4. Lock the stake (always: the risk engine accepts a zero-amount
           lock, so a free edit still gets an order + lockId rather than a
           None sentinel — see the task report for why).
        5. Re-triangulate/trade against the factored market; on
           ``JointMarketError``, release the lock just created and
           re-raise as the appropriate ``VenueError`` subtype (see
           ``_trade_error``) so the account and the joint are left exactly
           as before the call.
        6. Record and return the order.
        """
        context = dict(context or {})
        remaining_context = self._check_lifecycle(variable_id, context)
        before = self._before(variable_id, outcome_id, context)
        stake = self._stake_for_edit_or_raise(before, target)

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
            raise self._trade_error(err) from err

        self._order_seq += 1
        order = {
            "orderId": f"vb_{self._order_seq}",
            "accountId": account_id,
            "variableId": variable_id,
            "outcomeId": outcome_id,
            "target": target,
            "context": dict(context),
            "before": before,
            "after": target,
            "stake": str(stake),
            "lockId": lock.lock_id,
            "status": "open",
            "fill": fill,
            "remainingContext": remaining_context,
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
            raise MarketClosed(f"variable already resolved: {variable_id}")
        if variable_id in self._voided:
            raise MarketClosed(f"variable already voided: {variable_id}")
        return market_id, self._markets[market_id]

    def _call_off(self, order: dict[str, Any]) -> None:
        """Return the full frozen stake and retire the order."""
        self._risk_engine.release_lock(order["lockId"])
        order["status"] = "called_off"

    def _resolution_treasury_outflow(
        self, variable_id: str, outcome_id: str
    ) -> Decimal:
        """Total credits that would flow OUT of the treasury (to winners) if
        ``variable_id`` resolved to ``outcome_id`` right now.

        Read-only: mirrors the per-order settlement decisions in
        ``resolve_variable`` below without mutating any order, the joint, or
        balances. Only positive payouts (treasury -> trader) count; losing
        payouts flow INTO the treasury and can never cause an overdraft, so
        they're ignored — which makes the resulting check
        ``treasury.available >= outflow`` sufficient regardless of the order
        in which the real walk applies the transfers.

        Kept deliberately in lock-step with the walk; the
        ``test_resolve_precheck_matches_walk`` test asserts they agree.
        """
        outflow = Decimal("0")
        for order in self._orders:
            if order["status"] not in ("open", "awaiting_context"):
                continue
            remaining = dict(order["remainingContext"])
            if variable_id in remaining:
                if remaining[variable_id] != outcome_id:
                    continue  # context contradicted -> called off, no payout
                del remaining[variable_id]
            won = order.get("resolvedWon")
            if order["variableId"] == variable_id:
                won = outcome_id == order["outcomeId"]
            if won is None or remaining:
                continue  # win/loss undetermined or context still pending
            payout = payout_for_edit(
                self._liquidity, order["before"], order["target"], won,
            )
            if payout > 0:
                outflow += payout
        return outflow

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
            raise InvalidOutcome(f"unknown outcome: {outcome_id}")

        # Solvency precheck (I2): verify the treasury can cover every winning
        # payout BEFORE mutating anything, so an under-funded treasury fails
        # cleanly rather than aborting mid-walk with a half-applied
        # settlement. See _resolution_treasury_outflow.
        outflow = self._resolution_treasury_outflow(variable_id, outcome_id)
        treasury = self._risk_engine.get_account(self.treasury_account_id)
        if treasury.available_balance < outflow:
            raise InsufficientTreasury(
                f"treasury {self.treasury_account_id} cannot cover resolution "
                f"of {variable_id}->{outcome_id}: winning payouts total "
                f"{outflow}, treasury has {treasury.available_balance} "
                "available"
            )

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
            was_awaiting = order["status"] == "awaiting_context"

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
                if not was_awaiting:
                    # Only report orders that TRANSITIONED to
                    # awaiting_context during this call — an order that
                    # was already awaiting_context and just had one (of
                    # several) pending context keys satisfied, without
                    # emptying remainingContext, didn't transition and
                    # must not be re-listed.
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

    # -- persistence --------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Serializable venue state for ``core.persistence``.

        ``ordersByVar`` is deliberately NOT persisted — it's a derived index
        rebuilt from ``orders`` on load. ``marketStatus`` only carries the
        status/outcome deltas for markets that were resolved or voided
        (copy-on-write records in ``self._markets``); untouched seed records
        aren't duplicated here, since they come back from ``seeds`` again.
        """
        market_status: dict[str, dict[str, Any]] = {}
        for market_id, record in self._markets.items():
            status = record.get("status")
            if status is None:
                continue  # untouched seed record, nothing to persist
            delta = {"status": status}
            if "resolvedOutcome" in record:
                delta["resolvedOutcome"] = record["resolvedOutcome"]
            market_status[market_id] = delta

        return {
            "orders": self._orders,
            "orderSeq": self._order_seq,
            "resolutions": dict(self._resolutions),
            "voided": sorted(self._voided),
            "marketStatus": market_status,
            "treasuryAccountId": self.treasury_account_id,
            "liquidity": str(self._liquidity),
            "maxWidth": self._max_width,
            "fm": self._fm.snapshot(),
            "seedsSource": self._seeds_source,
        }

    @classmethod
    def from_snapshot(
        cls,
        data: Mapping[str, Any],
        risk_engine: RiskEngine,
        seeds: str | Path | dict,
    ) -> "JointVenue":
        """Rebuild a ``JointVenue`` from ``snapshot()`` output.

        ``risk_engine`` must already contain the restored treasury account
        (it comes from a persisted exchange snapshot, restored alongside
        every other account) — the constructor is told to skip its usual
        create_account/mint bootstrap and reuse ``treasuryAccountId`` as-is.

        ``seeds`` is the same seeds-v1 source (path or dict) the original
        venue was built from; it's needed regardless of whether the
        FactoredMarket snapshot verifies, since market metadata
        (``self._markets`` / ``self._var_to_market``) is always rebuilt from
        it.

        The constructor already rebuilds ``self._fm`` from ``seeds`` (fresh
        calibrated priors, no trade history). We then try to overwrite it
        with the *exact* traded beliefs from ``data["fm"]`` via
        ``FactoredMarket.from_snapshot``, which verifies the stored cluster
        structure against a deterministic rebuild of the same scopes before
        trusting the stored tables (see factored_market.py:809) — the same
        "verify by rebuild, don't trust blindly" discipline the bayes engine
        uses elsewhere. If that verification fails (structure mismatch, or
        any other error reading the stored snapshot), we log a warning and
        keep the seeds-only rebuild: traded prices are lost (marginals fall
        back to seed priors) but orders/resolutions/voids are preserved
        untouched, so settlement bookkeeping still works — this degraded
        state is an accepted, documented fallback, not a crash. To keep
        that fallback *consistent* rather than merely non-crashing, every
        already-recorded resolution is replayed onto the fresh seeds-only
        ``fm`` via ``fm.condition`` — otherwise a resolved variable's
        children would read back at their unconditioned prior instead of
        the correct conditioned value.

        Before any of that, the treasury account is checked eagerly: if
        ``treasuryAccountId`` isn't present in ``risk_engine``, this raises
        ``VenueError`` immediately rather than deferring the failure to the
        first settlement that tries to pay out of a treasury that was
        never restored.
        """
        liquidity = Decimal(str(data["liquidity"]))
        max_width = int(data["maxWidth"])
        treasury_account_id = int(data["treasuryAccountId"])

        try:
            risk_engine.get_account(treasury_account_id)
        except ValueError as err:
            raise VenueError(
                f"treasury account {treasury_account_id} not found in risk "
                "engine; refusing to restore a venue whose treasury vanished"
            ) from err

        venue = cls(
            risk_engine,
            seeds,
            liquidity=liquidity,
            max_width=max_width,
            _bootstrap_treasury=False,
            _treasury_account_id=treasury_account_id,
        )

        resolutions = dict(data.get("resolutions", {}))

        fm_data = data.get("fm")
        if fm_data is not None:
            try:
                venue._fm = FactoredMarket.from_snapshot(fm_data, max_width=max_width)
            except Exception as err:  # noqa: BLE001 - deliberately broad, see docstring
                logger.warning(
                    "JointVenue.from_snapshot: fm snapshot failed structure "
                    "verification (%s: %s); falling back to a fresh rebuild "
                    "from seeds — traded prices are lost, orders are kept.",
                    type(err).__name__, err,
                )
                for var, outcome in resolutions.items():
                    venue._fm.condition(var, outcome)

        venue._orders = [dict(order) for order in data.get("orders", [])]
        venue._order_seq = int(data.get("orderSeq", 0))
        venue._resolutions = resolutions
        venue._voided = set(data.get("voided", []))

        venue._orders_by_var = {}
        for order in venue._orders:
            venue._orders_by_var.setdefault(order["variableId"], []).append(order)

        for market_id, delta in data.get("marketStatus", {}).items():
            record = venue._markets.get(market_id)
            if record is not None:
                venue._markets[market_id] = {**record, **delta}

        return venue

    # -- public order accessors -------------------------------------------

    def orders_count(self) -> int:
        """Total number of orders ever placed on this venue (any status)."""
        return len(self._orders)

    def orders_for(self, account_id: int) -> list[dict[str, Any]]:
        """``account_id``'s orders, oldest-first (placement/append order).

        Returns the INTERNAL order dicts, not copies (same as the
        ``joint._orders`` reach-in this replaces) — a caller that intends
        to mutate an entry must copy it first.
        """
        return [order for order in self._orders if order["accountId"] == account_id]

    # -- internal bookkeeping --------------------------------------------

    def _vb_lock_market_id(self, variable_id: str) -> int:
        """Stable int id for RiskEngine lock bookkeeping: O(1) lookup.

        1_000_000 + the index of the market (owning ``variable_id``) in
        seed order, precomputed once in ``self._lock_ids`` at construction
        (both the bootstrap and the restore path run through ``__init__``,
        so there's a single place this table is built). Raises ``KeyError``
        for an unknown variable, same as the old ``self._var_to_market[...]``
        lookup did.
        """
        return self._lock_ids[variable_id]
