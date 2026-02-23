# Core Test Plan

The tests below define the contract the system must satisfy. Each encodes a specific belief about the system. Ordered by importance.

## Credit Conservation

1. **Total credits conserved through trading.** After N random trades across multiple traders, `sum(all available_balance + all frozen_balance)` = `total minted`. No credits created or destroyed, even with rounding.

2. **Total credits conserved through full lifecycle.** Create market → fund AMM → multiple traders trade → settlement. Total credits still equal total minted.

3. **Total credits conserved through void.** Same lifecycle but void instead of settlement. Total credits still conserved. Every account's balance restored.

## Rounding and Dust

4. **Rounding always favors the AMM.** Buy then sell the same amount. In exact math, net cost = 0. With rounding, net cost > 0. The trader loses a tiny amount (manifests as conditional_loss), the AMM gains at resolution.

5. **Path independence breaks predictably with rounding.** 10 small buys yield fewer tokens than 1 big buy of the same total. ROUND_FLOOR on token amounts means each small buy loses a fraction of a token. The AMM keeps the difference.

6. **Dust accumulates monotonically.** After many random buys and sells, total conditional_loss across traders is strictly positive. Rounding dust accumulates as trader CL (the AMM realizes the gain at resolution).

## Void Reversal

7. **Void returns exact amounts.** On void, each trader gets back exactly what they deposited. The AMM gets back its subsidy. All locks released. Total system credits unchanged.

8. **Void after complex trading.** N traders make random trades (buys and sells), some at profit, some at loss. Void the market. Every account's available_balance + frozen_balance returns to its pre-market state.

## Settlement Correctness

9. **AMM never loses more than b * ln(n).** Fuzz with random trades, resolve the market, verify the AMM's total loss from the subsidy is <= `b * ln(n)`. This is the mathematical guarantee of LMSR.

10. **Winners paid correctly, losers get zero.** After settlement: winners receive exactly their token holdings (quantized to market precision). Losers receive 0. No locks remain for the settled market.

## Numerical Stability

11. **Extreme prices don't break invariants.** Push price to 0.9999 or 0.0001, then trade. Math still works, no overflow, prices still sum to 1, credits still conserved.

12. **Small b with large trades.** Very small liquidity parameter (b=1) with large trades — extreme price sensitivity. All invariants still hold.

13. **Large q values don't overflow.** q values at 10000+. The normalization trick prevents overflow. Prices still sum to 1, costs still computed correctly.

## Liquidity Changes Mid-Market

14. **Liquidity addition preserves prices.** Add liquidity after trades have happened. Prices unchanged. AMM frozen increases by the right amount. Then more trades, then settle. Conservation holds throughout.

15. **Liquidity removal is safe.** Remove liquidity mid-market. Prices unchanged. New `b * ln(n)` is still sufficient to cover worst case at current q. Settlement still works.

16. **Liquidity round-trip.** Add X funding then remove X funding. b returns to original. Prices unchanged throughout.

## Cross-Domain Invariants

17. **Frozen balance always equals sum of locks.** `frozen_balance == sum(lock.amount for lock in locks)` — after every single operation. The two representations never disagree.

18. **Every trade produces matching transactions.** Each trade produces at least one tagged transaction. The trader side always has a transaction referencing the trade ID.

19. **Risk engine rejection leaves no trace.** Reject a trade when `available_balance < cost`. Verify market state is completely unchanged — no partial execution, no orphaned locks, no phantom position updates.

## Adversarial

20. **Can't sell tokens you don't hold.** Trader tries to sell more tokens than their position. Must fail cleanly, no state change.

21. **Can't trade on resolved or voided market.** Any trade attempt on a non-open market fails. No state change.

22. **Sequential execution, no stale reads.** Two traders buy in sequence. Second trader gets a worse price because the first trade moved the market. No possibility of both getting the "initial" price.

## Precision and Dust

23. **Token amounts at credit precision.** All q-values, positions, and trade amounts are quantized to CREDITS precision (6dp). No excess precision leaks through.

24. **Sell rejects excess precision.** Cannot sell an amount with more decimal places than amount_precision. Enforced at the API boundary.

25. **Buy trade leg deltas match balance changes.** The buyer's TradeLeg available_delta and frozen_delta exactly equal the actual balance changes on their account.

26. **Position zero means lock zero.** After round-tripping (buy then sell all tokens), the position is zero and the position lock is fully removed. Rounding dust accumulates as conditional_loss on the trader, not as residual position lock.

27. **No budget tolerance.** Budget exceeding available balance by even 0.000001 is rejected. No tolerance, no approximation.

28. **Settlement releases conditional profit.** On resolution, conditional_profit locks release at face value to the trader. Position locks settle based on the winning outcome. Total payouts equal total pool. No locks remain.

## Conditional PnL Netting

29. **Profit then loss nets to CL.** CP from a profitable sell is consumed when a subsequent sell creates a larger CL. Only CL remains after netting.

30. **Loss then profit nets to CP.** CL from a losing sell is consumed when a subsequent sell creates a larger CP. Only CP remains after netting.

31. **Equal PnL nets to zero.** When CP and CL are equal, both are fully consumed. No conditional locks remain.

32. **Netting frees capital.** After netting, at most one conditional lock exists per market per trader. Frozen balance reflects the net, not the gross.

33. **Void correct after mixed PnL.** 40 random buys and sells with netting. Invariant checked after every sell: never both CP and CL. Void returns exact deposits.

## Multi-Outcome Position Isolation

34. **Sell YES does not release NO margin.** Buy both YES and NO. Sell all YES. The NO position lock (`position:no`) must still exist and be > 0.

35. **Sell NO does not release YES margin.** Mirror of above. Buy both, sell all NO. YES position lock intact.

36. **Position zero per outcome.** Sell all of one outcome — only that outcome's lock is removed. The other outcome's lock remains.

37. **Void returns exact with multi-outcome.** Buy both outcomes, partial sells on each, void. Every account gets back exactly their original deposit.
