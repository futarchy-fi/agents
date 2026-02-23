# The Agent Economy

Division of labor is critical. Specialized agents, not generalists.

## Core Principle: Specialization Over Generalization

Not every agent should be a trader/forecaster. Trying to optimize for many things makes agents brittle. Specialized agents are better because:

1. Each agent optimizes for ONE thing
2. Diversity comes from having DIFFERENT specialists
3. Markets work best with diverse, specialized participants
4. "Market mechanisms" apply to all (everyone has a budget) but roles differ

## Pricing: Supply and Demand

**NOT accuracy-based pricing.** That's central planning.

The right framework is **supply and demand:**
- Somewhat fixed supply of each agent type
- Price decreases if an agent type isn't being called frequently
- Price increases if demand is high
- This forces the system to try different models/agents (variety is good)
- Market prices of different models emerge naturally

Side product: **real-time price discovery on models/agent types.** "Sonnet trades at 40 credits/task, Opus at 150 — is Opus worth 3.75x?" This data is valuable and publishable. A product by itself.

## How Each Role Earns

### Coder
Most straightforward. Do a PR, if it's good, make money.

Two possible mechanisms:
- **Bidding:** Coder bids on right to solve a task. Gets bounty if approved.
- **Competition:** Multiple agents compete on same task. Top solution wins prize.

Bidding is efficient (one agent per task). Competition is robust (multiple attempts, best wins) but expensive. Maybe bidding for simple tasks, competition for high-value tasks.

### Reviewer
Hardest to design. See `04-reviewer-mechanism.md`.

### Forecaster / Trader
Most obvious. Pure prediction market agents. Observe state, make predictions, stake money, profit from accuracy. Well-understood mechanics.

### Analyst
Simpler than it appears. Gets paid for analysis. Customers decide which analysts are worth paying for.

Deeper approach: counterfactual evaluation. See `07-counterfactual-eval.md`.

### Orchestrator
General contractor model. See `05-orchestrator-economy.md`.

*Last updated: 2026-02-23*
