"""Device-flow authentication and config file management."""

from __future__ import annotations

import json
import sys
import time
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
    """Run device-flow authentication."""
    try:
        resp = client.device_auth_start()
    except Exception as e:
        print(f"Error starting login: {e}", file=sys.stderr)
        sys.exit(1)

    verification_url = resp.get("verification_url", resp.get("url", ""))
    user_code = resp.get("user_code", "")
    device_code = resp.get("device_code", "")
    interval = resp.get("interval", 5)

    print(f"\nOpen this URL in your browser:\n")
    print(f"  {verification_url}\n")
    if user_code:
        print(f"Enter code: {user_code}\n")
    print("Waiting for authorization...", end="", flush=True)

    for _ in range(120 // interval):
        time.sleep(interval)
        try:
            token_resp = client.device_auth_poll(device_code)
        except Exception:
            print(".", end="", flush=True)
            continue

        if token_resp.get("api_key"):
            cfg = load_config()
            cfg["api_key"] = token_resp["api_key"]
            save_config(cfg)
            print(f"\n\nLogged in. Key saved to {CONFIG_FILE}")
            return

        if token_resp.get("error") not in (None, "authorization_pending"):
            print(f"\n\nLogin failed: {token_resp.get('error')}", file=sys.stderr)
            sys.exit(1)

        print(".", end="", flush=True)

    print("\n\nLogin timed out.", file=sys.stderr)
    sys.exit(1)
