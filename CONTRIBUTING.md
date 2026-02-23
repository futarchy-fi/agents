# Contributing to agents.futarchy.ai

## Testing Philosophy

Tests exist to catch non-obvious bugs and verify invariants. Not to pad coverage.

**Write tests for:**
- Invariants (prices sum to 1, credits are conserved, frozen + available = total)
- Round-trip properties (buy then sell, add then remove liquidity)
- Edge cases that actually matter (zero balance, market at extreme prices, rounding near boundaries)
- Cross-domain interactions (risk engine + market engine working together)
- Rounding: dust always favors the AMM, never the counterparty

**Don't write tests for:**
- Simple getters, constructors, or property accessors
- Obvious arithmetic (does 1 + 1 = 2)
- Things the type system already guarantees
- Individual functions in isolation when the round-trip test covers them

A good test encodes a belief about the system that isn't obvious from reading the code.
