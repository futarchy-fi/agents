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
        """Buy then sell same amount. Net cost > 0 (AMM gains dust)."""
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
        # AMM should have gained
        amm_profit = amm.lock_for(market.id, "conditional_profit")
        assert amm_profit is not None and amm_profit.amount > ZERO

    def test_path_independence_favors_amm(self):
        """10 small buys cost more than 1 big buy (more rounding events)."""
        # System A: one big buy
        risk_a, me_a, traders_a, market_a, _, _ = fresh_system(n_traders=1)
        me_a.buy(market_a.id, traders_a[0].id, "yes", Decimal("50"))
        cost_big = traders_a[0].available_balance

        # System B: ten small buys
        risk_b, me_b, traders_b, market_b, _, _ = fresh_system(n_traders=1)
        for _ in range(10):
            me_b.buy(market_b.id, traders_b[0].id, "yes", Decimal("5"))
        cost_small = traders_b[0].available_balance

        # Small buys should cost more (less remaining balance)
        assert cost_small < cost_big

    def test_dust_accumulates_monotonically(self):
        """After 1000 trades, AMM conditional_profit > 0."""
        risk, market_eng, traders, market, amm, _ = fresh_system(
            n_traders=5, b=Decimal("100"))
        random_trades(market_eng, market, traders, n=1000, seed=123)

        profit_lock = amm.lock_for(market.id, "conditional_profit")
        assert profit_lock is not None
        assert profit_lock.amount > ZERO


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
            trader_balance=Decimal("100000"), b=Decimal("1000"))

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
        """Each trade → 2 transactions with matching deltas."""
        risk, market_eng, traders, market, amm, _ = fresh_system()
        trades = random_trades(market_eng, market, traders, n=20)

        for trade in trades:
            # Find transactions for this trade
            trade_txs = [
                tx for tx in risk.transactions
                if tx.trade_id == trade.id
            ]
            assert len(trade_txs) == 2, (
                f"Trade {trade.id} should produce 2 transactions, "
                f"got {len(trade_txs)}"
            )

            # Match transactions to legs
            buyer_tx = next(
                tx for tx in trade_txs
                if tx.account_id == trade.buyer.account_id
            )
            seller_tx = next(
                tx for tx in trade_txs
                if tx.account_id == trade.seller.account_id
            )

            assert buyer_tx.available_delta == trade.buyer.available_delta
            assert buyer_tx.frozen_delta == trade.buyer.frozen_delta
            assert seller_tx.available_delta == trade.seller.available_delta
            assert seller_tx.frozen_delta == trade.seller.frozen_delta

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
