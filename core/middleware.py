"""
Auth dependencies and rate limiting middleware.
"""

import os
import time
from typing import Annotated

from fastapi import Depends, Request, Response

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
    if token == ADMIN_KEY and ADMIN_KEY:
        raise APIError(401, "invalid_api_key",
                       "Admin key cannot be used for user endpoints. "
                       "Use a user API key from /v1/auth/github.")

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
    if token != ADMIN_KEY:
        raise APIError(403, "admin_required", "Admin API key required")


AuthUser = Annotated[User, Depends(require_auth)]
AdminDep = Annotated[None, Depends(require_admin)]
OptionalUser = Annotated[User | None, Depends(optional_auth)]
