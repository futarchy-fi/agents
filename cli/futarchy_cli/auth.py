"""Authentication and config file management."""

from __future__ import annotations

import json
import time
import sys
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "futarchy"
CONFIG_FILE = CONFIG_DIR / "config.json"


def load_config() -> dict:
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    return {}


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


def get_api_key() -> str | None:
    return load_config().get("api_key")


def get_api_url() -> str:
    from .api import DEFAULT_API_URL
    return load_config().get("api_url", DEFAULT_API_URL)


def require_auth() -> str:
    key = get_api_key()
    if not key:
        print("Error: not logged in. Run `futarchy login` first.", file=sys.stderr)
        sys.exit(1)
    return key


def login(client) -> None:
    """GitHub device-flow login."""
    # Check if already logged in
    existing = get_api_key()
    if existing:
        print("\n  Already logged in.")
        print(f"  Config: {CONFIG_FILE}")
        print("  Run `futarchy logout` to reset.\n")
        return

    try:
        flow = client.device_auth_start()
    except Exception as e:
        print(f"\n  Error: {e}", file=sys.stderr)
        sys.exit(1)

    verification_uri = flow.get("verification_uri", "https://github.com/login/device")
    user_code = flow.get("user_code", "")
    device_code = flow.get("device_code", "")
    interval = int(flow.get("interval", 5))

    print("\n  Sign in with GitHub")
    print(f"  Open: {verification_uri}")
    print(f"  Code: {user_code}")
    print("\n  Waiting for authorization...\n")

    while True:
        try:
            resp = client.device_auth_poll(device_code)
            break
        except Exception as e:
            status = getattr(e, "status", None)
            detail = getattr(e, "detail", str(e))
            if status == 202:
                time.sleep(interval)
                continue
            if status == 410:
                print("\n  Device code expired. Run `futarchy login` again.\n",
                      file=sys.stderr)
                sys.exit(1)
            print(f"\n  Error: {detail}", file=sys.stderr)
            sys.exit(1)

    api_key = resp.get("api_key", "")
    account_id = resp.get("account_id", "?")
    github_login = resp.get("github_login", "")

    cfg = load_config()
    cfg["api_key"] = api_key
    cfg["github_login"] = github_login
    save_config(cfg)

    print(f"\n  Logged in as {github_login} (account #{account_id})")
    print(f"  Key saved to {CONFIG_FILE}")
    print("\n  You have 100 credits to start trading.")
    print("  Try: futarchy markets\n")


def logout() -> None:
    """Remove saved credentials."""
    cfg = load_config()
    cfg.pop("api_key", None)
    cfg.pop("github_login", None)
    cfg.pop("username", None)
    save_config(cfg)
    print(f"\n  Logged out. Config cleared at {CONFIG_FILE}\n")
