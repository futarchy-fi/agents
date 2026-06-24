import json
import sys
from pathlib import Path


CLI_ROOT = Path(__file__).resolve().parents[1] / "cli"
if str(CLI_ROOT) not in sys.path:
    sys.path.insert(0, str(CLI_ROOT))

from futarchy_cli import update_check


class _FakeClient:
    def __init__(self, api_url: str, timeout: float):
        self.api_url = api_url
        self.timeout = timeout

    def cli_version(self) -> dict:
        return {
            "latest_version": "0.9.9",
            "minimum_supported_version": None,
            "update_command": "futarchy update",
        }

    def close(self) -> None:
        pass


def test_update_check_warns_and_caches_latest_version(tmp_path, monkeypatch, capsys):
    cache_file = tmp_path / "version-check.json"
    monkeypatch.setattr(update_check, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(update_check, "CACHE_FILE", cache_file)
    monkeypatch.setattr(update_check, "__version__", "0.1.2")
    monkeypatch.setattr(update_check.api_mod, "Client", _FakeClient)
    monkeypatch.setattr(update_check.time, "time", lambda: 1_000)

    update_check.maybe_warn_about_update("https://api.example.com")

    stderr = capsys.readouterr().err
    assert "out of date" in stderr

    cached = json.loads(cache_file.read_text(encoding="utf-8"))
    assert cached["latest_version"] == "0.9.9"
    assert cached["update_command"] == "futarchy update"
    assert cached["warned_version"] == "0.9.9"


def test_update_check_uses_recent_cache_without_network(tmp_path, monkeypatch, capsys):
    cache_file = tmp_path / "version-check.json"
    cache_file.write_text(json.dumps({
        "checked_at": 1_000,
        "latest_version": "0.9.9",
        "minimum_supported_version": None,
        "update_command": "futarchy update",
    }), encoding="utf-8")

    def _unexpected_client(*args, **kwargs):
        raise AssertionError("network should not be used for a fresh cache")

    monkeypatch.setattr(update_check, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(update_check, "CACHE_FILE", cache_file)
    monkeypatch.setattr(update_check, "__version__", "0.1.2")
    monkeypatch.setattr(update_check.api_mod, "Client", _unexpected_client)
    monkeypatch.setattr(update_check.time, "time", lambda: 1_100)

    update_check.maybe_warn_about_update("https://api.example.com")

    stderr = capsys.readouterr().err
    assert "out of date" in stderr


def test_update_check_suppresses_warning_for_json_output(tmp_path, monkeypatch, capsys):
    cache_file = tmp_path / "version-check.json"
    cache_file.write_text(json.dumps({
        "checked_at": 1_000,
        "latest_version": "0.9.9",
        "minimum_supported_version": None,
        "update_command": "futarchy update",
    }), encoding="utf-8")

    monkeypatch.setattr(update_check, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(update_check, "CACHE_FILE", cache_file)
    monkeypatch.setattr(update_check, "__version__", "0.1.2")
    monkeypatch.setattr(update_check.time, "time", lambda: 1_100)

    update_check.maybe_warn_about_update("https://api.example.com", json_output=True)

    assert capsys.readouterr().err == ""
