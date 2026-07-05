"""
HTTP hardening tests (Task B6): CORS, request body size cap, OpenAPI
metadata, and rate limiting on the net-venue write routes.

Two test styles are used, matching the two conventions already in this
codebase:
  - CORS/body-cap-with-no-state/OpenAPI tests use the `client` fixture
    pattern from core/test_api.py (fresh app.state per test, no venue
    needed).
  - The oversized-Content-Length-with-real-state test and the net-order
    rate-limit test drive `core.api.lifespan` directly with
    EXCHANGE_SEEDS_PATH set, mirroring core/test_api_net.py, since they
    need a live joint venue behind /v1/net/orders(/preview).

A note on the "accumulated stream over cap, no/small Content-Length" case
from the brief: httpx (like most real HTTP clients) always computes and
sends an accurate Content-Length header for an in-memory bytes/str body,
so that path genuinely can't be driven honestly through an HTTP client in
this test suite. Per the task's own instruction ("do what's honest,
document it"), TestBodySizeLimitStreamGuard drives the ASGI middleware
directly with a fake `receive()` that mimics a chunked request with no
Content-Length at all -- exactly the case the accumulated guard exists for.
"""

import asyncio
import json
import os

import pytest
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("FUTARCHY_ADMIN_KEY", "test-admin-key")
os.environ.setdefault("FUTARCHY_STATE", "/tmp/futarchy_test_state_hardening.json")
os.environ.setdefault("INITIAL_CREDITS", "1000")

import core.api as api_module
from core.api import app, _authenticate_github_identity
from core.auth import AuthStore
from core.middleware import (
    rate_limiter,
    BodySizeLimitMiddleware,
    MAX_BODY_BYTES,
)
from core.models import reset_counters
from core.risk_engine import RiskEngine
from core.market_engine import MarketEngine
from venues.joint.test_venue import TINY_SEEDS


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}"}


def _write_seeds(tmp_path) -> str:
    path = tmp_path / "seeds.json"
    path.write_text(json.dumps(TINY_SEEDS))
    return str(path)


async def _post(path: str, body: dict | None = None, headers: dict | None = None,
                 content: bytes | None = None, extra_headers: dict | None = None):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        if content is not None:
            hdrs = {**(headers or {}), **(extra_headers or {})}
            return await c.post(path, content=content, headers=hdrs)
        return await c.post(path, json=body, headers=headers or {})


async def _authed_user(github_id: int = 1, login: str = "hardeninguser"):
    auth = await _authenticate_github_identity({"id": github_id, "login": login})
    return auth.api_key, auth.account_id


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Every test in this module manages its own env vars."""
    monkeypatch.delenv("EXCHANGE_SEEDS_PATH", raising=False)
    monkeypatch.delenv("JOINT_LIQUIDITY", raising=False)
    monkeypatch.delenv("JOINT_MAX_WIDTH", raising=False)
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    original_state_path = api_module.STATE_PATH
    yield
    api_module.STATE_PATH = original_state_path
    rate_limiter.rate = 60
    rate_limiter.buckets.clear()


@pytest.fixture
async def client():
    """Fresh app state per test, no net venue — mirrors core/test_api.py."""
    reset_counters()
    app.state.risk = RiskEngine()
    app.state.me = MarketEngine(app.state.risk)
    app.state.auth_store = AuthStore()
    app.state.tracked_repos = {}
    app.state.github_oauth_states = {}
    app.state.lock = asyncio.Lock()
    app.state.joint = None
    app.state.venues = {}
    rate_limiter.buckets.clear()

    try:
        os.remove(os.environ["FUTARCHY_STATE"])
    except FileNotFoundError:
        pass

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------

class TestCORS:
    async def test_preflight_default_wildcard(self, client):
        resp = await client.options(
            "/v1/net/markets",
            headers={
                "Origin": "https://example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert resp.status_code == 200
        assert resp.headers.get("access-control-allow-origin") == "*"
        assert "access-control-allow-credentials" not in {
            k.lower() for k in resp.headers.keys()
        }

    async def test_preflight_restricted_origin_echoed(self, client, monkeypatch):
        monkeypatch.setenv("CORS_ORIGINS", "https://futarchy.ai")
        resp = await client.options(
            "/v1/net/markets",
            headers={
                "Origin": "https://futarchy.ai",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert resp.status_code == 200
        assert resp.headers.get("access-control-allow-origin") == "https://futarchy.ai"
        assert resp.headers.get("access-control-allow-credentials") == "true"

    async def test_preflight_rejects_non_allowed_origin(self, client, monkeypatch):
        monkeypatch.setenv("CORS_ORIGINS", "https://futarchy.ai")
        resp = await client.options(
            "/v1/net/markets",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "GET",
            },
        )
        # Starlette's CORSMiddleware answers a disallowed-origin preflight
        # with 400 and no Access-Control-Allow-Origin header (it's the
        # browser's job to actually enforce the policy; the header is
        # simply never granted).
        assert resp.status_code == 400
        assert resp.headers.get("access-control-allow-origin") != "https://evil.example"

    async def test_allowed_methods_and_headers(self, client, monkeypatch):
        monkeypatch.setenv("CORS_ORIGINS", "https://futarchy.ai")
        resp = await client.options(
            "/v1/net/orders",
            headers={
                "Origin": "https://futarchy.ai",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )
        assert resp.status_code == 200
        allow_methods = resp.headers.get("access-control-allow-methods", "")
        for method in ("GET", "POST", "PATCH", "DELETE", "OPTIONS"):
            assert method in allow_methods


# ---------------------------------------------------------------------------
# Body size cap
# ---------------------------------------------------------------------------

class TestBodySizeCapContentLength:
    async def test_oversized_content_length_rejected_413_no_state_change(
        self, tmp_path, monkeypatch
    ):
        reset_counters()
        seeds_path = _write_seeds(tmp_path)
        monkeypatch.setenv("EXCHANGE_SEEDS_PATH", seeds_path)
        api_module.STATE_PATH = str(tmp_path / "state.json")

        async with api_module.lifespan(app):
            api_key, account_id = await _authed_user()
            balance_before = app.state.risk.get_account(account_id).available_balance
            orders_before = len(app.state.joint._orders)

            padding = b"x" * (MAX_BODY_BYTES + 4096)
            oversized_body = (
                b'{"variableId": "gcx_a", "outcomeId": "yes", "target": 0.8, '
                b'"pad": "' + padding + b'"}'
            )
            resp = await _post(
                "/v1/net/orders",
                headers=_headers(api_key),
                content=oversized_body,
                extra_headers={"Content-Type": "application/json"},
            )

            assert resp.status_code == 413
            assert resp.json()["error"]["code"] == "request_too_large"

            # No mutation: the handler never ran.
            account = app.state.risk.get_account(account_id)
            assert account.available_balance == balance_before
            assert len(app.state.joint._orders) == orders_before

    async def test_small_body_unaffected(self, tmp_path, monkeypatch):
        reset_counters()
        seeds_path = _write_seeds(tmp_path)
        monkeypatch.setenv("EXCHANGE_SEEDS_PATH", seeds_path)
        api_module.STATE_PATH = str(tmp_path / "state.json")

        async with api_module.lifespan(app):
            api_key, _ = await _authed_user()
            resp = await _post(
                "/v1/net/orders/preview",
                body={"variableId": "gcx_a", "outcomeId": "yes", "target": 0.8},
                headers=_headers(api_key),
            )
            assert resp.status_code == 200


class TestBodySizeCapStreamGuard:
    """Drives the ASGI middleware directly with a fake `receive()` that
    mimics a chunked request with no Content-Length header at all -- the
    one case that can't honestly be exercised through an HTTP client that
    always sets an accurate Content-Length for in-memory bodies."""

    async def test_streamed_body_over_cap_without_content_length(self):
        chunk = b"x" * 20000  # 4 chunks = 80000 bytes > MAX_BODY_BYTES
        chunks = [chunk] * 4
        received_chunk_sizes = []

        async def inner_app(scope, receive, send):
            while True:
                message = await receive()
                received_chunk_sizes.append(len(message.get("body", b"")))
                if not message.get("more_body", False):
                    break
            # Would only run if the guard failed to fire.
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})

        middleware = BodySizeLimitMiddleware(inner_app, max_bytes=MAX_BODY_BYTES)

        scope = {"type": "http", "method": "POST", "path": "/v1/net/orders",
                  "headers": []}  # no content-length at all

        remaining = list(chunks)

        async def receive():
            if remaining:
                body = remaining.pop(0)
                return {
                    "type": "http.request",
                    "body": body,
                    "more_body": bool(remaining),
                }
            return {"type": "http.request", "body": b"", "more_body": False}

        sent_messages = []

        async def send(message):
            sent_messages.append(message)

        await middleware(scope, receive, send)

        start = next(m for m in sent_messages if m["type"] == "http.response.start")
        assert start["status"] == 413
        body_msg = next(m for m in sent_messages if m["type"] == "http.response.body")
        payload = json.loads(body_msg["body"])
        assert payload["error"]["code"] == "request_too_large"

        # The guard fired partway through -- the inner app never drained
        # (let alone processed) the full oversized stream.
        assert sum(received_chunk_sizes) < sum(len(c) for c in chunks)

    async def test_stream_under_cap_passes_through(self):
        chunks = [b"x" * 100, b"y" * 100]
        received = []

        async def inner_app(scope, receive, send):
            while True:
                message = await receive()
                received.append(message.get("body", b""))
                if not message.get("more_body", False):
                    break
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        middleware = BodySizeLimitMiddleware(inner_app, max_bytes=MAX_BODY_BYTES)
        scope = {"type": "http", "method": "POST", "path": "/x", "headers": []}
        remaining = list(chunks)

        async def receive():
            if remaining:
                body = remaining.pop(0)
                return {"type": "http.request", "body": body, "more_body": bool(remaining)}
            return {"type": "http.request", "body": b"", "more_body": False}

        sent_messages = []

        async def send(message):
            sent_messages.append(message)

        await middleware(scope, receive, send)
        start = next(m for m in sent_messages if m["type"] == "http.response.start")
        assert start["status"] == 200
        assert b"".join(received) == b"x" * 100 + b"y" * 100


# ---------------------------------------------------------------------------
# OpenAPI metadata
# ---------------------------------------------------------------------------

class TestOpenAPIMetadata:
    async def test_openapi_title_and_net_routes(self, client):
        resp = await client.get("/openapi.json")
        assert resp.status_code == 200
        spec = resp.json()
        assert spec["info"]["title"] == "Futarchy Exchange API"
        assert "/v1/net/markets" in spec["paths"]
        assert "/v1/net/orders" in spec["paths"]
        assert "/v1/net/orders/preview" in spec["paths"]

    async def test_docs_served(self, client):
        resp = await client.get("/docs")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Rate limiting on net writes
# ---------------------------------------------------------------------------

class TestNetOrderRateLimiting:
    async def test_preview_rate_limited_past_low_cap(self, tmp_path, monkeypatch):
        reset_counters()
        seeds_path = _write_seeds(tmp_path)
        monkeypatch.setenv("EXCHANGE_SEEDS_PATH", seeds_path)
        api_module.STATE_PATH = str(tmp_path / "state.json")

        async with api_module.lifespan(app):
            api_key, _ = await _authed_user()
            rate_limiter.rate = 2
            rate_limiter.buckets.clear()

            body = {"variableId": "gcx_a", "outcomeId": "yes", "target": 0.8}
            r1 = await _post("/v1/net/orders/preview", body=body, headers=_headers(api_key))
            r2 = await _post("/v1/net/orders/preview", body=body, headers=_headers(api_key))
            r3 = await _post("/v1/net/orders/preview", body=body, headers=_headers(api_key))

            assert r1.status_code == 200
            assert r2.status_code == 200
            assert r3.status_code == 429
            assert r3.json()["error"]["code"] == "rate_limited"
