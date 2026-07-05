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
        # Sum of mint deltas that have been compacted out of ``transactions``
        # (see compact_transactions). total_minted() adds this back so the
        # money-conservation figure survives log compaction. Zero until the
        # first compaction; snapshots predating this field load it as zero,
        # which is correct because they still carry the full mint history.
        self._minted_base: Decimal = ZERO

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
    # Transfers
    # ------------------------------------------------------------------

    def transfer_available(self, from_account_id: int, to_account_id: int,
                           amount: Decimal, market_id: int = None,
                           reason: str = "transfer") -> tuple[Transaction, Transaction]:
        """
        Transfer credits between accounts' available balances.
        Produces two transactions: one debit, one credit.
        """
        from_acc = self.get_account(from_account_id)
        to_acc = self.get_account(to_account_id)
        if from_acc.available_balance < amount:
            raise InsufficientBalance(
                f"account {from_account_id}: need {amount}, "
                f"have {from_acc.available_balance} available"
            )
        from_acc.available_balance -= amount
        to_acc.available_balance += amount
        tx_from = Transaction.new(
            account_id=from_account_id,
            available_delta=-amount,
            frozen_delta=ZERO,
            reason=f"{reason}:out",
            market_id=market_id,
        )
        tx_to = Transaction.new(
            account_id=to_account_id,
            available_delta=amount,
            frozen_delta=ZERO,
            reason=f"{reason}:in",
            market_id=market_id,
        )
        self.transactions.extend([tx_from, tx_to])
        return tx_from, tx_to

    def transfer_frozen(self, from_lock_id: int, to_account_id: int,
                        amount: Decimal, market_id: int,
                        to_lock_type: str = "conditional_profit",
                        reason: str = "dust_transfer") -> tuple[Transaction, Transaction]:
        """
        Transfer credits between accounts, frozen-to-frozen.

        Decreases from_lock by amount, increases (or creates) a lock of
        to_lock_type on to_account. Both frozen_balances adjust. Neither
        account's available_balance changes. System total unchanged.

        Produces two transactions: one debit, one credit.
        """
        from_lock = self._find_lock(from_lock_id)
        from_acc = self.get_account(from_lock.account_id)
        to_acc = self.get_account(to_account_id)

        if amount > from_lock.amount:
            raise ValueError(
                f"lock {from_lock_id}: can't transfer {amount}, "
                f"only {from_lock.amount} locked"
            )

        # Decrease source lock
        from_lock.amount -= amount
        from_acc.frozen_balance -= amount
        if from_lock.amount == ZERO:
            from_acc.locks.remove(from_lock)

        # Increase destination lock
        to_lock = to_acc.lock_for(market_id, to_lock_type)
        if to_lock is not None:
            to_lock.amount += amount
        else:
            to_lock = Lock.new(to_account_id, market_id, amount,
                               lock_type=to_lock_type)
            to_acc.locks.append(to_lock)
        to_acc.frozen_balance += amount

        tx_from = Transaction.new(
            account_id=from_lock.account_id,
            available_delta=ZERO,
            frozen_delta=-amount,
            reason=f"{reason}:out",
            market_id=market_id,
            lock_id=from_lock_id,
        )
        tx_to = Transaction.new(
            account_id=to_account_id,
            available_delta=ZERO,
            frozen_delta=amount,
            reason=f"{reason}:in",
            market_id=market_id,
            lock_id=to_lock.lock_id,
        )
        self.transactions.extend([tx_from, tx_to])
        return tx_from, tx_to

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def check_available(self, account_id: int, amount: Decimal) -> bool:
        acc = self.get_account(account_id)
        return acc.available_balance >= amount

    def total_minted(self) -> Decimal:
        """Sum of all mint transactions. The total money in the system.

        ``_minted_base`` covers mints already compacted out of the log; the
        sum below covers mints still present. Together they equal the sum
        over every mint that ever happened.
        """
        return self._minted_base + sum(
            (tx.available_delta for tx in self.transactions
             if tx.reason == "mint"),
            ZERO,
        )

    # ------------------------------------------------------------------
    # Transaction-log compaction (bounds snapshot growth — I4)
    # ------------------------------------------------------------------

    def compact_transactions(self, keep_recent: int) -> int:
        """Collapse all but the most recent ``keep_recent`` transactions into
        one synthetic 'checkpoint' entry per affected account.

        The transaction log is append-only and is rewritten in full on every
        snapshot, so without a ceiling the state file grows without bound and
        each save costs O(n). Compaction bounds it while preserving the two
        things anything reads the log for:

        - Per-account running balances: ``_build_account_activity`` sums
          deltas from zero, so each checkpoint carries the SUM of that
          account's dropped ``(available_delta, frozen_delta)`` — the running
          balance at every retained entry is bit-identical to before.
        - ``total_minted()``: dropped mint deltas fold into ``_minted_base``.

        A checkpoint reuses the id and timestamp of the last dropped tx for
        its account, so ids stay monotonic with recency (the activity cursor
        relies on that) and the entry reads as of the cut-off time.

        Returns the number of transactions dropped.
        """
        n = len(self.transactions)
        if keep_recent < 0 or n <= keep_recent:
            return 0
        cutoff = n - keep_recent
        dropped = self.transactions[:cutoff]
        retained = self.transactions[cutoff:]

        agg: dict[int, dict] = {}
        order: list[int] = []
        for tx in dropped:
            if tx.reason == "mint":
                self._minted_base += tx.available_delta
            a = agg.get(tx.account_id)
            if a is None:
                a = {"avail": ZERO, "frozen": ZERO}
                agg[tx.account_id] = a
                order.append(tx.account_id)
            a["avail"] += tx.available_delta
            a["frozen"] += tx.frozen_delta
            a["id"] = tx.id            # last (highest) dropped id for account
            a["at"] = tx.created_at    # last dropped timestamp

        checkpoints = [
            Transaction(
                id=agg[aid]["id"],
                account_id=aid,
                available_delta=agg[aid]["avail"],
                frozen_delta=agg[aid]["frozen"],
                reason="checkpoint",
                created_at=agg[aid]["at"],
            )
            for aid in order
        ]
        self.transactions = checkpoints + retained
        return cutoff

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _find_lock(self, lock_id: int) -> Lock:
        for acc in self.accounts.values():
            lk = acc.lock_by_id(lock_id)
            if lk is not None:
                return lk
        raise ValueError(f"lock {lock_id} not found")
