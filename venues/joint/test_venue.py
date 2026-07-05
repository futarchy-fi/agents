from decimal import Decimal

import pytest

from core.risk_engine import RiskEngine
from venues.joint.inference import JointMarketError
from venues.joint.msr import payout_for_edit, stake_for_edit
from venues.joint.venue import (
    ContextContradicted,
    InsufficientCredits,
    InsufficientTreasury,
    InvalidOutcome,
    InvalidTarget,
    JointVenue,
    MarketClosed,
    TradeRejected,
    UnknownMarket,
    UnknownVariable,
    VenueError,
    WidthBudgetExceeded,
)

B = Decimal("50")

# Seeds-v1 shape per data/seeds_takeoff.json: "markets" is a dict keyed by
# market id, and "conditionalMarginals" is a top-level dict keyed by market
# id -> cpt_key ("<var>=<outcome>", joined with "," for multiple parents) ->
# outcome distribution. Root markets (no CPT rows) use their own
# "marginals" as the prior; CPT children get their prior from the CPT rows
# instead (see build_network_nodes in venues/joint/inference/network_model.py).
TINY_SEEDS = {
    "version": "seeds-v1",
    "markets": {
        "g1": {
            "id": "g1",
            "variableId": "gcx_a",
            "title": "A",
            "description": "root market A",
            "outcomes": [{"id": "yes", "name": "Yes"}, {"id": "no", "name": "No"}],
            "marginals": {"yes": 0.6, "no": 0.4},
        },
        "g2": {
            "id": "g2",
            "variableId": "gcx_b",
            "title": "B",
            "description": "child market B",
            "outcomes": [{"id": "yes", "name": "Yes"}, {"id": "no", "name": "No"}],
            "parents": ["gcx_a"],
        },
    },
    "conditionalMarginals": {
        "g2": {
            "gcx_a=yes": {"yes": 0.9, "no": 0.1},
            "gcx_a=no": {"yes": 0.2, "no": 0.8},
        },
    },
}


# Same as TINY_SEEDS plus an independent root market gcx_c, unrelated to
# gcx_a/gcx_b, used for tests needing an order with two independent
# context keys (item-5 awaiting-transition tests).
THREE_VAR_SEEDS = {
    "version": "seeds-v1",
    "markets": {
        **TINY_SEEDS["markets"],
        "g3": {
            "id": "g3",
            "variableId": "gcx_c",
            "title": "C",
            "description": "independent root market C",
            "outcomes": [{"id": "yes", "name": "Yes"}, {"id": "no", "name": "No"}],
            "marginals": {"yes": 0.5, "no": 0.5},
        },
    },
    "conditionalMarginals": dict(TINY_SEEDS["conditionalMarginals"]),
}


def _make_venue() -> JointVenue:
    return JointVenue(RiskEngine(), TINY_SEEDS)


def test_market_ids_preserve_seed_order():
    venue = _make_venue()
    assert venue.market_ids() == ["g1", "g2"]


def test_get_market_merges_live_marginals():
    venue = _make_venue()
    market = venue.get_market("g2")
    assert market["marginals"]["yes"] == pytest.approx(
        0.6 * 0.9 + 0.4 * 0.2, abs=1e-6
    )
    assert market["parents"] == ["gcx_a"]


def test_marginal_with_context():
    venue = _make_venue()
    result = venue.marginal("gcx_b", {"gcx_a": "yes"})
    assert result["yes"] == pytest.approx(0.9, abs=1e-6)


def test_treasury_account_funded():
    engine = RiskEngine()
    venue = JointVenue(engine, TINY_SEEDS)
    account = engine.get_account(venue.treasury_account_id)
    assert account.available_balance == Decimal("1000000")


def test_get_market_unknown_raises():
    venue = _make_venue()
    with pytest.raises(UnknownMarket):
        venue.get_market("nope")


def test_marginal_unknown_raises():
    venue = _make_venue()
    with pytest.raises(UnknownVariable):
        venue.marginal("nope")


def test_marginal_context_contradicted_raises_not_unknown_variable():
    """gcx_b is a KNOWN variable; a context that contradicts an already-
    conditioned resolution makes fm.marginal return None for a structural
    reason (zero probability), not because the variable is unknown."""
    engine = RiskEngine()
    venue = JointVenue(engine, TINY_SEEDS)
    venue.resolve_variable("gcx_a", "no")

    with pytest.raises(ContextContradicted):
        venue.marginal("gcx_b", {"gcx_a": "yes"})


def test_vb_lock_market_id_is_stable_offset():
    venue = _make_venue()
    assert venue._vb_lock_market_id("gcx_b") == 1_000_001
    assert venue._vb_lock_market_id("gcx_a") == 1_000_000


def test_lock_ids_precomputed_as_a_dict():
    venue = _make_venue()
    assert venue._lock_ids == {"gcx_a": 1_000_000, "gcx_b": 1_000_001}


def test_market_ids_returns_the_same_cached_list_object():
    venue = _make_venue()
    assert venue.market_ids() is venue.market_ids()
    assert venue.market_ids() == ["g1", "g2"]


def test_orders_count_and_orders_for_public_accessors():
    engine = RiskEngine()
    venue = JointVenue(engine, TINY_SEEDS)
    acc1 = _fund(engine, Decimal("1000"))
    acc2 = _fund(engine, Decimal("1000"))

    assert venue.orders_count() == 0
    assert venue.orders_for(acc1) == []

    order1 = venue.place_edit(acc1, "gcx_a", "yes", 0.7)
    order2 = venue.place_edit(acc2, "gcx_a", "yes", 0.75)
    order3 = venue.place_edit(acc1, "gcx_b", "yes", 0.5)

    assert venue.orders_count() == 3
    assert [o["orderId"] for o in venue.orders_for(acc1)] == [
        order1["orderId"], order3["orderId"],
    ]
    assert [o["orderId"] for o in venue.orders_for(acc2)] == [order2["orderId"]]


# -- place_edit / preview_edit ------------------------------------------


def _fund(engine: RiskEngine, amount: Decimal) -> int:
    account = engine.create_account()
    engine.mint(account.id, amount)
    return account.id


def test_place_edit_freezes_exact_worst_case_stake():
    engine = RiskEngine()
    venue = JointVenue(engine, TINY_SEEDS)
    account_id = _fund(engine, Decimal("1000"))
    before = venue.marginal("gcx_a")["yes"]

    order = venue.place_edit(account_id, "gcx_a", "yes", 0.8)

    expected_stake = stake_for_edit(Decimal("50"), before, 0.8)
    account = engine.get_account(account_id)
    assert account.frozen_balance == expected_stake
    assert account.available_balance == Decimal("1000") - expected_stake
    assert order["stake"] == str(expected_stake)
    assert order["orderId"] == "vb_1"
    assert order["lockId"] is not None


def test_place_edit_moves_marginal_and_reprices_child_coherently():
    engine = RiskEngine()
    venue = JointVenue(engine, TINY_SEEDS)
    account_id = _fund(engine, Decimal("1000"))

    venue.place_edit(account_id, "gcx_a", "yes", 0.8)

    assert venue.marginal("gcx_a")["yes"] == pytest.approx(0.8, abs=1e-9)
    assert venue.get_market("g2")["marginals"]["yes"] == pytest.approx(
        0.8 * 0.9 + 0.2 * 0.2, abs=1e-6
    )


def test_place_edit_insufficient_credits_leaves_no_state_change():
    engine = RiskEngine()
    venue = JointVenue(engine, TINY_SEEDS)
    before = venue.marginal("gcx_a")["yes"]
    expected_stake = stake_for_edit(Decimal("50"), before, 0.8)
    account_id = _fund(engine, expected_stake - Decimal("0.01"))

    with pytest.raises(InsufficientCredits):
        venue.place_edit(account_id, "gcx_a", "yes", 0.8)

    account = engine.get_account(account_id)
    assert account.frozen_balance == Decimal("0")
    assert venue.marginal("gcx_a")["yes"] == pytest.approx(before, abs=1e-9)
    assert venue._orders == []


def test_place_edit_conditional_context_leaves_parent_unchanged():
    engine = RiskEngine()
    venue = JointVenue(engine, TINY_SEEDS)
    account_id = _fund(engine, Decimal("1000"))
    parent_before = venue.marginal("gcx_a")["yes"]

    order = venue.place_edit(
        account_id, "gcx_b", "yes", 0.5, context={"gcx_a": "yes"}
    )

    assert order["context"] == {"gcx_a": "yes"}
    assert venue.marginal("gcx_a")["yes"] == pytest.approx(parent_before, abs=1e-9)


def test_preview_edit_is_idempotent_and_side_effect_free():
    engine = RiskEngine()
    venue = JointVenue(engine, TINY_SEEDS)
    account_id = _fund(engine, Decimal("1000"))

    first = venue.preview_edit(account_id, "gcx_a", "yes", 0.8)
    second = venue.preview_edit(account_id, "gcx_a", "yes", 0.8)

    assert first == second
    assert isinstance(first["stake"], str)
    assert venue.marginal("gcx_a")["yes"] == pytest.approx(0.6, abs=1e-9)
    account = engine.get_account(account_id)
    assert account.frozen_balance == Decimal("0")
    assert account.available_balance == Decimal("1000")
    assert venue._orders == []


def test_preview_edit_stake_matches_place_edit_stake_type_and_value():
    engine = RiskEngine()
    venue = JointVenue(engine, TINY_SEEDS)
    account_id = _fund(engine, Decimal("1000"))

    preview = venue.preview_edit(account_id, "gcx_a", "yes", 0.8)
    order = venue.place_edit(account_id, "gcx_a", "yes", 0.8)

    assert isinstance(preview["stake"], str)
    assert isinstance(order["stake"], str)
    assert preview["stake"] == order["stake"]


def test_place_edit_width_budget_rollback(monkeypatch):
    engine = RiskEngine()
    venue = JointVenue(engine, TINY_SEEDS)
    account_id = _fund(engine, Decimal("1000"))
    before = venue.marginal("gcx_a")["yes"]

    def _boom(*args, **kwargs):
        raise JointMarketError(
            "belief structure would exceed the treewidth budget (forced)"
        )

    monkeypatch.setattr(venue._fm, "trade_to_probability", _boom)

    with pytest.raises(WidthBudgetExceeded):
        venue.place_edit(account_id, "gcx_a", "yes", 0.8)

    account = engine.get_account(account_id)
    assert account.frozen_balance == Decimal("0")
    assert account.available_balance == Decimal("1000")
    assert venue.marginal("gcx_a")["yes"] == pytest.approx(before, abs=1e-9)
    assert venue._orders == []


def test_place_edit_generic_joint_market_error_raises_trade_rejected(monkeypatch):
    engine = RiskEngine()
    venue = JointVenue(engine, TINY_SEEDS)
    account_id = _fund(engine, Decimal("1000"))
    before = venue.marginal("gcx_a")["yes"]

    def _boom(*args, **kwargs):
        raise JointMarketError("re-triangulation failed to cover the trade scope")

    monkeypatch.setattr(venue._fm, "trade_to_probability", _boom)

    with pytest.raises(TradeRejected):
        venue.place_edit(account_id, "gcx_a", "yes", 0.8)

    account = engine.get_account(account_id)
    assert account.frozen_balance == Decimal("0")
    assert account.available_balance == Decimal("1000")
    assert venue.marginal("gcx_a")["yes"] == pytest.approx(before, abs=1e-9)
    assert venue._orders == []


def test_place_edit_degenerate_price_joint_market_error_raises_invalid_target(
    monkeypatch,
):
    engine = RiskEngine()
    venue = JointVenue(engine, TINY_SEEDS)
    account_id = _fund(engine, Decimal("1000"))

    def _boom(*args, **kwargs):
        raise JointMarketError("price is degenerate; the event is already settled")

    monkeypatch.setattr(venue._fm, "trade_to_probability", _boom)

    with pytest.raises(InvalidTarget):
        venue.place_edit(account_id, "gcx_a", "yes", 0.8)

    account = engine.get_account(account_id)
    assert account.frozen_balance == Decimal("0")
    assert account.available_balance == Decimal("1000")
    assert venue._orders == []


def test_place_edit_degenerate_target_raises_invalid_target():
    engine = RiskEngine()
    venue = JointVenue(engine, TINY_SEEDS)
    account_id = _fund(engine, Decimal("1000"))

    with pytest.raises(InvalidTarget):
        venue.place_edit(account_id, "gcx_a", "yes", 1.0)

    account = engine.get_account(account_id)
    assert account.frozen_balance == Decimal("0")
    assert account.available_balance == Decimal("1000")
    assert venue._orders == []


def test_preview_edit_degenerate_target_raises_invalid_target():
    engine = RiskEngine()
    venue = JointVenue(engine, TINY_SEEDS)
    account_id = _fund(engine, Decimal("1000"))

    with pytest.raises(InvalidTarget):
        venue.preview_edit(account_id, "gcx_a", "yes", 0.0)


# -- lifecycle guards: place_edit / preview_edit on dead markets ----------


class TestLifecycleGuards:
    def _setup(self) -> tuple[RiskEngine, JointVenue]:
        engine = RiskEngine()
        venue = JointVenue(engine, TINY_SEEDS)
        return engine, venue

    def _assert_no_funds_or_state_moved(self, engine, venue, account_id):
        account = engine.get_account(account_id)
        assert account.frozen_balance == Decimal("0")
        assert account.available_balance == Decimal("1000")
        assert venue._orders == []

    def test_place_edit_on_voided_variable_raises_market_closed(self):
        engine, venue = self._setup()
        account_id = _fund(engine, Decimal("1000"))
        venue.void_variable("gcx_a")

        with pytest.raises(MarketClosed):
            venue.place_edit(account_id, "gcx_a", "yes", 0.8)
        self._assert_no_funds_or_state_moved(engine, venue, account_id)

    def test_preview_edit_on_voided_variable_raises_market_closed(self):
        engine, venue = self._setup()
        account_id = _fund(engine, Decimal("1000"))
        venue.void_variable("gcx_a")

        with pytest.raises(MarketClosed):
            venue.preview_edit(account_id, "gcx_a", "yes", 0.8)

    def test_place_edit_on_resolved_variable_raises_market_closed(self):
        engine, venue = self._setup()
        account_id = _fund(engine, Decimal("1000"))
        venue.resolve_variable("gcx_a", "yes")

        with pytest.raises(MarketClosed):
            venue.place_edit(account_id, "gcx_a", "no", 0.2)
        self._assert_no_funds_or_state_moved(engine, venue, account_id)

    def test_preview_edit_on_resolved_variable_raises_market_closed(self):
        engine, venue = self._setup()
        account_id = _fund(engine, Decimal("1000"))
        venue.resolve_variable("gcx_a", "yes")

        with pytest.raises(MarketClosed):
            venue.preview_edit(account_id, "gcx_a", "no", 0.2)

    def test_place_edit_context_voided_raises_market_closed(self):
        engine, venue = self._setup()
        account_id = _fund(engine, Decimal("1000"))
        venue.void_variable("gcx_a")

        with pytest.raises(MarketClosed):
            venue.place_edit(account_id, "gcx_b", "yes", 0.5, context={"gcx_a": "yes"})
        self._assert_no_funds_or_state_moved(engine, venue, account_id)

    def test_preview_edit_context_voided_raises_market_closed(self):
        engine, venue = self._setup()
        account_id = _fund(engine, Decimal("1000"))
        venue.void_variable("gcx_a")

        with pytest.raises(MarketClosed):
            venue.preview_edit(
                account_id, "gcx_b", "yes", 0.5, context={"gcx_a": "yes"}
            )

    def test_place_edit_context_resolved_contradicted_raises_context_contradicted(self):
        engine, venue = self._setup()
        account_id = _fund(engine, Decimal("1000"))
        venue.resolve_variable("gcx_a", "no")

        with pytest.raises(ContextContradicted):
            venue.place_edit(account_id, "gcx_b", "yes", 0.5, context={"gcx_a": "yes"})
        self._assert_no_funds_or_state_moved(engine, venue, account_id)

    def test_preview_edit_context_resolved_contradicted_raises_context_contradicted(
        self,
    ):
        engine, venue = self._setup()
        account_id = _fund(engine, Decimal("1000"))
        venue.resolve_variable("gcx_a", "no")

        with pytest.raises(ContextContradicted):
            venue.preview_edit(
                account_id, "gcx_b", "yes", 0.5, context={"gcx_a": "yes"}
            )

    def test_place_edit_context_resolved_matching_strips_remaining_and_settles(self):
        engine, venue = self._setup()
        account_id = _fund(engine, Decimal("1000"))
        venue.resolve_variable("gcx_a", "yes")

        order = venue.place_edit(
            account_id, "gcx_b", "yes", 0.5, context={"gcx_a": "yes"}
        )

        # The order still records what it was placed against...
        assert order["context"] == {"gcx_a": "yes"}
        # ...but the already-resolved-matching key is stripped from the
        # bookkeeping that drives future settlement.
        assert order["remainingContext"] == {}

        # before is P(gcx_b=yes | gcx_a=yes) under the conditioned joint,
        # i.e. the CPT row (0.9), not the unconditioned mixture (0.62).
        assert order["before"] == pytest.approx(0.9, abs=1e-6)

        result = venue.resolve_variable("gcx_b", "no")
        assert result["settled"] == [order["orderId"]]
        assert order["status"] == "settled"

    # -- fund-freeze hole: unknown context key / bad context outcome ------
    #
    # A context key that names no real variable used to be silently
    # skipped by fm.marginal/fm.trade_to_probability, so the edit placed
    # successfully with a remainingContext entry no resolve_variable call
    # could ever match — the order (and its frozen stake) would sit in
    # awaiting_context forever. These guards must fire in _check_lifecycle,
    # shared by both place_edit and preview_edit, before any lock/order/fm
    # call.

    def test_place_edit_unknown_context_key_raises_unknown_variable(self):
        engine, venue = self._setup()
        account_id = _fund(engine, Decimal("1000"))
        before = venue.marginal("gcx_b")["yes"]

        with pytest.raises(UnknownVariable):
            venue.place_edit(
                account_id, "gcx_b", "yes", 0.5, context={"nope": "yes"}
            )
        self._assert_no_funds_or_state_moved(engine, venue, account_id)
        assert venue.marginal("gcx_b")["yes"] == pytest.approx(before, abs=1e-9)

    def test_preview_edit_unknown_context_key_raises_unknown_variable(self):
        engine, venue = self._setup()
        account_id = _fund(engine, Decimal("1000"))
        before = venue.marginal("gcx_b")["yes"]

        with pytest.raises(UnknownVariable):
            venue.preview_edit(
                account_id, "gcx_b", "yes", 0.5, context={"nope": "yes"}
            )
        self._assert_no_funds_or_state_moved(engine, venue, account_id)
        assert venue.marginal("gcx_b")["yes"] == pytest.approx(before, abs=1e-9)

    def test_place_edit_invalid_context_outcome_raises_invalid_outcome(self):
        engine, venue = self._setup()
        account_id = _fund(engine, Decimal("1000"))
        before = venue.marginal("gcx_b")["yes"]

        with pytest.raises(InvalidOutcome):
            venue.place_edit(
                account_id, "gcx_b", "yes", 0.5, context={"gcx_a": "maybe"}
            )
        self._assert_no_funds_or_state_moved(engine, venue, account_id)
        assert venue.marginal("gcx_b")["yes"] == pytest.approx(before, abs=1e-9)

    def test_preview_edit_invalid_context_outcome_raises_invalid_outcome(self):
        engine, venue = self._setup()
        account_id = _fund(engine, Decimal("1000"))
        before = venue.marginal("gcx_b")["yes"]

        with pytest.raises(InvalidOutcome):
            venue.preview_edit(
                account_id, "gcx_b", "yes", 0.5, context={"gcx_a": "maybe"}
            )
        self._assert_no_funds_or_state_moved(engine, venue, account_id)
        assert venue.marginal("gcx_b")["yes"] == pytest.approx(before, abs=1e-9)


# -- settlement: resolve_variable / void_variable -------------------------


class TestSettlement:
    def _setup(self) -> tuple[RiskEngine, JointVenue]:
        engine = RiskEngine()
        venue = JointVenue(engine, TINY_SEEDS)
        return engine, venue

    @staticmethod
    def _total(engine: RiskEngine, account_id: int) -> Decimal:
        acc = engine.get_account(account_id)
        return acc.available_balance + acc.frozen_balance

    # (a) winner
    def test_winner_gets_exact_log_score_payout_from_treasury(self):
        engine, venue = self._setup()
        aid = _fund(engine, Decimal("1000"))
        before = venue.marginal("gcx_a")["yes"]

        order = venue.place_edit(aid, "gcx_a", "yes", 0.8)
        result = venue.resolve_variable("gcx_a", "yes")

        payout = payout_for_edit(B, before, 0.8, True)
        assert payout > 0
        acc = engine.get_account(aid)
        assert acc.available_balance == Decimal("1000") + payout
        assert acc.frozen_balance == Decimal("0")
        treasury = engine.get_account(venue.treasury_account_id)
        assert treasury.available_balance == Decimal("1000000") - payout
        assert order["status"] == "settled"
        assert order["payout"] == str(payout)
        assert result["settled"] == [order["orderId"]]
        assert result["calledOff"] == []
        assert result["awaiting"] == []
        assert result["treasuryDelta"] == str(-payout)

    # (b) loser
    def test_loser_pays_at_most_stake_to_treasury(self):
        engine, venue = self._setup()
        aid = _fund(engine, Decimal("1000"))
        before = venue.marginal("gcx_a")["yes"]

        order = venue.place_edit(aid, "gcx_a", "yes", 0.8)
        stake = Decimal(order["stake"])
        result = venue.resolve_variable("gcx_a", "no")

        payout = payout_for_edit(B, before, 0.8, False)
        assert payout < 0
        assert -payout <= stake
        acc = engine.get_account(aid)
        assert acc.available_balance == Decimal("1000") + payout
        assert acc.frozen_balance == Decimal("0")
        treasury = engine.get_account(venue.treasury_account_id)
        assert treasury.available_balance == Decimal("1000000") - payout
        assert order["status"] == "settled"
        assert order["payout"] == str(payout)
        assert result["settled"] == [order["orderId"]]
        assert result["treasuryDelta"] == str(-payout)

    # (c) called off by contradicted context
    def test_contradicted_context_calls_off_with_full_stake_back(self):
        engine, venue = self._setup()
        aid = _fund(engine, Decimal("1000"))

        order = venue.place_edit(aid, "gcx_b", "yes", 0.5, context={"gcx_a": "yes"})
        result = venue.resolve_variable("gcx_a", "no")

        assert order["status"] == "called_off"
        assert result["calledOff"] == [order["orderId"]]
        assert result["settled"] == []
        assert result["treasuryDelta"] == str(Decimal("0"))
        acc = engine.get_account(aid)
        assert acc.available_balance == Decimal("1000")
        assert acc.frozen_balance == Decimal("0")
        treasury = engine.get_account(venue.treasury_account_id)
        assert treasury.available_balance == Decimal("1000000")

        # Resolving the order's own variable later has no further effect.
        result2 = venue.resolve_variable("gcx_b", "yes")
        assert result2["settled"] == []
        assert result2["calledOff"] == []
        assert order["status"] == "called_off"
        acc = engine.get_account(aid)
        assert acc.available_balance == Decimal("1000")
        assert engine.get_account(venue.treasury_account_id).available_balance == (
            Decimal("1000000")
        )

    # (d) conservation across a 3-trader mix
    def test_conservation_three_traders_full_resolution(self):
        engine, venue = self._setup()
        t1 = _fund(engine, Decimal("1000"))
        t2 = _fund(engine, Decimal("1000"))
        t3 = _fund(engine, Decimal("1000"))

        venue.place_edit(t1, "gcx_a", "yes", 0.8)
        venue.place_edit(t2, "gcx_b", "yes", 0.5, context={"gcx_a": "yes"})
        venue.place_edit(t3, "gcx_b", "no", 0.6)

        venue.resolve_variable("gcx_a", "yes")
        venue.resolve_variable("gcx_b", "no")

        for order in venue._orders:
            assert order["status"] == "settled"
        total = sum(
            (self._total(engine, aid) for aid in (t1, t2, t3)),
            self._total(engine, venue.treasury_account_id),
        )
        assert total == Decimal("3000") + Decimal("1000000")
        for aid in (t1, t2, t3):
            assert engine.get_account(aid).frozen_balance == Decimal("0")

    # (e) void makes everyone whole
    def test_void_calls_off_direct_and_context_orders(self):
        engine, venue = self._setup()
        t1 = _fund(engine, Decimal("1000"))
        t2 = _fund(engine, Decimal("1000"))

        o1 = venue.place_edit(t1, "gcx_a", "yes", 0.8)
        o2 = venue.place_edit(t2, "gcx_b", "yes", 0.5, context={"gcx_a": "yes"})

        result = venue.void_variable("gcx_a")

        assert set(result["calledOff"]) == {o1["orderId"], o2["orderId"]}
        assert o1["status"] == "called_off"
        assert o2["status"] == "called_off"
        for aid in (t1, t2):
            acc = engine.get_account(aid)
            assert acc.available_balance == Decimal("1000")
            assert acc.frozen_balance == Decimal("0")
        assert engine.get_account(venue.treasury_account_id).available_balance == (
            Decimal("1000000")
        )

    # (f1) context resolves first (satisfied), variable later
    def test_deferred_context_satisfied_then_variable_settles(self):
        engine, venue = self._setup()
        aid = _fund(engine, Decimal("1000"))

        order = venue.place_edit(aid, "gcx_b", "yes", 0.5, context={"gcx_a": "yes"})
        stake = Decimal(order["stake"])
        before = order["before"]

        r1 = venue.resolve_variable("gcx_a", "yes")
        assert order["status"] == "open"
        assert order["remainingContext"] == {}
        assert r1["settled"] == []
        assert r1["calledOff"] == []
        assert r1["awaiting"] == []
        acc = engine.get_account(aid)
        assert acc.frozen_balance == stake

        r2 = venue.resolve_variable("gcx_b", "no")
        payout = payout_for_edit(B, before, 0.5, False)
        assert order["status"] == "settled"
        assert order["payout"] == str(payout)
        assert r2["settled"] == [order["orderId"]]
        acc = engine.get_account(aid)
        assert acc.available_balance == Decimal("1000") + payout
        assert acc.frozen_balance == Decimal("0")

    # (f2) variable resolves first -> awaiting_context, settles on context
    def test_deferred_variable_first_awaits_context_then_settles(self):
        engine, venue = self._setup()
        aid = _fund(engine, Decimal("1000"))

        order = venue.place_edit(aid, "gcx_b", "yes", 0.5, context={"gcx_a": "yes"})
        stake = Decimal(order["stake"])
        before = order["before"]

        r1 = venue.resolve_variable("gcx_b", "yes")
        assert order["status"] == "awaiting_context"
        assert r1["awaiting"] == [order["orderId"]]
        assert r1["settled"] == []
        assert r1["calledOff"] == []
        acc = engine.get_account(aid)
        assert acc.available_balance == Decimal("1000") - stake
        assert acc.frozen_balance == stake

        r2 = venue.resolve_variable("gcx_a", "yes")
        payout = payout_for_edit(B, before, 0.5, True)
        assert order["status"] == "settled"
        assert order["payout"] == str(payout)
        assert r2["settled"] == [order["orderId"]]
        acc = engine.get_account(aid)
        assert acc.available_balance == Decimal("1000") + payout
        assert acc.frozen_balance == Decimal("0")

    # (f2 mirror) variable first, context then contradicted -> called off
    def test_deferred_variable_first_then_contradicted_context_calls_off(self):
        engine, venue = self._setup()
        aid = _fund(engine, Decimal("1000"))

        order = venue.place_edit(aid, "gcx_b", "yes", 0.5, context={"gcx_a": "yes"})

        venue.resolve_variable("gcx_b", "yes")
        assert order["status"] == "awaiting_context"

        r2 = venue.resolve_variable("gcx_a", "no")
        assert order["status"] == "called_off"
        assert r2["calledOff"] == [order["orderId"]]
        assert r2["settled"] == []
        acc = engine.get_account(aid)
        assert acc.available_balance == Decimal("1000")
        assert acc.frozen_balance == Decimal("0")
        assert engine.get_account(venue.treasury_account_id).available_balance == (
            Decimal("1000000")
        )

    # (f3) void, not resolve, of the pending context while awaiting
    def test_void_while_awaiting_context_calls_off_with_full_stake_back(self):
        engine, venue = self._setup()
        aid = _fund(engine, Decimal("1000"))

        order = venue.place_edit(aid, "gcx_b", "yes", 0.5, context={"gcx_a": "yes"})
        stake = Decimal(order["stake"])
        assert stake > Decimal("0")

        venue.resolve_variable("gcx_b", "yes")
        assert order["status"] == "awaiting_context"
        acc = engine.get_account(aid)
        assert acc.frozen_balance == stake

        result = venue.void_variable("gcx_a")

        assert order["status"] == "called_off"
        assert result["calledOff"] == [order["orderId"]]
        acc = engine.get_account(aid)
        assert acc.available_balance == Decimal("1000")
        assert acc.frozen_balance == Decimal("0")
        assert engine.get_account(venue.treasury_account_id).available_balance == (
            Decimal("1000000")
        )

    # (f4) awaiting is only orders that transitioned THIS call, not every
    # order that happens to still be awaiting_context afterward.
    def test_awaiting_only_lists_orders_transitioning_this_call(self):
        engine = RiskEngine()
        venue = JointVenue(engine, THREE_VAR_SEEDS)
        aid = _fund(engine, Decimal("1000"))

        order = venue.place_edit(
            aid, "gcx_b", "yes", 0.5, context={"gcx_a": "yes", "gcx_c": "yes"}
        )

        r1 = venue.resolve_variable("gcx_b", "yes")
        assert order["status"] == "awaiting_context"
        assert r1["awaiting"] == [order["orderId"]]

        # Satisfies one of the two remaining context keys; order STAYS
        # awaiting_context (one key still pending) rather than settling.
        # It did not transition this call, so it must not be re-listed.
        r2 = venue.resolve_variable("gcx_a", "yes")
        assert order["status"] == "awaiting_context"
        assert order["remainingContext"] == {"gcx_c": "yes"}
        assert r2["awaiting"] == []
        assert r2["settled"] == []

        r3 = venue.resolve_variable("gcx_c", "yes")
        assert order["status"] == "settled"
        assert r3["settled"] == [order["orderId"]]
        assert r3["awaiting"] == []

    # (g) lifecycle guards
    def test_double_resolve_and_resolve_after_void_raise(self):
        engine, venue = self._setup()

        venue.resolve_variable("gcx_a", "yes")
        with pytest.raises(VenueError):
            venue.resolve_variable("gcx_a", "yes")
        with pytest.raises(VenueError):
            venue.resolve_variable("gcx_a", "no")
        with pytest.raises(VenueError):
            venue.void_variable("gcx_a")

        venue.void_variable("gcx_b")
        with pytest.raises(VenueError):
            venue.resolve_variable("gcx_b", "yes")
        with pytest.raises(VenueError):
            venue.void_variable("gcx_b")

    def test_resolve_unknown_variable_and_bad_outcome(self):
        _, venue = self._setup()
        with pytest.raises(UnknownVariable):
            venue.resolve_variable("nope", "yes")
        with pytest.raises(UnknownVariable):
            venue.void_variable("nope")
        # A bad (real-variable, unreal-outcome) resolution is specifically
        # InvalidOutcome, not the generic VenueError catch-all — this is
        # what maps it to 400 invalid_outcome at the API boundary rather
        # than the generic 400 trade_rejected.
        with pytest.raises(InvalidOutcome):
            venue.resolve_variable("gcx_a", "maybe")


class TestTreasurySolvencyPrecheck:
    """I2: resolve_variable prechecks treasury solvency before mutating."""

    def _setup(self) -> tuple[RiskEngine, JointVenue]:
        engine = RiskEngine()
        venue = JointVenue(engine, TINY_SEEDS)
        return engine, venue

    def test_resolve_precheck_matches_walk(self):
        # Winner (treasury outflow), loser (inflow), and a contradicted-context
        # order (called off, no flow). The precheck's predicted outflow must
        # equal the sum of positive payouts the real walk actually pays out —
        # this is the drift guard keeping _resolution_treasury_outflow in sync
        # with resolve_variable.
        engine, venue = self._setup()
        winner = _fund(engine, Decimal("1000"))
        loser = _fund(engine, Decimal("1000"))
        ctx = _fund(engine, Decimal("1000"))
        venue.place_edit(winner, "gcx_a", "yes", 0.8)          # wins on 'yes'
        venue.place_edit(loser, "gcx_a", "no", 0.8)            # loses on 'yes'
        venue.place_edit(ctx, "gcx_b", "yes", 0.5,
                         context={"gcx_a": "no"})              # contradicted

        predicted = venue._resolution_treasury_outflow("gcx_a", "yes")
        venue.resolve_variable("gcx_a", "yes")

        actual_outflow = sum(
            (Decimal(o["payout"]) for o in venue._orders
             if o["status"] == "settled" and Decimal(o["payout"]) > 0),
            Decimal("0"),
        )
        assert predicted > 0            # the winner did pay out
        assert predicted == actual_outflow

    def test_insufficient_treasury_raises_before_any_mutation(self):
        engine, venue = self._setup()
        aid = _fund(engine, Decimal("1000"))
        before = venue.marginal("gcx_a")["yes"]
        order = venue.place_edit(aid, "gcx_a", "yes", 0.8)
        payout = payout_for_edit(B, before, 0.8, True)
        assert payout > 0

        # Drain the treasury to strictly less than the winning payout.
        treasury = engine.get_account(venue.treasury_account_id)
        sink = engine.create_account()
        keep = payout - Decimal("0.000001")
        engine.transfer_available(
            venue.treasury_account_id, sink.id,
            treasury.available_balance - keep,
        )
        assert treasury.available_balance == keep

        marg_before = venue.marginal("gcx_a")["yes"]
        acc = engine.get_account(aid)
        avail_before, frozen_before = acc.available_balance, acc.frozen_balance

        with pytest.raises(InsufficientTreasury):
            venue.resolve_variable("gcx_a", "yes")

        # Nothing moved: order still open, joint not conditioned, balances intact.
        assert order["status"] == "open"
        assert "gcx_a" not in venue._resolutions
        assert venue.marginal("gcx_a")["yes"] == marg_before
        assert treasury.available_balance == keep
        acc = engine.get_account(aid)
        assert acc.available_balance == avail_before
        assert acc.frozen_balance == frozen_before

        # And a refunded treasury resolves cleanly — proving the failed
        # attempt left the venue in a fully resolvable state.
        engine.transfer_available(sink.id, venue.treasury_account_id, payout)
        result = venue.resolve_variable("gcx_a", "yes")
        assert result["settled"] == [order["orderId"]]
        assert order["status"] == "settled"

    def test_precheck_ignores_losing_payouts(self):
        # A losing resolution is a treasury INFLOW, so the precheck reports
        # zero outflow and succeeds even with a near-empty treasury.
        engine, venue = self._setup()
        aid = _fund(engine, Decimal("1000"))
        order = venue.place_edit(aid, "gcx_a", "yes", 0.8)     # loses on 'no'

        treasury = engine.get_account(venue.treasury_account_id)
        sink = engine.create_account()
        engine.transfer_available(
            venue.treasury_account_id, sink.id,
            treasury.available_balance - Decimal("1"),
        )
        assert venue._resolution_treasury_outflow("gcx_a", "no") == Decimal("0")

        result = venue.resolve_variable("gcx_a", "no")         # must NOT raise
        assert result["settled"] == [order["orderId"]]
        assert order["status"] == "settled"
        assert treasury.available_balance > Decimal("1")       # loser paid in
