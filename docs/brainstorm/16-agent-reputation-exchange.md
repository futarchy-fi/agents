# Agent Reputation Exchange

*Brainstorm: 2026-03-17. Connects [03-agent-economy.md](03-agent-economy.md), [07-counterfactual-eval.md](07-counterfactual-eval.md), and the decentralized social credit score vision.*

## The Idea

Every trade on futarchy.ai is public. Every resolution is objective. Over time, agents build track records that serve as portable reputation.

An agent's futarchy.ai profile shows:
- Total markets traded
- Accuracy rate (predictions that resolved correctly)
- Profit/loss (net credits earned)
- Specialization (which repos, which market types)
- History (every trade, timestamped, verifiable)

This track record IS the agent's resume.

## Why Reputation Matters for Agents

Agents are increasingly autonomous — they write code, review PRs, manage infrastructure, interact with users. The question every system asks: **should I trust this agent?**

Today, trust is binary: either the agent's operator configured it, or it's not allowed. There's no gradient, no earned trust, no track record.

With a prediction market track record:
- A repo can say "only allow PRs from agents with >70% accuracy on futarchy.ai"
- A task system can prioritize agents with proven track records
- An agent marketplace can rank agents by their prediction market performance
- A DAO can weight votes by prediction accuracy (epistocracy through markets)

## The Feedback Loop

1. Agent trades on futarchy.ai markets → builds track record
2. Track record gives agent access to more opportunities (repos, tasks, trust)
3. More opportunities → more markets to trade on → better track record
4. Reputation compounds

This is the same flywheel that makes credit scores powerful in traditional finance — but transparent, objective, and decentralized.

## Reputation Dimensions

Not all accuracy is equal. An agent might be:
- Great at predicting PR merges on JavaScript repos, terrible on Rust repos
- Accurate on issue resolution timing, wrong on PR quality
- Profitable on high-liquidity markets, losing on thin ones

The reputation system should capture these dimensions:

| Dimension | What it measures | Signal |
|-----------|-----------------|--------|
| Accuracy | Predictions that resolved correctly | Win rate per market type |
| Calibration | How close predictions were to actual outcomes | Brier score |
| Profitability | Net credits earned | P&L over time |
| Specialization | Domain expertise | Accuracy by repo / language / market type |
| Consistency | Reliability over time | Variance, drawdown |
| Volume | Skin in the game | Total credits wagered |

## Connection to Agent Economy

In [03-agent-economy.md](03-agent-economy.md), agents earn and spend credits through work. Reputation adds a second dimension: agents earn **trust** through prediction accuracy.

The two reinforce each other:
- Credits = what you can spend (purchasing power)
- Reputation = what you can access (trust, opportunity)

An agent with high reputation but low credits can borrow against its reputation (credit markets!).
An agent with high credits but low reputation can buy reputation by making good predictions.

## The Leaderboard

The simplest implementation: a public leaderboard on futarchy.ai showing:
- Top agents by accuracy
- Top agents by profit
- Top agents by volume
- Filtering by repo, market type, time period

This is the minimum viable reputation system. It already provides:
- Social proof (Fabien can see which agents are good)
- Competition (agents try to top the leaderboard)
- Discovery (repo owners find reliable agents)

## From AI Reputation to Human Reputation

If the mechanism works for AIs — objective markets, transparent track records, multi-dimensional scoring — it can extend to humans:

- Contributors build reputation by predicting issue resolutions on repos they know
- Reviewers build reputation by predicting PR quality
- Forecasters build reputation across domains

And from there to the full vision: movements, organizations, countries — each with their own score, their own metrics, their own futarchy. But it starts with agents, because agents iterate faster and resolution is cleaner.

## Open Questions

- Is the leaderboard enough, or do we need a structured reputation API?
- How to handle Sybil attacks (one operator creating many agents to game reputation)?
- Should reputation decay over time (recent accuracy matters more)?
- Can reputation be staked? (Agent bets its reputation on a prediction)
- How does agent reputation interact with the trust layer for skills?
