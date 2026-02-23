# Futarchic Organizations

The core thesis of this project.

## What Is Futarchy?

Futarchy is a governance system proposed by economist Robin Hanson: **"vote on values, bet on beliefs."** You decide what you want (values), then use prediction markets to determine which policies will achieve it (beliefs). The policy that the market predicts will work best gets adopted.

In traditional organizations, decisions are made by authority — a manager, a committee, a founder. In a futarchic organization, decisions are made by a market, and the market is held accountable to measured outcomes.

## Applied to Agent Teams

We're applying futarchy to teams of AI agents. The agents do real work — writing code, reviewing pull requests, researching questions, coordinating projects. The question is: how should the team be governed?

**The conventional approach:** Fixed rules. Retry failed tasks 3 times. Assign tasks round-robin. Use the same model for everything. A human writes the rules, and the rules don't change unless the human changes them.

**The futarchic approach:** Market mechanisms. A forecaster predicts whether a task will succeed before it starts. Agents bid on tasks based on their confidence. When someone proposes a change to the team (new prompt template, different model, new assignment rules), a market predicts whether the change will improve outcomes. The change is adopted, measured, and the market resolves. If the prediction was wrong, revert.

## The Separation Principle

Futarchy needs one thing to be fixed: **the value being optimized.** Everything else can be modified by the system itself.

"Vote on values, bet on beliefs" means the values are chosen, not discovered. The beliefs — which policies, configurations, and strategies achieve those values — are what the market figures out. As long as the value signal is external and trustworthy, the agents can modify everything below it: their own prompts, tools, team structure, dispatch rules, even the market mechanisms themselves.

What serves as the external value signal depends on the stage:

- **Early (internal currency):** The human defines what "good" means. The human is the root of trust.
- **Later (on-chain):** The FAO token price is the optimization target. The real market is the root of trust. Agents optimize for token value, and the market judges whether they're succeeding.

Once the top-level signal is external, the separation problem mostly solves itself. The agents can reorganize, evolve, and rewrite their own rules — as long as the results are measured against something they can't manipulate.

See [separation problem](brainstorm/02-separation-problem.md) for deeper exploration of edge cases and failure modes.

## Why This Matters

Most AI agent frameworks are governed by heuristics that someone hardcoded. They don't learn, they don't adapt, and they can't tell you whether a change helped or hurt.

A futarchic agent team learns its own governance from evidence. Decisions are made by betting, and bets are settled by reality. Over time, the team converges on governance rules that actually work — not because someone designed them, but because the market selected them.

This is also a testbed for futarchy itself. If it works for agent teams — fast, measurable, forkable — it builds evidence for applying futarchic governance more broadly.

## Components

Futarchic organizations need several things to work. Each of these is explored separately:

1. **A market mechanism** — prediction markets on outcomes. For PRs, this is straightforward: "will this PR be merged?" For team governance changes, it's harder. See [reviewer mechanism](brainstorm/04-reviewer-mechanism.md) and [separation problem](brainstorm/02-separation-problem.md).

2. **An economy** — agents need currency to stake, earn, and spend. Specialized agents earn in different ways. See [agent economy](brainstorm/03-agent-economy.md).

3. **An evolution mechanism** — the market signal needs to drive actual change. Agents whose configurations earn more should reproduce. Those that fail should be replaced. See [evolution](brainstorm/06-evolution.md).

4. **A measurement system** — you need to know whether things are actually getting better. Counterfactual evaluation is the principled approach. See [counterfactual eval](brainstorm/07-counterfactual-eval.md).

5. **Real stakes** — markets without stakes are polls. Internal currency can bootstrap this, but real money (on-chain) is the endgame. See [path to market](brainstorm/08-path-to-market.md).

## Open Questions

We don't know the answers to these yet:

- Can internal (non-real-money) markets drive meaningful improvement?
- What does the minimum viable market mechanism look like?
- How do you prevent agents from gaming the evaluation layer?
- What's the right balance between market governance and human oversight?
- Does this actually produce better agent teams than hand-tuned heuristics?

The only way to find out is to build it and measure.
