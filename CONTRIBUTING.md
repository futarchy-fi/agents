# Contributing to agents.futarchy.ai

## Testing Philosophy

Tests exist to catch non-obvious bugs and verify invariants. Not to pad coverage.

A good test encodes a belief about the system that isn't obvious from reading the code. If a test would pass just by reading the function it exercises, it's not worth writing.

### What makes a good test

**Test invariants** — properties that must hold regardless of input. These are the most valuable tests because they catch entire classes of bugs, not just specific cases.

**Test round-trips** — do something, undo it, verify you're back where you started. These catch asymmetries between paired operations (buy/sell, lock/unlock, resolve/void).

**Test conservation laws** — money doesn't appear or disappear. Total credits in the system = total minted, always, after every operation, including rounding.

**Test boundaries and adversarial inputs** — not toy edge cases, but the boundaries that actually matter in production: extreme prices (0.0001, 0.9999), zero balances, maximum positions, numerical overflow zones.

**Test cross-domain contracts** — the risk engine and market engine talk to each other. A test that exercises the full path (trade request → risk check → lock → position update → settlement → unlock) catches integration bugs that unit tests never will.

### What NOT to test

- Simple getters, constructors, or property accessors
- Obvious arithmetic
- Things the type system already guarantees
- Individual functions in isolation when the round-trip test covers them
- Anything where the test is just restating the implementation

### How to structure tests

Each test should read like a story: setup a scenario, do something interesting, verify a non-obvious property. Name tests after the property they verify, not the function they call. `test_credits_conserved_after_random_trades` is better than `test_buy_function`.

Use randomized inputs (fuzzing) for invariant tests. If a property should hold for ALL inputs, test it with many random inputs, not three hand-picked ones.

## Test Plan

The tests below define the contract the system must satisfy. They are ordered by importance. Each encodes a specific belief about the system.

### Credit Conservation

1. **Total credits conserved through trading.** After N random trades across multiple traders, `sum(all available_balance + all frozen_balance)` = `total minted`. No credits created or destroyed, even with rounding.

2. **Total credits conserved through full lifecycle.** Create market → fund AMM → multiple traders trade → settlement. Total credits still equal total minted.

3. **Total credits conserved through void.** Same lifecycle but void instead of settlement. Total credits still conserved. Every account's balance restored.

### Rounding and Dust

4. **Rounding always favors the AMM.** Buy then sell the same amount. In exact math, net cost = 0. With rounding, net cost > 0. The trader always loses a tiny amount on the round-trip, the AMM always gains.

5. **Path independence breaks predictably with rounding.** Buying 10 tokens as 10 individual trades costs MORE than buying 10 in one shot. More rounding events = more dust to AMM. The difference is always >= 0.

6. **Dust accumulates monotonically.** After 1000 small random trades, total AMM conditional_profit locks are strictly positive and equal the sum of all individual rounding differences.

### Void Reversal

7. **Void returns exact amounts.** On void, each trader gets back exactly what they paid (the rounded amount from their position lock). The AMM gets back its subsidy. All conditional_profit locks are released. Total system credits unchanged.

8. **Void after complex trading.** N traders make random trades (buys and sells), some at profit, some at loss. Void the market. Every account's available_balance + frozen_balance returns to its pre-market state.

### Settlement Correctness

9. **AMM never loses more than b * ln(n).** Fuzz with random trades, resolve the market, verify the AMM's total loss from the subsidy is <= `b * ln(n)`. This is the mathematical guarantee of LMSR.

10. **Winners paid correctly, losers get zero.** After settlement: winners receive exactly their token holdings (quantized to market precision). Losers receive 0. No locks remain for the settled market.

### Numerical Stability

11. **Extreme prices don't break invariants.** Push price to 0.9999 or 0.0001, then trade. Math still works, no overflow, prices still sum to 1, credits still conserved.

12. **Small b with large trades.** Very small liquidity parameter (b=1) with large trades — extreme price sensitivity. All invariants still hold.

13. **Large q values don't overflow.** q values at 10000+. The normalization trick prevents overflow. Prices still sum to 1, costs still computed correctly.

### Liquidity Changes Mid-Market

14. **Liquidity addition preserves prices.** Add liquidity after trades have happened. Prices unchanged. AMM frozen increases by the right amount. Then more trades, then settle. Conservation holds throughout.

15. **Liquidity removal is safe.** Remove liquidity mid-market. Prices unchanged. New `b * ln(n)` is still sufficient to cover worst case at current q. Settlement still works.

16. **Liquidity round-trip.** Add X funding then remove X funding. b returns to original. Prices unchanged throughout.

### Cross-Domain Invariants

17. **Frozen balance always equals sum of locks.** `frozen_balance == sum(lock.amount for lock in locks)` — after every single operation. The two representations never disagree.

18. **Every trade produces matching transactions.** Each trade produces exactly 2 transactions (one per leg). Each transaction's deltas match the corresponding TradeLeg's deltas. Each references the correct trade, leg, lock, and market.

19. **Risk engine rejection leaves no trace.** Reject a trade when `available_balance < cost`. Verify market state is completely unchanged — no partial execution, no orphaned locks, no phantom position updates.

### Adversarial

20. **Can't sell tokens you don't hold.** Trader tries to sell more tokens than their position. Must fail cleanly, no state change.

21. **Can't trade on resolved or voided market.** Any trade attempt on a non-open market fails. No state change.

22. **Sequential execution, no stale reads.** Two traders buy in sequence. Second trader gets a worse price because the first trade moved the market. No possibility of both getting the "initial" price.
