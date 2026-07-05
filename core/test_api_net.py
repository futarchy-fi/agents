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
from core.api import app, _authenticate_github_identity
from core.models import reset_counters
from core.persistence import load_snapshot
from venues.joint.msr import payout_for_edit, stake_for_edit
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


async def _get(path: str, headers: dict | None = None):
    """GET without asserting status — for tests exercising error paths."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.get(path, headers=headers or {})


async def _post(path: str, body: dict, headers: dict | None = None):
    """POST without asserting status — for tests exercising error paths."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.post(path, json=body, headers=headers or {})


async def _authed_user(github_id: int = 1, login: str = "netuser") -> tuple[str, int]:
    """Create a user (minted INITIAL_CREDITS) and return (api_key, account_id).

    Mirrors ``_mock_auth`` in core/test_api.py, adapted to this module's
    "drive lifespan directly" pattern (no ``client`` fixture here).
    """
    auth = await _authenticate_github_identity({"id": github_id, "login": login})
    return auth.api_key, auth.account_id


def _headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}"}


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


# ---------------------------------------------------------------------------
# Task B3: net trading endpoints (authed)
# ---------------------------------------------------------------------------

class TestNetOrderPreview:
    async def test_preview_matches_msr_and_leaves_state_untouched(
        self, tmp_path, monkeypatch
    ):
        reset_counters()
        seeds_path = _write_seeds(tmp_path)
        monkeypatch.setenv("EXCHANGE_SEEDS_PATH", seeds_path)
        api_module.STATE_PATH = str(tmp_path / "state.json")

        async with api_module.lifespan(app):
            api_key, account_id = await _authed_user()
            before = app.state.joint.marginal("gcx_a")["yes"]
            expected_stake = stake_for_edit(Decimal("50"), before, 0.8)

            resp = await _post(
                "/v1/net/orders/preview",
                {"variableId": "gcx_a", "outcomeId": "yes", "target": 0.8},
                headers=_headers(api_key),
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["stake"] == str(expected_stake)
            assert data["before"] == pytest.approx(before, abs=1e-9)
            assert data["after"] == pytest.approx(0.8, abs=1e-9)
            assert data["b"] == "50"

            # No mutation: marginal and balances unchanged.
            assert app.state.joint.marginal("gcx_a")["yes"] == pytest.approx(
                before, abs=1e-9
            )
            account = app.state.risk.get_account(account_id)
            assert account.available_balance == Decimal("1000")
            assert account.frozen_balance == Decimal("0")
            assert len(app.state.joint._orders) == 0


class TestNetOrderPlace:
    async def test_place_freezes_exact_stake_and_moves_marginal(
        self, tmp_path, monkeypatch
    ):
        reset_counters()
        seeds_path = _write_seeds(tmp_path)
        monkeypatch.setenv("EXCHANGE_SEEDS_PATH", seeds_path)
        api_module.STATE_PATH = str(tmp_path / "state.json")

        async with api_module.lifespan(app):
            api_key, account_id = await _authed_user()
            before = app.state.joint.marginal("gcx_a")["yes"]
            expected_stake = stake_for_edit(Decimal("50"), before, 0.8)

            resp = await _post(
                "/v1/net/orders",
                {"variableId": "gcx_a", "outcomeId": "yes", "target": 0.8},
                headers=_headers(api_key),
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["orderId"] == "vb_1"
            assert data["accountId"] == account_id
            assert data["stake"] == str(expected_stake)
            assert data["balance"]["frozen"] == str(expected_stake)
            assert data["balance"]["available"] == str(
                Decimal("1000") - expected_stake
            )

            # Balance read back directly off the account (not just trusting
            # the response echo).
            account = app.state.risk.get_account(account_id)
            assert account.frozen_balance == expected_stake
            assert account.available_balance == Decimal("1000") - expected_stake

            # And via /v1/me over HTTP.
            me_resp = await _get("/v1/me", headers=_headers(api_key))
            assert me_resp.status_code == 200
            me_data = me_resp.json()
            assert me_data["frozen"] == str(expected_stake)
            assert me_data["available"] == str(Decimal("1000") - expected_stake)

            # Marginal moved to the target.
            assert app.state.joint.marginal("gcx_a")["yes"] == pytest.approx(
                0.8, abs=1e-6
            )

    async def test_place_response_never_leaks_another_accounts_data(
        self, tmp_path, monkeypatch
    ):
        reset_counters()
        seeds_path = _write_seeds(tmp_path)
        monkeypatch.setenv("EXCHANGE_SEEDS_PATH", seeds_path)
        api_module.STATE_PATH = str(tmp_path / "state.json")

        async with api_module.lifespan(app):
            key1, acc1 = await _authed_user(github_id=1, login="u1")
            key2, acc2 = await _authed_user(github_id=2, login="u2")
            assert acc1 != acc2

            resp1 = await _post(
                "/v1/net/orders",
                {"variableId": "gcx_a", "outcomeId": "yes", "target": 0.7},
                headers=_headers(key1),
            )
            assert resp1.status_code == 200
            assert resp1.json()["accountId"] == acc1

            resp2 = await _post(
                "/v1/net/orders",
                {"variableId": "gcx_b", "outcomeId": "yes", "target": 0.5},
                headers=_headers(key2),
            )
            assert resp2.status_code == 200
            assert resp2.json()["accountId"] == acc2
            assert resp2.json()["accountId"] != acc1


class TestNetOrderInsufficientCredits:
    async def test_place_with_tiny_balance_400s_with_zero_state_change(
        self, tmp_path, monkeypatch
    ):
        reset_counters()
        seeds_path = _write_seeds(tmp_path)
        monkeypatch.setenv("EXCHANGE_SEEDS_PATH", seeds_path)
        api_module.STATE_PATH = str(tmp_path / "state.json")

        async with api_module.lifespan(app):
            api_key, account_id = await _authed_user()
            account = app.state.risk.get_account(account_id)
            account.available_balance = Decimal("0.01")
            before = app.state.joint.marginal("gcx_a")["yes"]

            resp = await _post(
                "/v1/net/orders",
                {"variableId": "gcx_a", "outcomeId": "yes", "target": 0.8},
                headers=_headers(api_key),
            )
            assert resp.status_code == 400
            assert resp.json()["error"]["code"] == "insufficient_credits"

            # Zero state change: no lock, no order, no marginal move.
            assert account.available_balance == Decimal("0.01")
            assert account.frozen_balance == Decimal("0")
            assert app.state.joint.marginal("gcx_a")["yes"] == pytest.approx(
                before, abs=1e-9
            )
            assert len(app.state.joint._orders) == 0


class TestNetOrderTargetClamp:
    async def test_target_above_clamp_400s_without_venue_call(
        self, tmp_path, monkeypatch
    ):
        reset_counters()
        seeds_path = _write_seeds(tmp_path)
        monkeypatch.setenv("EXCHANGE_SEEDS_PATH", seeds_path)
        api_module.STATE_PATH = str(tmp_path / "state.json")

        async with api_module.lifespan(app):
            api_key, account_id = await _authed_user()
            before = app.state.joint.marginal("gcx_a")["yes"]

            resp = await _post(
                "/v1/net/orders",
                {"variableId": "gcx_a", "outcomeId": "yes", "target": 0.9995},
                headers=_headers(api_key),
            )
            assert resp.status_code == 400
            assert resp.json()["error"]["code"] == "invalid_target"

            # Venue never touched: no order created, marginal unmoved.
            assert len(app.state.joint._orders) == 0
            assert app.state.joint.marginal("gcx_a")["yes"] == pytest.approx(
                before, abs=1e-9
            )

    async def test_target_below_clamp_400s_on_preview_too(
        self, tmp_path, monkeypatch
    ):
        reset_counters()
        seeds_path = _write_seeds(tmp_path)
        monkeypatch.setenv("EXCHANGE_SEEDS_PATH", seeds_path)
        api_module.STATE_PATH = str(tmp_path / "state.json")

        async with api_module.lifespan(app):
            api_key, account_id = await _authed_user()

            resp = await _post(
                "/v1/net/orders/preview",
                {"variableId": "gcx_a", "outcomeId": "yes", "target": 0.0005},
                headers=_headers(api_key),
            )
            assert resp.status_code == 400
            assert resp.json()["error"]["code"] == "invalid_target"
            assert len(app.state.joint._orders) == 0


class TestNetOrderContextValidation:
    """Fund-freeze hole (final-review item 1): a context key that names no
    real variable used to be silently ignored by fm.marginal/
    fm.trade_to_probability, so an edit would place successfully with a
    remainingContext entry no future resolve_variable call could ever
    match — the order (and its frozen stake) would sit in
    awaiting_context forever. Both /v1/net/orders and /v1/net/orders/preview
    must reject before touching balances or the marginal.
    """

    async def test_place_unknown_context_key_404s_with_zero_state_change(
        self, tmp_path, monkeypatch
    ):
        reset_counters()
        seeds_path = _write_seeds(tmp_path)
        monkeypatch.setenv("EXCHANGE_SEEDS_PATH", seeds_path)
        api_module.STATE_PATH = str(tmp_path / "state.json")

        async with api_module.lifespan(app):
            api_key, account_id = await _authed_user()
            before = app.state.joint.marginal("gcx_b")["yes"]

            resp = await _post(
                "/v1/net/orders",
                {
                    "variableId": "gcx_b", "outcomeId": "yes", "target": 0.5,
                    "context": {"nope": "yes"},
                },
                headers=_headers(api_key),
            )
            assert resp.status_code == 404
            assert resp.json()["error"]["code"] == "unknown_market"

            account = app.state.risk.get_account(account_id)
            assert account.frozen_balance == Decimal("0")
            assert account.available_balance == Decimal("1000")
            assert len(app.state.joint._orders) == 0
            assert app.state.joint.marginal("gcx_b")["yes"] == pytest.approx(
                before, abs=1e-9
            )

    async def test_preview_unknown_context_key_404s_with_zero_state_change(
        self, tmp_path, monkeypatch
    ):
        reset_counters()
        seeds_path = _write_seeds(tmp_path)
        monkeypatch.setenv("EXCHANGE_SEEDS_PATH", seeds_path)
        api_module.STATE_PATH = str(tmp_path / "state.json")

        async with api_module.lifespan(app):
            api_key, account_id = await _authed_user()

            resp = await _post(
                "/v1/net/orders/preview",
                {
                    "variableId": "gcx_b", "outcomeId": "yes", "target": 0.5,
                    "context": {"nope": "yes"},
                },
                headers=_headers(api_key),
            )
            assert resp.status_code == 404
            assert resp.json()["error"]["code"] == "unknown_market"
            assert len(app.state.joint._orders) == 0

    async def test_place_invalid_context_outcome_400s_with_zero_state_change(
        self, tmp_path, monkeypatch
    ):
        reset_counters()
        seeds_path = _write_seeds(tmp_path)
        monkeypatch.setenv("EXCHANGE_SEEDS_PATH", seeds_path)
        api_module.STATE_PATH = str(tmp_path / "state.json")

        async with api_module.lifespan(app):
            api_key, account_id = await _authed_user()
            before = app.state.joint.marginal("gcx_b")["yes"]

            resp = await _post(
                "/v1/net/orders",
                {
                    "variableId": "gcx_b", "outcomeId": "yes", "target": 0.5,
                    "context": {"gcx_a": "maybe"},
                },
                headers=_headers(api_key),
            )
            assert resp.status_code == 400
            assert resp.json()["error"]["code"] == "invalid_outcome"

            account = app.state.risk.get_account(account_id)
            assert account.frozen_balance == Decimal("0")
            assert account.available_balance == Decimal("1000")
            assert len(app.state.joint._orders) == 0
            assert app.state.joint.marginal("gcx_b")["yes"] == pytest.approx(
                before, abs=1e-9
            )

    async def test_preview_invalid_context_outcome_400s_with_zero_state_change(
        self, tmp_path, monkeypatch
    ):
        reset_counters()
        seeds_path = _write_seeds(tmp_path)
        monkeypatch.setenv("EXCHANGE_SEEDS_PATH", seeds_path)
        api_module.STATE_PATH = str(tmp_path / "state.json")

        async with api_module.lifespan(app):
            api_key, account_id = await _authed_user()

            resp = await _post(
                "/v1/net/orders/preview",
                {
                    "variableId": "gcx_b", "outcomeId": "yes", "target": 0.5,
                    "context": {"gcx_a": "maybe"},
                },
                headers=_headers(api_key),
            )
            assert resp.status_code == 400
            assert resp.json()["error"]["code"] == "invalid_outcome"
            assert len(app.state.joint._orders) == 0


class TestNetOrderResolvedVariable:
    async def test_edit_on_resolved_variable_409s(self, tmp_path, monkeypatch):
        reset_counters()
        seeds_path = _write_seeds(tmp_path)
        monkeypatch.setenv("EXCHANGE_SEEDS_PATH", seeds_path)
        api_module.STATE_PATH = str(tmp_path / "state.json")

        async with api_module.lifespan(app):
            api_key, account_id = await _authed_user()
            # Resolve directly on the venue object — admin routes are B4.
            app.state.joint.resolve_variable("gcx_a", "yes")

            resp = await _post(
                "/v1/net/orders",
                {"variableId": "gcx_a", "outcomeId": "yes", "target": 0.8},
                headers=_headers(api_key),
            )
            assert resp.status_code == 409
            assert resp.json()["error"]["code"] == "market_closed"

            preview_resp = await _post(
                "/v1/net/orders/preview",
                {"variableId": "gcx_a", "outcomeId": "yes", "target": 0.8},
                headers=_headers(api_key),
            )
            assert preview_resp.status_code == 409
            assert preview_resp.json()["error"]["code"] == "market_closed"


class TestNetOrderAuth:
    async def test_unauthenticated_401s_on_all_three_routes(
        self, tmp_path, monkeypatch
    ):
        reset_counters()
        seeds_path = _write_seeds(tmp_path)
        monkeypatch.setenv("EXCHANGE_SEEDS_PATH", seeds_path)
        api_module.STATE_PATH = str(tmp_path / "state.json")

        async with api_module.lifespan(app):
            body = {"variableId": "gcx_a", "outcomeId": "yes", "target": 0.8}

            resp = await _post("/v1/net/orders/preview", body)
            assert resp.status_code == 401

            resp = await _post("/v1/net/orders", body)
            assert resp.status_code == 401

            resp = await _get("/v1/net/orders/mine")
            assert resp.status_code == 401

            # A bad Bearer key is likewise rejected.
            bad = {"Authorization": "Bearer not-a-real-key"}
            resp = await _post("/v1/net/orders/preview", body, headers=bad)
            assert resp.status_code == 401
            resp = await _post("/v1/net/orders", body, headers=bad)
            assert resp.status_code == 401
            resp = await _get("/v1/net/orders/mine", headers=bad)
            assert resp.status_code == 401


class TestNetOrderVenueDisabled:
    async def test_all_three_routes_503_when_venue_disabled(self, tmp_path):
        reset_counters()
        api_module.STATE_PATH = str(tmp_path / "state.json")

        async with api_module.lifespan(app):
            assert app.state.joint is None
            api_key, account_id = await _authed_user()
            body = {"variableId": "gcx_a", "outcomeId": "yes", "target": 0.8}

            resp = await _post(
                "/v1/net/orders/preview", body, headers=_headers(api_key)
            )
            assert resp.status_code == 503
            assert resp.json()["error"]["code"] == "net_venue_disabled"

            resp = await _post("/v1/net/orders", body, headers=_headers(api_key))
            assert resp.status_code == 503
            assert resp.json()["error"]["code"] == "net_venue_disabled"

            resp = await _get("/v1/net/orders/mine", headers=_headers(api_key))
            assert resp.status_code == 503
            assert resp.json()["error"]["code"] == "net_venue_disabled"


class TestNetOrdersMine:
    async def test_each_user_sees_only_own_orders_newest_first(
        self, tmp_path, monkeypatch
    ):
        reset_counters()
        seeds_path = _write_seeds(tmp_path)
        monkeypatch.setenv("EXCHANGE_SEEDS_PATH", seeds_path)
        api_module.STATE_PATH = str(tmp_path / "state.json")

        async with api_module.lifespan(app):
            key1, acc1 = await _authed_user(github_id=1, login="u1")
            key2, acc2 = await _authed_user(github_id=2, login="u2")

            r1 = await _post(
                "/v1/net/orders",
                {"variableId": "gcx_a", "outcomeId": "yes", "target": 0.7},
                headers=_headers(key1),
            )
            assert r1.status_code == 200
            r2 = await _post(
                "/v1/net/orders",
                {"variableId": "gcx_b", "outcomeId": "yes", "target": 0.5},
                headers=_headers(key2),
            )
            assert r2.status_code == 200
            r3 = await _post(
                "/v1/net/orders",
                {"variableId": "gcx_a", "outcomeId": "no", "target": 0.55},
                headers=_headers(key1),
            )
            assert r3.status_code == 200

            resp1 = await _get("/v1/net/orders/mine", headers=_headers(key1))
            assert resp1.status_code == 200
            data1 = resp1.json()
            assert [o["orderId"] for o in data1["orders"]] == ["vb_3", "vb_1"]
            assert all(o["accountId"] == acc1 for o in data1["orders"])

            resp2 = await _get("/v1/net/orders/mine", headers=_headers(key2))
            assert resp2.status_code == 200
            data2 = resp2.json()
            assert [o["orderId"] for o in data2["orders"]] == ["vb_2"]
            assert data2["orders"][0]["accountId"] == acc2


# ---------------------------------------------------------------------------
# Task B4: net admin endpoints (resolve/void)
# ---------------------------------------------------------------------------

B = Decimal("50")


class TestNetAdminAuth:
    async def test_resolve_and_void_reject_non_admin(self, tmp_path, monkeypatch):
        reset_counters()
        seeds_path = _write_seeds(tmp_path)
        monkeypatch.setenv("EXCHANGE_SEEDS_PATH", seeds_path)
        api_module.STATE_PATH = str(tmp_path / "state.json")

        async with api_module.lifespan(app):
            api_key, _account_id = await _authed_user()

            # No key at all -> 401, same as existing /v1/admin/* routes.
            resp = await _post(
                "/v1/net/markets/g1/resolve", {"outcome": "yes"}
            )
            assert resp.status_code == 401

            resp = await _post("/v1/net/markets/g1/void", {})
            assert resp.status_code == 401

            # A regular (non-admin) user key -> 403 admin_required, matching
            # the existing admin routes' behavior exactly.
            resp = await _post(
                "/v1/net/markets/g1/resolve", {"outcome": "yes"},
                headers=_headers(api_key),
            )
            assert resp.status_code == 403
            assert resp.json()["error"]["code"] == "admin_required"

            resp = await _post(
                "/v1/net/markets/g1/void", {}, headers=_headers(api_key)
            )
            assert resp.status_code == 403
            assert resp.json()["error"]["code"] == "admin_required"

            # Zero state change: the market is untouched by the rejected calls.
            assert app.state.joint.get_market("g1")["marginals"] == {
                "yes": pytest.approx(0.6, abs=1e-9),
                "no": pytest.approx(0.4, abs=1e-9),
            }


class TestNetAdminResolve:
    async def test_resolve_settles_a_winning_order_and_pays_out(
        self, tmp_path, monkeypatch
    ):
        reset_counters()
        seeds_path = _write_seeds(tmp_path)
        monkeypatch.setenv("EXCHANGE_SEEDS_PATH", seeds_path)
        api_module.STATE_PATH = str(tmp_path / "state.json")

        async with api_module.lifespan(app):
            api_key, account_id = await _authed_user()
            before = app.state.joint.marginal("gcx_a")["yes"]

            place_resp = await _post(
                "/v1/net/orders",
                {"variableId": "gcx_a", "outcomeId": "yes", "target": 0.8},
                headers=_headers(api_key),
            )
            assert place_resp.status_code == 200
            order_id = place_resp.json()["orderId"]
            stake = Decimal(place_resp.json()["stake"])

            account = app.state.risk.get_account(account_id)
            available_before_resolve = account.available_balance
            assert account.frozen_balance == stake

            expected_payout = payout_for_edit(B, before, 0.8, won=True)

            resp = await _post(
                "/v1/net/markets/g1/resolve",
                {"outcome": "yes"},
                headers=ADMIN_HEADERS,
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["marketId"] == "g1"
            assert data["variableId"] == "gcx_a"
            assert data["outcome"] == "yes"
            assert data["settled"] == [order_id]
            assert data["calledOff"] == []
            assert Decimal(data["treasuryDelta"]) == -expected_payout

            # The order's frozen stake is released and the account is
            # credited exactly the msr payout — nothing more, nothing less.
            assert account.frozen_balance == Decimal("0")
            assert account.available_balance == (
                available_before_resolve + stake + expected_payout
            )

    async def test_resolve_unknown_market_404s(self, tmp_path, monkeypatch):
        reset_counters()
        seeds_path = _write_seeds(tmp_path)
        monkeypatch.setenv("EXCHANGE_SEEDS_PATH", seeds_path)
        api_module.STATE_PATH = str(tmp_path / "state.json")

        async with api_module.lifespan(app):
            resp = await _post(
                "/v1/net/markets/nope/resolve",
                {"outcome": "yes"},
                headers=ADMIN_HEADERS,
            )
            assert resp.status_code == 404
            assert resp.json()["error"]["code"] == "unknown_market"

            resp = await _post(
                "/v1/net/markets/nope/void", {}, headers=ADMIN_HEADERS
            )
            assert resp.status_code == 404
            assert resp.json()["error"]["code"] == "unknown_market"

    async def test_resolve_invalid_outcome_400s(self, tmp_path, monkeypatch):
        # Final-review item 4: resolve_variable's own outcome validation
        # raises InvalidOutcome (not a bare VenueError), which must map to
        # 400 invalid_outcome here rather than the generic trade_rejected.
        reset_counters()
        seeds_path = _write_seeds(tmp_path)
        monkeypatch.setenv("EXCHANGE_SEEDS_PATH", seeds_path)
        api_module.STATE_PATH = str(tmp_path / "state.json")

        async with api_module.lifespan(app):
            resp = await _post(
                "/v1/net/markets/g1/resolve",
                {"outcome": "maybe"},
                headers=ADMIN_HEADERS,
            )
            assert resp.status_code == 400
            assert resp.json()["error"]["code"] == "invalid_outcome"

            # Zero state change: the market is untouched by the rejected call.
            assert app.state.joint.get_market("g1")["marginals"] == {
                "yes": pytest.approx(0.6, abs=1e-9),
                "no": pytest.approx(0.4, abs=1e-9),
            }

    async def test_venue_disabled_503s(self, tmp_path):
        reset_counters()
        api_module.STATE_PATH = str(tmp_path / "state.json")

        async with api_module.lifespan(app):
            assert app.state.joint is None

            resp = await _post(
                "/v1/net/markets/g1/resolve",
                {"outcome": "yes"},
                headers=ADMIN_HEADERS,
            )
            assert resp.status_code == 503
            assert resp.json()["error"]["code"] == "net_venue_disabled"

            resp = await _post(
                "/v1/net/markets/g1/void", {}, headers=ADMIN_HEADERS
            )
            assert resp.status_code == 503
            assert resp.json()["error"]["code"] == "net_venue_disabled"


class TestNetAdminDoubleResolveAndVoid:
    async def test_double_resolve_and_resolve_after_void_409s(
        self, tmp_path, monkeypatch
    ):
        reset_counters()
        seeds_path = _write_seeds(tmp_path)
        monkeypatch.setenv("EXCHANGE_SEEDS_PATH", seeds_path)
        api_module.STATE_PATH = str(tmp_path / "state.json")

        async with api_module.lifespan(app):
            resp = await _post(
                "/v1/net/markets/g1/resolve",
                {"outcome": "yes"},
                headers=ADMIN_HEADERS,
            )
            assert resp.status_code == 200

            # Double-resolve.
            resp = await _post(
                "/v1/net/markets/g1/resolve",
                {"outcome": "yes"},
                headers=ADMIN_HEADERS,
            )
            assert resp.status_code == 409
            assert resp.json()["error"]["code"] == "market_closed"

            # Resolve after void (on a different market, g2 -> gcx_b).
            resp = await _post(
                "/v1/net/markets/g2/void", {}, headers=ADMIN_HEADERS
            )
            assert resp.status_code == 200

            resp = await _post(
                "/v1/net/markets/g2/resolve",
                {"outcome": "yes"},
                headers=ADMIN_HEADERS,
            )
            assert resp.status_code == 409
            assert resp.json()["error"]["code"] == "market_closed"


class TestNetAdminVoid:
    async def test_void_refunds_staked_order_in_full(self, tmp_path, monkeypatch):
        reset_counters()
        seeds_path = _write_seeds(tmp_path)
        monkeypatch.setenv("EXCHANGE_SEEDS_PATH", seeds_path)
        api_module.STATE_PATH = str(tmp_path / "state.json")

        async with api_module.lifespan(app):
            api_key, account_id = await _authed_user()
            account = app.state.risk.get_account(account_id)
            balance_before_stake = account.available_balance

            place_resp = await _post(
                "/v1/net/orders",
                {"variableId": "gcx_a", "outcomeId": "yes", "target": 0.8},
                headers=_headers(api_key),
            )
            assert place_resp.status_code == 200
            order_id = place_resp.json()["orderId"]
            assert account.frozen_balance > Decimal("0")
            assert account.available_balance < balance_before_stake

            resp = await _post(
                "/v1/net/markets/g1/void", {}, headers=ADMIN_HEADERS
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["marketId"] == "g1"
            assert data["variableId"] == "gcx_a"
            assert data["calledOff"] == [order_id]

            # Balance restored exactly to its pre-stake value: no gain, no loss.
            assert account.frozen_balance == Decimal("0")
            assert account.available_balance == balance_before_stake


class TestNetAdminPersistence:
    async def test_resolve_persists_across_restart(self, tmp_path, monkeypatch):
        reset_counters()
        seeds_path = _write_seeds(tmp_path)
        monkeypatch.setenv("EXCHANGE_SEEDS_PATH", seeds_path)
        state_path = tmp_path / "state.json"
        api_module.STATE_PATH = str(state_path)

        async with api_module.lifespan(app):
            api_key, account_id = await _authed_user()

            place_resp = await _post(
                "/v1/net/orders",
                {"variableId": "gcx_a", "outcomeId": "yes", "target": 0.8},
                headers=_headers(api_key),
            )
            assert place_resp.status_code == 200

            resp = await _post(
                "/v1/net/markets/g1/resolve",
                {"outcome": "yes"},
                headers=ADMIN_HEADERS,
            )
            assert resp.status_code == 200

        # Rebuild the app from the same STATE_PATH + seeds env.
        async with api_module.lifespan(app):
            data = await _get_json("/v1/net/markets/g1")
            assert data["status"] == "resolved"

            restored = app.state.joint
            assert restored is not None
            # No open orders resurrect on the resolved variable.
            assert all(
                o["status"] != "open"
                for o in restored._orders
                if o["variableId"] == "gcx_a"
            )
            settled = [
                o for o in restored._orders
                if o["variableId"] == "gcx_a"
            ]
            assert len(settled) == 1
            assert settled[0]["status"] == "settled"
