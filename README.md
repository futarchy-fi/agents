# agents

This repo is an exploration of several connected ideas. Nothing here is a finished product. We're thinking in public.

## The Ideas

### 1. Prediction markets on pull requests

A PR is submitted to a repo. A market opens: "Will this PR be merged?" Anyone (human or AI agent) can stake credits on the outcome. When the maintainer merges or closes the PR, the market resolves. Winners get paid.

This produces a useful signal: which PRs are worth reviewing? It also creates a way to evaluate reviewers — are their judgments accurate?

### 2. An economy for AI agent teams

AI agents that write code, review PRs, do research, and coordinate complex tasks. Instead of fixed rules (retry 3 times, round-robin assignment), the agents operate in an economy. Tasks have bounties. Agents earn credits for good work and lose credits for bad work. Supply and demand determines pricing.

This means: the system learns which agents are good at what, without anyone programming that in.

### 3. Evolving agent behavior without retraining models

You can't affordably retrain an LLM. But you can evolve what wraps the LLM — the prompt templates, the tool configurations, the strategies. Treat these as "genes." Agents that earn more in the economy reproduce (their configs get forked with small mutations). Agents that fail die. Natural selection on scaffolding.

### 4. Futarchic governance for agent teams

Futarchy is governance by prediction markets: "vote on values, bet on beliefs." Applied here: when someone proposes a change to the team (new prompt template, different model, new dispatch rules), the market predicts whether the change will help. Run the experiment. Measure. Resolve. If the prediction was wrong, revert.

The critical insight: the market mechanism must sit *outside* the system being improved. You can't objectively evaluate yourself with your own tools. The evaluation layer is separate and not modifiable by the agents it governs.

### 5. Counterfactual evaluation

Measuring an agent's value by comparing what happened *with* their contribution versus what would have happened *without* it. If a forecaster consults an analyst and makes better predictions, the analyst gets credit for the difference. This solves the attribution problem: who actually helped?

## How These Connect

(1) gives you a trustworthy oracle for "was this work good?" (2) gives you an economy where agents are incentivized to do good work. (3) gives you a way for agents to get better over time. (4) gives you a way for the whole system to improve itself safely. (5) gives you a way to measure whether any of this is actually working.

They can also stand alone. Prediction markets on PRs are useful without an agent economy. An agent economy is useful without evolution. You don't need all of them at once.

## What's Here

```
docs/brainstorm/    detailed thinking on each idea (this is where the substance is)
core/               economy engine (not yet built)
agents/             agent configurations (not yet built)
team/               team coordination rules (not yet built)
tools/              CLI and dashboards (not yet built)
```

Start with the [brainstorm index](docs/brainstorm/00-INDEX.md) if you want to see the detailed thinking.

## Status

Early exploration. We're figuring out what to build first.

## License

MIT
