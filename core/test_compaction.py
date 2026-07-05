"""I4: transaction-log compaction bounds snapshot growth while preserving
total_minted() and per-account activity running balances.
"""
from decimal import Decimal

from core.market_engine import MarketEngine
from core.models import ZERO, reset_counters
from core.persistence import load_snapshot, save_snapshot
from core.risk_engine import RiskEngine


def _build_log(engine):
    """A varied log across two accounts: mints, a lock, and transfers."""
    a = engine.create_account()
    b = engine.create_account()
    engine.mint(a.id, Decimal("1000"))
    engine.mint(b.id, Decimal("500"))
    engine.lock(a.id, market_id=1, amount=Decimal("300"))
    engine.transfer_available(a.id, b.id, Decimal("100"))
    engine.transfer_available(b.id, a.id, Decimal("50"))
    engine.mint(a.id, Decimal("200"))
    return a.id, b.id


def _running(txs, account_id):
    """Reconstruct running balances the way _build_account_activity does:
    accumulate deltas from zero, in order. Returns {tx_id: (avail, frozen)}."""
    avail = ZERO
    frozen = ZERO
    out = {}
    for tx in txs:
        if tx.account_id != account_id:
            continue
        avail += tx.available_delta
        frozen += tx.frozen_delta
        out[tx.id] = (avail, frozen)
    return out


def test_compact_noop_when_under_ceiling():
    reset_counters()
    e = RiskEngine()
    _build_log(e)
    n = len(e.transactions)
    assert e.compact_transactions(n) == 0
    assert e.compact_transactions(n + 5) == 0
    assert len(e.transactions) == n


def test_compact_bounds_the_log():
    reset_counters()
    e = RiskEngine()
    _build_log(e)
    n = len(e.transactions)
    dropped = e.compact_transactions(2)
    assert dropped == n - 2
    # <= 2 retained + at most one checkpoint per account touched by drops.
    assert len(e.transactions) <= 2 + 2


def test_compact_preserves_total_minted():
    reset_counters()
    e = RiskEngine()
    _build_log(e)
    before = e.total_minted()
    assert before == Decimal("1700")  # 1000 + 500 + 200
    e.compact_transactions(2)
    assert e.total_minted() == before
    # Idempotent-ish: compacting again holds the figure.
    e.compact_transactions(1)
    assert e.total_minted() == before


def test_compact_preserves_running_balances_and_final_equals_account():
    reset_counters()
    e = RiskEngine()
    a, b = _build_log(e)
    full_a = _running(e.transactions, a)
    full_b = _running(e.transactions, b)

    e.compact_transactions(3)  # force checkpoints

    # Every id still present keeps its exact running balance (the checkpoint
    # reuses the last dropped id, whose cumulative balance equals the sum of
    # all dropped deltas — so it lines up too).
    for tx_id, bal in _running(e.transactions, a).items():
        assert bal == full_a[tx_id]
    for tx_id, bal in _running(e.transactions, b).items():
        assert bal == full_b[tx_id]

    # Final reconstructed balance equals the account's stored balance.
    acc_a = e.get_account(a)
    last_a = list(_running(e.transactions, a).values())[-1]
    assert last_a == (acc_a.available_balance, acc_a.frozen_balance)


def test_checkpoint_ids_stay_below_retained_ids():
    # The activity cursor (tx_id < before_tx_id) needs ids monotonic with
    # recency: a checkpoint must sort older than every retained tx.
    reset_counters()
    e = RiskEngine()
    _build_log(e)
    keep = 2
    retained_ids = {tx.id for tx in e.transactions[-keep:]}
    e.compact_transactions(keep)
    checkpoint_ids = [tx.id for tx in e.transactions if tx.reason == "checkpoint"]
    assert checkpoint_ids
    assert max(checkpoint_ids) < min(retained_ids)


def test_minted_base_survives_snapshot_round_trip(tmp_path):
    reset_counters()
    e = RiskEngine()
    _build_log(e)
    e.compact_transactions(2)
    assert e._minted_base > ZERO  # some mints were compacted out
    minted = e.total_minted()

    me = MarketEngine(e)
    path = str(tmp_path / "state.json")
    save_snapshot(e, me, path)
    risk2, _, _, _, _ = load_snapshot(path)

    assert risk2._minted_base == e._minted_base
    assert risk2.total_minted() == minted
