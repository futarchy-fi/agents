"""
Data models for the futarchic agent economy.

Two separate domains:
- Risk side: accounts, locks, transactions (the risk engine's world)
- Market side: markets, positions, trades (the market engine's world)

The risk engine doesn't know about outcome tokens. It just tracks
credits: available, locked, and where they're locked. The market
engine owns positions and LMSR state.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
import uuid


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id() -> str:
    return uuid.uuid4().hex[:12]


# ---------------------------------------------------------------------------
# Risk side
# ---------------------------------------------------------------------------

@dataclass
class Lock:
    """Credits locked in a market. The risk engine's receipt."""
    lock_id: str
    market_id: str
    amount: float  # always positive

    @staticmethod
    def new(market_id: str, amount: float) -> "Lock":
        return Lock(lock_id=_id(), market_id=market_id, amount=amount)


@dataclass
class Account:
    """
    An account in the risk engine.

    balance: credits available to spend or stake.
    locks: credits committed to open markets, itemized.
    """
    id: str
    balance: float = 0.0
    locks: list[Lock] = field(default_factory=list)
    created_at: str = field(default_factory=_now)

    @property
    def locked(self) -> float:
        return sum(lock.amount for lock in self.locks)

    @property
    def total(self) -> float:
        return self.balance + self.locked

    def locks_for_market(self, market_id: str) -> list[Lock]:
        return [l for l in self.locks if l.market_id == market_id]

    def locked_in_market(self, market_id: str) -> float:
        return sum(l.amount for l in self.locks if l.market_id == market_id)


@dataclass
class Transaction:
    """
    Append-only ledger entry. Every credit movement gets one of these.
    Positive amount = credits in. Negative = credits out.
    """
    id: str
    account_id: str
    amount: float
    reason: str
    market_id: Optional[str] = None
    created_at: str = field(default_factory=_now)

    @staticmethod
    def new(account_id: str, amount: float, reason: str,
            market_id: Optional[str] = None) -> "Transaction":
        return Transaction(
            id=_id(),
            account_id=account_id,
            amount=amount,
            reason=reason,
            market_id=market_id,
        )


# ---------------------------------------------------------------------------
# Market side
# ---------------------------------------------------------------------------

@dataclass
class Trade:
    """A single trade in a market. The market engine's record."""
    id: str
    market_id: str
    account_id: str
    outcome: str    # e.g. "yes" or "no"
    amount: float   # tokens bought (negative if selling)
    cost: float     # credits paid (negative if received back)
    created_at: str = field(default_factory=_now)

    @staticmethod
    def new(market_id: str, account_id: str, outcome: str,
            amount: float, cost: float) -> "Trade":
        return Trade(
            id=_id(),
            market_id=market_id,
            account_id=account_id,
            outcome=outcome,
            amount=amount,
            cost=cost,
        )


@dataclass
class Market:
    """
    A market instance. Owns LMSR state and positions.

    type: the mechanism — "conditional_prediction_market"
    category: what it's about — "pr_merge", "task_completion", etc.
    outcomes: the possible results — ["yes", "no"]
    q: LMSR quantities sold per outcome
    positions: tokens held per account per outcome (market owns this)
    """
    id: str
    type: str                                  # "conditional_prediction_market"
    category: str                              # "pr_merge", etc.
    question: str
    status: str = "open"                       # "open", "resolved", "void"
    outcomes: list[str] = field(default_factory=lambda: ["yes", "no"])
    resolution: Optional[str] = None           # winning outcome
    metadata: dict = field(default_factory=dict)
    b: float = 100.0                           # LMSR liquidity parameter
    q: dict[str, float] = field(default_factory=dict)  # filled on creation
    positions: dict[str, dict[str, float]] = field(default_factory=dict)
    trades: list[Trade] = field(default_factory=list)
    deadline: Optional[str] = None             # void if unresolved by then
    created_at: str = field(default_factory=_now)
    resolved_at: Optional[str] = None

    @staticmethod
    def new(question: str, category: str, metadata: dict,
            b: float = 100.0, outcomes: list[str] = None,
            deadline: Optional[str] = None) -> "Market":
        outcomes = outcomes or ["yes", "no"]
        return Market(
            id=_id(),
            type="conditional_prediction_market",
            category=category,
            question=question,
            outcomes=outcomes,
            metadata=metadata,
            b=b,
            q={outcome: 0.0 for outcome in outcomes},
            deadline=deadline,
        )

    def position(self, account_id: str) -> dict[str, float]:
        return self.positions.get(account_id, {o: 0.0 for o in self.outcomes})
