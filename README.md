# agents

What if an organization was governed by prediction markets instead of management?

## The Idea

A **futarchic organization** is one where decisions are made by betting, not by authority. Instead of a manager deciding "use this tool" or "assign this task to that person," a market decides — and the market is accountable to measurable outcomes.

We're building this for AI agent teams first. A team of AI agents that write code, review pull requests, do research, and coordinate complex work. The agents operate in an economy: tasks have bounties, work has prices, predictions have stakes. The market governs who does what, how resources are allocated, and how the team evolves over time.

This isn't a new idea. [Futarchy](https://mason.gmu.edu/~rhanson/futarchy.html) was proposed by Robin Hanson in 2000 as a form of government. [Jeffrey Wernick](https://www.overcomingbias.com/p/hail-jeffrey-wernick) proposed applying it to corporate governance — replacing boards of directors with prediction markets. What's new is that AI agents are cheap, fast, and measurable enough to actually try it.

## Why Agent Teams?

Futarchy is hard to test with human organizations — too slow, too political, too many confounding variables. Agent teams are the perfect testbed:

- **Fast iteration.** An agent team can run hundreds of tasks per day. You get statistically meaningful data quickly.
- **Measurable outcomes.** Did the code pass review? Did the prediction come true? Did the change improve performance? Binary, verifiable, no ambiguity.
- **Controlled experiments.** You can fork a team configuration, run both versions, and compare. Try that with a human org.
- **Real stakes without real risk.** Start with internal currency. Graduate to real money when the mechanics are proven.

## What's in This Repo

We're exploring several connected ideas. Each can stand alone, but they're strongest together:

| Idea | What it is | Status |
|------|-----------|--------|
| [Futarchic governance](docs/brainstorm/02-separation-problem.md) | Market mechanisms governing an agent team. The evaluation layer sits outside the system being evaluated. | Brainstorming |
| [Agent economy](docs/brainstorm/03-agent-economy.md) | Agents earn and spend currency through specialized work. Supply and demand pricing. | Brainstorming |
| [PR prediction markets](docs/brainstorm/04-reviewer-mechanism.md) | Prediction markets on whether a PR will be merged. Useful signal for maintainers. Standalone product. | Brainstorming |
| [Scaffold evolution](docs/brainstorm/06-evolution.md) | Evolving agent prompts and configs via natural selection, driven by market fitness signals. No model retraining. | Research needed |
| [Counterfactual evaluation](docs/brainstorm/07-counterfactual-eval.md) | Measuring an agent's value by comparing outcomes with and without their contribution. | Research needed |

See the [brainstorm index](docs/brainstorm/00-INDEX.md) for detailed thinking on each.

## Status

Early stage. We're figuring out what to build first and writing things down as we go. The brainstorm folder is where the real thinking lives.

## License

MIT
