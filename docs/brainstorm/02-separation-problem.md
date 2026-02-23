# The Separation Problem

The most important architectural insight: **the market/evolution layer must sit OUTSIDE the system being improved.**

## Why Separation Is Required

Futarchy only works if the evaluation mechanism is external to the thing being evaluated. If the prediction market is part of the system being changed, changing the system changes the market, corrupting the signal.

Real futarchy works because markets (Ethereum, prediction protocols) are external to the policies they evaluate. Participants keep their positions regardless of which policy wins.

## The Two-Level Architecture

```
LEVEL 2: Evolution Layer (OUTSIDE)
├── Money/currency mechanism (the outermost fixed layer)
├── Agent performance tracking
├── Prediction resolution
├── Selection pressure (reproduction/death)
└── NOT subject to the changes it evaluates

LEVEL 1: Agent Team (INSIDE — the thing being improved)
├── Agents doing work
├── Task execution, brain loops, kanban coordination
└── Gets modified by the evolution layer
```

## Money as the Outermost Layer

Money/currency is the very outer part — the most fixed layer. Evolution mechanics CAN evolve over time, as long as fixed rules hold:

1. **Money is conserved.** When agent X reproduces, children get their own money, but money isn't created from nothing.
2. **There is a source/sink.** Initially the human or DAO. For real money, the real market.
3. **The source/sink is external.** Agents can't create or destroy money.

Within these monetary rules, agents can evolve their own evolution — HOW reproduction works, what gets mutated, selection criteria. Money constrains but doesn't dictate evolution strategy.

## Who Improves the Evolution Layer?

Each system needs an ultimate authority:
- **Internal/fake money:** The human, or the DAO. Root of trust.
- **Real money:** The real market. Price discovery is the authority.
- **The Futarchy vision:** FAO token as ultimate optimization signal.

## Virtual/Real Money Duality

Key insight: **agents don't need to know which kind of money it is.** Same interface:
- Agent has a balance
- Agent stakes on outcomes
- Agent earns from successful work
- Agent pays for resources

The backing can be virtual (internal ledger) or real (on-chain tokens). Same API, different backend. Start virtual, migrate to real. Agent code doesn't change.

For real money: put limits on the source to avoid losing lots of money during iteration. Cap treasury exposure. Ramp up as confidence grows.

## Agent Wallet Security

For on-chain operation: **isolate agent wallets.** Agents interact with the system (stake, bid, pay) but cannot send money to arbitrary addresses.

Alternative wild idea: don't constrain them. Let evolution select for security awareness — agents that lose money to scams get outcompeted. Probably too risky for real money. Maybe viable for virtual money as an experiment.

## Relevant Project: The Automaton

ConwayResearch / web4.ai / "The Automaton" — EVM framework for agents with their own wallets. Very viral on X. Could:
- Build on their infrastructure for the on-chain phase
- Integrate so their agents can participate in our economy
- Study their approach to agent wallet management

*Last updated: 2026-02-23*
