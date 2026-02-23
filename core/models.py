"""
Data models for the futarchic agent economy.

Two separate domains:
- Risk side: accounts, locks, transactions (the risk engine's world)
- Market side: markets, positions, trades (the market engine's world)

The risk engine tracks assets (credits today, other token types later)
and knows where they're locked. What it doesn't know is the internal
structure of markets — positions, outcome tokens, LMSR state. That
belongs to the market engine.

All monetary amounts use Decimal. Two precisions:
- Asset precision (e.g. CREDITS = 6 dp) for credit amounts
- Market precision (e.g. 4 dp) for prices and token amounts
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional


ZERO = Decimal("0")

# ---------------------------------------------------------------------------
# Asset precision
# ---------------------------------------------------------------------------

ASSET_PRECISION: dict[str, int] = {
    "CREDITS": 6,
}


def quantize(amount: Decimal, asset: str = "CREDITS") -> Decimal:
    """Quantize a credit amount to asset precision."""
    precision = ASSET_PRECISION.get(asset, 6)
    return amount.quantize(Decimal(10) ** -precision)


# ---------------------------------------------------------------------------
# Sequential IDs
# ---------------------------------------------------------------------------

_counters: dict[str, int] = defaultdict(int)


def next_id(kind: str) -> int:
    """Sequential ID. Kinds: account, market, lock, trade, tx."""
    _counters[kind] += 1
    return _counters[kind]


def reset_counters() -> None:
    """Reset all counters. For testing."""
    _counters.clear()


def set_counter(kind: str, value: int) -> None:
    """Set a counter. For loading persisted state."""
    _counters[kind] = value


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Risk side
# ---------------------------------------------------------------------------

@dataclass
class Lock:
    """
    Credits locked for a reason. The risk engine's receipt.

    lock_type values:
      "position"           — credits backing a market position
      "conditional_profit" — unrealized profit, frozen until resolution
      "limit_order"        — credits reserved for a pending order

    An account can have multiple locks per market. Typical pattern:
    one "position" lock + one "conditional_profit" lock per market.
    On resolve: position settles, conditional_profit becomes available.
    On void: both locks return to available (all trades revert).
    """
    lock_id: int
    account_id: int
    market_id: int
    amount: Decimal     # always positive
    lock_type: str      # "position", "limit_order", etc.

    @staticmethod
    def new(account_id: int, market_id: int, amount: Decimal,
            lock_type: str = "position") -> "Lock":
        return Lock(
            lock_id=next_id("lock"),
            account_id=account_id,
            market_id=market_id,
            amount=amount,
            lock_type=lock_type,
        )


@dataclass
class Account:
    """
    An account in the risk engine.
    The AMM for each market is also an account.

    available_balance: credits free to spend or stake.
    frozen_balance: credits locked in markets (sum of all locks).
    """
    id: int
    available_balance: Decimal = ZERO
    frozen_balance: Decimal = ZERO
    locks: list[Lock] = field(default_factory=list)
    created_at: str = field(default_factory=_now)

    @staticmethod
    def new(available_balance: Decimal = ZERO) -> "Account":
        return Account(id=next_id("account"),
                       available_balance=available_balance)

    @property
    def total(self) -> Decimal:
        return self.available_balance + self.frozen_balance

    def locks_for_market(self, market_id: int) -> list[Lock]:
        return [l for l in self.locks if l.market_id == market_id]

    def frozen_in_market(self, market_id: int) -> Decimal:
        return sum((l.amount for l in self.locks
                    if l.market_id == market_id), ZERO)

    def lock_by_id(self, lock_id: int) -> Optional[Lock]:
        return next((l for l in self.locks if l.lock_id == lock_id), None)

    def lock_for(self, market_id: int, lock_type: str) -> Optional[Lock]:
        return next((l for l in self.locks
                     if l.market_id == market_id
                     and l.lock_type == lock_type), None)


@dataclass
class Transaction:
    """
    Append-only ledger entry. Every balance change gets one of these.

    available_delta: change to available balance (positive = credit)
    frozen_delta: change to frozen balance (positive = lock)

    On trade open:   available_delta = -cost, frozen_delta = +cost
    On settlement:   frozen_delta = -locked, available_delta = +payout
    On mint:         available_delta = +amount, frozen_delta = 0
    """
    id: int
    account_id: int
    available_delta: Decimal
    frozen_delta: Decimal
    reason: str
    market_id: Optional[int] = None
    trade_id: Optional[int] = None
    trade_leg_id: Optional[int] = None
    lock_id: Optional[int] = None
    created_at: str = field(default_factory=_now)

    @staticmethod
    def new(account_id: int, available_delta: Decimal,
            frozen_delta: Decimal, reason: str,
            market_id: Optional[int] = None,
            trade_id: Optional[int] = None,
            trade_leg_id: Optional[int] = None,
            lock_id: Optional[int] = None) -> "Transaction":
        return Transaction(
            id=next_id("tx"),
            account_id=account_id,
            available_delta=available_delta,
            frozen_delta=frozen_delta,
            reason=reason,
            market_id=market_id,
            trade_id=trade_id,
            trade_leg_id=trade_leg_id,
            lock_id=lock_id,
        )


# ---------------------------------------------------------------------------
# Market side
# ---------------------------------------------------------------------------

@dataclass
class TradeLeg:
    """
    One side of a trade. Records balance changes for one account.

    On open:   available_delta = -cost, frozen_delta = +cost
    On settle: frozen_delta = -original_cost, available_delta = +payout
    Profit if payout > original cost. Loss if less.
    """
    trade_leg_id: int
    account_id: int
    available_delta: Decimal
    frozen_delta: Decimal
    lock_id: Optional[int] = None
    tx_id: Optional[int] = None

    @staticmethod
    def new(account_id: int, available_delta: Decimal,
            frozen_delta: Decimal, lock_id: Optional[int] = None,
            tx_id: Optional[int] = None) -> "TradeLeg":
        return TradeLeg(
            trade_leg_id=next_id("trade_leg"),
            account_id=account_id,
            available_delta=available_delta,
            frozen_delta=frozen_delta,
            lock_id=lock_id,
            tx_id=tx_id,
        )


@dataclass
class Trade:
    """
    A single trade in a market. Both sides recorded.
    In LMSR markets, one side is always the AMM.
    """
    id: int
    market_id: int
    outcome: str        # e.g. "yes" or "no"
    amount: Decimal     # tokens traded (market precision)
    price: Decimal      # average execution price (market precision)
    buyer: TradeLeg
    seller: TradeLeg
    created_at: str = field(default_factory=_now)

    @staticmethod
    def new(market_id: int, outcome: str, amount: Decimal,
            price: Decimal, buyer: TradeLeg,
            seller: TradeLeg) -> "Trade":
        return Trade(
            id=next_id("trade"),
            market_id=market_id,
            outcome=outcome,
            amount=amount,
            price=price,
            buyer=buyer,
            seller=seller,
        )


@dataclass
class Market:
    """
    A market instance. Owns LMSR state and positions.

    type: the mechanism — "conditional_prediction_market"
    category: what it's about — "pr_merge", "task_completion", etc.
    category_id: specific instance — "futarchy-fi/agents#1", etc.
    precision: decimal places for prices and token amounts
    q: LMSR quantities sold per outcome
    positions: tokens held per account per outcome

    Conditional profits (rounding dust, unrealized gains) are tracked
    as per-account locks with lock_type="conditional_profit", not as
    a market-level field. Any account can accumulate conditional profit.
    """
    id: int
    amm_account_id: int
    type: str                                  # "conditional_prediction_market"
    category: str                              # "pr_merge", etc.
    category_id: str                           # "futarchy-fi/agents#1", etc.
    question: str
    precision: int = 4                         # decimal places for prices/amounts
    status: str = "open"                       # "open", "resolved", "void"
    outcomes: list[str] = field(default_factory=lambda: ["yes", "no"])
    resolution: Optional[str] = None           # winning outcome
    metadata: dict = field(default_factory=dict)
    b: Decimal = Decimal("100")                # LMSR liquidity parameter
    q: dict[str, Decimal] = field(default_factory=dict)
    positions: dict[int, dict[str, Decimal]] = field(default_factory=dict)
    trades: list[Trade] = field(default_factory=list)
    deadline: Optional[str] = None             # void if unresolved by then
    created_at: str = field(default_factory=_now)
    resolved_at: Optional[str] = None

    @staticmethod
    def new(question: str, category: str, category_id: str,
            metadata: dict, amm_account_id: int,
            b: Decimal = Decimal("100"), precision: int = 4,
            outcomes: list[str] = None,
            deadline: Optional[str] = None) -> "Market":
        outcomes = outcomes or ["yes", "no"]
        return Market(
            id=next_id("market"),
            amm_account_id=amm_account_id,
            type="conditional_prediction_market",
            category=category,
            category_id=category_id,
            question=question,
            precision=precision,
            outcomes=outcomes,
            metadata=metadata,
            b=b,
            q={outcome: ZERO for outcome in outcomes},
            deadline=deadline,
        )

    def quantize_price(self, price: Decimal) -> Decimal:
        return price.quantize(Decimal(10) ** -self.precision)

    def quantize_amount(self, amount: Decimal) -> Decimal:
        return amount.quantize(Decimal(10) ** -self.precision)

    def position(self, account_id: int) -> dict[str, Decimal]:
        return self.positions.get(account_id,
                                  {o: ZERO for o in self.outcomes})
