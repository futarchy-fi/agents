# Counterfactual Evaluation

Measuring agent value by comparing outcomes with and without their contribution.

## The Insight

From kas's conversations with Tiptree Systems founders and others: you can measure an agent's *counterfactual contribution* by recording outcomes with and without their input.

## How It Works

1. A forecaster queries an analyst before making a prediction
2. Record the forecast WITH the analyst's input
3. Record what the forecast WOULD have been WITHOUT the analyst (run it, or use baseline)
4. Compare outcomes: did the analyst-informed forecast make more money?
5. Difference = analyst's counterfactual value (positive or negative)

## Generality

This works for ANY role:
- Did the reviewer's feedback improve the coder's next attempt?
- Did the orchestrator's decomposition beat a naive approach?
- Did the forecaster's prediction improve budget allocation vs. uniform baseline?

**You can measure the value of ANY agent in ANY role**, as long as you can run (or estimate) the counterfactual.

## Shapley Values for Multi-Agent Attribution

With multiple agents contributing to an outcome, use **Shapley values**:
- Answers: "What is each agent's marginal contribution to the group outcome?"
- Game-theoretic, principled, fair attribution
- Computationally expensive: O(2^n) for n agents
- Approximations exist: sampling-based Shapley, tractable for small teams

## Why This Is Powerful

Solves the attribution problem that kills every team-performance tracking system. Most systems either:
- Credit everyone equally (unfair, no signal)
- Credit the last agent in the chain (ignores upstream contributions)
- Use heuristics (brittle, gameable)

Counterfactual evaluation + Shapley values gives *principled, objective, ungameable* attribution. It's the theoretically correct answer.

## Practical Considerations

- Running counterfactuals = running things twice (expensive)
- Can use baseline models as proxy instead of full re-runs
- For high-value decisions, cost is easily justified
- Don't need to run counterfactuals on every task — sample
- Frequency is tunable (like the reviewer audit rate)

## Research Questions

- Best approximation methods for Shapley values?
- How often to run counterfactuals? (Every task? Sampled? Only high-value?)
- Can you estimate counterfactuals without actually re-running? (Cheaper proxy models?)
- How to handle temporal effects? (An analyst's insight helps 10 tasks later — how to attribute?)

*Last updated: 2026-02-23*
