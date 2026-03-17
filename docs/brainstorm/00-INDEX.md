# Brainstorm Index

Detailed thinking on each idea. These are explorations, not specs. Some of this will turn out to be wrong.

## The Ideas

- `01-core-thesis.md` — What makes this combination of ideas unique
- `02-separation-problem.md` — Why the evaluation layer must be separate from the thing being evaluated
- `03-agent-economy.md` — How agents earn, spend, and specialize
- `04-reviewer-mechanism.md` — Review as a product, escalation games, prediction markets on PRs
- `05-orchestrator-economy.md` — The orchestrator as general contractor
- `06-evolution.md` — Evolving prompts and scaffolds without retraining models
- `07-counterfactual-eval.md` — Measuring agent value via counterfactuals and Shapley values
- `08-path-to-market.md` — What to build first, how to get to real users
- `09-open-questions.md` — Things we don't know yet

## MVP Design

- `12-mvp-design.md` — Conditional prediction markets on PRs. LMSR AMMs, risk engine / market engine separation, data models, implementation plan.
- `17-mvp-status-2026-03-17.md` — **MVP shipped.** What's live, architecture decisions made, lessons learned.

## Beyond PRs (Q4 Vision — 2026-03-17)

- `13-issue-resolution-markets.md` — Markets where agents bet on issues and then solve them. Prediction + action linked. Compound markets (issue → PR → quality).
- `14-market-types-beyond-prs.md` — Full taxonomy: issue resolution, repo adoption, CI stability, skill safety, governance. Four business hypotheses and how to test them.
- `15-trust-layer-for-ai.md` — Prediction markets as a trust/safety layer for AI skills and agents. Bug bounties through markets. The decentralized social credit score, for AIs first.
- `16-agent-reputation-exchange.md` — Portable agent reputation from prediction market track records. The feedback loop: trade → accuracy → trust → opportunity → trade.

## Architecture Thinking

- `10-autonomous-team-architecture.md` — How an autonomous agent team might work (task model, coordination layers, agent tiers). Speculative — this is brainstorming, not a roadmap.
- `11-architecture-decisions.md` — Tentative decisions made during brainstorming. Subject to change as we learn more.

## Current Leanings

These are directions we're leaning toward, not final decisions:

- Start with internal currency, graduate to on-chain later
- Specialized agents over generalists
- Supply and demand pricing, not fixed rates
- Escalation game with trusted authority as backstop for reviews
- Run full autonomy on our own repo first
- **Issue resolution markets** as the next market type (objective resolution, actionable)
- **Trust layer for AI** as the most timely business opportunity
- **Agent reputation** as the long-term moat

*Last updated: 2026-03-17*
