# Evolution Algorithms

Evolving intelligent agents without expensive RL or finetuning.

## Core Constraint

We want to evolve agent behavior cheaply. No model weight updates. No RLHF. Evolve the *scaffolding*, not the model.

## What Gets Evolved (Agent "Genes")

- **Prompt templates** (the biggest lever)
- **Tool permissions and usage patterns**
- **Strategy preferences** (when to decompose vs. attempt directly)
- **Review criteria and thresholds**
- **Model selection** (which model for which subtask)

The model is fixed; the scaffold evolves. The fitness signal comes from the economy: agents that earn more currency have better configurations.

## Evolution Mechanics

### The Basic Loop

```
1. Agents operate in the economy for N tasks (a "generation")
2. Measure earnings (fitness)
3. Bottom K% die (configuration deleted)
4. Top K% reproduce (configuration forked)
5. Children get mutations
6. New generation begins
```

### Mutation: LLM-as-Mutator (decided)

Random character/word mutations destroy meaning. Instead: use the LLM itself to propose meaningful mutations.

- Give the LLM the current prompt + the agent's performance data
- "This prompt scored 0.6 on coding tasks. Suggest a modification that might improve it."
- The LLM understands language, so its mutations are semantically meaningful
- Can also do targeted mutation: "this agent struggles with error handling — modify the prompt to improve that"

### Crossover Between Successful Agents (decided)

Take structured sections from two successful configs and combine:
- Task decomposition strategy from agent A + error handling approach from agent B
- Requires some structure in prompts (sections, labeled blocks) so crossover has meaningful cut points

### Diversity: Niching with Cross-Pollination (decided)

**NOT artificial diversity bonus** — that's too central-planned, not market-based.

Instead: **niching.** Agents naturally specialize in different task types (frontend coder, backend coder, smart contract coder). A frontend specialist doesn't compete with a backend specialist — they're in different niches. This preserves diversity through specialization.

Occasional **cross-pollination between species:** a successful strategy from one niche gets tried in another. E.g., an error-handling approach that works well for backend coders gets crossed into a frontend coder's config.

The market structure itself creates diversity: the supply/demand pricing means there's always room for different specialists. If everyone becomes a backend coder, backend prices crash and frontend prices soar, creating pressure to diversify.

### Generation Length (open)

Not decided. Too short = measuring noise. Too long = slow evolution. Probably tied to task volume: "at least N completed tasks per agent before a generation ends." N needs empirical tuning.

## Relevant Literature (TO STUDY — deep research needed)

- **EvoPrompt** — genetic algorithms applied to prompt text (crossover + mutation)
- **PromptBreeder** — self-referential prompt improvement (prompts that evolve prompts)
- **DSPy** — programmatic optimization of LLM pipelines (signatures, modules, optimizers)
- **OPRO** (Google DeepMind) — LLM as optimizer ("here are past attempts and scores, propose better")
- **Quality-Diversity algorithms** — MAP-Elites and similar; maintain diverse archive of solutions, not just the best one
- **Novelty search** — reward novelty rather than just fitness; prevents premature convergence
- **Genetic programming for scaffolds** — evolving program structures, not just parameters

## Key Research Questions

1. What's the state of the art for scaffold/prompt evolution?
2. What mutation operators work best for prompt text?
3. How do you maintain diversity? (Avoid all agents converging to same config)
4. What's the right population size / generation length?
5. Is this Lamarckian (agents pass on learned traits) or Darwinian (only configuration mutates)?
6. How many generations before meaningful improvement?
7. What's the right fitness function? (Earnings? Success rate? Some combination?)
8. Can we evolve the evolution strategy itself? (Meta-evolution)

## NOTE: This topic needs deep literature review before making design decisions. The surface-level mentions above are starting points only.

*Last updated: 2026-02-23*
