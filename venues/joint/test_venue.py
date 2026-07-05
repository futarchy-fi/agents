from decimal import Decimal

import pytest

from core.risk_engine import RiskEngine
from venues.joint.msr import stake_for_edit
from venues.joint.venue import (
    InsufficientCredits,
    JointVenue,
    UnknownMarket,
    UnknownVariable,
    WidthBudgetExceeded,
)

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


def test_vb_lock_market_id_is_stable_offset():
    venue = _make_venue()
    assert venue._vb_lock_market_id("gcx_b") == 1_000_001
    assert venue._vb_lock_market_id("gcx_a") == 1_000_000


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
    assert venue.marginal("gcx_a")["yes"] == pytest.approx(0.6, abs=1e-9)
    account = engine.get_account(account_id)
    assert account.frozen_balance == Decimal("0")
    assert account.available_balance == Decimal("1000")
    assert venue._orders == []


def test_place_edit_width_budget_rollback(monkeypatch):
    engine = RiskEngine()
    venue = JointVenue(engine, TINY_SEEDS)
    account_id = _fund(engine, Decimal("1000"))
    before = venue.marginal("gcx_a")["yes"]

    from venues.joint.inference import JointMarketError

    def _boom(*args, **kwargs):
        raise JointMarketError("forced")

    monkeypatch.setattr(venue._fm, "trade_to_probability", _boom)

    with pytest.raises(WidthBudgetExceeded):
        venue.place_edit(account_id, "gcx_a", "yes", 0.8)

    account = engine.get_account(account_id)
    assert account.frozen_balance == Decimal("0")
    assert account.available_balance == Decimal("1000")
    assert venue.marginal("gcx_a")["yes"] == pytest.approx(before, abs=1e-9)
    assert venue._orders == []
