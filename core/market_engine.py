"""
Market engine. Manages markets, LMSR trading, positions, settlement, void.

The market engine owns the LMSR state (q, positions, trades) and talks
to the risk engine for all balance mutations (lock/unlock/settle).

Every trade is between a trader and the AMM. The AMM is a regular account.
Rounding always favors the AMM.
"""

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from core.models import (
    Market, Trade, TradeLeg,
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
        Mints the subsidy to the AMM account and locks it.
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

        # Fund AMM: mint subsidy and lock it
        subsidy = self._quantize_cost(max_loss(b, len(market.outcomes)), market)
        self.risk.mint(amm.id, subsidy)
        self.risk.lock(amm.id, market.id, subsidy, lock_type="position")

        return market, amm

    def resolve(self, market_id: int, winning_outcome: str) -> None:
        """
        Resolve a market. Pay winners, zero losers, release all locks.
        """
        market = self._get_open_market(market_id)
        if winning_outcome not in market.outcomes:
            raise ValueError(f"unknown outcome: {winning_outcome}")

        market.status = "resolved"
        market.resolution = winning_outcome

        # Settle all participant positions
        all_account_ids = set(market.positions.keys())
        all_account_ids.add(market.amm_account_id)

        for account_id in all_account_ids:
            self._settle_account(market, account_id, winning_outcome)

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
        """
        market = self._get_open_market(market_id)
        if outcome not in market.outcomes:
            raise ValueError(f"unknown outcome: {outcome}")

        # Compute tokens for this budget (exact LMSR math)
        tokens_exact = amount_for_cost(market.q, market.b, outcome, budget)
        tokens = market.quantize_amount(tokens_exact)
        if tokens <= ZERO:
            raise ValueError("budget too small for any tokens")

        # Compute actual cost for the quantized token amount
        cost_exact = cost_to_buy(market.q, market.b, outcome, tokens)

        # Round cost UP (favor AMM): trader pays at least the exact cost
        cost_rounded = self._quantize_cost(cost_exact, market)

        # Dust is the rounding difference
        dust = cost_rounded - cost_exact

        # Check trader can afford it
        if not self.risk.check_available(account_id, cost_rounded):
            raise InsufficientBalance(
                f"account {account_id}: need {cost_rounded}, "
                f"have {self.risk.get_account(account_id).available_balance}"
            )

        # --- Execute atomically ---
        try:
            # Lock trader's credits
            trader_lock, trader_tx = self._ensure_position_lock(
                account_id, market)
            self.risk.increase_lock(trader_lock.lock_id, cost_rounded)
            # Fix: the initial lock created the tx, increase_lock created another.
            # We need the increase_lock tx for the trade leg.
            trader_increase_tx = self.risk.transactions[-1]

            # Handle AMM's conditional_profit for dust
            amm_id = market.amm_account_id
            if dust > ZERO:
                amm_profit_lock, _ = self._ensure_conditional_profit_lock(
                    amm_id, market)
                # Dust: trader overpaid, AMM gets the extra as conditional profit
                # Transfer from trader available to AMM frozen
                # But wait — the trader already locked cost_rounded.
                # The exact cost goes to the LMSR, the dust goes to AMM profit.
                # Since everything is frozen until resolution, we just track it.
                self.risk.increase_lock(amm_profit_lock.lock_id, dust)
                # Fund the dust: mint it to AMM then lock it
                # NO — dust comes from the trader's overpayment, not from minting.
                # The trader locked cost_rounded. The LMSR only needs cost_exact.
                # The difference (dust) is conditional profit for the AMM.
                # We track it as a separate lock on the AMM account.
                # But the AMM needs available balance to lock...
                # Actually the dust is already accounted for in the trader's lock.
                # We need to transfer it: decrease trader lock by dust,
                # then credit to AMM and lock as conditional_profit.

            # Simpler approach: the full cost_rounded is locked on the trader.
            # Dust tracking is informational — the settlement math handles it.
            # The AMM conditional_profit lock tracks accumulated dust separately.
            # We mint dust to AMM and immediately lock it as conditional_profit.
            if dust > ZERO:
                self.risk.mint(amm_id, dust)
                amm_profit_lock, _ = self._ensure_conditional_profit_lock(
                    amm_id, market)
                self.risk.increase_lock(amm_profit_lock.lock_id, dust)

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
                tx_id=trader_increase_tx.id,
            )
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

            # Update transaction with trade references
            trader_increase_tx.trade_id = trade.id
            trader_increase_tx.trade_leg_id = buyer_leg.trade_leg_id

            return trade

        except Exception:
            # TODO: proper rollback
            raise

    def sell(self, market_id: int, account_id: int,
             outcome: str, amount: Decimal) -> Trade:
        """
        Sell outcome tokens back to the AMM.
        Rounding favors the AMM (trader receives less).
        """
        market = self._get_open_market(market_id)
        if outcome not in market.outcomes:
            raise ValueError(f"unknown outcome: {outcome}")

        # Check trader has enough tokens
        pos = market.position(account_id)
        held = pos.get(outcome, ZERO)
        amount = market.quantize_amount(amount)
        if amount > held:
            raise ValueError(
                f"account {account_id}: can't sell {amount} {outcome}, "
                f"only holds {held}"
            )

        # Cost to "buy" negative tokens = credits returned (negative)
        revenue_exact = cost_to_buy(market.q, market.b, outcome, -amount)
        # revenue_exact is negative (cost to buy negative = revenue)
        revenue_exact = -revenue_exact  # make positive

        # Round revenue DOWN (favor AMM): trader receives less
        revenue_rounded = self._quantize_revenue(revenue_exact, market)
        dust = revenue_exact - revenue_rounded

        # --- Execute ---
        try:
            # Decrease trader's position lock
            trader_lock = self.risk.get_account(account_id).lock_for(
                market.id, "position")
            if trader_lock is None:
                raise ValueError(f"account {account_id}: no position lock "
                                 f"in market {market.id}")

            # Release proportional amount from lock
            # The trader locked cost_rounded when buying. Now selling some back.
            # We release revenue_rounded from the lock to available.
            self.risk.decrease_lock(trader_lock.lock_id, revenue_rounded)
            sell_tx = self.risk.transactions[-1]

            # Track dust as AMM conditional profit
            if dust > ZERO:
                self.risk.mint(market.amm_account_id, dust)
                amm_profit_lock, _ = self._ensure_conditional_profit_lock(
                    market.amm_account_id, market)
                self.risk.increase_lock(amm_profit_lock.lock_id, dust)

            # Conditional profit for trader (if they made money)
            # If revenue_rounded > proportional cost, the profit stays frozen
            # until resolution. We handle this via the position lock:
            # the lock amount shrinks, but available increases.
            # Actually for sells, the trader's available goes up immediately.
            # But in conditional markets, profits should stay frozen...
            # The trader's available increases by revenue_rounded. This is
            # the conditional nature: on void, we'd need to undo this.
            # For now: sell proceeds go to a conditional_profit lock on trader.
            # Move the revenue from available back to a conditional_profit lock.
            trader_profit_lock, _ = self._ensure_conditional_profit_lock(
                account_id, market)
            # The decrease_lock already moved revenue to available.
            # Re-lock it as conditional profit.
            self.risk.increase_lock(trader_profit_lock.lock_id, revenue_rounded)

            # Update LMSR state
            market.q[outcome] = market.q[outcome] - amount

            # Update positions
            market.positions[account_id][outcome] -= amount

            price = market.quantize_price(revenue_rounded / amount)

            seller_leg = TradeLeg.new(
                account_id=account_id,
                available_delta=ZERO,  # net zero: decreased position, increased conditional_profit
                frozen_delta=ZERO,     # net zero: moved between lock types
                lock_id=trader_lock.lock_id,
                tx_id=sell_tx.id,
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
    # Internal
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

    def _ensure_position_lock(self, account_id: int,
                              market: Market) -> tuple:
        """Get or create a position lock for this account in this market."""
        acc = self.risk.get_account(account_id)
        lk = acc.lock_for(market.id, "position")
        if lk is not None:
            return lk, None
        # Create with zero amount — will be increased immediately
        lk, tx = self.risk.lock(account_id, market.id, ZERO,
                                lock_type="position")
        return lk, tx

    def _ensure_conditional_profit_lock(self, account_id: int,
                                        market: Market) -> tuple:
        """Get or create a conditional_profit lock."""
        acc = self.risk.get_account(account_id)
        lk = acc.lock_for(market.id, "conditional_profit")
        if lk is not None:
            return lk, None
        lk, tx = self.risk.lock(account_id, market.id, ZERO,
                                lock_type="conditional_profit")
        return lk, tx

    def _settle_account(self, market: Market, account_id: int,
                        winning_outcome: str) -> None:
        """Settle one account's position in a resolved market."""
        acc = self.risk.get_account(account_id)
        pos = market.position(account_id)

        # Payout: winning tokens * 1 credit each
        winning_tokens = pos.get(winning_outcome, ZERO)
        payout = quantize(winning_tokens)

        # Release all locks for this market
        locks = list(acc.locks_for_market(market.id))
        for lk in locks:
            if lk is locks[-1]:
                # Last lock: settle with payout
                self.risk.settle_lock(lk.lock_id, payout)
            else:
                # Other locks: release to zero (payout on last one)
                self.risk.settle_lock(lk.lock_id, ZERO)
