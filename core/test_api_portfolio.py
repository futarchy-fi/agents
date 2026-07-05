"""
Task B5: signup credits grant, /v1/me/net portfolio, /v1/leaderboard.

Follows the "drive lifespan directly" pattern established by
core/test_api_net.py (rather than test_api.py's manual client fixture) so
each test controls STATE_PATH / EXCHANGE_SEEDS_PATH precisely. The very
first test runs in a clean SUBPROCESS with INITIAL_CREDITS removed from
the environment, so it proves the module *default* (not core/test_api.py's
explicit env override, which happens to already be "1000") is what
actually mints on signup — an in-process importlib.reload of core.api
would instead leave a second FastAPI ``app`` object behind and desync
every other test module's ``from core.api import app`` binding.
"""

import json
import os
import subprocess
import sys
import textwrap
from decimal import Decimal
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

# Set before core.middleware's import-time ADMIN_KEY read, exactly like
# core/test_api.py (no-op in a full-suite run where test_api.py imported
# first; required for a standalone run of this file).
os.environ["FUTARCHY_ADMIN_KEY"] = "test-admin-key"

import core.api as api_module
from core.api import app, _authenticate_github_identity
from core.models import reset_counters
from venues.joint.msr import payout_for_edit
from venues.joint.test_venue import TINY_SEEDS

REPO_ROOT = Path(__file__).resolve().parents[1]

ADMIN_HEADERS = {"Authorization": "Bearer test-admin-key"}

B = Decimal("50")  # JOINT_LIQUIDITY default, matches core/test_api_net.py


def _write_seeds(tmp_path) -> str:
    path = tmp_path / "seeds.json"
    path.write_text(json.dumps(TINY_SEEDS))
    return str(path)


async def _get(path: str, headers: dict | None = None, target_app=app):
    transport = ASGITransport(app=target_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.get(path, headers=headers or {})


async def _post(path: str, body: dict, headers: dict | None = None, target_app=app):
    transport = ASGITransport(app=target_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.post(path, json=body, headers=headers or {})


async def _authed_user(github_id: int = 1, login: str = "portfoliouser",
                       identity_fn=_authenticate_github_identity) -> tuple[str, int]:
    auth = await identity_fn({"id": github_id, "login": login})
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


# ---------------------------------------------------------------------------
# 1. Signup grant: fresh account gets exactly 1000, from the *default*.
#    Isolation: core/test_api.py sets os.environ["INITIAL_CREDITS"] = "1000"
#    at import time, and core.api reads the env once at import — so in a
#    full-suite run the module constant reflects that override, which would
#    mask a regression back to the old "100" default (the two values now
#    coincide). A fresh subprocess with the variable REMOVED is the only
#    setup that exercises the default itself.
# ---------------------------------------------------------------------------

class TestSignupCreditsDefault:
    def test_fresh_signup_gets_1000_from_the_unset_env_default(self, tmp_path):
        env = {k: v for k, v in os.environ.items() if k != "INITIAL_CREDITS"}
        env["FUTARCHY_STATE"] = str(tmp_path / "state.json")
        env.pop("EXCHANGE_SEEDS_PATH", None)

        script = textwrap.dedent("""
            import asyncio
            import os
            from decimal import Decimal

            assert "INITIAL_CREDITS" not in os.environ

            import core.api as api_module
            from core.models import reset_counters

            # The module-level default, with no env override in sight.
            assert api_module.INITIAL_CREDITS == Decimal("1000"), (
                api_module.INITIAL_CREDITS
            )

            async def main():
                reset_counters()
                async with api_module.lifespan(api_module.app):
                    auth = await api_module._authenticate_github_identity(
                        {"id": 42, "login": "freshsignup"}
                    )
                    acc = api_module.app.state.risk.get_account(auth.account_id)
                    assert acc.total == Decimal("1000"), acc.total
                    assert acc.available_balance == Decimal("1000")
                    assert acc.frozen_balance == Decimal("0")

            asyncio.run(main())
            print("SIGNUP_GRANT_OK")
        """)

        result = subprocess.run(
            [sys.executable, "-c", script],
            env=env, cwd=REPO_ROOT, capture_output=True, text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert "SIGNUP_GRANT_OK" in result.stdout


# ---------------------------------------------------------------------------
# 2/3. GET /v1/me/net
# ---------------------------------------------------------------------------

class TestMyNetPortfolio:
    async def test_math_after_resolving_one_of_two_edits(self, tmp_path, monkeypatch):
        """Two edits: one plain (on gcx_a), one conditional (on gcx_b,
        contextualized on gcx_a=yes). Resolving gcx_a settles the first
        (contributing to settledPnl) and leaves the second open
        (contributing to openStake) — exactly as computed via the venue's
        own msr math.
        """
        reset_counters()
        seeds_path = _write_seeds(tmp_path)
        monkeypatch.setenv("EXCHANGE_SEEDS_PATH", seeds_path)
        api_module.STATE_PATH = str(tmp_path / "state.json")

        async with api_module.lifespan(app):
            api_key, account_id = await _authed_user()
            before_a = app.state.joint.marginal("gcx_a")["yes"]

            # Edit 1: plain, on the root variable itself.
            r1 = await _post(
                "/v1/net/orders",
                {"variableId": "gcx_a", "outcomeId": "yes", "target": 0.8},
                headers=_headers(api_key),
            )
            assert r1.status_code == 200
            stake1 = Decimal(r1.json()["stake"])

            # Edit 2: conditional, on the child variable, contextualized on
            # gcx_a=yes (gcx_a is still unresolved at placement time).
            r2 = await _post(
                "/v1/net/orders",
                {
                    "variableId": "gcx_b", "outcomeId": "yes", "target": 0.5,
                    "context": {"gcx_a": "yes"},
                },
                headers=_headers(api_key),
            )
            assert r2.status_code == 200
            order2_id = r2.json()["orderId"]
            stake2 = Decimal(r2.json()["stake"])

            expected_payout = payout_for_edit(B, before_a, 0.8, won=True)

            resolve_resp = await _post(
                "/v1/net/markets/g1/resolve",
                {"outcome": "yes"},
                headers=ADMIN_HEADERS,
            )
            assert resolve_resp.status_code == 200

            resp = await _get("/v1/me/net", headers=_headers(api_key))
            assert resp.status_code == 200
            data = resp.json()

            assert [o["orderId"] for o in data["orders"]] == [order2_id, "vb_1"]
            assert Decimal(data["openStake"]) == stake2
            assert Decimal(data["settledPnl"]) == expected_payout

    async def test_empty_shape_when_venue_disabled(self, tmp_path):
        """/v1/me/net must NOT 503 when the venue is off — an account's
        portfolio is empty, not an error, per the B5 deviation from the
        usual venue-market 503 rule."""
        reset_counters()
        api_module.STATE_PATH = str(tmp_path / "state.json")

        async with api_module.lifespan(app):
            assert app.state.joint is None
            api_key, _account_id = await _authed_user()

            resp = await _get("/v1/me/net", headers=_headers(api_key))
            assert resp.status_code == 200
            assert resp.json() == {
                "orders": [], "openStake": "0", "settledPnl": "0",
            }

    async def test_requires_auth(self, tmp_path):
        reset_counters()
        api_module.STATE_PATH = str(tmp_path / "state.json")

        async with api_module.lifespan(app):
            resp = await _get("/v1/me/net")
            assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 4/5. GET /v1/leaderboard
# ---------------------------------------------------------------------------

class TestLeaderboard:
    async def test_ordering_and_exclusions(self, tmp_path, monkeypatch):
        reset_counters()
        seeds_path = _write_seeds(tmp_path)
        monkeypatch.setenv("EXCHANGE_SEEDS_PATH", seeds_path)
        api_module.STATE_PATH = str(tmp_path / "state.json")

        async with api_module.lifespan(app):
            # Three GitHub users with different balances (mint on top of
            # the 1000 signup grant so ordering is unambiguous).
            key_low, acc_low = await _authed_user(github_id=1, login="low")
            key_mid, acc_mid = await _authed_user(github_id=2, login="mid")
            key_high, acc_high = await _authed_user(github_id=3, login="high")

            for account_id, extra in ((acc_mid, "500"), (acc_high, "9000")):
                r = await _post(
                    "/v1/admin/mint",
                    {"account_id": account_id, "amount": extra},
                    headers=ADMIN_HEADERS,
                )
                assert r.status_code == 200

            # A market -> an AMM account with 100 (default b) worth locked.
            market_resp = await _post(
                "/v1/admin/markets",
                {
                    "question": "Will it rain?", "category": "weather",
                    "category_id": "wx",
                },
                headers=ADMIN_HEADERS,
            )
            assert market_resp.status_code == 200
            amm_account_id = market_resp.json()["amm_account_id"]

            # A service account — must never appear regardless of balance.
            svc_resp = await _post(
                "/v1/admin/service-accounts",
                {"username": "bot1", "initial_credits": "50000"},
                headers=ADMIN_HEADERS,
            )
            assert svc_resp.status_code == 200
            svc_account_id = svc_resp.json()["account_id"]

            treasury_id = app.state.joint.treasury_account_id

            resp = await _get("/v1/leaderboard")
            assert resp.status_code == 200
            data = resp.json()
            entries = data["entries"]

            entry_ids = [e["accountId"] for e in entries]
            assert amm_account_id not in entry_ids
            assert svc_account_id not in entry_ids
            assert treasury_id not in entry_ids

            totals = [Decimal(e["total"]) for e in entries]
            assert totals == sorted(totals, reverse=True)

            by_account = {e["accountId"]: e for e in entries}
            assert by_account[acc_low]["total"] == "1000"
            assert by_account[acc_low]["login"] == "low"
            assert by_account[acc_mid]["total"] == "1500"
            assert by_account[acc_mid]["login"] == "mid"
            assert by_account[acc_high]["total"] == "10000"
            assert by_account[acc_high]["login"] == "high"

            # high > mid > low, and none of the excluded accounts (whose
            # balances, especially the 1,000,000 treasury, would otherwise
            # dominate) leak into the ranking.
            idx = {e["accountId"]: i for i, e in enumerate(entries)}
            assert idx[acc_high] < idx[acc_mid] < idx[acc_low]

    async def test_public_no_auth_required(self, tmp_path):
        reset_counters()
        api_module.STATE_PATH = str(tmp_path / "state.json")

        async with api_module.lifespan(app):
            await _authed_user()
            resp = await _get("/v1/leaderboard")
            assert resp.status_code == 200
            assert "entries" in resp.json()

    async def test_legacy_local_human_appears_service_account_does_not(
        self, tmp_path
    ):
        """A funded ``auth_store.local_users`` entry is not necessarily a
        service account: the removed ``POST /v1/auth/register`` path used
        to store real humans there too (kept for auth continuity, see
        core/auth.py). Hand-construct that legacy shape directly on the
        auth store (bypassing the admin endpoint, which always sets the
        flag) so the exclusion test is proven to be ``is_service_account``
        and not ``local_users`` membership.
        """
        reset_counters()
        api_module.STATE_PATH = str(tmp_path / "state.json")

        async with api_module.lifespan(app):
            from core.auth import User

            auth_store = app.state.auth_store
            legacy_acc = app.state.risk.create_account()
            app.state.risk.mint(legacy_acc.id, Decimal("777"))
            legacy_user = User(
                github_id=0,
                github_login="legacy_human",
                account_id=legacy_acc.id,
                api_key_hash="deadbeef",
                # is_service_account intentionally omitted -> defaults False,
                # exactly the shape a pre-flag snapshot would produce.
            )
            auth_store.local_users["legacy_human"] = legacy_user

            svc_resp = await _post(
                "/v1/admin/service-accounts",
                {"username": "bot2", "initial_credits": "777"},
                headers=ADMIN_HEADERS,
            )
            assert svc_resp.status_code == 200
            svc_account_id = svc_resp.json()["account_id"]

            resp = await _get("/v1/leaderboard")
            assert resp.status_code == 200
            entry_ids = [e["accountId"] for e in resp.json()["entries"]]

            assert legacy_acc.id in entry_ids
            assert svc_account_id not in entry_ids


# ---------------------------------------------------------------------------
# 6/6. is_service_account flag — persistence roundtrip
# ---------------------------------------------------------------------------

class TestServiceAccountFlagPersistence:
    def test_roundtrip_survives_save_and_load(self, tmp_path):
        from core.auth import AuthStore, User
        from core.market_engine import MarketEngine
        from core.persistence import load_snapshot, save_snapshot
        from core.risk_engine import RiskEngine

        reset_counters()
        risk = RiskEngine()
        me = MarketEngine(risk)
        auth_store = AuthStore()

        svc_acc = risk.create_account()
        auth_store.local_users["bot"] = User(
            github_id=0, github_login="bot", account_id=svc_acc.id,
            api_key_hash="hash1", is_service_account=True,
        )
        human_acc = risk.create_account()
        auth_store.local_users["human"] = User(
            github_id=0, github_login="human", account_id=human_acc.id,
            api_key_hash="hash2", is_service_account=False,
        )

        state_path = tmp_path / "state.json"
        save_snapshot(risk, me, str(state_path), auth_store=auth_store,
                       tracked_repos={})

        _, _, loaded_auth, _, _ = load_snapshot(str(state_path))
        assert loaded_auth.local_users["bot"].is_service_account is True
        assert loaded_auth.local_users["human"].is_service_account is False

    def test_legacy_snapshot_without_field_loads_as_false(self):
        from core.persistence import _load_auth

        auth_data = {
            "users": [],
            "local_users": [
                {
                    "username": "legacy",
                    "account_id": 1,
                    "api_key_hash": "hash3",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "last_seen_at": "2026-01-01T00:00:00+00:00",
                    # no "is_service_account" key at all -- pre-flag shape.
                },
            ],
        }
        store = _load_auth(auth_data)
        assert store.local_users["legacy"].is_service_account is False
