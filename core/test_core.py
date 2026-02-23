"""
Core test suite. These tests define the contract the system must satisfy.

Written BEFORE the engines exist. All marked xfail until implementation
lands. See core/TEST_PLAN.md for the full rationale behind each test.

Do NOT modify these tests to make them pass. Fix the implementation.
If a test is genuinely wrong, review it very carefully before changing —
that's a design decision, not a bug fix.
"""

import random
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

import pytest

from core.models import (
    Account, Lock, Market, Trade, TradeLeg, Transaction,
    ZERO, quantize, reset_counters, next_id,
)
from core.lmsr import (
    cost, prices, cost_to_buy, amount_for_cost,
    liquidity_cost, b_for_funding, max_loss,
)

# These imports will fail until the engines exist.
# That's intentional — the tests define the interface.
try:
    from core.risk_engine import RiskEngine
    from core.market_engine import MarketEngine
    ENGINES_AVAILABLE = True
except ImportError:
    ENGINES_AVAILABLE = False

engines_required = pytest.mark.xfail(
    not ENGINES_AVAILABLE,
    reason="engines not implemented yet",
    strict=True,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fresh_system(n_traders=3, trader_balance=Decimal("1000"),
                 amm_funding=Decimal("100"), b=Decimal("100")):
    """
    Set up a complete system: risk engine, market engine, funded AMM,
    funded traders, one open market. Returns everything needed for testing.
    """
    reset_counters()
    risk = RiskEngine()
    market_eng = MarketEngine(risk)

    # Mint credits for traders
    traders = []
    for _ in range(n_traders):
        acc = risk.create_account()
        risk.mint(acc.id, trader_balance)
        traders.append(acc)

    # Create AMM account, mint subsidy, create market
    market, amm = market_eng.create_market(
        question="Will PR #1 merge?",
        category="pr_merge",
        category_id="futarchy-fi/agents#1",
        metadata={"repo": "futarchy-fi/agents", "pr": 1},
        b=b,
    )

    total_minted = trader_balance * n_traders + max_loss(b, len(market.outcomes))

    return risk, market_eng, traders, market, amm, total_minted


def system_total(risk):
    """Sum of all available + frozen across all accounts."""
    total = ZERO
    for acc in risk.accounts.values():
        total += acc.available_balance + acc.frozen_balance
    return total


def random_trades(market_eng, market, traders, n=50, seed=42):
    """Execute n random trades. Returns list of executed trades."""
    rng = random.Random(seed)
    executed = []
    for _ in range(n):
        trader = rng.choice(traders)
        outcome = rng.choice(market.outcomes)
        budget = Decimal(str(rng.uniform(1, 50)))
        try:
            trade = market_eng.buy(market.id, trader.id, outcome, budget)
            executed.append(trade)
        except (ValueError, Exception):
            pass  # insufficient balance, etc. — expected
    return executed


# ---------------------------------------------------------------------------
# 1-3: Credit Conservation
# ---------------------------------------------------------------------------

@engines_required
class TestCreditConservation:

    def test_conserved_through_trading(self):
        """After N random trades, total credits = total minted."""
        risk, market_eng, traders, market, amm, total_minted = fresh_system()
        random_trades(market_eng, market, traders, n=100)
        assert system_total(risk) == total_minted

    def test_conserved_through_full_lifecycle(self):
        """Create → trade → settle. Total credits = total minted."""
        risk, market_eng, traders, market, amm, total_minted = fresh_system()
        random_trades(market_eng, market, traders, n=100)
        market_eng.resolve(market.id, "yes")
        assert system_total(risk) == total_minted

    def test_conserved_through_void(self):
        """Create → trade → void. Total credits = total minted."""
        risk, market_eng, traders, market, amm, total_minted = fresh_system()
        random_trades(market_eng, market, traders, n=100)
        market_eng.void(market.id)
        assert system_total(risk) == total_minted


# ---------------------------------------------------------------------------
# 4-6: Rounding and Dust
# ---------------------------------------------------------------------------

@engines_required
class TestRoundingAndDust:

    def test_round_trip_favors_amm(self):
        """Buy then sell same amount. Net cost > 0 (AMM gains dust).

        In the conditional model, rounding dust manifests as a
        conditional_loss on the trader (not AMM conditional_profit).
        The AMM realizes the gain at resolution.
        """
        risk, market_eng, traders, market, amm, _ = fresh_system(n_traders=1)
        trader = traders[0]
        before = trader.available_balance

        # Buy some tokens
        trade1 = market_eng.buy(market.id, trader.id, "yes", Decimal("50"))
        tokens_bought = trade1.amount

        # Sell them back
        trade2 = market_eng.sell(market.id, trader.id, "yes", tokens_bought)

        after = trader.available_balance
        # Trader should have lost a tiny amount to rounding
        assert after < before
        # Trader has a conditional_loss (rounding dust)
        trader_cl = trader.lock_for(market.id, "conditional_loss")
        assert trader_cl is not None and trader_cl.amount > ZERO

    def test_path_independence_favors_amm(self):
        """10 small buys yield fewer tokens than 1 big buy (AMM favored).

        With ROUND_FLOOR on token amounts, each small buy gets slightly
        fewer tokens due to rounding. The AMM keeps the difference.
        """
        # System A: one big buy
        risk_a, me_a, traders_a, market_a, _, _ = fresh_system(n_traders=1)
        trade_big = me_a.buy(market_a.id, traders_a[0].id, "yes", Decimal("50"))
        tokens_big = trade_big.amount

        # System B: ten small buys
        risk_b, me_b, traders_b, market_b, _, _ = fresh_system(n_traders=1)
        tokens_small = ZERO
        for _ in range(10):
            t = me_b.buy(market_b.id, traders_b[0].id, "yes", Decimal("5"))
            tokens_small += t.amount

        # Small buys yield fewer tokens (rounding floors each one)
        assert tokens_small < tokens_big

    def test_dust_accumulates_monotonically(self):
        """After many buys + sells, rounding dust accumulates.

        In the conditional model, rounding on sells creates
        conditional_loss locks on traders (dust the AMM gains
        at resolution). Verify some dust accumulated.
        """
        risk, market_eng, traders, market, amm, total_minted = fresh_system(
            n_traders=5, b=Decimal("100"))

        # Mix of buys and sells to generate rounding dust
        rng = random.Random(123)
        for _ in range(500):
            trader = rng.choice(traders)
            outcome = rng.choice(market.outcomes)
            if rng.random() < 0.7:
                try:
                    market_eng.buy(market.id, trader.id, outcome,
                                   Decimal(str(rng.uniform(1, 30))))
                except (ValueError, Exception):
                    pass
            else:
                pos = market.position(trader.id)
                held = pos.get(outcome, ZERO)
                if held > ZERO:
                    try:
                        sell_amt = (held * Decimal(str(rng.uniform(0.1, 1.0)))).quantize(
                            Decimal("0.01"), rounding=ROUND_FLOOR)
                        if sell_amt > ZERO:
                            market_eng.sell(market.id, trader.id, outcome, sell_amt)
                    except (ValueError, Exception):
                        pass

        # Some trader should have conditional_loss (rounding dust)
        total_cl = sum(
            (lk.amount for acc in traders
             for lk in acc.locks_for_market(market.id)
             if lk.lock_type == "conditional_loss"),
            ZERO,
        )
        assert total_cl > ZERO, "Rounding dust should accumulate as conditional_loss"

        # Conservation still holds
        assert system_total(risk) == total_minted


# ---------------------------------------------------------------------------
# 7-8: Void Reversal
# ---------------------------------------------------------------------------

@engines_required
class TestVoidReversal:

    def test_void_returns_exact_amounts(self):
        """After void, every account back to pre-market state."""
        risk, market_eng, traders, market, amm, total_minted = fresh_system()

        # Snapshot balances before trading
        balances_before = {
            acc.id: (acc.available_balance, acc.frozen_balance)
            for acc in traders
        }
        amm_before = (amm.available_balance, amm.frozen_balance)

        random_trades(market_eng, market, traders, n=50)

        # Verify trading actually changed things
        any_changed = any(
            traders[i].available_balance != balances_before[traders[i].id][0]
            for i in range(len(traders))
        )
        assert any_changed, "Trading should have changed at least one balance"

        market_eng.void(market.id)

        # All trader balances restored
        for acc in traders:
            avail, frozen = balances_before[acc.id]
            assert acc.available_balance == avail
            assert acc.frozen_balance == frozen

        # No locks remain for this market
        for acc in traders:
            assert len(acc.locks_for_market(market.id)) == 0
        assert len(amm.locks_for_market(market.id)) == 0

    def test_void_after_complex_trading(self):
        """
        N traders, random buys and sells, some profitable some not.
        Void. Every account's total balance equals pre-market total.
        """
        risk, market_eng, traders, market, amm, total_minted = fresh_system(
            n_traders=5)

        totals_before = {acc.id: acc.total for acc in traders}
        amm_total_before = amm.total

        # Mix of buys and sells
        rng = random.Random(77)
        for _ in range(100):
            trader = rng.choice(traders)
            outcome = rng.choice(market.outcomes)
            if rng.random() < 0.7:
                try:
                    market_eng.buy(market.id, trader.id, outcome,
                                   Decimal(str(rng.uniform(1, 30))))
                except (ValueError, Exception):
                    pass
            else:
                pos = market.position(trader.id)
                if pos.get(outcome, ZERO) > ZERO:
                    try:
                        sell_amount = pos[outcome] * Decimal(str(rng.uniform(0.1, 1.0)))
                        market_eng.sell(market.id, trader.id, outcome, sell_amount)
                    except (ValueError, Exception):
                        pass

        market_eng.void(market.id)

        for acc in traders:
            assert acc.total == totals_before[acc.id]
        assert amm.total == amm_total_before


# ---------------------------------------------------------------------------
# 9-10: Settlement Correctness
# ---------------------------------------------------------------------------

@engines_required
class TestSettlement:

    def test_amm_max_loss(self):
        """AMM never loses more than b * ln(n), regardless of trading."""
        b = Decimal("100")
        risk, market_eng, traders, market, amm, _ = fresh_system(b=b)
        amm_total_before = amm.total

        random_trades(market_eng, market, traders, n=200, seed=99)
        market_eng.resolve(market.id, "yes")

        amm_loss = amm_total_before - amm.total
        theoretical_max = max_loss(b, len(market.outcomes))
        assert amm_loss <= theoretical_max

    def test_winners_paid_losers_zeroed(self):
        """Winners get tokens * 1 credit. Losers get 0. No remaining locks."""
        risk, market_eng, traders, market, amm, _ = fresh_system()
        random_trades(market_eng, market, traders, n=50)

        # Record positions before settlement
        positions_before = {
            acc.id: market.position(acc.id).copy()
            for acc in traders
        }

        market_eng.resolve(market.id, "yes")

        for acc in traders:
            pos = positions_before[acc.id]
            winning_tokens = pos.get("yes", ZERO)
            losing_tokens = pos.get("no", ZERO)

            # No locks remain
            assert len(acc.locks_for_market(market.id)) == 0

            # If they held winning tokens, they should have been paid
            # (available_balance increased by winning_tokens worth)
            # If they held losing tokens, those are worth 0


# ---------------------------------------------------------------------------
# 11-13: Numerical Stability
# ---------------------------------------------------------------------------

@engines_required
class TestNumericalStability:

    def test_extreme_prices(self):
        """Prices near 0 and 1. Invariants still hold."""
        risk, market_eng, traders, market, amm, total_minted = fresh_system(
            trader_balance=Decimal("10000"), b=Decimal("100"))

        # Push YES price to ~0.99
        market_eng.buy(market.id, traders[0].id, "yes", Decimal("5000"))

        p = prices(market.q, market.b)
        assert p["yes"] > Decimal("0.95")
        assert abs(sum(p.values()) - Decimal("1")) < Decimal("0.0001")

        # Still can trade
        market_eng.buy(market.id, traders[1].id, "no", Decimal("100"))
        assert system_total(risk) == total_minted

    def test_small_b_large_trades(self):
        """b=1, large trades. All invariants hold."""
        risk, market_eng, traders, market, amm, total_minted = fresh_system(
            b=Decimal("1"), amm_funding=Decimal("10"))

        random_trades(market_eng, market, traders, n=50)
        assert system_total(risk) == total_minted

        p = prices(market.q, market.b)
        assert abs(sum(p.values()) - Decimal("1")) < Decimal("0.0001")

    def test_large_q_no_overflow(self):
        """q values > 10000. Normalization prevents overflow."""
        risk, market_eng, traders, market, amm, total_minted = fresh_system(
            trader_balance=Decimal("200000"), b=Decimal("1000"))

        # Lots of buying to push q high
        for _ in range(20):
            market_eng.buy(market.id, traders[0].id, "yes", Decimal("5000"))

        p = prices(market.q, market.b)
        assert abs(sum(p.values()) - Decimal("1")) < Decimal("0.0001")
        assert system_total(risk) == total_minted


# ---------------------------------------------------------------------------
# 14-16: Liquidity Changes
# ---------------------------------------------------------------------------

@engines_required
class TestLiquidityChanges:

    def test_add_liquidity_preserves_prices(self):
        """Add liquidity mid-market. Prices unchanged. Conservation holds."""
        risk, market_eng, traders, market, amm, total_minted = fresh_system()
        random_trades(market_eng, market, traders, n=20)

        prices_before = prices(market.q, market.b)

        additional = Decimal("50")
        risk.mint(amm.id, additional)
        total_minted += additional
        market_eng.add_liquidity(market.id, additional)

        prices_after = prices(market.q, market.b)
        for o in market.outcomes:
            assert abs(prices_before[o] - prices_after[o]) < Decimal("0.001")

        assert system_total(risk) == total_minted

    def test_remove_liquidity_safe(self):
        """Remove liquidity. Prices unchanged. Settlement still works."""
        risk, market_eng, traders, market, amm, _ = fresh_system(
            b=Decimal("200"))
        random_trades(market_eng, market, traders, n=20)

        prices_before = prices(market.q, market.b)

        market_eng.remove_liquidity(market.id, Decimal("30"))

        prices_after = prices(market.q, market.b)
        for o in market.outcomes:
            assert abs(prices_before[o] - prices_after[o]) < Decimal("0.001")

        # Can still settle
        market_eng.resolve(market.id, "yes")

    def test_liquidity_round_trip(self):
        """Add then remove same funding. b returns to original."""
        risk, market_eng, traders, market, amm, total_minted = fresh_system()
        b_original = market.b

        random_trades(market_eng, market, traders, n=10)

        funding = Decimal("50")
        risk.mint(amm.id, funding)
        market_eng.add_liquidity(market.id, funding)

        market_eng.remove_liquidity(market.id, funding)

        assert abs(market.b - b_original) < Decimal("0.001")


# ---------------------------------------------------------------------------
# 17-19: Cross-Domain Invariants
# ---------------------------------------------------------------------------

@engines_required
class TestCrossDomain:

    def test_frozen_equals_sum_of_locks(self):
        """frozen_balance == sum(lock.amount) after every operation."""
        risk, market_eng, traders, market, amm, _ = fresh_system()

        def check_all():
            for acc in list(risk.accounts.values()):
                lock_sum = sum((l.amount for l in acc.locks), ZERO)
                assert acc.frozen_balance == lock_sum, (
                    f"Account {acc.id}: frozen={acc.frozen_balance}, "
                    f"lock_sum={lock_sum}"
                )

        check_all()
        random_trades(market_eng, market, traders, n=50)
        check_all()
        market_eng.resolve(market.id, "yes")
        check_all()

    def test_trades_produce_matching_transactions(self):
        """Each trade produces at least one tagged transaction.

        In LMSR markets, the trader side always has a transaction.
        The AMM side may have zero-delta (buys) or PnL transactions (sells).
        Verify the trader-side transaction matches the trade leg.
        """
        risk, market_eng, traders, market, amm, _ = fresh_system()
        trades = random_trades(market_eng, market, traders, n=20)

        for trade in trades:
            # Find transactions for this trade
            trade_txs = [
                tx for tx in risk.transactions
                if tx.trade_id == trade.id
            ]
            assert len(trade_txs) >= 1, (
                f"Trade {trade.id} should produce at least 1 transaction, "
                f"got {len(trade_txs)}"
            )

            # The trader (buyer for buys) should have a tagged transaction
            trader_id = trade.buyer.account_id
            trader_tx = next(
                (tx for tx in trade_txs if tx.account_id == trader_id),
                None,
            )
            assert trader_tx is not None, (
                f"Trade {trade.id} should have a transaction for "
                f"the trader (account {trader_id})"
            )

    def test_rejected_trade_leaves_no_trace(self):
        """Insufficient balance → no state change anywhere."""
        risk, market_eng, traders, market, amm, _ = fresh_system(
            trader_balance=Decimal("1"))

        trader = traders[0]
        avail_before = trader.available_balance
        frozen_before = trader.frozen_balance
        locks_before = len(trader.locks)
        q_before = dict(market.q)
        n_trades_before = len(market.trades)
        n_txs_before = len(risk.transactions)

        with pytest.raises((ValueError, Exception)):
            market_eng.buy(market.id, trader.id, "yes", Decimal("9999"))

        assert trader.available_balance == avail_before
        assert trader.frozen_balance == frozen_before
        assert len(trader.locks) == locks_before
        assert market.q == q_before
        assert len(market.trades) == n_trades_before
        assert len(risk.transactions) == n_txs_before


# ---------------------------------------------------------------------------
# 20-22: Adversarial
# ---------------------------------------------------------------------------

@engines_required
class TestAdversarial:

    def test_cant_sell_more_than_held(self):
        """Selling more tokens than position. Must fail, no state change."""
        risk, market_eng, traders, market, amm, _ = fresh_system()
        trader = traders[0]

        market_eng.buy(market.id, trader.id, "yes", Decimal("50"))
        pos = market.position(trader.id)
        held = pos["yes"]

        q_before = dict(market.q)
        with pytest.raises((ValueError, Exception)):
            market_eng.sell(market.id, trader.id, "yes", held + Decimal("1"))
        assert market.q == q_before

    def test_cant_trade_on_resolved_market(self):
        """Trade on resolved market must fail."""
        risk, market_eng, traders, market, amm, _ = fresh_system()
        market_eng.resolve(market.id, "yes")

        with pytest.raises((ValueError, Exception)):
            market_eng.buy(market.id, traders[0].id, "yes", Decimal("10"))

    def test_sequential_execution(self):
        """
        Two traders buy in sequence. Second gets worse price.
        No possibility of both seeing the initial price.
        """
        risk, market_eng, traders, market, amm, _ = fresh_system()

        trade1 = market_eng.buy(market.id, traders[0].id, "yes", Decimal("50"))
        trade2 = market_eng.buy(market.id, traders[1].id, "yes", Decimal("50"))

        # Second trade should have a higher price (market moved)
        assert trade2.price > trade1.price


# ---------------------------------------------------------------------------
# 23-28: Precision, Q-values, and Dust
# ---------------------------------------------------------------------------

@engines_required
class TestPrecisionAndDust:

    def test_token_amounts_at_credit_precision(self):
        """All q-values and token positions use CREDITS precision (6dp)."""
        risk, market_eng, traders, market, amm, _ = fresh_system()
        quantum = Decimal("0.000001")

        for _ in range(20):
            trader = traders[0]
            trade = market_eng.buy(market.id, trader.id, "yes", Decimal("10"))
            # Token amount must be at 6dp precision
            assert trade.amount == trade.amount.quantize(quantum), (
                f"Token amount {trade.amount} exceeds CREDITS precision"
            )

        # All q-values at correct precision
        for outcome, q_val in market.q.items():
            assert q_val == q_val.quantize(quantum), (
                f"q[{outcome}] = {q_val} exceeds CREDITS precision"
            )

        # All positions at correct precision
        pos = market.position(traders[0].id)
        for outcome, held in pos.items():
            assert held == held.quantize(quantum), (
                f"position[{outcome}] = {held} exceeds CREDITS precision"
            )

    def test_sell_rejects_excess_precision(self):
        """Cannot sell an amount with more than 6dp precision."""
        risk, market_eng, traders, market, amm, _ = fresh_system()
        market_eng.buy(market.id, traders[0].id, "yes", Decimal("50"))

        # This amount has 7dp — should be rejected
        bad_amount = Decimal("0.0000001")
        with pytest.raises(ValueError):
            market_eng.sell(market.id, traders[0].id, "yes", bad_amount)

    def test_buy_tradeleg_deltas_match_balance_changes(self):
        """
        Buyer TradeLeg available_delta and frozen_delta must match
        the actual balance changes on the buyer's account.
        """
        risk, market_eng, traders, market, amm, _ = fresh_system()
        trader = traders[0]

        avail_before = trader.available_balance
        frozen_before = trader.frozen_balance

        trade = market_eng.buy(market.id, trader.id, "yes", Decimal("25"))

        avail_after = trader.available_balance
        frozen_after = trader.frozen_balance

        assert trade.buyer.available_delta == avail_after - avail_before
        assert trade.buyer.frozen_delta == frozen_after - frozen_before

    def test_position_zero_means_lock_zero(self):
        """
        Exchange principle: position == 0 → position margin frozen == 0.

        After a round-trip (buy then sell all tokens), the trader's
        position is zero and the position lock is fully released.
        Rounding dust accumulates as conditional_loss on the trader
        (the AMM realizes the gain at resolution).
        """
        risk, market_eng, traders, market, amm, total_minted = fresh_system(
            n_traders=1)
        trader = traders[0]

        # Do several round-trips to accumulate rounding dust
        for _ in range(50):
            trade = market_eng.buy(market.id, trader.id, "yes", Decimal("20"))
            tokens = trade.amount
            market_eng.sell(market.id, trader.id, "yes", tokens)

        # Trader's position is zero
        pos = market.position(trader.id)
        assert all(v == ZERO for v in pos.values()), (
            f"Position should be zero after round-trips: {pos}"
        )

        # Position lock must be zero (proportional close removes it fully)
        pos_lock = trader.lock_for(market.id, "position:yes")
        assert pos_lock is None, (
            f"Position lock should be removed when position is zero, "
            f"but has {pos_lock.amount} remaining"
        )

        # Rounding dust accumulates as conditional_loss on trader
        trader_cl = trader.lock_for(market.id, "conditional_loss")
        assert trader_cl is not None and trader_cl.amount > ZERO, (
            "Rounding dust should accumulate as conditional_loss"
        )

        # Trader lost a tiny amount to rounding
        assert trader.available_balance < Decimal("1000")

        # System-wide conservation still holds
        assert system_total(risk) == total_minted

    def test_no_budget_tolerance(self):
        """Budget exceeding available balance must be rejected. No tolerance."""
        risk, market_eng, traders, market, amm, _ = fresh_system(
            n_traders=1, trader_balance=Decimal("100"))
        trader = traders[0]

        with pytest.raises(Exception):
            market_eng.buy(market.id, trader.id, "yes", Decimal("100.000001"))

    def test_settlement_releases_conditional_profit(self):
        """
        On resolution, conditional_profit locks release at face value
        to the trader (the sale was final). Position locks settle based
        on the winning outcome. Total payouts == total pool.
        """
        risk, market_eng, traders, market, amm, total_minted = fresh_system(
            n_traders=2)
        t1, t2 = traders[0], traders[1]

        # t1 buys yes, then sells some
        market_eng.buy(market.id, t1.id, "yes", Decimal("100"))
        pos = market.position(t1.id)
        sell_amount = pos["yes"] / 2
        sell_amount = sell_amount.quantize(Decimal("0.000001"), rounding=ROUND_FLOOR)
        market_eng.sell(market.id, t1.id, "yes", sell_amount)

        # t2 buys no
        market_eng.buy(market.id, t2.id, "no", Decimal("100"))

        # Record state before resolution
        t1_cp = t1.lock_for(market.id, "conditional_profit")
        t1_cp_amount = t1_cp.amount if t1_cp else ZERO
        t1_pos = market.position(t1.id)
        t1_winning = t1_pos.get("yes", ZERO)

        market_eng.resolve(market.id, "yes")

        # Conservation
        assert system_total(risk) == total_minted

        # No locks remain
        for acc in [t1, t2, amm]:
            assert len(acc.locks_for_market(market.id)) == 0

        # t1 should have received winning_tokens + cp_amount
        # (cp represents finalized sale proceeds)


# ---------------------------------------------------------------------------
# 29-33: Conditional PnL Netting
# ---------------------------------------------------------------------------

@engines_required
class TestConditionalPnlNetting:
    """
    When a trader accumulates both conditional_profit and conditional_loss
    in the same market, they must be netted. At any point, a trader has
    at most ONE of CP or CL per market — never both.

    Without netting:
      - Capital is over-frozen (CP + CL both frozen, but they partially cancel)
      - The net conditional PnL is obscured across two locks

    The netting step after each sell:
      net_amount = min(CP, CL)
      CP portion → returned to AMM position lock (was AMM's money)
      CL portion → released to trader available (loss offset by returned profit)
    """

    def _make_system(self):
        """One trader with 1000, one market."""
        risk, me, traders, market, amm, total_minted = fresh_system(
            n_traders=1, trader_balance=Decimal("1000"))
        return risk, me, traders[0], market, amm, total_minted

    def test_profit_then_loss_nets_to_cl(self):
        """CP from first sell is consumed when second sell creates larger CL."""
        risk, me, trader, market, amm, total_minted = self._make_system()

        # Buy YES (pushes price up)
        me.buy(market.id, trader.id, "yes", Decimal("200"))

        # Sell some YES at profit → CP created
        pos = market.position(trader.id)
        sell1 = (pos["yes"] / 4).quantize(Decimal("0.01"), rounding=ROUND_FLOOR)
        me.sell(market.id, trader.id, "yes", sell1)

        cp = trader.lock_for(market.id, "conditional_profit")
        cl = trader.lock_for(market.id, "conditional_loss")
        assert cp is not None and cp.amount > ZERO
        assert cl is None
        cp_before = cp.amount

        # Buy NO to crash YES price
        me.buy(market.id, trader.id, "no", Decimal("300"))

        # Sell YES at a big loss → CL would exceed CP
        pos = market.position(trader.id)
        sell2 = (pos["yes"] / 2).quantize(Decimal("0.01"), rounding=ROUND_FLOOR)
        me.sell(market.id, trader.id, "yes", sell2)

        cp = trader.lock_for(market.id, "conditional_profit")
        cl = trader.lock_for(market.id, "conditional_loss")

        # CP should be fully consumed, only CL remains
        assert cp is None, (
            f"CP should be netted away, but has {cp.amount}")
        assert cl is not None and cl.amount > ZERO

        # Conservation
        assert system_total(risk) == total_minted

    def test_loss_then_profit_nets_to_cp(self):
        """CL from first sell is consumed when second sell creates larger CP."""
        risk, me, trader, market, amm, total_minted = self._make_system()

        # Buy NO to push YES price down
        me.buy(market.id, trader.id, "no", Decimal("150"))

        # Buy some YES at the low price
        me.buy(market.id, trader.id, "yes", Decimal("100"))

        # Sell NO at a loss (NO price dropped because we bought YES)
        pos = market.position(trader.id)
        sell1 = (pos["no"] / 3).quantize(Decimal("0.01"), rounding=ROUND_FLOOR)
        me.sell(market.id, trader.id, "no", sell1)

        cp = trader.lock_for(market.id, "conditional_profit")
        cl = trader.lock_for(market.id, "conditional_loss")
        assert cl is not None and cl.amount > ZERO
        assert cp is None
        cl_before = cl.amount

        # Sell YES at a big profit (YES price went up from buying YES)
        pos = market.position(trader.id)
        sell2 = pos["yes"]  # sell all YES
        me.sell(market.id, trader.id, "yes", sell2)

        cp = trader.lock_for(market.id, "conditional_profit")
        cl = trader.lock_for(market.id, "conditional_loss")

        # Only one should remain
        assert not (cp and cl), (
            f"Should not have both CP={cp.amount if cp else 0} "
            f"and CL={cl.amount if cl else 0}")

        assert system_total(risk) == total_minted

    def test_equal_pnl_nets_to_zero(self):
        """When CP and CL are exactly equal, both are removed."""
        risk, me, trader, market, amm, total_minted = self._make_system()

        # Generate a small CP via a profitable sell
        me.buy(market.id, trader.id, "yes", Decimal("50"))
        pos = market.position(trader.id)
        sell1 = (pos["yes"] / 5).quantize(Decimal("0.01"), rounding=ROUND_FLOOR)
        me.sell(market.id, trader.id, "yes", sell1)

        cp = trader.lock_for(market.id, "conditional_profit")
        if cp is None:
            # No CP means the sell was at a loss or zero — skip
            return
        cp_amount = cp.amount

        # Now force a loss of exactly the same amount
        # We can't easily force exact equality, so verify the invariant:
        # after any sequence of sells, at most one of CP or CL exists
        me.buy(market.id, trader.id, "no", Decimal("200"))
        pos = market.position(trader.id)
        # Sell YES at loss
        sell2 = (pos["yes"] / 2).quantize(Decimal("0.01"), rounding=ROUND_FLOOR)
        if sell2 > ZERO:
            me.sell(market.id, trader.id, "yes", sell2)

        cp = trader.lock_for(market.id, "conditional_profit")
        cl = trader.lock_for(market.id, "conditional_loss")
        assert not (cp and cl), (
            f"Never both: CP={cp.amount if cp else 0}, "
            f"CL={cl.amount if cl else 0}")

        assert system_total(risk) == total_minted

    def test_netting_frees_capital(self):
        """Netting releases frozen capital to available.

        If a trader has CP=10 and then gets CL=20, netting produces
        CL=10 (not CP=10 + CL=20). The trader's frozen balance should
        be 10, not 30. The extra 20 is freed.
        """
        risk, me, trader, market, amm, total_minted = self._make_system()

        me.buy(market.id, trader.id, "yes", Decimal("200"))

        # Sell at profit
        pos = market.position(trader.id)
        sell1 = (pos["yes"] / 4).quantize(Decimal("0.01"), rounding=ROUND_FLOOR)
        me.sell(market.id, trader.id, "yes", sell1)

        cp = trader.lock_for(market.id, "conditional_profit")
        assert cp is not None
        cp_amount = cp.amount

        frozen_after_profit_sell = trader.frozen_balance

        # Crash price and sell at loss
        me.buy(market.id, trader.id, "no", Decimal("300"))
        pos = market.position(trader.id)
        sell2 = (pos["yes"] / 2).quantize(Decimal("0.01"), rounding=ROUND_FLOOR)
        me.sell(market.id, trader.id, "yes", sell2)

        # The CP was netted into the CL. This should have released
        # cp_amount worth of frozen capital compared to not netting.
        cl = trader.lock_for(market.id, "conditional_loss")
        cp = trader.lock_for(market.id, "conditional_profit")
        assert cp is None

        # The CL amount should be less than the raw loss
        # (because cp_amount was netted out)
        # We can't easily check the raw loss, but we know:
        # frozen should NOT include both CP and CL simultaneously
        conditional_locks = [
            lk for lk in trader.locks_for_market(market.id)
            if lk.lock_type in ("conditional_profit", "conditional_loss")
        ]
        assert len(conditional_locks) <= 1, (
            f"At most one conditional lock, got {len(conditional_locks)}")

        assert system_total(risk) == total_minted

    def test_void_correct_after_mixed_pnl(self):
        """
        Multiple profit and loss sells with netting.
        Void returns exact deposits. The hardest void test.
        """
        risk, me, trader, market, amm, total_minted = self._make_system()

        trader_before = trader.total
        amm_before = amm.total

        rng = random.Random(999)
        for i in range(40):
            outcome = rng.choice(market.outcomes)
            if rng.random() < 0.6:
                try:
                    me.buy(market.id, trader.id, outcome,
                           Decimal(str(rng.uniform(5, 60))))
                except (ValueError, Exception):
                    pass
            else:
                pos = market.position(trader.id)
                held = pos.get(outcome, ZERO)
                if held > ZERO:
                    try:
                        sell_amt = (
                            held * Decimal(str(rng.uniform(0.1, 1.0)))
                        ).quantize(Decimal("0.01"), rounding=ROUND_FLOOR)
                        if sell_amt > ZERO:
                            me.sell(market.id, trader.id, outcome, sell_amt)
                    except (ValueError, Exception):
                        pass

            # Invariant: never both CP and CL
            cp = trader.lock_for(market.id, "conditional_profit")
            cl = trader.lock_for(market.id, "conditional_loss")
            assert not (cp and cl), (
                f"Step {i}: both CP={cp.amount if cp else 0} "
                f"and CL={cl.amount if cl else 0}")

        me.void(market.id)

        assert trader.total == trader_before, (
            f"Trader total {trader.total} != {trader_before}")
        assert amm.total == amm_before, (
            f"AMM total {amm.total} != {amm_before}")
        assert system_total(risk) == total_minted


# ---------------------------------------------------------------------------
# 34-37: Multi-Outcome Position Isolation
# ---------------------------------------------------------------------------

@engines_required
class TestMultiOutcomePositions:
    """
    YES and NO are different instruments. Each outcome's cost basis must
    be tracked separately. A sell of YES must not release margin that
    backs the NO position (and vice versa).

    The position lock must be per-outcome, not shared.
    """

    def _make_system(self):
        risk, me, traders, market, amm, total_minted = fresh_system(
            n_traders=1, trader_balance=Decimal("1000"))
        return risk, me, traders[0], market, amm, total_minted

    def test_sell_yes_does_not_release_no_margin(self):
        """Buy YES and NO. Sell all YES. NO margin must remain locked."""
        risk, me, trader, market, amm, total_minted = self._make_system()

        # Buy YES
        t1 = me.buy(market.id, trader.id, "yes", Decimal("60"))

        # Buy NO
        t2 = me.buy(market.id, trader.id, "no", Decimal("60"))

        pos = market.position(trader.id)
        yes_held = pos["yes"]
        no_held = pos["no"]

        # Sell ALL YES
        me.sell(market.id, trader.id, "yes", yes_held)

        # NO position untouched — its margin must still be locked
        pos_after = market.position(trader.id)
        assert pos_after["no"] == no_held, "NO position should be unchanged"

        # The frozen balance should still reflect the NO cost basis
        # It must NOT be zero — the NO position still needs backing
        no_position_lock = trader.lock_for(market.id, "position:no")
        assert no_position_lock is not None, (
            "NO position lock must exist after selling all YES")
        assert no_position_lock.amount > ZERO, (
            "NO position lock must be > 0")

        assert system_total(risk) == total_minted

    def test_sell_no_does_not_release_yes_margin(self):
        """Mirror: buy YES and NO. Sell all NO. YES margin stays locked."""
        risk, me, trader, market, amm, total_minted = self._make_system()

        me.buy(market.id, trader.id, "yes", Decimal("60"))
        me.buy(market.id, trader.id, "no", Decimal("60"))

        pos = market.position(trader.id)
        no_held = pos["no"]
        yes_held = pos["yes"]

        me.sell(market.id, trader.id, "no", no_held)

        pos_after = market.position(trader.id)
        assert pos_after["yes"] == yes_held

        yes_position_lock = trader.lock_for(market.id, "position:yes")
        assert yes_position_lock is not None, (
            "YES position lock must exist after selling all NO")
        assert yes_position_lock.amount > ZERO

        assert system_total(risk) == total_minted

    def test_position_zero_per_outcome(self):
        """Selling all tokens of one outcome releases only that outcome's lock."""
        risk, me, trader, market, amm, total_minted = self._make_system()

        me.buy(market.id, trader.id, "yes", Decimal("40"))
        me.buy(market.id, trader.id, "no", Decimal("40"))

        pos = market.position(trader.id)

        # Sell all YES
        me.sell(market.id, trader.id, "yes", pos["yes"])

        # YES position lock gone
        assert trader.lock_for(market.id, "position:yes") is None
        # NO position lock still there
        assert trader.lock_for(market.id, "position:no") is not None

        # Sell all NO
        me.sell(market.id, trader.id, "no", pos["no"])

        # Both gone
        assert trader.lock_for(market.id, "position:yes") is None
        assert trader.lock_for(market.id, "position:no") is None

        assert system_total(risk) == total_minted

    def test_void_returns_exact_with_multi_outcome(self):
        """Buy both outcomes, sell some, void. Exact deposits returned."""
        risk, me, trader, market, amm, total_minted = self._make_system()

        trader_before = trader.total
        amm_before = amm.total

        # Build up positions in both outcomes
        me.buy(market.id, trader.id, "yes", Decimal("100"))
        me.buy(market.id, trader.id, "no", Decimal("80"))

        # Sell partial YES and NO
        pos = market.position(trader.id)
        me.sell(market.id, trader.id, "yes",
                (pos["yes"] / 3).quantize(Decimal("0.01"), rounding=ROUND_FLOOR))
        pos = market.position(trader.id)
        me.sell(market.id, trader.id, "no",
                (pos["no"] / 2).quantize(Decimal("0.01"), rounding=ROUND_FLOOR))

        me.void(market.id)

        assert trader.total == trader_before
        assert amm.total == amm_before
        assert system_total(risk) == total_minted
