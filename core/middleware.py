"""
Auth dependencies, rate limiting, CORS, and body-size hardening middleware.
"""

import os
import secrets
import time
from typing import Annotated

from fastapi import Depends, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse

from core.api_errors import APIError
from core.auth import User


ADMIN_KEY = os.environ.get("FUTARCHY_ADMIN_KEY", "")
RATE_LIMIT_PER_MIN = int(os.environ.get("RATE_LIMIT_PER_MIN", "60"))


# ---------------------------------------------------------------------------
# Rate limiter (token bucket per API key)
# ---------------------------------------------------------------------------

class RateLimiter:
    """In-memory token bucket rate limiter, per API key hash."""

    def __init__(self, rate: int = 60):
        self.rate = rate              # tokens per minute
        self.buckets: dict[str, tuple[float, float]] = {}  # key_hash -> (tokens, last_refill)

    def check(self, key_hash: str) -> tuple[bool, dict]:
        """
        Check and consume one token. Returns (allowed, headers).
        Headers are always populated for the response.
        """
        now = time.monotonic()
        tokens, last = self.buckets.get(key_hash, (float(self.rate), now))

        # Refill
        elapsed = now - last
        tokens = min(float(self.rate), tokens + elapsed * self.rate / 60.0)

        headers = {
            "X-RateLimit-Limit": str(self.rate),
            "X-RateLimit-Remaining": str(max(0, int(tokens) - 1)),
            "X-RateLimit-Reset": str(int(now + 60)),
        }

        if tokens < 1.0:
            headers["Retry-After"] = "60"
            self.buckets[key_hash] = (tokens, now)
            return False, headers

        tokens -= 1.0
        self.buckets[key_hash] = (tokens, now)
        return True, headers


# Singleton — created at import, replaced in tests
rate_limiter = RateLimiter(RATE_LIMIT_PER_MIN)


# ---------------------------------------------------------------------------
# Auth dependencies
# ---------------------------------------------------------------------------

def _get_bearer_token(request: Request) -> str | None:
    """Extract Bearer token from Authorization header."""
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return None


def _is_admin_key(token: str) -> bool:
    """Constant-time compare of ``token`` against ``ADMIN_KEY``.

    Fails closed when ``ADMIN_KEY`` is unset: an empty configured key never
    matches, regardless of ``token`` (``secrets.compare_digest`` alone
    would happily return True for two empty strings, which is why the
    ``ADMIN_KEY`` truthiness check comes first).
    """
    return bool(ADMIN_KEY) and secrets.compare_digest(
        token.encode("utf-8"), ADMIN_KEY.encode("utf-8")
    )


async def optional_auth(request: Request) -> User | None:
    """Return authenticated user or None. No error on missing auth."""
    token = _get_bearer_token(request)
    if not token:
        return None
    auth_store = request.app.state.auth_store
    return auth_store.authenticate(token)


async def require_auth(request: Request, response: Response) -> User:
    """Require a valid API key. Returns the authenticated User."""
    token = _get_bearer_token(request)
    if not token:
        raise APIError(401, "auth_required", "Authorization header required")

    # Check if it's the admin key (admin can also use auth endpoints)
    if _is_admin_key(token):
        raise APIError(401, "invalid_api_key",
                       "Admin key cannot be used for user endpoints. "
                       "Use a user API key from the dashboard or `futarchy login`.")

    auth_store = request.app.state.auth_store
    user = auth_store.authenticate(token)
    if user is None:
        raise APIError(401, "invalid_api_key", "Invalid or rotated API key")

    # Rate limit
    allowed, headers = rate_limiter.check(user.api_key_hash)
    for k, v in headers.items():
        response.headers[k] = v
    if not allowed:
        raise APIError(429, "rate_limited", "Rate limit exceeded")

    return user


async def require_admin(request: Request) -> None:
    """Require the admin API key."""
    if not ADMIN_KEY:
        raise APIError(500, "admin_required",
                       "FUTARCHY_ADMIN_KEY not configured")
    token = _get_bearer_token(request)
    if not token:
        raise APIError(401, "auth_required", "Authorization header required")
    if not _is_admin_key(token):
        raise APIError(403, "admin_required", "Admin API key required")


AuthUser = Annotated[User, Depends(require_auth)]
AdminDep = Annotated[None, Depends(require_admin)]
OptionalUser = Annotated[User | None, Depends(optional_auth)]


# ---------------------------------------------------------------------------
# CORS (Task B6)
# ---------------------------------------------------------------------------

CORS_ALLOWED_METHODS = ["GET", "POST", "PATCH", "DELETE", "OPTIONS"]
CORS_ALLOWED_HEADERS = ["Authorization", "Content-Type"]


def _cors_origins() -> list[str]:
    """Parse CORS_ORIGINS (comma-separated) from the environment.

    Default is ``"*"``. Read fresh on every call (not cached at import
    time) so tests can flip the env var per-test against the single
    module-level ``app`` instance — the same technique already used for
    ``ADMIN_KEY``/``RATE_LIMIT_PER_MIN`` elsewhere in this module, except
    those two are read once at import while this one is read live because
    CORS behavior needs to vary per-request in tests.
    """
    raw = os.environ.get("CORS_ORIGINS", "*")
    origins = [o.strip() for o in raw.split(",") if o.strip()]
    return origins or ["*"]


class DynamicCORSMiddleware:
    """Thin pure-ASGI wrapper around Starlette's ``CORSMiddleware`` that
    re-reads ``CORS_ORIGINS`` from the environment on every request instead
    of baking it into the middleware stack at app-construction time.

    Starlette's ``CORSMiddleware`` computes all its response headers once,
    in ``__init__``, from whatever ``allow_origins``/``allow_credentials``
    it's given. Baking that in at import time would make ``CORS_ORIGINS``
    untestable without rebuilding the whole ASGI middleware stack (which
    Starlette explicitly forbids once the app has started). Reconstructing
    a real ``CORSMiddleware`` per request keeps the actual CORS logic
    delegated to Starlette while keeping the origin list live.

    When the resolved origin list is exactly ``["*"]``, credentials are
    forced off — Starlette (and browsers) treat
    ``Access-Control-Allow-Origin: *`` combined with
    ``Access-Control-Allow-Credentials: true`` as unsafe/invalid.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        origins = _cors_origins()
        allow_credentials = origins != ["*"]
        cors = CORSMiddleware(
            self.app,
            allow_origins=origins,
            allow_credentials=allow_credentials,
            allow_methods=CORS_ALLOWED_METHODS,
            allow_headers=CORS_ALLOWED_HEADERS,
        )
        await cors(scope, receive, send)


# ---------------------------------------------------------------------------
# Request body size cap (Task B6)
# ---------------------------------------------------------------------------

MAX_BODY_BYTES = 65536


class _RequestBodyTooLarge(Exception):
    """Internal signal raised from the wrapped ``receive()`` once the
    accumulated body size crosses ``MAX_BODY_BYTES``. Caught only by
    ``BodySizeLimitMiddleware`` itself — never seen by route handlers."""


def _too_large_response() -> JSONResponse:
    return JSONResponse(
        status_code=413,
        content={
            "error": {
                "code": "request_too_large",
                "message": f"Request body exceeds the {MAX_BODY_BYTES}-byte limit",
                "details": {},
            }
        },
    )


class BodySizeLimitMiddleware:
    """Pure ASGI middleware (not ``BaseHTTPMiddleware`` — it never buffers
    the body into memory) enforcing a hard cap on request body size via
    two independent layers:

    1. **Content-Length header.** If present and over the cap, reject
       immediately with 413 before the request reaches routing, auth, or
       any handler — the body is never read at all.
    2. **Accumulated stream guard.** Wraps ``receive()`` and keeps a
       running byte counter of ``http.request`` body chunks actually
       delivered. If the count crosses the cap — e.g. a chunked request
       with no (or a dishonest) Content-Length — it raises internally and
       the middleware sends the 413 itself. Only a counter is kept; the
       body is never accumulated in full.

    Caveat, documented rather than hidden: the stream guard relies on its
    internal exception propagating back up through the ASGI call stack to
    this middleware before any response bytes have been sent downstream.
    That holds for every route in this API — FastAPI/Starlette fully
    drains and parses the request body during dependency resolution
    before a handler runs, so nothing has been sent when the cap is
    crossed. It would not hold for a hypothetical handler that starts
    streaming a response before finishing reading the request body.
    """

    def __init__(self, app, max_bytes: int = MAX_BODY_BYTES):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    too_big = int(value) > self.max_bytes
                except ValueError:
                    too_big = False
                if too_big:
                    await _too_large_response()(scope, receive, send)
                    return
                break

        total = 0

        async def guarded_receive():
            nonlocal total
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > self.max_bytes:
                    raise _RequestBodyTooLarge()
            return message

        try:
            await self.app(scope, guarded_receive, send)
        except _RequestBodyTooLarge:
            await _too_large_response()(scope, receive, send)
