"""
CLI venues-section preservation (Plan B final-review item 2).

Bug: core.cli.load_or_create discarded the loaded ``venues`` section
returned by ``load_snapshot`` (bound to ``_venues`` and dropped), so every
mutating CLI command's ``save_snapshot`` call wrote an EMPTY venues section
back out — silently wiping any net-venue (JointVenue) state a prior API/CLI
session had persisted.

These tests drive ``core.cli.main()`` directly (no existing CLI test
module to follow conventions from), with ``sys.argv`` patched and
``FUTARCHY_STATE``/``--state`` pointed at a tmp_path file.
"""

import json
import sys
from decimal import Decimal

from core.cli import main
from core.models import reset_counters
from core.persistence import load_snapshot, save_snapshot
from core.market_engine import MarketEngine
from core.risk_engine import RiskEngine


def _seed_state_with_venues(path: str, venues_section: dict) -> None:
    """Write an initial snapshot with a hand-built, non-empty venues section."""
    reset_counters()
    risk = RiskEngine()
    me = MarketEngine(risk)
    save_snapshot(risk, me, path, venues=venues_section)


def test_mutating_cli_command_preserves_venues_section(tmp_path, monkeypatch, capsys):
    state_path = str(tmp_path / "state.json")
    venues_section = {
        "joint": {
            "orders": [{"orderId": "vb_1", "accountId": 1, "status": "open"}],
            "orderSeq": 1,
            "resolutions": {"gcx_a": "yes"},
            "voided": [],
            "marketStatus": {"g1": {"status": "resolved", "resolvedOutcome": "yes"}},
            "treasuryAccountId": 1,
            "liquidity": "50",
            "maxWidth": 8,
            "seedsSource": "<inline>",
        }
    }
    _seed_state_with_venues(state_path, venues_section)

    # Sanity: the section really did land in the file as written.
    with open(state_path) as f:
        pre_state = json.load(f)
    assert pre_state["venues"] == venues_section

    # Run a mutating CLI command (create-account) through the real entry
    # point — this is the load -> execute -> save path that used to wipe
    # the venues section on every save.
    monkeypatch.setattr(
        sys, "argv", ["futarchy-cli", "--state", state_path, "create-account"]
    )
    main()

    out = capsys.readouterr().out
    result = json.loads(out.strip().splitlines()[-1])
    assert result["ok"] is True

    with open(state_path) as f:
        post_state = json.load(f)

    # Byte-identical (same JSON-serializable structure) venues section —
    # nothing about the mutating command touched or erased it.
    assert post_state["venues"] == venues_section

    # And it round-trips through load_snapshot the same way.
    _risk, _me, _auth, _repos, loaded_venues = load_snapshot(state_path)
    assert loaded_venues == venues_section
