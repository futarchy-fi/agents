# MVP Design: Prediction Markets on PRs

## What We're Building

Conditional prediction markets on GitHub PRs, powered by LMSR AMMs. Two repos tracked initially: `futarchy-fi/agents` (our repo) and `openclaw/openclaw` (external).

Anyone (human or agent) can bet on whether a PR will be merged. Markets are conditional: if the PR isn't evaluated (merged or rejected) within 24 hours, the market is void and all trades revert.

## Architecture: Two Separate Systems

### 1. Risk Engine (per-account)

Tracks each account's credits. Knows:
- Available balance (free to use)
- Locked amounts and WHERE they're locked (which market, which order)
- Eventually handles inflight orders

The risk engine does NOT track positions directly. It only knows: "50 credits are locked in market X." The details of what those credits bought (which outcome tokens, at what price) live in the market.

This separation matters because:
- The risk engine can approve or reject new orders synchronously (do you have enough balance?)
- Position tracking and settlement happen inside the market, possibly async
- Eventually this becomes a real risk engine that handles multiple assets, margin, etc.

### 2. Market Engine (per-market)

Each market is an independent LMSR instance. Tracks:
- The LMSR state (quantities sold per outcome, liquidity parameter b)
- All positions (who holds how many tokens of each outcome)
- All trades (history)
- Deadline (void if unresolved by then)
- Resolution logic

The market engine owns position tracking. When it needs credits from an account, it asks the risk engine to lock them. When it resolves, it tells the risk engine to unlock/transfer.

### Communication

```
Trader wants to buy YES on market M1:

1. Market engine computes cost (LMSR math): 47 credits
2. Market engine asks risk engine: "lock 47 credits from account A for market M1"
3. Risk engine checks balance, locks if sufficient, rejects if not
4. Market engine records the trade and updates positions
5. Market engine confirms to trader

Market M1 resolves (PR merged):

1. Market engine computes payouts per account
2. Market engine tells risk engine: "unlock all M1 locks, credit winners"
3. Risk engine processes settlements
```

## Market Structure

### Conditional Prediction Market

- **Type:** `conditional_prediction_market`
- **Condition:** PR is evaluated (merged or rejected) within deadline
- **Question:** Will it be merged?
- **Outcomes:** `["yes", "no"]`
- **If condition met:** Winning tokens redeem at 1 credit. Losing tokens worth 0.
- **If condition NOT met:** Market is void. All trades revert. All credits returned.

### LMSR (Logarithmic Market Scoring Rule)

Hanson's market scoring rule. The system is the market maker.

- Liquidity parameter `b` controls depth (higher = more liquidity = less price impact = higher max subsidy)
- Maximum loss for market maker: `b * ln(num_outcomes)` — known upfront
- Prices always sum to 1 (proper probabilities)
- Works with any number of traders (even zero — prices still exist)

### Why LMSR, not constant-product AMM

- Bounded loss for market maker (we know the max subsidy per market)
- Designed for prediction markets specifically
- Prices are proper probabilities
- Works with very few participants

## Data Models

See `core/models.py` for the implementation. Key structures:

### Risk Side

```
Lock:
  lock_id: int (sequential)
  account_id: int
  market_id: int
  amount: Decimal             # always positive, asset precision (6dp)
  lock_type: str              # "position", "limit_order", etc.

Account:
  id: int (sequential)
  balance: Decimal            # available credits
  locks: [Lock]               # credits locked in markets, itemized
  # derived: locked, total
  # AMM pools are accounts too

Transaction:
  id: int (sequential)
  account_id: int
  amount: Decimal             # positive = credit, negative = debit
  reason: str
  market_id: int              # null for minting
  trade_id: int               # which trade caused this
  lock_id: int                # which lock was affected
```

### Market Side

```
TradeLeg:                     # one side of a trade
  account_id: int
  available_delta: Decimal    # change to available balance
  frozen_delta: Decimal       # change to locked balance
  lock_id: int                # which lock is affected
  tx_id: int                  # corresponding ledger entry

Trade:
  id: int (sequential)
  market_id: int
  outcome: str                # "yes" or "no"
  amount: Decimal             # tokens traded (market precision, 4dp)
  price: Decimal              # execution price (market precision)
  buyer: TradeLeg
  seller: TradeLeg

Market:
  id: int (sequential)
  amm_account_id: int         # AMM is a regular account
  type: str                   # "conditional_prediction_market"
  category: str               # "pr_merge"
  category_id: str            # "futarchy-fi/agents#1"
  question: str
  precision: int              # decimal places for prices/amounts (default 4)
  status: str                 # "open", "resolved", "void"
  outcomes: ["yes", "no"]
  b: Decimal                  # LMSR liquidity parameter
  q: { "yes": Decimal, ... }  # LMSR quantities sold
  positions: {                # who holds what (keyed by account ID)
    1: { "yes": Decimal, "no": Decimal },
    2: { "yes": Decimal, "no": Decimal }
  }
  trades: [Trade]
  deadline: str               # void if unresolved by then
```

Two precisions: asset precision (CREDITS = 6dp) for credit amounts,
market precision (default 4dp) for prices and token amounts.

## Implementation Plan

One file per PR, each reviewed before merge:

1. `core/models.py` — data classes
2. `core/lmsr.py` — pure math, no state
3. `core/lmsr_test.py` — tests for the math
4. `core/risk_engine.py` — account management, balance, locks
5. `core/market_engine.py` — LMSR market state, positions, trades, resolution/void
6. `api/main.py` — FastAPI endpoints
7. `tools/github_poller.py` — watches repos, creates/resolves markets
8. `agents/forecaster/` — agent that places predictions

## Design Principles

- Risk engine and market engine are separate. They communicate through lock/unlock requests.
- The risk engine is synchronous and fast (can I afford this trade?). The market engine handles the complexity.
- LMSR math is pure functions with no side effects. Easy to test, easy to verify.
- All state is JSON on disk for now. The structure supports migration to a real database later.
- Append-only transaction log means we can always reconstruct state and audit.
- Conditional markets: void and revert if condition isn't met within deadline.

## What This Demonstrates

The MVP shows the core futarchic thesis in action:
- Markets produce probability estimates on PR outcomes
- The probability signal is useful (which PRs are worth reviewing?)
- Anyone can participate (humans or agents)
- The system is its own market maker (LMSR)
- Conditional structure means we're not just predicting — we're structured for decision-making

*Last updated: 2026-02-23*
