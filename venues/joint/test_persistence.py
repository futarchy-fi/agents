"""Snapshot persistence (v3-for-JointVenue / schema v4) tests.

Covers:
  (a) full-fidelity roundtrip through core.persistence save/load on disk,
      including settlement parity between the original venue and a twin
      rebuilt from the persisted snapshot.
  (b) migrating a version-2-shaped exchange snapshot forward — the venues
      section should show up empty, no error.
  (c) FactoredMarket snapshot structure mismatch falls back to rebuilding
      the inference engine from seeds (losing traded prices, keeping
      orders) instead of crashing.
"""

import logging
from decimal import Decimal

import pytest

from core.market_engine import MarketEngine
from core.persistence import CURRENT_VERSION, load_snapshot, save_snapshot
from core.risk_engine import RiskEngine
from venues.joint.inference import JointMarketError
from venues.joint.test_venue import TINY_SEEDS, _fund
from venues.joint.venue import JointVenue, VenueError


# -- (a) full-fidelity roundtrip ------------------------------------------


def test_roundtrip_through_disk_snapshot_preserves_state_and_settlement(tmp_path):
    engine = RiskEngine()
    venue = JointVenue(engine, TINY_SEEDS)
    t1 = _fund(engine, Decimal("1000"))
    t2 = _fund(engine, Decimal("1000"))

    venue.place_edit(t1, "gcx_a", "yes", 0.8)
    venue.place_edit(t2, "gcx_b", "yes", 0.5, context={"gcx_a": "yes"})

    me = MarketEngine(engine)
    path = str(tmp_path / "state.json")
    save_snapshot(engine, me, path, joint_venue=venue)

    risk2, me2, _auth2, _repos2, venues2 = load_snapshot(path)
    assert "joint" in venues2
    twin = JointVenue.from_snapshot(venues2["joint"], risk2, TINY_SEEDS)

    # Marginals identical to 1e-9.
    assert twin.marginal("gcx_a")["yes"] == pytest.approx(
        venue.marginal("gcx_a")["yes"], abs=1e-9
    )
    assert twin.marginal("gcx_b")["yes"] == pytest.approx(
        venue.marginal("gcx_b")["yes"], abs=1e-9
    )
    assert twin.marginal("gcx_b", {"gcx_a": "yes"})["yes"] == pytest.approx(
        venue.marginal("gcx_b", {"gcx_a": "yes"})["yes"], abs=1e-9
    )

    # Orders + order_seq identical.
    assert twin._orders == venue._orders
    assert twin._order_seq == venue._order_seq
    assert twin.treasury_account_id == venue.treasury_account_id

    # Resolve the same variable on both; settled payouts + balances match.
    r1 = venue.resolve_variable("gcx_a", "yes")
    r2 = twin.resolve_variable("gcx_a", "yes")
    assert r1 == r2

    for order_a, order_b in zip(
        sorted(venue._orders, key=lambda o: o["orderId"]),
        sorted(twin._orders, key=lambda o: o["orderId"]),
    ):
        assert order_a["orderId"] == order_b["orderId"]
        assert order_a["status"] == order_b["status"]
        assert order_a.get("payout") == order_b.get("payout")

    for account_id in (t1, t2, venue.treasury_account_id):
        original = engine.get_account(account_id)
        restored = risk2.get_account(account_id)
        assert original.available_balance == restored.available_balance
        assert original.frozen_balance == restored.frozen_balance


def test_snapshot_orders_and_market_status_survive_a_resolve(tmp_path):
    engine = RiskEngine()
    venue = JointVenue(engine, TINY_SEEDS)
    t1 = _fund(engine, Decimal("1000"))
    venue.place_edit(t1, "gcx_a", "yes", 0.8)
    venue.resolve_variable("gcx_a", "yes")

    data = venue.snapshot()
    assert data["marketStatus"] == {
        "g1": {"status": "resolved", "resolvedOutcome": "yes"}
    }
    assert data["resolutions"] == {"gcx_a": "yes"}
    assert data["seedsSource"] == "<inline>"
    assert data["treasuryAccountId"] == venue.treasury_account_id
    assert data["orderSeq"] == 1

    twin = JointVenue.from_snapshot(data, engine, TINY_SEEDS)
    assert twin.get_market("g1")["status"] == "resolved"
    assert twin.get_market("g1")["resolvedOutcome"] == "yes"
    assert twin._resolutions == {"gcx_a": "yes"}


# -- (b) migration ---------------------------------------------------------


def test_version_2_snapshot_migrates_to_current_with_empty_venues(tmp_path):
    v2_state = {
        "version": 2,
        "counters": {},
        "accounts": [],
        "transactions": [],
        "markets": [],
        "auth": {"users": [], "local_users": []},
    }
    path = tmp_path / "v2_state.json"
    import json

    path.write_text(json.dumps(v2_state))

    risk, me, auth_store, tracked_repos, venues = load_snapshot(str(path))

    assert venues == {}
    assert tracked_repos == {}
    assert CURRENT_VERSION >= 4


# -- (c) fm structure mismatch fallback ------------------------------------


def test_fm_structure_mismatch_falls_back_to_seed_rebuild(caplog):
    engine = RiskEngine()
    venue = JointVenue(engine, TINY_SEEDS)
    t1 = _fund(engine, Decimal("1000"))
    venue.place_edit(t1, "gcx_a", "yes", 0.8)
    assert venue.marginal("gcx_a")["yes"] == pytest.approx(0.8, abs=1e-9)

    data = venue.snapshot()
    # Corrupt the fm snapshot: drop a required structural key.
    corrupted_fm = dict(data["fm"])
    del corrupted_fm["tradeScopes"]
    data = {**data, "fm": corrupted_fm}

    with caplog.at_level(logging.WARNING):
        twin = JointVenue.from_snapshot(data, engine, TINY_SEEDS)

    assert any(
        "fm" in rec.message.lower() or "rebuild" in rec.message.lower()
        for rec in caplog.records
    )
    # Traded prices lost -> back at seed prior.
    assert twin.marginal("gcx_a")["yes"] == pytest.approx(0.6, abs=1e-9)
    # Orders kept.
    assert len(twin._orders) == 1
    assert twin._orders[0]["orderId"] == "vb_1"
    assert twin._order_seq == 1


def test_fm_bad_format_string_falls_back_too(caplog):
    engine = RiskEngine()
    venue = JointVenue(engine, TINY_SEEDS)
    data = venue.snapshot()
    data = {**data, "fm": {**data["fm"], "format": "not-a-real-format"}}

    with caplog.at_level(logging.WARNING):
        twin = JointVenue.from_snapshot(data, engine, TINY_SEEDS)

    assert twin.marginal("gcx_a")["yes"] == pytest.approx(0.6, abs=1e-9)


def test_fm_fallback_replays_resolutions_so_child_marginal_stays_conditioned(caplog):
    """A snapshot taken AFTER gcx_a resolved to "yes", whose fm section then
    fails structure verification, must not just fall back to the seed-prior
    fm untouched — the fresh rebuild has to be re-conditioned on every
    already-recorded resolution, or a resolved variable's children read
    back at their unconditioned prior (0.62 here) instead of the correct
    conditioned value (0.9)."""
    engine = RiskEngine()
    venue = JointVenue(engine, TINY_SEEDS)
    venue.resolve_variable("gcx_a", "yes")

    data = venue.snapshot()
    corrupted_fm = dict(data["fm"])
    del corrupted_fm["tradeScopes"]
    data = {**data, "fm": corrupted_fm}

    with caplog.at_level(logging.WARNING):
        twin = JointVenue.from_snapshot(data, engine, TINY_SEEDS)

    assert twin.marginal("gcx_b")["yes"] == pytest.approx(0.9, abs=1e-6)


# -- (d) eager treasury check ------------------------------------------------


def test_from_snapshot_raises_when_treasury_account_missing():
    engine = RiskEngine()
    venue = JointVenue(engine, TINY_SEEDS)
    data = venue.snapshot()

    fresh_engine = RiskEngine()  # treasury account was never created here
    with pytest.raises(VenueError):
        JointVenue.from_snapshot(data, fresh_engine, TINY_SEEDS)
