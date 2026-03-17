"""Lightweight remote CLI version checks with a local cache."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from . import __version__
from . import api as api_mod


CACHE_DIR = Path.home() / ".cache" / "futarchy"
CACHE_FILE = CACHE_DIR / "version-check.json"
CACHE_TTL_SECONDS = 24 * 60 * 60
WARN_TTL_SECONDS = 24 * 60 * 60


def _parse_version(version: str) -> tuple[int, ...] | None:
    parts = version.split(".")
    try:
        return tuple(int(part) for part in parts)
    except ValueError:
        return None


def _is_newer(version: str | None, current: str) -> bool:
    if not version:
        return False
    parsed = _parse_version(version)
    current_parsed = _parse_version(current)
    if parsed is None or current_parsed is None:
        return False
    return parsed > current_parsed


def _load_cache() -> dict:
    if not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(data: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _fetch_remote_version(api_url: str) -> dict | None:
    client = api_mod.Client(api_url=api_url, timeout=1.5)
    try:
        return client.cli_version()
    except Exception:
        return None
    finally:
        client.close()


def maybe_warn_about_update(api_url: str, json_output: bool = False) -> None:
    cache = _load_cache()
    now = int(time.time())

    should_refresh = (
        "latest_version" not in cache or
        now - int(cache.get("checked_at", 0)) >= CACHE_TTL_SECONDS
    )
    if should_refresh:
        latest = _fetch_remote_version(api_url)
        if latest:
            cache["checked_at"] = now
            cache["latest_version"] = latest.get("latest_version")
            cache["minimum_supported_version"] = latest.get(
                "minimum_supported_version"
            )
            cache["update_command"] = latest.get("update_command", "futarchy update")
            _save_cache(cache)

    if json_output:
        return

    latest_version = cache.get("latest_version")
    minimum_supported_version = cache.get("minimum_supported_version")
    warned_version = cache.get("warned_version")
    warned_at = int(cache.get("warned_at", 0))

    if minimum_supported_version and _is_newer(minimum_supported_version, __version__):
        if warned_version != minimum_supported_version or now - warned_at >= WARN_TTL_SECONDS:
            print(
                f"Warning: CLI {__version__} is below the minimum supported version "
                f"{minimum_supported_version}. Run `{cache.get('update_command', 'futarchy update')}`.",
                file=sys.stderr,
            )
            cache["warned_version"] = minimum_supported_version
            cache["warned_at"] = now
            _save_cache(cache)
        return

    if latest_version and _is_newer(latest_version, __version__):
        if warned_version != latest_version or now - warned_at >= WARN_TTL_SECONDS:
            print(
                f"Notice: CLI {__version__} is out of date; latest is {latest_version}. "
                f"Run `{cache.get('update_command', 'futarchy update')}`.",
                file=sys.stderr,
            )
            cache["warned_version"] = latest_version
            cache["warned_at"] = now
            _save_cache(cache)
