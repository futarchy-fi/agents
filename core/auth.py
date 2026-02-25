"""
Authentication module. GitHub OAuth identity, API key management.

Two auth paths:
1. Token exchange: POST /v1/auth/github with a GitHub PAT → validate → mint API key
2. Device flow: standard OAuth device flow for interactive setup

One account per GitHub user ID. Re-auth rotates the API key.
Only the sha256 hash of the API key is stored; the raw key is returned once.
"""

import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class User:
    github_id: int
    github_login: str
    account_id: int
    api_key_hash: str
    created_at: str = field(default_factory=_now)
    last_seen_at: str = field(default_factory=_now)


def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


class AuthStore:
    """In-memory auth store. Serialized via persistence module."""

    def __init__(self):
        self.users: dict[int, User] = {}         # github_id -> User
        self.key_to_user: dict[str, User] = {}   # api_key_hash -> User
        self.local_users: dict[str, User] = {}   # username -> User (non-GitHub)

    def register_user(self, username: str, account_id: int) -> tuple[User, str]:
        """Register a local user (no GitHub). Returns (user, raw_api_key)."""
        if username in self.local_users:
            raise ValueError("username_taken")
        raw_key = secrets.token_urlsafe(32)
        key_hash = _hash_key(raw_key)
        user = User(
            github_id=0,
            github_login=username,
            account_id=account_id,
            api_key_hash=key_hash,
        )
        self.local_users[username] = user
        self.key_to_user[key_hash] = user
        return user, raw_key

    def create_user(self, github_id: int, github_login: str,
                    account_id: int) -> tuple[User, str]:
        """
        Create or rotate a user. Returns (user, raw_api_key).
        If user exists, rotates the API key.
        """
        raw_key = secrets.token_urlsafe(32)
        key_hash = _hash_key(raw_key)

        existing = self.users.get(github_id)
        if existing:
            # Rotate: remove old key mapping, update user
            self.key_to_user.pop(existing.api_key_hash, None)
            existing.api_key_hash = key_hash
            existing.github_login = github_login
            existing.last_seen_at = _now()
            self.key_to_user[key_hash] = existing
            return existing, raw_key

        user = User(
            github_id=github_id,
            github_login=github_login,
            account_id=account_id,
            api_key_hash=key_hash,
        )
        self.users[github_id] = user
        self.key_to_user[key_hash] = user
        return user, raw_key

    def authenticate(self, raw_key: str) -> User | None:
        """Validate an API key. Returns User or None."""
        key_hash = _hash_key(raw_key)
        user = self.key_to_user.get(key_hash)
        if user:
            user.last_seen_at = _now()
        return user

    def get_by_github_id(self, github_id: int) -> User | None:
        return self.users.get(github_id)


async def validate_github_token(token: str) -> dict:
    """
    Validate a GitHub token by calling GET /user.
    Returns {"id": int, "login": str} on success.
    Raises ValueError on failure.
    """
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://api.github.com/user",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
            timeout=10.0,
        )
    if resp.status_code == 401:
        raise ValueError("github_token_invalid")
    if resp.status_code != 200:
        raise ValueError(f"github_api_error:{resp.status_code}")
    data = resp.json()
    return {"id": data["id"], "login": data["login"]}


async def start_device_flow(client_id: str) -> dict:
    """
    Start GitHub OAuth device flow.
    Returns the device flow response (device_code, user_code, verification_uri, etc.)
    """
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://github.com/login/device/code",
            data={"client_id": client_id, "scope": "read:user"},
            headers={"Accept": "application/json"},
            timeout=10.0,
        )
    if resp.status_code != 200:
        raise ValueError(f"github_api_error:{resp.status_code}")
    return resp.json()


async def poll_device_flow(client_id: str, device_code: str) -> dict:
    """
    Poll GitHub OAuth device flow for access token.
    Returns {"access_token": str} on success.
    Raises ValueError with code on pending/expired/etc.
    """
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://github.com/login/oauth/access_token",
            data={
                "client_id": client_id,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            headers={"Accept": "application/json"},
            timeout=10.0,
        )
    if resp.status_code != 200:
        raise ValueError(f"github_api_error:{resp.status_code}")
    data = resp.json()
    if "error" in data:
        error = data["error"]
        if error == "authorization_pending":
            raise ValueError("device_flow_pending")
        if error == "slow_down":
            raise ValueError("device_flow_pending")
        if error == "expired_token":
            raise ValueError("device_flow_expired")
        raise ValueError(f"github_api_error:{error}")
    return data
