"""
Market engine. Manages markets, LMSR trading, positions, settlement, void.

The market engine owns the LMSR state (q, positions, trades) and talks
to the risk engine for all balance mutations (lock/unlock/settle).

Every trade is between a trader and the AMM. The AMM is a regular account.

Precision model (inspired by the matching engine):
  ASSET_PRECISION = price_precision + amount_precision
  - Prices quantized to price_precision (rounding favors AMM)
  - Token amounts quantized to amount_precision
  - Trade value = |amount| * price — exact at ASSET_PRECISION, no rounding
  Only the price is rounded. Cost/revenue rounding is eliminated.

Position close (proportional, inspired by liquidation_calculate_position_update):
  Each outcome has its own position lock (position:yes, position:no, etc.).
  On each sell, collateral is released proportionally from that outcome's lock:
    close_margin = floor(outcome_lock * sell_amount / outcome_held)
  Collateral (close_margin) goes back to available immediately.
  PnL handling:
    pnl > 0 → profit stays frozen as conditional_profit (funded by AMM lock)
    pnl < 0 → loss re-frozen as conditional_loss (from trader's available)
  Invariant: position[outcome] == 0 → position:{outcome} lock == 0.

Void semantics (no clawbacks):
  - Position locks → trader available (face value)
  - conditional_loss → trader available (loss forgiven)
  - conditional_profit → AMM available (profit returned to source)
  Everyone gets back exactly their original deposit.

Buy and sell share a single execution core (_execute_trade).
"""

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from core.models import (
    Market, Trade, TradeLeg, Transaction,
    ZERO, quantize, next_id,
    ASSET_PRECISION,
)
from core.lmsr import (
    cost as lmsr_cost, cost_to_buy, amount_for_cost, prices,
    liquidity_cost, b_for_funding, max_loss,
)
from core.risk_engine import RiskEngine, InsufficientBalance


# Credit precision quantum (ASSET_PRECISION)
_CREDIT_QUANTUM = Decimal(10) ** -ASSET_PRECISION.get("CREDITS", 6)


class MarketEngine:

    def __init__(self, risk: RiskEngine):
        self.risk = risk
        self.markets: dict[int, Market] = {}

    # ------------------------------------------------------------------
    # Market lifecycle
    # ------------------------------------------------------------------

    def create_market(self, question: str, category: str, category_id: str,
                      metadata: dict, b: Decimal = Decimal("100"),
                      price_precision: int = 4,
                      amount_precision: int = 2,
                      outcomes: list[str] = None,
                      deadline: str = None,
                      funding_account_id: int | None = None,
                      ) -> tuple[Market, 'Account']:
        """
        Create a market with a funded AMM.

        If funding_account_id is provided, transfers the subsidy from that
        account to the AMM (treasury mode). Otherwise mints fresh credits.
        """
        amm = self.risk.create_account()
        market = Market.new(
            question=question,
            category=category,
            category_id=category_id,
            metadata=metadata,
            amm_account_id=amm.id,
            b=b,
            price_precision=price_precision,
            amount_precision=amount_precision,
            outcomes=outcomes,
            deadline=deadline,
        )
        self.markets[market.id] = market

        subsidy = max_loss(b, len(market.outcomes))
        if funding_account_id is not None:
            self.risk.transfer_available(
                funding_account_id, amm.id, subsidy,
                market_id=market.id, reason="market_funding")
        else:
            self.risk.mint(amm.id, subsidy)
        self.risk.lock(amm.id, market.id, subsidy, lock_type="position")

        return market, amm

    def resolve(self, market_id: int, winning_outcome: str) -> None:
        """
        Resolve a market. Settle all positions and release all locks.

        Settlement:
        - conditional_profit → trader available (profit realized)
        - conditional_loss → payout 0, loss realized (goes to AMM via pool)
        - position lock → settles at winning_tokens value
        - AMM gets the remainder of the pool
        - Conservation: sum(all payouts) == sum(all lock amounts) == pool
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

        # Settle traders
        total_trader_payout = ZERO
        for account_id in list(market.positions.keys()):
            if account_id == amm_id:
                continue
            pos = market.position(account_id)
            winning_tokens = quantize(pos.get(winning_outcome, ZERO))

            acc = self.risk.get_account(account_id)

            # CP releases at face value (profit realized)
            cp_lock = acc.lock_for(market_id, "conditional_profit")
            cp_amount = cp_lock.amount if cp_lock else ZERO
            if cp_lock:
                self.risk.settle_lock(cp_lock.lock_id, cp_amount)

            # CL settles at 0 (loss realized, goes to AMM via pool)
            cl_lock = acc.lock_for(market_id, "conditional_loss")
            if cl_lock:
                self.risk.settle_lock(cl_lock.lock_id, ZERO)

            # Per-outcome position locks: winning → token value, losing → 0
            for outcome_name in market.outcomes:
                outcome_lock = acc.lock_for(
                    market_id, f"position:{outcome_name}")
                if outcome_lock:
                    if outcome_name == winning_outcome:
                        self.risk.settle_lock(
                            outcome_lock.lock_id, winning_tokens)
                    else:
                        self.risk.settle_lock(outcome_lock.lock_id, ZERO)

            trader_payout = winning_tokens + cp_amount
            total_trader_payout += trader_payout

        # AMM gets the remainder
        amm_payout = total_pool - total_trader_payout
        amm_acc = self.risk.get_account(amm_id)
        amm_pos = amm_acc.lock_for(market_id, "position")
        if amm_pos:
            self.risk.settle_lock(amm_pos.lock_id, amm_payout)

        from core.models import _now
        market.resolved_at = _now()

    def void(self, market_id: int) -> None:
        """
        Void a market. No clawbacks. Everyone gets back their deposit.

        - Position locks → owner's available (face value)
        - conditional_loss → owner's available (loss forgiven)
        - conditional_profit → AMM's available (profit returned to source)
        """
        market = self._get_open_market(market_id)
        market.status = "void"

        amm_id = market.amm_account_id

        for acc in list(self.risk.accounts.values()):
            locks = list(acc.locks_for_market(market_id))
            for lk in locks:
                if lk.lock_type == "conditional_profit" and acc.id != amm_id:
                    # Return conditional profit to AMM (it was funded by AMM)
                    amount = lk.amount
                    self.risk.release_lock(lk.lock_id)
                    self.risk.transfer_available(
                        acc.id, amm_id, amount,
                        market_id=market_id, reason="void_return_cp")
                else:
                    # Position and conditional_loss: release to owner
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

        Computes max tokens for budget, quantizes to amount_precision
        (ROUND_FLOOR — trader gets fewer tokens). Computes average price
        at price_precision (ROUND_CEILING — trader pays more). Trade value
        = tokens * price is exact at ASSET_PRECISION.
        """
        market = self._get_open_market(market_id)
        if outcome not in market.outcomes:
            raise ValueError(f"unknown outcome: {outcome}")

        available = self.risk.get_account(account_id).available_balance
        if budget > available:
            raise InsufficientBalance(
                f"account {account_id}: need {budget}, have {available}")

        amount_quantum = Decimal(10) ** -market.amount_precision
        price_quantum = Decimal(10) ** -market.price_precision

        # Compute tokens from budget, quantize DOWN (fewer tokens)
        tokens_raw = amount_for_cost(market.q, market.b, outcome, budget)
        tokens = tokens_raw.quantize(amount_quantum, rounding=ROUND_FLOOR)
        if tokens <= ZERO:
            raise ValueError("budget too small for any tokens")

        # Compute average price (ROUND_CEILING — trader pays more)
        exact_cost = cost_to_buy(market.q, market.b, outcome, tokens)
        avg_price = (exact_cost / tokens).quantize(
            price_quantum, rounding=ROUND_CEILING)

        # Trade value: exact at ASSET_PRECISION (no rounding needed)
        trade_value = tokens * avg_price

        # Price rounding may push cost above available — reduce by one tick
        if trade_value > available:
            tokens -= amount_quantum
            if tokens <= ZERO:
                raise ValueError("budget too small for any tokens")
            exact_cost = cost_to_buy(market.q, market.b, outcome, tokens)
            avg_price = (exact_cost / tokens).quantize(
                price_quantum, rounding=ROUND_CEILING)
            trade_value = tokens * avg_price

        if trade_value > available:
            raise InsufficientBalance(
                f"account {account_id}: need {trade_value}, have {available}")

        return self._execute_trade(
            market, account_id, outcome, tokens, avg_price, trade_value)

    def sell(self, market_id: int, account_id: int,
             outcome: str, amount: Decimal) -> Trade:
        """
        Sell outcome tokens back to the AMM.

        Amount must be at amount_precision. Average price is rounded
        DOWN (ROUND_FLOOR — trader receives less). Trade value =
        amount * price is exact at ASSET_PRECISION.

        Margin is released proportionally from position lock. PnL is
        transferred between trader and AMM via transfer_frozen.
        """
        market = self._get_open_market(market_id)
        if outcome not in market.outcomes:
            raise ValueError(f"unknown outcome: {outcome}")

        amount_quantum = Decimal(10) ** -market.amount_precision
        price_quantum = Decimal(10) ** -market.price_precision

        # Validate precision
        if amount != amount.quantize(amount_quantum):
            raise ValueError(
                f"sell amount {amount} exceeds precision "
                f"(max {market.amount_precision} dp)")

        # Check trader has enough tokens
        pos = market.position(account_id)
        held = pos.get(outcome, ZERO)
        if amount > held:
            raise ValueError(
                f"account {account_id}: can't sell {amount} {outcome}, "
                f"only holds {held}")
        if amount <= ZERO:
            raise ValueError("sell amount must be positive")

        # Compute revenue from LMSR
        exact_revenue = -cost_to_buy(market.q, market.b, outcome, -amount)

        # Compute average price (ROUND_FLOOR — trader receives less)
        avg_price = (exact_revenue / amount).quantize(
            price_quantum, rounding=ROUND_FLOOR)
        if avg_price < ZERO:
            avg_price = ZERO

        # Trade value: exact at ASSET_PRECISION
        trade_value = amount * avg_price

        return self._execute_trade(
            market, account_id, outcome, -amount, avg_price, trade_value)

    def _execute_trade(self, market: Market, account_id: int,
                       outcome: str, signed_amount: Decimal,
                       avg_price: Decimal, trade_value: Decimal) -> Trade:
        """
        Core trade execution. Symmetric for buys and sells.

        signed_amount > 0: buy (open position)
        signed_amount < 0: sell (close position)
        avg_price: always positive, quantized to price_precision
        trade_value: |amount| * price, exact at ASSET_PRECISION

        For buys: trade_value is locked as position margin.
        For sells: proportional margin is released from position lock,
        revenue goes to CP lock, PnL transferred between trader and AMM.
        """
        from core.models import _now

        acc = self.risk.get_account(account_id)
        amm_id = market.amm_account_id

        # Pre-allocate trade ID so risk operations can reference it
        trade_id = next_id("trade")

        pos_lock_type = f"position:{outcome}"

        if signed_amount > ZERO:
            # --- OPEN: lock trade_value as position margin ---
            trader_lock = acc.lock_for(market.id, pos_lock_type)
            if trader_lock is not None:
                trader_tx = self.risk.increase_lock(
                    trader_lock.lock_id, trade_value,
                    trade_id=trade_id)
            else:
                trader_lock, trader_tx = self.risk.lock(
                    account_id, market.id, trade_value,
                    lock_type=pos_lock_type, trade_id=trade_id)

            trader_leg = TradeLeg.new(
                account_id=account_id,
                available_delta=-trade_value,
                frozen_delta=trade_value,
                lock_id=trader_lock.lock_id,
                tx_id=trader_tx.id,
            )
            amm_leg = TradeLeg.new(
                account_id=amm_id,
                available_delta=ZERO,
                frozen_delta=ZERO,
            )

        else:
            # --- CLOSE: collateral released, PnL conditional ---
            sell_amount = abs(signed_amount)
            held = market.position(account_id).get(outcome, ZERO)
            trader_lock = acc.lock_for(market.id, pos_lock_type)

            if trader_lock is None:
                raise ValueError(
                    f"account {account_id}: no {pos_lock_type} lock "
                    f"in market {market.id}")

            # Proportional margin release (floor — keeps remaining safe)
            if held == sell_amount:
                close_margin = trader_lock.amount
            else:
                close_margin = (
                    trader_lock.amount * sell_amount / held
                ).quantize(_CREDIT_QUANTUM, rounding=ROUND_FLOOR)

            revenue = trade_value
            pnl = revenue - close_margin

            # Step 1: Release collateral (close_margin) → available
            if close_margin > ZERO:
                self.risk.decrease_lock(trader_lock.lock_id, close_margin)

            # Step 2: Handle PnL
            if pnl > ZERO:
                # Profit: AMM position lock → trader's CP (frozen-to-frozen)
                # Collateral is available, profit stays conditional
                amm_pos_lock = self.risk.get_account(amm_id).lock_for(
                    market.id, "position")
                self.risk.transfer_frozen(
                    from_lock_id=amm_pos_lock.lock_id,
                    to_account_id=account_id,
                    amount=pnl,
                    market_id=market.id,
                    to_lock_type="conditional_profit",
                    reason="trade_pnl",
                )
            elif pnl < ZERO:
                # Loss: re-freeze |loss| from available as conditional_loss
                # Net available change = close_margin - |loss| = revenue
                loss = abs(pnl)
                cl_lock = acc.lock_for(market.id, "conditional_loss")
                if cl_lock is not None:
                    self.risk.increase_lock(cl_lock.lock_id, loss)
                else:
                    self.risk.lock(
                        account_id, market.id, loss,
                        lock_type="conditional_loss")

            # Step 3: Net CP and CL
            # If trader has both, the smaller one is fully consumed.
            # CP returns to AMM (was AMM's money), CL releases to available.
            cp = acc.lock_for(market.id, "conditional_profit")
            cl = acc.lock_for(market.id, "conditional_loss")
            if cp and cl:
                net_amount = min(cp.amount, cl.amount)
                # Return CP portion to AMM's position lock
                self.risk.transfer_frozen(
                    from_lock_id=cp.lock_id,
                    to_account_id=amm_id,
                    amount=net_amount,
                    market_id=market.id,
                    to_lock_type="position",
                    reason="pnl_net",
                )
                # Release same from CL → available (loss offset)
                self.risk.decrease_lock(cl.lock_id, net_amount)

            # Compute net balance deltas for trade legs:
            #   Profit: available += close_margin, frozen += pnl - close_margin
            #   Loss:   available += revenue,       frozen -= revenue
            #   Zero:   available += close_margin,  frozen -= close_margin
            if pnl >= ZERO:
                trader_avail_delta = close_margin
                trader_frozen_delta = pnl - close_margin
            else:
                trader_avail_delta = revenue
                trader_frozen_delta = -revenue

            remaining_lock = acc.lock_for(market.id, pos_lock_type)
            trader_leg = TradeLeg.new(
                account_id=account_id,
                available_delta=trader_avail_delta,
                frozen_delta=trader_frozen_delta,
                lock_id=(remaining_lock.lock_id
                         if remaining_lock else None),
            )
            amm_leg = TradeLeg.new(
                account_id=amm_id,
                available_delta=ZERO,
                frozen_delta=-pnl if pnl > ZERO else ZERO,
            )

        # --- Update LMSR state ---
        market.q[outcome] = market.q[outcome] + signed_amount

        # --- Update positions ---
        if account_id not in market.positions:
            market.positions[account_id] = {
                o: ZERO for o in market.outcomes
            }
        market.positions[account_id][outcome] += signed_amount

        # --- Build trade record (uses pre-allocated trade_id) ---
        if signed_amount > ZERO:
            buyer, seller = trader_leg, amm_leg
        else:
            buyer, seller = amm_leg, trader_leg

        trade = Trade(
            id=trade_id,
            market_id=market.id,
            outcome=outcome,
            amount=abs(signed_amount),
            price=avg_price,
            buyer=buyer,
            seller=seller,
            created_at=_now(),
        )
        market.trades.append(trade)

        return trade

    # ------------------------------------------------------------------
    # Liquidity
    # ------------------------------------------------------------------

    def add_liquidity(self, market_id: int, funding: Decimal,
                      funding_account_id: int | None = None) -> None:
        """Add liquidity to a market.

        If funding_account_id is provided, transfers from that account to the
        AMM first. Otherwise the AMM must already have sufficient available.
        """
        market = self._get_open_market(market_id)
        amm_id = market.amm_account_id
        funding = quantize(funding)

        new_b, new_q = b_for_funding(market.q, market.b, funding)

        if funding_account_id is not None:
            self.risk.transfer_available(
                funding_account_id, amm_id, funding,
                market_id=market.id, reason="add_liquidity_funding")

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
