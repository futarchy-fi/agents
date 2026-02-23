"""
Risk engine. Manages accounts, balances, locks, and the transaction ledger.

Every balance mutation produces a Transaction. The risk engine is the
single source of truth for who has how much and where it's locked.

The risk engine does NOT know about markets, positions, or LMSR.
It just knows: accounts have available and frozen balances, and
frozen balances are itemized as locks.

Invariant: account.frozen_balance == sum(lock.amount for lock in account.locks)

The risk engine stores Decimal amounts at full precision. It never
rounds or quantizes — that is the market engine's responsibility when
computing costs and revenues.
"""

from decimal import Decimal
from typing import Optional

from core.models import (
    Account, Lock, Transaction,
    ZERO, quantize, reset_counters,
)


class InsufficientBalance(Exception):
    pass


class RiskEngine:

    def __init__(self):
        self.accounts: dict[int, Account] = {}
        self.transactions: list[Transaction] = []

    def create_account(self, balance: Decimal = ZERO) -> Account:
        acc = Account.new(available_balance=balance)
        self.accounts[acc.id] = acc
        return acc

    def get_account(self, account_id: int) -> Account:
        acc = self.accounts.get(account_id)
        if acc is None:
            raise ValueError(f"account {account_id} not found")
        return acc

    # ------------------------------------------------------------------
    # Minting
    # ------------------------------------------------------------------

    def mint(self, account_id: int, amount: Decimal) -> Transaction:
        """Create credits from nothing. The only way money enters."""
        acc = self.get_account(account_id)
        acc.available_balance += amount
        tx = Transaction.new(
            account_id=account_id,
            available_delta=amount,
            frozen_delta=ZERO,
            reason="mint",
        )
        self.transactions.append(tx)
        return tx

    # ------------------------------------------------------------------
    # Locking
    # ------------------------------------------------------------------

    def lock(self, account_id: int, market_id: int, amount: Decimal,
             lock_type: str = "position",
             trade_id: Optional[int] = None) -> tuple[Lock, Transaction]:
        """
        Move credits from available to frozen. Creates a new Lock.
        Raises InsufficientBalance if not enough available.
        """
        acc = self.get_account(account_id)
        if acc.available_balance < amount:
            raise InsufficientBalance(
                f"account {account_id}: need {amount}, "
                f"have {acc.available_balance} available"
            )
        lk = Lock.new(account_id, market_id, amount, lock_type=lock_type)
        acc.available_balance -= amount
        acc.frozen_balance += amount
        acc.locks.append(lk)
        tx = Transaction.new(
            account_id=account_id,
            available_delta=-amount,
            frozen_delta=amount,
            reason=f"lock:{lock_type}",
            market_id=market_id,
            trade_id=trade_id,
            lock_id=lk.lock_id,
        )
        self.transactions.append(tx)
        return lk, tx

    def increase_lock(self, lock_id: int, amount: Decimal,
                      trade_id: Optional[int] = None) -> Transaction:
        """
        Increase an existing lock. Moves more from available to frozen.
        Raises InsufficientBalance if not enough available.
        """
        lk = self._find_lock(lock_id)
        acc = self.get_account(lk.account_id)
        if acc.available_balance < amount:
            raise InsufficientBalance(
                f"account {lk.account_id}: need {amount}, "
                f"have {acc.available_balance} available"
            )
        lk.amount += amount
        acc.available_balance -= amount
        acc.frozen_balance += amount
        tx = Transaction.new(
            account_id=lk.account_id,
            available_delta=-amount,
            frozen_delta=amount,
            reason=f"increase_lock:{lk.lock_type}",
            market_id=lk.market_id,
            trade_id=trade_id,
            lock_id=lock_id,
        )
        self.transactions.append(tx)
        return tx

    def decrease_lock(self, lock_id: int, amount: Decimal,
                      trade_id: Optional[int] = None) -> Transaction:
        """
        Decrease an existing lock. Moves from frozen back to available.
        If amount == lock.amount, removes the lock entirely.
        """
        lk = self._find_lock(lock_id)
        acc = self.get_account(lk.account_id)
        if amount > lk.amount:
            raise ValueError(
                f"lock {lock_id}: can't decrease by {amount}, "
                f"only {lk.amount} locked"
            )
        lk.amount -= amount
        acc.frozen_balance -= amount
        acc.available_balance += amount
        if lk.amount == ZERO:
            acc.locks.remove(lk)
        tx = Transaction.new(
            account_id=lk.account_id,
            available_delta=amount,
            frozen_delta=-amount,
            reason=f"decrease_lock:{lk.lock_type}",
            market_id=lk.market_id,
            trade_id=trade_id,
            lock_id=lock_id,
        )
        self.transactions.append(tx)
        return tx

    def release_lock(self, lock_id: int,
                     trade_id: Optional[int] = None) -> Transaction:
        """Release an entire lock. All frozen goes back to available."""
        lk = self._find_lock(lock_id)
        return self.decrease_lock(lock_id, lk.amount, trade_id=trade_id)

    def settle_lock(self, lock_id: int, payout: Decimal,
                    trade_id: Optional[int] = None) -> Transaction:
        """
        Settle a lock: release frozen, credit payout to available.
        payout can be more or less than the locked amount (profit/loss).
        The lock is removed entirely.
        """
        lk = self._find_lock(lock_id)
        acc = self.get_account(lk.account_id)
        frozen_released = lk.amount
        acc.frozen_balance -= frozen_released
        acc.available_balance += payout
        acc.locks.remove(lk)
        tx = Transaction.new(
            account_id=lk.account_id,
            available_delta=payout,
            frozen_delta=-frozen_released,
            reason="settlement",
            market_id=lk.market_id,
            trade_id=trade_id,
            lock_id=lock_id,
        )
        self.transactions.append(tx)
        return tx

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def check_available(self, account_id: int, amount: Decimal) -> bool:
        acc = self.get_account(account_id)
        return acc.available_balance >= amount

    def total_minted(self) -> Decimal:
        """Sum of all mint transactions. The total money in the system."""
        return sum(
            (tx.available_delta for tx in self.transactions
             if tx.reason == "mint"),
            ZERO,
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _find_lock(self, lock_id: int) -> Lock:
        for acc in self.accounts.values():
            lk = acc.lock_by_id(lock_id)
            if lk is not None:
                return lk
        raise ValueError(f"lock {lock_id} not found")
