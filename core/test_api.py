"""
API tests. Uses httpx AsyncClient with FastAPI's TestClient transport.

Covers:
- Auth round-trip (GitHub token exchange via mock)
- Public market data (no auth)
- Full trading lifecycle via HTTP
- Auth boundaries (no key, wrong key, admin key on user endpoints)
- Admin operations
- Rate limiting
- Dashboard route integrity (static files and API path alignment)
"""

import asyncio
import os
import re
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

# Set admin key before importing app
os.environ["FUTARCHY_ADMIN_KEY"] = "test-admin-key"
os.environ["FUTARCHY_STATE"] = "/tmp/futarchy_test_state.json"
os.environ["INITIAL_CREDITS"] = "1000"

from core.api import app
from core.auth import AuthStore
from core.middleware import rate_limiter, RateLimiter
from core.models import reset_counters
from core.risk_engine import RiskEngine
from core.market_engine import MarketEngine


ADMIN_HEADERS = {"Authorization": "Bearer test-admin-key"}


@pytest.fixture
async def client():
    """Fresh app state for each test."""
    # Reset state
    reset_counters()
    app.state.risk = RiskEngine()
    app.state.me = MarketEngine(app.state.risk)
    app.state.auth_store = AuthStore()
    app.state.lock = asyncio.Lock()

    # Reset rate limiter
    rate_limiter.buckets.clear()

    # Remove state file if exists
    try:
        os.remove("/tmp/futarchy_test_state.json")
    except FileNotFoundError:
        pass

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _mock_auth(client: AsyncClient, github_id=1,
                     login="testuser") -> str:
    """Helper: create a user via mocked GitHub and return the API key."""
    mock_gh = AsyncMock(return_value={"id": github_id, "login": login})
    with patch("core.api.validate_github_token", mock_gh):
        resp = await client.post("/v1/auth/github",
                                 json={"github_token": "ghp_fake"})
    assert resp.status_code == 200
    return resp.json()["api_key"]


def _user_headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}"}


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

class TestHealth:
    async def test_health(self, client):
        resp = await client.get("/v1/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["markets"] == 0
        assert data["accounts"] == 0


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class TestAuth:
    async def test_github_auth_creates_account(self, client):
        key = await _mock_auth(client, github_id=42, login="octocat")
        assert len(key) > 20

        # Can use the key
        resp = await client.get("/v1/me", headers=_user_headers(key))
        assert resp.status_code == 200
        data = resp.json()
        assert data["available"] == "1000"

    async def test_reauth_rotates_key(self, client):
        key1 = await _mock_auth(client, github_id=42, login="octocat")
        key2 = await _mock_auth(client, github_id=42, login="octocat")
        assert key1 != key2

        # Old key is invalid
        resp = await client.get("/v1/me", headers=_user_headers(key1))
        assert resp.status_code == 401

        # New key works
        resp = await client.get("/v1/me", headers=_user_headers(key2))
        assert resp.status_code == 200

    async def test_same_github_id_same_account(self, client):
        key1 = await _mock_auth(client, github_id=42, login="octocat")
        resp1 = await client.get("/v1/me", headers=_user_headers(key1))
        acct1 = resp1.json()["account_id"]

        key2 = await _mock_auth(client, github_id=42, login="octocat2")
        resp2 = await client.get("/v1/me", headers=_user_headers(key2))
        acct2 = resp2.json()["account_id"]

        assert acct1 == acct2

    async def test_different_github_id_different_account(self, client):
        key1 = await _mock_auth(client, github_id=1, login="alice")
        key2 = await _mock_auth(client, github_id=2, login="bob")

        resp1 = await client.get("/v1/me", headers=_user_headers(key1))
        resp2 = await client.get("/v1/me", headers=_user_headers(key2))

        assert resp1.json()["account_id"] != resp2.json()["account_id"]

    async def test_invalid_github_token(self, client):
        mock = AsyncMock(side_effect=ValueError("github_token_invalid"))
        with patch("core.api.validate_github_token", mock):
            resp = await client.post("/v1/auth/github",
                                     json={"github_token": "bad"})
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "github_token_invalid"


# ---------------------------------------------------------------------------
# Auth Boundaries
# ---------------------------------------------------------------------------

class TestAuthBoundaries:
    async def test_no_auth_on_protected(self, client):
        resp = await client.get("/v1/me")
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "auth_required"

    async def test_bad_key(self, client):
        resp = await client.get("/v1/me",
                                headers={"Authorization": "Bearer bad-key"})
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "invalid_api_key"

    async def test_admin_key_rejected_on_user_endpoint(self, client):
        resp = await client.get("/v1/me", headers=ADMIN_HEADERS)
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "invalid_api_key"

    async def test_user_key_rejected_on_admin_endpoint(self, client):
        key = await _mock_auth(client)
        resp = await client.post("/v1/admin/markets",
                                 headers=_user_headers(key),
                                 json={"question": "Test?",
                                       "category": "t", "category_id": "t#1"})
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "admin_required"

    async def test_public_endpoints_no_auth(self, client):
        # All these should work without auth
        resp = await client.get("/v1/health")
        assert resp.status_code == 200

        resp = await client.get("/v1/markets")
        assert resp.status_code == 200
        assert resp.json() == []


# ---------------------------------------------------------------------------
# Public Market Data
# ---------------------------------------------------------------------------

class TestPublicMarketData:
    async def _create_market(self, client):
        """Admin creates a market, returns market_id."""
        resp = await client.post("/v1/admin/markets", headers=ADMIN_HEADERS,
                                 json={"question": "Will it rain?",
                                       "category": "weather",
                                       "category_id": "weather#1"})
        assert resp.status_code == 200
        return resp.json()["market_id"]

    async def test_list_markets_public(self, client):
        mid = await self._create_market(client)
        resp = await client.get("/v1/markets")
        assert resp.status_code == 200
        markets = resp.json()
        assert len(markets) == 1
        assert markets[0]["market_id"] == mid
        assert markets[0]["question"] == "Will it rain?"
        assert "yes" in markets[0]["prices"]
        assert "no" in markets[0]["prices"]

    async def test_market_detail_public(self, client):
        mid = await self._create_market(client)
        resp = await client.get(f"/v1/markets/{mid}")
        assert resp.status_code == 200
        detail = resp.json()
        assert detail["market_id"] == mid
        assert detail["status"] == "open"
        assert "q" in detail
        assert "volume" in detail
        assert detail["amm_account_id"] > 0

    async def test_market_not_found(self, client):
        resp = await client.get("/v1/markets/999")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "market_not_found"

    async def test_positions_public(self, client):
        mid = await self._create_market(client)
        resp = await client.get(f"/v1/markets/{mid}/positions")
        assert resp.status_code == 200
        assert resp.json() == []  # No traders yet

    async def test_trades_public(self, client):
        mid = await self._create_market(client)
        resp = await client.get(f"/v1/markets/{mid}/trades")
        assert resp.status_code == 200
        assert resp.json() == []  # No trades yet

    async def test_list_markets_filter_by_category(self, client):
        await self._create_market(client)
        # Create a second market with different category
        await client.post("/v1/admin/markets", headers=ADMIN_HEADERS,
                          json={"question": "Will PR merge?",
                                "category": "pr_merge",
                                "category_id": "repo#1@2026-02-24"})

        # Filter by category
        resp = await client.get("/v1/markets", params={"category": "pr_merge"})
        assert resp.status_code == 200
        markets = resp.json()
        assert len(markets) == 1
        assert markets[0]["category"] == "pr_merge"

        # Filter by non-existent category
        resp = await client.get("/v1/markets", params={"category": "nope"})
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_list_markets_filter_by_category_id_prefix(self, client):
        # Create two markets with same PR but different dates
        await client.post("/v1/admin/markets", headers=ADMIN_HEADERS,
                          json={"question": "Merge today?",
                                "category": "pr_merge",
                                "category_id": "repo#7@2026-02-24"})
        await client.post("/v1/admin/markets", headers=ADMIN_HEADERS,
                          json={"question": "Merge tomorrow?",
                                "category": "pr_merge",
                                "category_id": "repo#7@2026-02-25"})
        await client.post("/v1/admin/markets", headers=ADMIN_HEADERS,
                          json={"question": "Other PR?",
                                "category": "pr_merge",
                                "category_id": "repo#8@2026-02-24"})

        # Prefix match: all markets for PR #7
        resp = await client.get("/v1/markets",
                                params={"category_id": "repo#7"})
        assert resp.status_code == 200
        assert len(resp.json()) == 2

        # Exact match
        resp = await client.get("/v1/markets",
                                params={"category_id": "repo#7@2026-02-24"})
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    async def test_list_markets_filter_by_status(self, client):
        mid = await self._create_market(client)

        # All open
        resp = await client.get("/v1/markets", params={"status": "open"})
        assert resp.status_code == 200
        assert len(resp.json()) == 1

        # None resolved yet
        resp = await client.get("/v1/markets", params={"status": "resolved"})
        assert resp.status_code == 200
        assert resp.json() == []

        # Resolve the market
        await client.post(f"/v1/admin/markets/{mid}/resolve",
                          headers=ADMIN_HEADERS,
                          json={"outcome": "yes"})

        # Now resolved
        resp = await client.get("/v1/markets", params={"status": "resolved"})
        assert resp.status_code == 200
        assert len(resp.json()) == 1

        # No open markets
        resp = await client.get("/v1/markets", params={"status": "open"})
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_list_markets_combined_filters(self, client):
        await client.post("/v1/admin/markets", headers=ADMIN_HEADERS,
                          json={"question": "PR?", "category": "pr_merge",
                                "category_id": "repo#1@2026-02-24"})
        resp = await client.post("/v1/admin/markets", headers=ADMIN_HEADERS,
                                  json={"question": "Other?",
                                        "category": "pr_merge",
                                        "category_id": "repo#2@2026-02-24"})
        mid2 = resp.json()["market_id"]

        # Resolve one
        await client.post(f"/v1/admin/markets/{mid2}/resolve",
                          headers=ADMIN_HEADERS,
                          json={"outcome": "no"})

        # Combined: pr_merge + open
        resp = await client.get("/v1/markets",
                                params={"category": "pr_merge",
                                        "status": "open"})
        assert resp.status_code == 200
        assert len(resp.json()) == 1
        assert resp.json()[0]["category_id"] == "repo#1@2026-02-24"

    async def test_positions_show_after_trade(self, client):
        mid = await self._create_market(client)
        key = await _mock_auth(client)
        headers = _user_headers(key)

        # Buy
        resp = await client.post(f"/v1/markets/{mid}/buy", headers=headers,
                                 json={"outcome": "yes", "budget": "50"})
        assert resp.status_code == 200

        # Check positions (public, no auth)
        resp = await client.get(f"/v1/markets/{mid}/positions")
        assert resp.status_code == 200
        positions = resp.json()
        assert len(positions) == 1
        assert Decimal(positions[0]["positions"]["yes"]) > 0

    async def test_trades_show_after_trade(self, client):
        mid = await self._create_market(client)
        key = await _mock_auth(client)
        headers = _user_headers(key)

        resp = await client.post(f"/v1/markets/{mid}/buy", headers=headers,
                                 json={"outcome": "yes", "budget": "50"})
        assert resp.status_code == 200

        resp = await client.get(f"/v1/markets/{mid}/trades")
        assert resp.status_code == 200
        trades = resp.json()
        assert len(trades) == 1
        assert trades[0]["outcome"] == "yes"
        assert Decimal(trades[0]["value"]) > 0


# ---------------------------------------------------------------------------
# Full Trading Lifecycle
# ---------------------------------------------------------------------------

class TestTradingLifecycle:
    async def test_buy_sell_resolve(self, client):
        # Admin creates market
        resp = await client.post("/v1/admin/markets", headers=ADMIN_HEADERS,
                                 json={"question": "Test?", "category": "t",
                                       "category_id": "t#1"})
        assert resp.status_code == 200
        mid = resp.json()["market_id"]

        # User signs up
        key = await _mock_auth(client)
        headers = _user_headers(key)

        # Check balance
        resp = await client.get("/v1/me", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["available"] == "1000"

        # Buy YES
        resp = await client.post(f"/v1/markets/{mid}/buy", headers=headers,
                                 json={"outcome": "yes", "budget": "100"})
        assert resp.status_code == 200
        trade = resp.json()
        assert trade["outcome"] == "yes"
        tokens = Decimal(trade["amount"])
        assert tokens > 0

        # Check balance decreased
        resp = await client.get("/v1/me", headers=headers)
        data = resp.json()
        assert Decimal(data["available"]) < Decimal("1000")
        assert Decimal(data["frozen"]) > 0

        # Sell half
        sell_amount = str(tokens / 2)
        resp = await client.post(f"/v1/markets/{mid}/sell", headers=headers,
                                 json={"outcome": "yes",
                                       "amount": sell_amount})
        assert resp.status_code == 200

        # Resolve YES
        resp = await client.post(f"/v1/admin/markets/{mid}/resolve",
                                 headers=ADMIN_HEADERS,
                                 json={"outcome": "yes"})
        assert resp.status_code == 200

        # Check market resolved
        resp = await client.get(f"/v1/markets/{mid}")
        assert resp.json()["status"] == "resolved"
        assert resp.json()["resolution"] == "yes"

        # User balance should have no frozen (all settled)
        resp = await client.get("/v1/me", headers=headers)
        data = resp.json()
        assert Decimal(data["frozen"]) == 0

    async def test_void_market(self, client):
        resp = await client.post("/v1/admin/markets", headers=ADMIN_HEADERS,
                                 json={"question": "Void?", "category": "t",
                                       "category_id": "t#2"})
        mid = resp.json()["market_id"]

        key = await _mock_auth(client)
        headers = _user_headers(key)

        # Buy
        resp = await client.post(f"/v1/markets/{mid}/buy", headers=headers,
                                 json={"outcome": "yes", "budget": "50"})
        assert resp.status_code == 200

        # Void
        resp = await client.post(f"/v1/admin/markets/{mid}/void",
                                 headers=ADMIN_HEADERS)
        assert resp.status_code == 200

        # Market voided
        resp = await client.get(f"/v1/markets/{mid}")
        assert resp.json()["status"] == "void"

    async def test_two_users_trading(self, client):
        resp = await client.post("/v1/admin/markets", headers=ADMIN_HEADERS,
                                 json={"question": "Race?", "category": "t",
                                       "category_id": "t#3"})
        mid = resp.json()["market_id"]

        key1 = await _mock_auth(client, github_id=1, login="alice")
        key2 = await _mock_auth(client, github_id=2, login="bob")

        # Alice buys YES
        resp = await client.post(f"/v1/markets/{mid}/buy",
                                 headers=_user_headers(key1),
                                 json={"outcome": "yes", "budget": "100"})
        assert resp.status_code == 200

        # Bob buys NO
        resp = await client.post(f"/v1/markets/{mid}/buy",
                                 headers=_user_headers(key2),
                                 json={"outcome": "no", "budget": "100"})
        assert resp.status_code == 200

        # Public positions shows both
        resp = await client.get(f"/v1/markets/{mid}/positions")
        assert len(resp.json()) == 2

        # Public trades shows both
        resp = await client.get(f"/v1/markets/{mid}/trades")
        assert len(resp.json()) == 2


# ---------------------------------------------------------------------------
# Trading Errors
# ---------------------------------------------------------------------------

class TestTradingErrors:
    async def _setup(self, client):
        resp = await client.post("/v1/admin/markets", headers=ADMIN_HEADERS,
                                 json={"question": "?", "category": "t",
                                       "category_id": "t#e"})
        mid = resp.json()["market_id"]
        key = await _mock_auth(client)
        return mid, _user_headers(key)

    async def test_buy_insufficient_balance(self, client):
        mid, headers = await self._setup(client)
        resp = await client.post(f"/v1/markets/{mid}/buy", headers=headers,
                                 json={"outcome": "yes", "budget": "99999"})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "insufficient_balance"

    async def test_buy_invalid_outcome(self, client):
        mid, headers = await self._setup(client)
        resp = await client.post(f"/v1/markets/{mid}/buy", headers=headers,
                                 json={"outcome": "maybe", "budget": "10"})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_outcome"

    async def test_buy_market_not_found(self, client):
        key = await _mock_auth(client)
        resp = await client.post("/v1/markets/999/buy",
                                 headers=_user_headers(key),
                                 json={"outcome": "yes", "budget": "10"})
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "market_not_found"

    async def test_sell_more_than_held(self, client):
        mid, headers = await self._setup(client)
        # Buy some first
        resp = await client.post(f"/v1/markets/{mid}/buy", headers=headers,
                                 json={"outcome": "yes", "budget": "10"})
        amount = resp.json()["amount"]

        # Try to sell more
        resp = await client.post(f"/v1/markets/{mid}/sell", headers=headers,
                                 json={"outcome": "yes",
                                       "amount": str(Decimal(amount) * 2)})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_amount"

    async def test_buy_on_resolved_market(self, client):
        mid, headers = await self._setup(client)
        # Resolve it
        await client.post(f"/v1/admin/markets/{mid}/resolve",
                          headers=ADMIN_HEADERS,
                          json={"outcome": "yes"})
        # Try to buy
        resp = await client.post(f"/v1/markets/{mid}/buy", headers=headers,
                                 json={"outcome": "yes", "budget": "10"})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "market_closed"

    async def test_buy_negative_budget(self, client):
        mid, headers = await self._setup(client)
        resp = await client.post(f"/v1/markets/{mid}/buy", headers=headers,
                                 json={"outcome": "yes", "budget": "-10"})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_amount"

    async def test_buy_invalid_budget(self, client):
        mid, headers = await self._setup(client)
        resp = await client.post(f"/v1/markets/{mid}/buy", headers=headers,
                                 json={"outcome": "yes", "budget": "abc"})
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

class TestAdmin:
    async def test_mint(self, client):
        key = await _mock_auth(client)
        me_resp = await client.get("/v1/me", headers=_user_headers(key))
        acct_id = me_resp.json()["account_id"]

        resp = await client.post("/v1/admin/mint", headers=ADMIN_HEADERS,
                                 json={"account_id": acct_id, "amount": "500"})
        assert resp.status_code == 200
        assert resp.json()["available"] == "1500"

    async def test_create_market_custom_b(self, client):
        resp = await client.post("/v1/admin/markets", headers=ADMIN_HEADERS,
                                 json={"question": "Q?", "category": "t",
                                       "category_id": "t#c", "b": "50"})
        assert resp.status_code == 200
        assert resp.json()["b"] == "50"

    async def test_create_market_with_funding(self, client):
        resp = await client.post("/v1/admin/markets", headers=ADMIN_HEADERS,
                                  json={"question": "Fund?", "category": "t",
                                        "category_id": "t#f", "funding": "200"})
        assert resp.status_code == 200
        data = resp.json()
        # b should be funding / ln(2) ≈ 288.54
        b_val = Decimal(data["b"])
        assert b_val > Decimal("288") and b_val < Decimal("289")

    async def test_create_market_funding_and_b_rejected(self, client):
        resp = await client.post("/v1/admin/markets", headers=ADMIN_HEADERS,
                                  json={"question": "?", "category": "t",
                                        "category_id": "t#x",
                                        "b": "100", "funding": "200"})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_request"

    async def test_add_liquidity(self, client):
        # Create market
        resp = await client.post("/v1/admin/markets", headers=ADMIN_HEADERS,
                                  json={"question": "Liq?", "category": "t",
                                        "category_id": "t#liq",
                                        "funding": "40"})
        assert resp.status_code == 200
        mid = resp.json()["market_id"]
        amm_id = resp.json()["amm_account_id"]
        b_before = Decimal(resp.json()["b"])

        # Mint extra to AMM
        resp = await client.post("/v1/admin/mint", headers=ADMIN_HEADERS,
                                  json={"account_id": amm_id, "amount": "160"})
        assert resp.status_code == 200

        # Add liquidity
        resp = await client.post(f"/v1/admin/markets/{mid}/add-liquidity",
                                  headers=ADMIN_HEADERS,
                                  json={"amount": "40"})
        assert resp.status_code == 200
        b_after = Decimal(resp.json()["b"])
        assert b_after > b_before
        assert resp.json()["funding_added"] == "40"

        # Prices should still sum to ~1
        resp = await client.get(f"/v1/markets/{mid}")
        prices = resp.json()["prices"]
        total = sum(Decimal(v) for v in prices.values())
        assert abs(total - 1) < Decimal("0.01")

    async def test_add_liquidity_insufficient_balance(self, client):
        # Create market — AMM has no extra available
        resp = await client.post("/v1/admin/markets", headers=ADMIN_HEADERS,
                                  json={"question": "?", "category": "t",
                                        "category_id": "t#liq2",
                                        "funding": "40"})
        mid = resp.json()["market_id"]

        # Try to add liquidity without minting extra
        resp = await client.post(f"/v1/admin/markets/{mid}/add-liquidity",
                                  headers=ADMIN_HEADERS,
                                  json={"amount": "40"})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "insufficient_balance"

    async def test_update_metadata(self, client):
        resp = await client.post("/v1/admin/markets", headers=ADMIN_HEADERS,
                                  json={"question": "Meta?", "category": "t",
                                        "category_id": "t#meta"})
        mid = resp.json()["market_id"]

        # Update metadata
        resp = await client.patch(f"/v1/admin/markets/{mid}/metadata",
                                   headers=ADMIN_HEADERS,
                                   json={"metadata": {
                                       "liquidity_steps_remaining": 3,
                                       "next_liquidity_at": "2026-02-24T12:30:00Z",
                                   }})
        assert resp.status_code == 200
        assert resp.json()["metadata"]["liquidity_steps_remaining"] == 3

        # Verify via market detail
        resp = await client.get(f"/v1/markets/{mid}")
        assert resp.json()["metadata"]["liquidity_steps_remaining"] == 3

        # Merge update (existing keys preserved)
        resp = await client.patch(f"/v1/admin/markets/{mid}/metadata",
                                   headers=ADMIN_HEADERS,
                                   json={"metadata": {
                                       "liquidity_steps_remaining": 2,
                                   }})
        assert resp.status_code == 200
        # next_liquidity_at should still be there
        assert resp.json()["metadata"]["next_liquidity_at"] == "2026-02-24T12:30:00Z"
        assert resp.json()["metadata"]["liquidity_steps_remaining"] == 2

    async def test_create_account(self, client):
        resp = await client.post("/v1/admin/accounts", headers=ADMIN_HEADERS)
        assert resp.status_code == 200
        assert "account_id" in resp.json()
        assert resp.json()["account_id"] > 0

    async def test_create_market_with_treasury(self, client):
        """Create market funded from a treasury account instead of minting."""
        # Create treasury and mint to it
        resp = await client.post("/v1/admin/accounts", headers=ADMIN_HEADERS)
        treasury_id = resp.json()["account_id"]
        await client.post("/v1/admin/mint", headers=ADMIN_HEADERS,
                          json={"account_id": treasury_id, "amount": "8000"})

        # Create market funded from treasury
        resp = await client.post("/v1/admin/markets", headers=ADMIN_HEADERS,
                                  json={"question": "Treasury?", "category": "t",
                                        "category_id": "t#treasury",
                                        "funding": "200",
                                        "funding_account_id": treasury_id})
        assert resp.status_code == 200
        mid = resp.json()["market_id"]
        b_val = Decimal(resp.json()["b"])
        assert b_val > Decimal("288") and b_val < Decimal("289")

        # Treasury balance should have decreased
        treasury = app.state.risk.get_account(treasury_id)
        assert treasury.available_balance < Decimal("8000")

        # Market should be functional (buy works)
        key = await _mock_auth(client)
        resp = await client.post(f"/v1/markets/{mid}/buy",
                                  headers=_user_headers(key),
                                  json={"outcome": "yes", "budget": "10"})
        assert resp.status_code == 200

    async def test_add_liquidity_with_treasury(self, client):
        """Add liquidity funded from treasury (no need to mint to AMM)."""
        # Create treasury
        resp = await client.post("/v1/admin/accounts", headers=ADMIN_HEADERS)
        treasury_id = resp.json()["account_id"]
        await client.post("/v1/admin/mint", headers=ADMIN_HEADERS,
                          json={"account_id": treasury_id, "amount": "8000"})

        # Create market from treasury with initial funding
        resp = await client.post("/v1/admin/markets", headers=ADMIN_HEADERS,
                                  json={"question": "Ramp?", "category": "t",
                                        "category_id": "t#ramp",
                                        "funding": "40",
                                        "funding_account_id": treasury_id})
        assert resp.status_code == 200
        mid = resp.json()["market_id"]
        b_before = Decimal(resp.json()["b"])

        treasury_before = app.state.risk.get_account(treasury_id).available_balance

        # Add liquidity from treasury (no mint to AMM needed)
        resp = await client.post(f"/v1/admin/markets/{mid}/add-liquidity",
                                  headers=ADMIN_HEADERS,
                                  json={"amount": "40",
                                        "funding_account_id": treasury_id})
        assert resp.status_code == 200
        b_after = Decimal(resp.json()["b"])
        assert b_after > b_before

        # Treasury should have decreased by 40
        treasury_after = app.state.risk.get_account(treasury_id).available_balance
        assert treasury_before - treasury_after == Decimal("40")

    async def test_treasury_insufficient_balance(self, client):
        """Treasury with insufficient balance returns 400."""
        # Create treasury with small balance
        resp = await client.post("/v1/admin/accounts", headers=ADMIN_HEADERS)
        treasury_id = resp.json()["account_id"]
        await client.post("/v1/admin/mint", headers=ADMIN_HEADERS,
                          json={"account_id": treasury_id, "amount": "10"})

        # Try to create market needing more than 10 credits
        resp = await client.post("/v1/admin/markets", headers=ADMIN_HEADERS,
                                  json={"question": "Broke?", "category": "t",
                                        "category_id": "t#broke",
                                        "funding": "200",
                                        "funding_account_id": treasury_id})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "insufficient_balance"

    async def test_no_admin_key_configured(self, client):
        # Temporarily clear admin key
        import core.middleware
        old = core.middleware.ADMIN_KEY
        core.middleware.ADMIN_KEY = ""
        try:
            resp = await client.post("/v1/admin/markets",
                                     headers={"Authorization": "Bearer x"},
                                     json={"question": "?", "category": "t",
                                           "category_id": "t#x"})
            assert resp.status_code == 500
        finally:
            core.middleware.ADMIN_KEY = old


# ---------------------------------------------------------------------------
# Rate Limiting
# ---------------------------------------------------------------------------

class TestRateLimiting:
    async def test_rate_limit_headers(self, client):
        key = await _mock_auth(client)
        headers = _user_headers(key)

        resp = await client.get("/v1/me", headers=headers)
        assert resp.status_code == 200
        assert "x-ratelimit-limit" in resp.headers
        assert "x-ratelimit-remaining" in resp.headers

    async def test_rate_limit_enforced(self, client):
        key = await _mock_auth(client)
        headers = _user_headers(key)

        # Set very low rate limit
        rate_limiter.rate = 2
        rate_limiter.buckets.clear()

        # First two should succeed
        resp1 = await client.get("/v1/me", headers=headers)
        assert resp1.status_code == 200
        resp2 = await client.get("/v1/me", headers=headers)
        assert resp2.status_code == 200

        # Third should be rate limited
        resp3 = await client.get("/v1/me", headers=headers)
        assert resp3.status_code == 429
        assert resp3.json()["error"]["code"] == "rate_limited"

        # Restore
        rate_limiter.rate = 60
        rate_limiter.buckets.clear()


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

class TestPersistence:
    async def test_state_persists_through_save_load(self, client):
        # Create market via admin
        resp = await client.post("/v1/admin/markets", headers=ADMIN_HEADERS,
                                 json={"question": "Persist?", "category": "t",
                                       "category_id": "t#p"})
        mid = resp.json()["market_id"]

        # Create user and trade
        key = await _mock_auth(client)
        resp = await client.post(f"/v1/markets/{mid}/buy",
                                 headers=_user_headers(key),
                                 json={"outcome": "yes", "budget": "50"})
        assert resp.status_code == 200

        # Reload state from disk
        from core.persistence import load_snapshot
        risk, me, auth_store = load_snapshot("/tmp/futarchy_test_state.json")

        # Verify market exists
        assert mid in me.markets
        assert me.markets[mid].question == "Persist?"
        assert len(me.markets[mid].trades) == 1

        # Verify auth store
        assert auth_store is not None
        assert len(auth_store.users) == 1

        # Verify user can still authenticate
        user = auth_store.authenticate(key)
        assert user is not None
        assert user.github_login == "testuser"


# ---------------------------------------------------------------------------
# Error Format
# ---------------------------------------------------------------------------

class TestErrorFormat:
    async def test_error_format(self, client):
        resp = await client.get("/v1/me")
        assert resp.status_code == 401
        data = resp.json()
        assert "error" in data
        assert "code" in data["error"]
        assert "message" in data["error"]
        assert "details" in data["error"]


# ---------------------------------------------------------------------------
# Dashboard & Static Files
# ---------------------------------------------------------------------------

class TestDashboard:
    async def test_dashboard_route_serves_html(self, client):
        """Dashboard route must return 200 with HTML content."""
        resp = await client.get("/dashboard")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "Futarchy" in resp.text

    async def test_landing_page_links_to_dashboard(self, client):
        """Landing page must contain a link to the dashboard."""
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "/dashboard" in resp.text

    async def test_dashboard_api_paths_match_registered_routes(self, client):
        """Every fetch() path in dashboard.html must correspond to a real API route.

        This prevents the dashboard from silently breaking when API routes
        are renamed or prefixed (the exact bug from PR #8 → v1 migration).
        """
        static_dir = Path(__file__).resolve().parent.parent / "static"
        dashboard_html = (static_dir / "dashboard.html").read_text()

        # Extract all paths from fetch('/v1' + path) calls.
        # The dashboard uses: api('/markets' + params), api('/markets/' + id), etc.
        # The api() function prepends '/v1', so effective paths are /v1/markets, etc.
        # We extract the path fragments passed to api() and prepend /v1.
        api_calls = re.findall(r"api\(['\"]([^'\"]+)['\"]", dashboard_html)
        # Also catch template-literal patterns like api('/markets/' + id)
        api_calls += re.findall(r"api\(['\"/]([^'\"+ )]+)", dashboard_html)

        # Normalize: strip leading slash, dedupe
        raw_paths = set()
        for p in api_calls:
            p = p.lstrip("/")
            raw_paths.add(p)

        # Build the set of registered route path templates (strip /v1 prefix for comparison)
        registered = set()
        for route in app.routes:
            path = getattr(route, "path", "")
            if path.startswith("/v1/"):
                # Normalize path params: /markets/{market_id} → /markets/
                normalized = re.sub(r"\{[^}]+\}", "", path[4:]).rstrip("/")
                registered.add(normalized)

        # Each dashboard API path (after stripping dynamic suffixes) must match
        missing = []
        for raw in raw_paths:
            # Strip trailing dynamic parts: '/markets/' + id → 'markets'
            base = raw.split("?")[0].rstrip("/")
            # Remove trailing path segments that look dynamic (numbers)
            base = re.sub(r"/\d+.*", "", base)
            if base and base not in registered:
                missing.append(f"/v1/{raw}")

        assert not missing, (
            f"Dashboard fetches API paths that don't exist as routes: {missing}. "
            f"Registered /v1 routes: {sorted('/v1/' + r for r in registered)}"
        )
