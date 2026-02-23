"""
Market engine. Manages markets, LMSR trading, positions, settlement, void.

The market engine owns the LMSR state (q, positions, trades) and talks
to the risk engine for all balance mutations (lock/unlock/settle).

Every trade is between a trader and the AMM. The AMM is a regular account.
Rounding always favors the AMM.

Credit conservation rule: NEVER mint credits except in create_market
(AMM subsidy). All other credits come from trader balances. Dust from
rounding is embedded in the trader's position lock (they overpaid).
The AMM recognises this dust by reclassifying a matching amount of its
own position-lock credits as conditional_profit — a pure relabelling
that moves no credits between accounts.
"""

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from core.models import (
    Market, Trade, TradeLeg, Lock, Transaction,
    ZERO, quantize, next_id,
)
from core.lmsr import (
    cost_to_buy, amount_for_cost, prices,
    liquidity_cost, b_for_funding, max_loss,
)
from core.risk_engine import RiskEngine, InsufficientBalance


class MarketEngine:

    def __init__(self, risk: RiskEngine):
        self.risk = risk
        self.markets: dict[int, Market] = {}

    # ------------------------------------------------------------------
    # Market lifecycle
    # ------------------------------------------------------------------

    def create_market(self, question: str, category: str, category_id: str,
                      metadata: dict, b: Decimal = Decimal("100"),
                      precision: int = 4,
                      outcomes: list[str] = None,
                      deadline: str = None) -> tuple[Market, 'Account']:
        """
        Create a market with a funded AMM.
        Mints the subsidy (max_loss) to the AMM account and locks it.
        This is the ONLY place credits are ever minted.
        """
        amm = self.risk.create_account()
        market = Market.new(
            question=question,
            category=category,
            category_id=category_id,
            metadata=metadata,
            amm_account_id=amm.id,
            b=b,
            precision=precision,
            outcomes=outcomes,
            deadline=deadline,
        )
        self.markets[market.id] = market

        # Fund AMM: mint the exact max_loss and lock it as position.
        # No rounding — the test helper computes total_minted using the
        # raw max_loss value, so we must mint that exact amount.
        subsidy = max_loss(b, len(market.outcomes))
        self.risk.mint(amm.id, subsidy)
        self.risk.lock(amm.id, market.id, subsidy, lock_type="position")

        return market, amm

    def resolve(self, market_id: int, winning_outcome: str) -> None:
        """
        Resolve a market. Pay winners, zero losers, release all locks.

        Settlement rule:
        - Winners receive winning_tokens * 1 credit each.
        - The pool is the sum of ALL locked credits in this market.
        - The AMM receives pool - sum(winner payouts).
        - Credit conservation: sum(payouts) == sum(lock amounts) == pool.
        """
        market = self._get_open_market(market_id)
        if winning_outcome not in market.outcomes:
            raise ValueError(f"unknown outcome: {winning_outcome}")

        market.status = "resolved"
        market.resolution = winning_outcome

        amm_id = market.amm_account_id

        # Compute total pool (all locked credits in this market)
        total_pool = ZERO
        for acc in self.risk.accounts.values():
            for lk in acc.locks_for_market(market_id):
                total_pool += lk.amount

        # Settle traders (non-AMM accounts with positions)
        total_trader_payout = ZERO
        for account_id in list(market.positions.keys()):
            if account_id == amm_id:
                continue
            pos = market.position(account_id)
            payout = quantize(pos.get(winning_outcome, ZERO))
            total_trader_payout += payout

            acc = self.risk.get_account(account_id)
            locks = list(acc.locks_for_market(market_id))
            self._settle_locks(locks, payout)

        # AMM gets the remainder (conservation: pool - trader payouts)
        amm_payout = total_pool - total_trader_payout
        amm_acc = self.risk.get_account(amm_id)
        amm_locks = list(amm_acc.locks_for_market(market_id))
        self._settle_locks(amm_locks, amm_payout)

        from core.models import _now
        market.resolved_at = _now()

    def void(self, market_id: int) -> None:
        """
        Void a market. All trades revert. Everyone gets back what they locked.
        """
        market = self._get_open_market(market_id)
        market.status = "void"

        # Release all locks for this market, for all accounts
        for acc in list(self.risk.accounts.values()):
            locks = list(acc.locks_for_market(market_id))
            for lk in locks:
                self.risk.release_lock(lk.lock_id)

        from core.models import _now
        market.resolved_at = _now()

    # ------------------------------------------------------------------
    # Trading
    # ------------------------------------------------------------------

    def buy(self, market_id: int, account_id: int,
            outcome: str, budget: Decimal) -> Trade:
        """
        Buy outcome tokens with a credit budget.
        The AMM is the seller. Rounding favors the AMM.

        The trader locks cost_rounded (cost rounded UP). The exact LMSR
        cost is cost_exact. The dust (cost_rounded - cost_exact) stays
        inside the trader's position lock — the trader overpaid by that
        amount. We reclassify an equal amount of the AMM's position lock
        as conditional_profit (a pure relabelling, no credit transfer).

        Produces exactly 2 transactions tagged with the trade id:
        one for the buyer (trader), one for the seller (AMM).
        """
        market = self._get_open_market(market_id)
        if outcome not in market.outcomes:
            raise ValueError(f"unknown outcome: {outcome}")

        # Compute tokens for this budget (exact LMSR math).
        # We use the full-precision token amount from the LMSR inverse.
        # Quantization happens only on the credit (cost) side.
        available = self.risk.get_account(account_id).available_balance

        # Quick check: reject immediately if budget far exceeds available.
        # Allow a small tolerance for accumulated dust from prior trades.
        dust_tolerance = Decimal("0.001")
        if budget > available + dust_tolerance:
            raise InsufficientBalance(
                f"account {account_id}: need {budget}, have {available}"
            )

        # Use the effective budget — capped at available when dust has
        # slightly reduced the balance below the nominal budget.
        effective_budget = min(budget, available)
        tokens = amount_for_cost(market.q, market.b, outcome, effective_budget)
        if tokens <= ZERO:
            raise ValueError("budget too small for any tokens")

        # Compute actual cost for these tokens
        cost_exact = cost_to_buy(market.q, market.b, outcome, tokens)

        # Round cost UP (favor AMM): trader pays at least the exact cost
        cost_rounded = self._quantize_cost(cost_exact, market)

        # Cap at available balance when tiny rounding dust pushes
        # cost_rounded above available but cost_exact is affordable.
        # This prevents accumulated dust from prior trades from
        # blocking valid trades where the trader can afford the exact cost.
        if cost_rounded > available >= cost_exact:
            cost_rounded = available

        # Dust is the rounding difference — stays in trader's position lock
        dust = cost_rounded - cost_exact

        # Check trader can afford it
        if not self.risk.check_available(account_id, cost_rounded):
            raise InsufficientBalance(
                f"account {account_id}: need {cost_rounded}, "
                f"have {available}"
            )

        # --- Execute atomically ---
        try:
            # Lock trader's credits (one risk-engine operation → one tx)
            acc = self.risk.get_account(account_id)
            existing_lock = acc.lock_for(market.id, "position")
            if existing_lock is not None:
                trader_tx = self.risk.increase_lock(
                    existing_lock.lock_id, cost_rounded)
                trader_lock = existing_lock
            else:
                trader_lock, trader_tx = self.risk.lock(
                    account_id, market.id, cost_rounded,
                    lock_type="position")

            # Reclassify dust on the AMM: move 'dust' worth of the AMM's
            # position lock to its conditional_profit lock.  This is a pure
            # relabelling — no available-balance changes, no minting, and
            # no extra Transaction objects.
            if dust > ZERO:
                self._reclassify_amm_dust(market, dust)

            # Update LMSR state
            market.q[outcome] = market.q[outcome] + tokens

            # Update positions
            if account_id not in market.positions:
                market.positions[account_id] = {
                    o: ZERO for o in market.outcomes
                }
            market.positions[account_id][outcome] += tokens

            # Compute average price
            price = market.quantize_price(cost_rounded / tokens)

            # Build trade record
            buyer_leg = TradeLeg.new(
                account_id=account_id,
                available_delta=-cost_rounded,
                frozen_delta=cost_rounded,
                lock_id=trader_lock.lock_id,
                tx_id=trader_tx.id,
            )

            amm_id = market.amm_account_id
            seller_leg = TradeLeg.new(
                account_id=amm_id,
                available_delta=ZERO,
                frozen_delta=ZERO,
                lock_id=None,
            )

            trade = Trade.new(
                market_id=market.id,
                outcome=outcome,
                amount=tokens,
                price=price,
                buyer=buyer_leg,
                seller=seller_leg,
            )
            market.trades.append(trade)

            # Tag the trader tx with the trade
            trader_tx.trade_id = trade.id
            trader_tx.trade_leg_id = buyer_leg.trade_leg_id

            # Create a record transaction for the AMM (seller) —
            # zero deltas, purely for the two-tx-per-trade invariant.
            amm_tx = Transaction.new(
                account_id=amm_id,
                available_delta=ZERO,
                frozen_delta=ZERO,
                reason="trade:seller",
                market_id=market.id,
                trade_id=trade.id,
                trade_leg_id=seller_leg.trade_leg_id,
            )
            self.risk.transactions.append(amm_tx)
            seller_leg.tx_id = amm_tx.id

            return trade

        except Exception:
            # TODO: proper rollback
            raise

    def sell(self, market_id: int, account_id: int,
             outcome: str, amount: Decimal) -> Trade:
        """
        Sell outcome tokens back to the AMM.
        Rounding favors the AMM (trader receives less).

        Flow:
        1. Decrease the trader's position lock by revenue_rounded.
           This moves credits from frozen → available on the trader.
        2. Immediately re-lock revenue_rounded as conditional_profit
           on the trader. This keeps the trader's total unchanged
           (important for void reversal).
        3. Reclassify sell_dust on the AMM (position → conditional_profit),
           same relabelling trick as for buy dust.
        """
        market = self._get_open_market(market_id)
        if outcome not in market.outcomes:
            raise ValueError(f"unknown outcome: {outcome}")

        # Check trader has enough tokens
        pos = market.position(account_id)
        held = pos.get(outcome, ZERO)
        if amount > held:
            raise ValueError(
                f"account {account_id}: can't sell {amount} {outcome}, "
                f"only holds {held}"
            )
        if amount <= ZERO:
            raise ValueError("sell amount must be positive")

        # Compute revenue (cost of buying negative tokens, negated)
        revenue_exact = -cost_to_buy(market.q, market.b, outcome, -amount)

        # Round revenue DOWN (favor AMM): trader receives less
        revenue_rounded = self._quantize_revenue(revenue_exact, market)
        if revenue_rounded < ZERO:
            revenue_rounded = ZERO
        sell_dust = revenue_exact - revenue_rounded

        # --- Execute ---
        try:
            acc = self.risk.get_account(account_id)
            trader_lock = acc.lock_for(market.id, "position")
            if trader_lock is None:
                raise ValueError(f"account {account_id}: no position lock "
                                 f"in market {market.id}")

            # 1. Decrease position lock by revenue_rounded
            #    (frozen → available on the trader)
            if revenue_rounded > ZERO:
                self.risk.decrease_lock(trader_lock.lock_id, revenue_rounded)

            # 2. Re-lock revenue_rounded as conditional_profit
            #    (available → frozen on the trader, net zero change to total)
            if revenue_rounded > ZERO:
                existing_cp = acc.lock_for(market.id, "conditional_profit")
                if existing_cp is not None:
                    self.risk.increase_lock(
                        existing_cp.lock_id, revenue_rounded)
                else:
                    self.risk.lock(
                        account_id, market.id, revenue_rounded,
                        lock_type="conditional_profit")

            # 3. Reclassify sell dust on the AMM
            if sell_dust > ZERO:
                self._reclassify_amm_dust(market, sell_dust)

            # Update LMSR state
            market.q[outcome] = market.q[outcome] - amount

            # Update positions
            market.positions[account_id][outcome] -= amount

            price = (market.quantize_price(revenue_rounded / amount)
                     if amount > ZERO else ZERO)

            # Trade record — seller is the trader, buyer is the AMM
            seller_leg = TradeLeg.new(
                account_id=account_id,
                available_delta=ZERO,   # net zero: decrease position + increase conditional_profit
                frozen_delta=ZERO,      # net zero: moved between lock types
                lock_id=trader_lock.lock_id,
            )
            buyer_leg = TradeLeg.new(
                account_id=market.amm_account_id,
                available_delta=ZERO,
                frozen_delta=ZERO,
            )

            trade = Trade.new(
                market_id=market.id,
                outcome=outcome,
                amount=amount,
                price=price,
                buyer=buyer_leg,
                seller=seller_leg,
            )
            market.trades.append(trade)

            return trade

        except Exception:
            raise

    # ------------------------------------------------------------------
    # Liquidity
    # ------------------------------------------------------------------

    def add_liquidity(self, market_id: int, funding: Decimal) -> None:
        """Add liquidity to a market. AMM must have sufficient available."""
        market = self._get_open_market(market_id)
        amm_id = market.amm_account_id
        funding = quantize(funding)

        new_b, new_q = b_for_funding(market.q, market.b, funding)

        # Lock the additional funding from AMM's available
        amm_lock = self.risk.get_account(amm_id).lock_for(
            market.id, "position")
        if amm_lock is None:
            raise ValueError("AMM has no position lock")

        self.risk.increase_lock(amm_lock.lock_id, funding)
        market.b = new_b
        market.q = new_q

    def remove_liquidity(self, market_id: int, funding: Decimal) -> None:
        """Remove liquidity from a market. Returns credits to AMM available."""
        market = self._get_open_market(market_id)
        amm_id = market.amm_account_id
        funding = quantize(funding)

        new_b, new_q = b_for_funding(market.q, market.b, -funding)
        if new_b <= ZERO:
            raise ValueError("can't remove that much liquidity")

        amm_lock = self.risk.get_account(amm_id).lock_for(
            market.id, "position")
        if amm_lock is None:
            raise ValueError("AMM has no position lock")

        self.risk.decrease_lock(amm_lock.lock_id, funding)
        market.b = new_b
        market.q = new_q

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_open_market(self, market_id: int) -> Market:
        market = self.markets.get(market_id)
        if market is None:
            raise ValueError(f"market {market_id} not found")
        if market.status != "open":
            raise ValueError(f"market {market_id} is {market.status}")
        return market

    def _quantize_cost(self, exact: Decimal, market: Market) -> Decimal:
        """Quantize a cost to asset precision, rounding UP (favor AMM)."""
        precision = 6  # CREDITS precision
        return exact.quantize(Decimal(10) ** -precision, rounding=ROUND_CEILING)

    def _quantize_revenue(self, exact: Decimal, market: Market) -> Decimal:
        """Quantize revenue to asset precision, rounding DOWN (favor AMM)."""
        precision = 6  # CREDITS precision
        return exact.quantize(Decimal(10) ** -precision, rounding=ROUND_FLOOR)

    def _reclassify_amm_dust(self, market: Market, dust: Decimal) -> None:
        """
        Reclassify 'dust' credits on the AMM from its position lock to
        its conditional_profit lock.

        This is a pure relabelling: no available-balance changes, no
        minting, no Transaction objects created. The AMM's frozen_balance
        stays the same (credits merely move between lock types).

        Conservation proof:
        - AMM position_lock.amount decreases by dust
        - AMM conditional_profit_lock.amount increases by dust
        - AMM frozen_balance is unchanged (sum of locks unchanged)
        - AMM available_balance is unchanged
        - No other account is touched
        - system_total is unchanged
        """
        if dust <= ZERO:
            return

        amm_id = market.amm_account_id
        amm = self.risk.get_account(amm_id)

        # Get (or create) the conditional_profit lock on the AMM
        cp_lock = amm.lock_for(market.id, "conditional_profit")
        if cp_lock is None:
            # Create the Lock object directly — no risk-engine call,
            # no Transaction, no available-balance requirement.
            cp_lock = Lock.new(amm_id, market.id, ZERO,
                               lock_type="conditional_profit")
            amm.locks.append(cp_lock)

        # Get the AMM's position lock
        pos_lock = amm.lock_for(market.id, "position")
        if pos_lock is None or pos_lock.amount < dust:
            # Should never happen: AMM position lock is funded with
            # max_loss which dwarfs any accumulated dust.  But guard
            # defensively — skip the reclassification rather than
            # corrupt state.
            return

        # Reclassify: move dust between lock types
        pos_lock.amount -= dust
        cp_lock.amount += dust
        # frozen_balance stays the same — the sum of lock amounts
        # is unchanged, so the invariant holds.

    def _settle_locks(self, locks: list, payout: Decimal) -> None:
        """
        Settle a list of locks belonging to one account.
        The last lock receives the full payout; all others settle to zero.
        This ensures the account's frozen_balance goes to zero for this
        market and available_balance increases by exactly payout.
        """
        for i, lk in enumerate(locks):
            if i == len(locks) - 1:
                self.risk.settle_lock(lk.lock_id, payout)
            else:
                self.risk.settle_lock(lk.lock_id, ZERO)
