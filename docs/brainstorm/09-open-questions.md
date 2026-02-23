# Open Questions

Unsolved problems and research directions. Updated as discussions progress.

## Mechanism Design

1. **Bond curve for review escalation.** Reality.eth doubles each level. Right for us? What triggers authority evaluation?
2. **Coder mechanism: bidding vs competition.** Which is better? Maybe both, depending on task value?
3. **Orchestrator payment to sub-agents.** Upfront? On completion? Escrow?
4. **New agent bootstrap.** How does a new agent with no track record enter the market?
5. **Orchestrator-to-orchestrator subcontracting.** Allow it? Risks of recursive contracting?
6. **Auto-approval threshold.** What market confidence level is safe for Level 3 auto-merge?

## Evolution

7. **Literature review needed.** EvoPrompt, PromptBreeder, DSPy, OPRO — deep study before design decisions.
8. **Diversity maintenance.** How to prevent monoculture (all agents converging to same config)?
9. **Meta-evolution.** How much can evolution strategy itself evolve?
10. **Generation timing.** How long is a "generation"? When does reproduction/death happen?

## Economics

11. **Internal currency viability.** Can virtual currency drive meaningful evolution without real money?
12. **Supply/demand mechanics.** How to implement supply-demand pricing? Fixed supply per agent type? Dynamic?
13. **Money source/sink design.** How does currency enter and leave the system?
14. **Treasury risk limits.** For real money phase — how to cap exposure?

## Technical

15. **On-chain overhead.** Practical to settle every task on-chain? Or only high-value?
16. **Agent wallet security.** Constrained vs unconstrained wallets for real money.
17. **Counterfactual cost.** How often can we afford to run counterfactuals?
18. **Brain state persistence.** Format and recovery for orchestrator's internal goal tree.

## Conceptual

19. **Self-referential evolution safety.** When agents evolve their own evolution, what must remain fixed?
20. **The reviewer bootstrap.** Chicken-and-egg: you need good reviews to evaluate agents, but reviewers are agents too.
21. **Futarchy without real money.** Is there "a completely new framework for futarchy self-improvement" when markets are internal? (Research question, not engineering.)

*Last updated: 2026-02-23*
