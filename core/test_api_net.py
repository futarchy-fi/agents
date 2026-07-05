"""
Venue lifecycle wiring tests (Task B1).

Covers:
- App boots without EXCHANGE_SEEDS_PATH -> app.state.joint is None, health
  reports net.enabled == False.
- App boots with EXCHANGE_SEEDS_PATH -> app.state.joint is a fresh JointVenue
  built from the tiny seeds, health reports net.enabled/markets correctly.
- Restart fidelity: an edit placed directly on the venue object survives a
  _save() + rebuild-from-STATE_PATH round trip (JointVenue.from_snapshot).
- No-erase: booting WITHOUT seeds against a state file whose venues section
  is non-empty must not wipe that section out on the next _save().

Each test drives ``core.api.lifespan`` directly (the same pattern used by
``TestExpiredMarketReconciliation`` in core/test_api.py) so every scenario
gets full control over STATE_PATH and the EXCHANGE_SEEDS_PATH/JOINT_*
env vars without disturbing the module-level ``app`` object used by other
test modules.
"""

import json
import os
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

import core.api as api_module
from core.api import app
from core.models import reset_counters
from core.persistence import load_snapshot
from venues.joint.test_venue import TINY_SEEDS, THREE_VAR_SEEDS

ADMIN_HEADERS = {"Authorization": "Bearer test-admin-key"}


def _write_seeds(tmp_path) -> str:
    path = tmp_path / "seeds.json"
    path.write_text(json.dumps(TINY_SEEDS))
    return str(path)


async def _get_json(path: str) -> dict:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get(path)
        assert resp.status_code == 200
        return resp.json()


async def _get(path: str):
    """GET without asserting status — for tests exercising error paths."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.get(path)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Every test in this module manages its own STATE_PATH / seeds env."""
    monkeypatch.delenv("EXCHANGE_SEEDS_PATH", raising=False)
    monkeypatch.delenv("JOINT_LIQUIDITY", raising=False)
    monkeypatch.delenv("JOINT_MAX_WIDTH", raising=False)
    original_state_path = api_module.STATE_PATH
    yield
    api_module.STATE_PATH = original_state_path


class TestNoSeedsPath:
    async def test_app_without_seeds_env_has_no_joint(self, tmp_path):
        reset_counters()
        api_module.STATE_PATH = str(tmp_path / "state.json")

        async with api_module.lifespan(app):
            assert app.state.joint is None
            data = await _get_json("/v1/health")
            assert data["net"]["enabled"] is False
            assert data["net"]["markets"] == 0
            assert data["net"]["orders"] == 0


class TestWithSeedsPath:
    async def test_app_with_seeds_env_builds_joint(self, tmp_path, monkeypatch):
        reset_counters()
        seeds_path = _write_seeds(tmp_path)
        monkeypatch.setenv("EXCHANGE_SEEDS_PATH", seeds_path)
        api_module.STATE_PATH = str(tmp_path / "state.json")

        async with api_module.lifespan(app):
            assert app.state.joint is not None
            assert app.state.joint.market_ids() == ["g1", "g2"]

            data = await _get_json("/v1/health")
            assert data["net"]["enabled"] is True
            assert data["net"]["markets"] == 2
            assert data["net"]["orders"] == 0


class TestRestartFidelity:
    async def test_edit_survives_save_and_restart(self, tmp_path, monkeypatch):
        reset_counters()
        seeds_path = _write_seeds(tmp_path)
        monkeypatch.setenv("EXCHANGE_SEEDS_PATH", seeds_path)
        state_path = tmp_path / "state.json"
        api_module.STATE_PATH = str(state_path)

        async with api_module.lifespan(app):
            venue = app.state.joint
            assert venue is not None
            account = app.state.risk.create_account()
            app.state.risk.mint(account.id, Decimal("1000"))

            order = venue.place_edit(account.id, "gcx_a", "yes", 0.8)
            api_module._save()

            assert venue.marginal("gcx_a")["yes"] == pytest.approx(0.8, abs=1e-9)

        # Rebuild the app from the same STATE_PATH + seeds env.
        async with api_module.lifespan(app):
            restored = app.state.joint
            assert restored is not None
            assert len(restored._orders) == 1
            restored_order = restored._orders[0]
            assert restored_order["orderId"] == order["orderId"]

            # Marginal restored to the traded value, not the seed prior.
            assert restored.marginal("gcx_a")["yes"] == pytest.approx(
                0.8, abs=1e-6
            )

            # The account's frozen stake round-tripped through the RE
            # snapshot alongside the venue's own persisted order/lock.
            restored_account = app.state.risk.get_account(account.id)
            assert restored_account.frozen_balance == Decimal(
                restored_order["stake"]
            )

            data = await _get_json("/v1/health")
            assert data["net"]["enabled"] is True
            assert data["net"]["markets"] == 2
            assert data["net"]["orders"] == 1


class TestVenuesSectionNotErased:
    async def test_disabled_boot_preserves_existing_venues_section(
        self, tmp_path, monkeypatch
    ):
        """Booting WITHOUT seeds against a state file that HAS venue data,
        then triggering a save, must NOT erase the venues section."""
        reset_counters()
        seeds_path = _write_seeds(tmp_path)
        state_path = tmp_path / "state.json"
        api_module.STATE_PATH = str(state_path)

        # First boot: venue enabled, place an edit, save -> non-empty
        # venues section on disk.
        monkeypatch.setenv("EXCHANGE_SEEDS_PATH", seeds_path)
        async with api_module.lifespan(app):
            venue = app.state.joint
            account = app.state.risk.create_account()
            app.state.risk.mint(account.id, Decimal("1000"))
            venue.place_edit(account.id, "gcx_a", "yes", 0.8)
            api_module._save()

        _, _, _, _, venues_after_first_save = load_snapshot(str(state_path))
        assert venues_after_first_save.get("joint") is not None
        assert len(venues_after_first_save["joint"]["orders"]) == 1

        # Second boot: seeds path unset -> joint disabled, but the state
        # file's venues section must be preserved on the next save.
        monkeypatch.delenv("EXCHANGE_SEEDS_PATH", raising=False)
        async with api_module.lifespan(app):
            assert app.state.joint is None

            data = await _get_json("/v1/health")
            assert data["net"]["enabled"] is False

            # Trigger an unrelated save (e.g. minting) with the venue off.
            new_account = app.state.risk.create_account()
            app.state.risk.mint(new_account.id, Decimal("5"))
            api_module._save()

        _, _, _, _, venues_after_second_save = load_snapshot(str(state_path))
        assert venues_after_second_save.get("joint") is not None
        assert len(venues_after_second_save["joint"]["orders"]) == 1
        assert venues_after_second_save == venues_after_first_save


# ---------------------------------------------------------------------------
# Task B2: net read endpoints
# ---------------------------------------------------------------------------

class TestNetMarketsList:
    async def test_list_returns_both_markets_with_live_marginals(
        self, tmp_path, monkeypatch
    ):
        reset_counters()
        seeds_path = _write_seeds(tmp_path)
        monkeypatch.setenv("EXCHANGE_SEEDS_PATH", seeds_path)
        api_module.STATE_PATH = str(tmp_path / "state.json")

        async with api_module.lifespan(app):
            data = await _get_json("/v1/net/markets")
            assert data["count"] == 2
            by_id = {m["id"]: m for m in data["markets"]}
            assert set(by_id) == {"g1", "g2"}

            g1 = by_id["g1"]
            assert g1["variableId"] == "gcx_a"
            assert g1["marginals"]["yes"] == pytest.approx(0.6, abs=1e-6)
            assert g1["parents"] == []

            g2 = by_id["g2"]
            assert g2["variableId"] == "gcx_b"
            assert g2["parents"] == ["gcx_a"]
            assert g2["marginals"]["yes"] == pytest.approx(
                0.6 * 0.9 + 0.4 * 0.2, abs=1e-6
            )


class TestNetMarketDetail:
    async def test_detail_known_market(self, tmp_path, monkeypatch):
        reset_counters()
        seeds_path = _write_seeds(tmp_path)
        monkeypatch.setenv("EXCHANGE_SEEDS_PATH", seeds_path)
        api_module.STATE_PATH = str(tmp_path / "state.json")

        async with api_module.lifespan(app):
            data = await _get_json("/v1/net/markets/g2")
            assert data["id"] == "g2"
            assert data["parents"] == ["gcx_a"]

    async def test_detail_unknown_market_404s(self, tmp_path, monkeypatch):
        reset_counters()
        seeds_path = _write_seeds(tmp_path)
        monkeypatch.setenv("EXCHANGE_SEEDS_PATH", seeds_path)
        api_module.STATE_PATH = str(tmp_path / "state.json")

        async with api_module.lifespan(app):
            resp = await _get("/v1/net/markets/nope")
            assert resp.status_code == 404
            assert resp.json()["error"]["code"] == "unknown_market"


class TestNetMarginal:
    async def test_marginal_with_context(self, tmp_path, monkeypatch):
        reset_counters()
        seeds_path = _write_seeds(tmp_path)
        monkeypatch.setenv("EXCHANGE_SEEDS_PATH", seeds_path)
        api_module.STATE_PATH = str(tmp_path / "state.json")

        async with api_module.lifespan(app):
            data = await _get_json("/v1/net/marginal?variable=gcx_b&context=gcx_a%3Dyes")
            assert data["variable"] == "gcx_b"
            assert data["context"] == {"gcx_a": "yes"}
            assert data["marginal"]["yes"] == pytest.approx(0.9, abs=1e-6)

    async def test_marginal_unknown_variable_404s(self, tmp_path, monkeypatch):
        reset_counters()
        seeds_path = _write_seeds(tmp_path)
        monkeypatch.setenv("EXCHANGE_SEEDS_PATH", seeds_path)
        api_module.STATE_PATH = str(tmp_path / "state.json")

        async with api_module.lifespan(app):
            resp = await _get("/v1/net/marginal?variable=nope")
            assert resp.status_code == 404
            assert resp.json()["error"]["code"] == "unknown_market"

    async def test_marginal_malformed_context_400s(self, tmp_path, monkeypatch):
        reset_counters()
        seeds_path = _write_seeds(tmp_path)
        monkeypatch.setenv("EXCHANGE_SEEDS_PATH", seeds_path)
        api_module.STATE_PATH = str(tmp_path / "state.json")

        async with api_module.lifespan(app):
            resp = await _get("/v1/net/marginal?variable=gcx_b&context=gcx_a-yes")
            assert resp.status_code == 400
            assert resp.json()["error"]["code"] == "invalid_context"

    async def test_marginal_contradicted_context_409s(self, tmp_path, monkeypatch):
        reset_counters()
        seeds_path = _write_seeds(tmp_path)
        monkeypatch.setenv("EXCHANGE_SEEDS_PATH", seeds_path)
        api_module.STATE_PATH = str(tmp_path / "state.json")

        async with api_module.lifespan(app):
            # Resolve gcx_a to "no", then query a context that assumes "yes".
            app.state.joint.resolve_variable("gcx_a", "no")
            resp = await _get("/v1/net/marginal?variable=gcx_b&context=gcx_a%3Dyes")
            assert resp.status_code == 409
            assert resp.json()["error"]["code"] == "context_contradicted"

    async def test_marginal_multi_variable_context(self, tmp_path, monkeypatch):
        reset_counters()
        # Write THREE_VAR_SEEDS (includes independent gcx_c) to tmp file.
        path = tmp_path / "seeds.json"
        path.write_text(json.dumps(THREE_VAR_SEEDS))
        seeds_path = str(path)
        monkeypatch.setenv("EXCHANGE_SEEDS_PATH", seeds_path)
        api_module.STATE_PATH = str(tmp_path / "state.json")

        async with api_module.lifespan(app):
            # Query gcx_b with two-variable context: gcx_a=yes and gcx_c=no.
            # Since gcx_c is independent, it doesn't affect gcx_b|gcx_a.
            data = await _get_json("/v1/net/marginal?variable=gcx_b&context=gcx_a%3Dyes|gcx_c%3Dno")
            assert data["variable"] == "gcx_b"
            assert data["context"] == {"gcx_a": "yes", "gcx_c": "no"}
            # gcx_b given gcx_a=yes should be 0.9 (independent of gcx_c).
            assert data["marginal"]["yes"] == pytest.approx(0.9, abs=1e-6)


class TestNetRoutesDisabled:
    async def test_all_three_routes_503_when_venue_disabled(self, tmp_path):
        reset_counters()
        api_module.STATE_PATH = str(tmp_path / "state.json")

        async with api_module.lifespan(app):
            assert app.state.joint is None

            for path in (
                "/v1/net/markets",
                "/v1/net/markets/g1",
                "/v1/net/marginal?variable=gcx_a",
            ):
                resp = await _get(path)
                assert resp.status_code == 503, path
                assert resp.json()["error"]["code"] == "net_venue_disabled", path
