from decimal import Decimal

import pytest

from core.risk_engine import RiskEngine
from venues.joint.venue import JointVenue, UnknownMarket, UnknownVariable

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
