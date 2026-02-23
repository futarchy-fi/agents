"""
Persistence layer. JSON snapshot + atomic writes.

The snapshot contains the complete state of both engines:
  - RE: accounts, locks, transactions
  - ME: markets, positions, trades, LMSR state
  - ID counters (so IDs resume correctly after restart)

Save after every complete ME operation (buy/sell/resolve/void/create).
On startup, load the snapshot. No replay needed.

Atomic write: write to .tmp, then os.replace. A crash mid-write
leaves the previous snapshot intact.
"""

import dataclasses
import json
import os
from decimal import Decimal

from core.models import (
    Lock, Account, Transaction, TradeLeg, Trade, Market,
    ZERO, _counters, set_counter, reset_counters,
)
from core.risk_engine import RiskEngine
from core.market_engine import MarketEngine


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def _serialize(obj):
    """Recursively serialize dataclasses and Decimals to JSON-safe types."""
    if isinstance(obj, Decimal):
        return str(obj)
    if dataclasses.is_dataclass(obj):
        return {
            f.name: _serialize(getattr(obj, f.name))
            for f in dataclasses.fields(obj)
        }
    if isinstance(obj, list):
        return [_serialize(item) for item in obj]
    if isinstance(obj, dict):
        return {str(k): _serialize(v) for k, v in obj.items()}
    return obj


# ---------------------------------------------------------------------------
# Deserialization helpers
# ---------------------------------------------------------------------------

def _load_lock(d: dict) -> Lock:
    return Lock(
        lock_id=d["lock_id"],
        account_id=d["account_id"],
        market_id=d["market_id"],
        amount=Decimal(d["amount"]),
        lock_type=d["lock_type"],
    )


def _load_account(d: dict) -> Account:
    return Account(
        id=d["id"],
        available_balance=Decimal(d["available_balance"]),
        frozen_balance=Decimal(d["frozen_balance"]),
        locks=[_load_lock(l) for l in d["locks"]],
        created_at=d["created_at"],
    )


def _load_transaction(d: dict) -> Transaction:
    return Transaction(
        id=d["id"],
        account_id=d["account_id"],
        available_delta=Decimal(d["available_delta"]),
        frozen_delta=Decimal(d["frozen_delta"]),
        reason=d["reason"],
        market_id=d.get("market_id"),
        trade_id=d.get("trade_id"),
        trade_leg_id=d.get("trade_leg_id"),
        lock_id=d.get("lock_id"),
        created_at=d["created_at"],
    )


def _load_trade_leg(d: dict) -> TradeLeg:
    return TradeLeg(
        trade_leg_id=d["trade_leg_id"],
        account_id=d["account_id"],
        available_delta=Decimal(d["available_delta"]),
        frozen_delta=Decimal(d["frozen_delta"]),
        lock_id=d.get("lock_id"),
        tx_id=d.get("tx_id"),
    )


def _load_trade(d: dict) -> Trade:
    return Trade(
        id=d["id"],
        market_id=d["market_id"],
        outcome=d["outcome"],
        amount=Decimal(d["amount"]),
        price=Decimal(d["price"]),
        buyer=_load_trade_leg(d["buyer"]),
        seller=_load_trade_leg(d["seller"]),
        created_at=d["created_at"],
    )


def _load_market(d: dict) -> Market:
    positions = {
        int(acc_id): {
            outcome: Decimal(amount)
            for outcome, amount in pos.items()
        }
        for acc_id, pos in d["positions"].items()
    }
    q = {outcome: Decimal(val) for outcome, val in d["q"].items()}

    return Market(
        id=d["id"],
        amm_account_id=d["amm_account_id"],
        type=d["type"],
        category=d["category"],
        category_id=d["category_id"],
        question=d["question"],
        price_precision=d["price_precision"],
        amount_precision=d["amount_precision"],
        status=d["status"],
        outcomes=d["outcomes"],
        resolution=d.get("resolution"),
        metadata=d["metadata"],
        b=Decimal(d["b"]),
        q=q,
        positions=positions,
        trades=[_load_trade(t) for t in d["trades"]],
        deadline=d.get("deadline"),
        created_at=d["created_at"],
        resolved_at=d.get("resolved_at"),
    )


# ---------------------------------------------------------------------------
# Schema versioning
# ---------------------------------------------------------------------------

CURRENT_VERSION = 1

# Migrations: each takes a state dict at version N and returns version N+1.
# Add new migrations here as the schema evolves.
#
# Example:
#   def _migrate_1_to_2(state):
#       for m in state["markets"]:
#           m["new_field"] = "default"
#       state["version"] = 2
#       return state
#
#   _MIGRATIONS = {1: _migrate_1_to_2, 2: _migrate_2_to_3, ...}

_MIGRATIONS: dict[int, callable] = {}


def _apply_migrations(state: dict) -> dict:
    """Apply all needed migrations to bring state to CURRENT_VERSION."""
    version = state.get("version", 1)
    while version < CURRENT_VERSION:
        migrate = _MIGRATIONS.get(version)
        if migrate is None:
            raise ValueError(
                f"no migration from version {version} to {version + 1}")
        state = migrate(state)
        version = state["version"]
    return state


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def save_snapshot(risk: RiskEngine, market_engine: MarketEngine,
                  path: str) -> None:
    """
    Save complete RE + ME state to a JSON file.
    Atomic: writes to .tmp then renames.
    """
    state = {
        "version": CURRENT_VERSION,
        "counters": dict(_counters),
        "accounts": [_serialize(acc) for acc in risk.accounts.values()],
        "transactions": [_serialize(tx) for tx in risk.transactions],
        "markets": [_serialize(m) for m in market_engine.markets.values()],
    }
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


def load_snapshot(path: str) -> tuple[RiskEngine, MarketEngine]:
    """
    Load RE + ME state from a JSON snapshot.
    Applies migrations automatically if the snapshot is an older version.
    Returns (risk_engine, market_engine) ready to use.
    """
    with open(path) as f:
        state = json.load(f)

    state = _apply_migrations(state)

    # Restore ID counters
    reset_counters()
    for kind, value in state["counters"].items():
        set_counter(kind, value)

    # Restore risk engine
    risk = RiskEngine()
    for adata in state["accounts"]:
        acc = _load_account(adata)
        risk.accounts[acc.id] = acc

    risk.transactions = [_load_transaction(t) for t in state["transactions"]]

    # Restore market engine
    me = MarketEngine(risk)
    for mdata in state["markets"]:
        market = _load_market(mdata)
        me.markets[market.id] = market

    return risk, me
