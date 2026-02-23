# Task Executor Vision

Where we're going with autonomous task execution. Phase 1 (the delegate-and-dispatch daemon) is live. This document captures what comes next — drawn heavily from the [midpoint-agi](https://github.com/futarchy-fi/midpoint-agi) project, adapted to a multi-agent team architecture.

This may outgrow OpenClaw. The ideas here describe a general-purpose autonomous agent team — the task system, the coordination layer, and the self-improvement loop are not specific to any one CLI tool. If this becomes its own project, so be it.

---

## The Conceptual Model

Five things that must be clearly separated:

### 1. The Agent

An agent is an identity with state. It has:

- **Memory** — version-controlled files (MEMORY.md, daily notes, decision logs). The agent's accumulated knowledge.
- **Origin pointer** — what software the agent is built from. E.g., `openclaw:commit-abc123`. The agent is an *instance* of this software, not the software itself.
- **Internal scripts** — the tools available to the agent (task_update.py, tasks.py, prompt templates). Part of the agent's capability set.
- **Configuration** — which models to use, thinking levels, tool permissions, agent identity (SOUL.md, IDENTITY.md).

The agent's state is the combination of all of the above. It should be version-controlled so that any change to the agent itself (new scripts, updated memory, new config) is trackable and revertible.

Today this lives unversioned in `~/.openclaw/workspace/`. Long-term, this is one or two git repos:
- **Agent repo** — identity, config, origin pointer, internal scripts
- **Agent memory repo** (optional, separate) — memory files, execution history, decision logs

### 2. The Project

A project is what the agent works *on*. It's a separate codebase — a separate git repo with its own history, branches, and contributors. Examples:
- The OpenClaw repo (`github.com/openclaw/...`)
- A smart contract repo (`github.com/futarchy-fi/...`)
- A documentation site
- Infrastructure-as-code (Terraform, etc.)

The agent does not own the project. Multiple agents might work on the same project. One agent might work on multiple projects. The project has its own lifecycle independent of the agent.

### 3. The Team

The team is the set of agents, their roles, their coordination rules, and the shared task board. It's a thing that exists above any individual agent.

The team has:
- **Roster** — which agents exist, their capabilities, their tier (see Agent Categories below)
- **Coordination rules** — concurrency limits, dispatch priority, review policies
- **Shared task board** — the kanban: what work exists, who's doing it, what's blocked
- **Performance history** — how agents and the team as a whole are doing over time
- **Team configuration** — the rules, policies, and structure that define how this team operates

The team itself improves. Adding a new agent, changing dispatch rules, adjusting review thresholds, updating prompt templates — these are all changes to the team, not to any individual agent. The team should be version-controlled so you can checkpoint, compare, and revert team-level changes.

### 4. The Recursive Case

The agent can work on the repo it's built from. An OpenClaw agent can work on improving OpenClaw. But:

- The **running agent instance** is separate from the **project being modified**. The agent operates at `openclaw:abc123` while proposing changes that would become `openclaw:def456`.
- **Self-update is an explicit step.** The agent doesn't auto-adopt its own changes. There's a controlled "update origin" operation tracked in the agent repo: "agent now runs openclaw:def456 (was abc123)." This step can be reverted.
- **The agent repo tracks the origin pointer.** So you can always answer: "what version of itself was this agent running when it did that work?"

This gives you safe recursion — the agent improves its own codebase through the same task/dispatch/validate cycle as any other project, but adoption is gated and reversible.

### 5. The Meta-Recursive Case

The team can improve itself. An orchestrator agent can propose changes to dispatch rules, prompt templates, or team structure — and those changes go through the same task/review/validate cycle. But adoption is gated: the team at configuration-version N decides whether to become configuration-version N+1.

This is the "team itself is a thing that improves" idea. It needs the same safety properties as individual self-improvement: versioned, checkpointed, revertible.

### Today vs Ideal

| Concept | Today | Ideal |
|---------|-------|-------|
| Agent state | Unversioned files in `~/.openclaw/workspace/` | Version-controlled agent repo |
| Agent memory | `MEMORY.md` + `memory/*.md` in workspace | Separate memory repo with commit-per-session |
| Origin pointer | `openclaw` binary at whatever version is installed | Explicit `openclaw:<hash>` tracked in agent repo |
| Internal scripts | `scripts/*.py`, `scripts/*.mjs` in workspace | Part of agent repo, versioned alongside config |
| Project | Mixed into workspace alongside agent files | Separate repo(s), referenced by tasks |
| Self-update | Not controlled — `apt upgrade` or manual | Explicit origin-update step, revertible |
| Team | Implicit — hardcoded in executor config | Version-controlled team repo with roster, rules, history |
| Performance | Not tracked | Per-task, per-agent, per-team metrics |

We don't need to reach the ideal in one step. But every phase should move toward this separation, not away from it.

---

## The Two-Layer Task Model: Brain and Kanban

This is the central architectural tension. There are two kinds of task management happening simultaneously, and they serve different purposes.

### The Kanban (Team Coordination)

The team's shared task board. This is what `tasks.json` is today. It tracks:
- What work exists (title, description, priority)
- Who's doing it (assignee)
- What state it's in (pending, in_progress, review, done)
- Who reviews it (reviewer)

Every team member can see the kanban. The human overseer can see the kanban. It's the source of truth for "what is the team working on?"

This is straightforward project management — a Jira board, a GitHub project, a kanban wall.

### The Brain (Internal Reasoning)

When a capable agent (like an orchestrator) takes on a complex task, it doesn't just execute — it *thinks*. It decomposes the goal into subgoals, tries approaches, validates results, diagnoses failures, and retries. This is the midpoint-agi model: recursive goal decomposition with OODA loops.

The brain is internal to the agent. It's the agent's private reasoning about how to accomplish a kanban task. Other agents don't see it. The kanban just shows "T502: in_progress (orchestrator)" — it doesn't show the orchestrator's internal subgoal tree.

### Why They Must Be Separate

If you merge them, you get noise. The kanban becomes cluttered with internal reasoning steps that only matter to one agent. Other agents see decomposition artifacts they can't act on. The human loses the high-level view.

If you keep them totally separate, you lose visibility. The human can't see *why* a task is taking so long or *where* an agent is stuck internally.

### The Nesting Model

The solution is nesting with a visibility boundary:

```
KANBAN (team-visible)
├── T502: "Build search feature" (orchestrator, in_progress)
│   │
│   │  ┌─── BRAIN (orchestrator-internal) ───────────────┐
│   │  │ Goal: Build search feature                       │
│   │  │ ├── Subgoal 1: Design API schema    [done]       │
│   │  │ ├── Subgoal 2: Implement backend    [in_progress]│
│   │  │ │   └── attempt 1: failed (wrong index type)     │
│   │  │ │   └── attempt 2: in progress                   │
│   │  │ └── Subgoal 3: Build UI component   [pending]    │
│   │  └─────────────────────────────────────────────────-─┘
│   │
│   └── (human can "drill in" to see the brain state)
│
├── T503: "Fix login bug" (coder, pending)
└── T504: "Research caching options" (analyst, done)
```

Properties:
- **The kanban is the coordination layer.** Dispatch, review, and inter-agent work all happen here.
- **The brain is the reasoning layer.** Goal decomposition, retry logic, failure analysis all happen here.
- **The human can drill in.** If T502 is taking too long, the overseer can inspect the orchestrator's internal subgoal tree to see where it's stuck.
- **Other agents can't see inside.** The coder doing T503 doesn't know or care about the orchestrator's internal subgoals.

### The Boundary Crossing Problem

Here's the hard part. Sometimes the brain needs external help.

The orchestrator is working on T502 (build search feature). Internally, it has decomposed this into subgoals. Subgoal 2 (implement backend) turns out to need specialized database work. The orchestrator wants to ask the coder to do it.

Now we have a brain-subgoal that needs to become a kanban-task. The internal reasoning has crossed the team boundary.

```
KANBAN
├── T502: "Build search feature" (orchestrator, in_progress)
│   └── BRAIN: Subgoal 2 needs DB work → creates T505
├── T505: "Implement search index" (coder, pending)
│   ├── parentTask: T502
│   └── brainContext: "This is subgoal 2 of T502's decomposition"
```

Rules for boundary crossing:
1. **The kanban task is a real kanban task.** T505 shows up on the board. The coder gets dispatched. Everyone can see it.
2. **The link to the brain is metadata.** T505 carries `parentTask: T502` so the system knows it exists because of the orchestrator's internal reasoning.
3. **The brain tracks the external dependency.** Subgoal 2 in the orchestrator's brain is now "waiting on T505." When T505 completes, the brain is notified and can continue.
4. **The orchestrator doesn't expose its full brain.** T505's description is self-contained — the coder doesn't need to understand the orchestrator's full goal tree. Context is selective.

This means the kanban task model needs:
- `parentTask` — which kanban task spawned this one (optional)
- `brainSubgoalId` — which internal subgoal this corresponds to (optional, opaque to everyone except the parent)

And the brain model needs:
- The ability to mark a subgoal as "delegated to kanban task T505"
- A notification mechanism when that kanban task completes

### What This Means For Implementation

Phase 4 (goal decomposition) becomes two things:
- **4a: Kanban hierarchy** — tasks can have parent/child relationships on the board. Simple, visible, no internal reasoning.
- **4b: Brain loop** — the orchestrator (and any agent in the "thinker" tier) gets an internal goal decomposition engine. This is the midpoint-agi core loop. It lives inside the agent, not in the executor.

The executor only knows about the kanban. It dispatches kanban tasks. If an agent internally decomposes and then creates child kanban tasks via `delegate`, the executor sees new kanban tasks — it doesn't know or care that they came from a brain.

---

## Agent Categories

Not every agent needs a brain. Agents fall into tiers based on their internal complexity:

### Tier 1: Simple Agents

**Examples:** coder-lite, analyst

These agents are stateless executors. They receive a task, do it, report back. No internal decomposition, no retry logic beyond what the executor provides, no persistent learning within a task.

The executor handles their lifecycle: dispatch, monitor, retry on failure, mark blocked on max retries.

### Tier 2: Resilient Agents

**Examples:** coder

These agents have basic internal resilience. They can:
- Detect when they're stuck and report it (mark blocked with a useful message)
- Try alternative approaches within a single task
- Report structured evidence of what they did

But they don't decompose goals or maintain internal state across retries. Each dispatch is a fresh start (with failure history from the executor).

### Tier 3: Thinking Agents

**Examples:** orchestrator

These agents have a brain. They can:
- Decompose complex goals into subgoals (internally)
- Run OODA loops: observe → orient → decide → act
- Create kanban tasks for work they can't do themselves (boundary crossing)
- Maintain state across the lifetime of a goal
- Learn from failures and adjust strategy
- Validate their own progress against criteria

The orchestrator is the primary Tier 3 agent today. Over time, other agents could gain Tier 3 capabilities — but it's not automatic. It's a capability you add deliberately because the agent's role demands it.

### Tier Determines Infrastructure

| Capability | Tier 1 | Tier 2 | Tier 3 |
|-----------|--------|--------|--------|
| Executor dispatch | Yes | Yes | Yes |
| Executor retry | Yes | Yes | Yes |
| Self-reporting | Basic | Structured | Structured |
| Internal retry | No | Within-task | Across-subgoals |
| Goal decomposition | No | No | Yes (brain) |
| Creates kanban tasks | No | No | Yes (boundary crossing) |
| Failure analysis | Executor-side | Self + Executor | Self + Brain |
| Persistent learning | No | No | Within-goal lifetime |

The key insight: the self-improvement loop (midpoint-style decompose → execute → validate → learn) is not something every agent needs. It's a specific capability tier for agents that handle complex, multi-step work. Simple agents are fine being simple.

---

## Performance Tracking

You can't improve what you can't measure. Performance tracking is essential at three levels:

### Per-Task Metrics

Every task records:
- Duration (dispatch to completion)
- Retry count and failure reasons
- Token/cost usage
- Validation score (when structured validation exists)
- Scope adherence (did the agent stay within bounds?)

### Per-Agent Metrics

Aggregated from tasks:
- Success rate (tasks completed / tasks attempted)
- Average retries needed
- Average validation score
- Cost efficiency (quality per token)
- Scope discipline (how often does the agent go out of bounds?)
- Time-to-completion distribution

### Per-Team Metrics

Aggregated from agents and the team's overall output:
- Throughput (tasks completed per time period)
- Quality (average validation scores)
- Cost (total spend per time period)
- Cycle time (from task creation to done)
- Block rate (how often do tasks get stuck?)

### Using Metrics For Improvement

Metrics are only useful if they feed back into decisions:
- **Agent-level:** If coder's success rate drops after a prompt template change, revert the change.
- **Team-level:** If throughput improves after adding a second coder agent, keep it. If it doesn't, the coordination overhead isn't worth it.
- **Self-improvement validation:** When an agent proposes changes to itself or the team, the metrics from before and after are the evidence for whether the change was good.

This requires A/B-style comparison: run the team at configuration N, measure, apply change, run at configuration N+1, measure, compare. The version-controlled team repo makes this possible — you can always go back to N.

---

## What Midpoint Got Right

Midpoint is a multi-agent system for recursive goal decomposition and repository automation. It never reached production, but the core ideas are sound:

1. **Goals decompose recursively.** A complex goal becomes subgoals becomes executable tasks. Depth-first — always work on the most concrete thing first.

2. **Git is the state machine.** Every task gets a branch. Every meaningful change is a commit. If a task fails, revert the branch. The repository *is* the checkpoint.

3. **Validation is structured, not vibes.** A validator agent scores results 0.0–1.0 against explicit criteria. Not "looks good" — did the specific acceptance criteria pass?

4. **Failure is data.** When a task fails, a failure analyzer diagnoses why. The diagnosis feeds the next attempt. Agents learn within a goal's lifetime.

5. **Context is selective.** Child tasks don't inherit the full parent context. They get exactly what they need — the relevant files, the specific criteria, the failure history. Nothing more.

6. **The orchestrator doesn't do work.** It decomposes, dispatches, validates, and reacts. The OODA loop: observe the current state, orient against the goal, decide the next action, act (dispatch or decompose further).

---

## Where Phase 1 Stands

What we shipped:

| Component | Status | Gap |
|-----------|--------|-----|
| Task creation | `delegate` MCP tool | No decomposition — flat tasks only |
| Dispatch | Daemon polls tasks.json, spawns `openclaw agent` | No branch isolation, no project separation |
| Status reporting | `task_update.py` CLI | Agent must remember to call it |
| Review cycle | Daemon dispatches reviewer | No structured validation or scoring |
| Retry | Revert to pending on unclean exit, max 3 | No failure analysis |
| Concurrency | Max 2 total, max 1 per agent type | No dependency awareness |

What we learned from the first live run (T500):
- The coder committed all pre-existing WIP because there's no branch isolation.
- There's no way to verify what the agent actually did vs what was asked.
- Agent state and project code are mixed in one directory — no clean separation.

---

## The Phases Ahead

### Phase 2: Branch Isolation and Project Separation

**Problem:** Agents work on whatever branch is checked out. They commit whatever is in the working tree, including other people's WIP. Agent infrastructure and project code are mixed.

**Solution:** Two changes:

**2a. Task branches.** The executor creates a branch for each task before dispatching. The agent works exclusively on that branch. When the agent finishes, the executor can diff the branch against the base to see exactly what changed.

```
Before dispatch:
  git stash (save any WIP)
  git checkout -b task/T502 HEAD
  → dispatch agent on task/T502

After agent exits:
  git diff main..task/T502  → this is exactly what the agent did
  git checkout main
  git stash pop (restore WIP)
```

**2b. Project pointer.** Tasks gain a `project` field — the path to the repo the agent should work in. The executor `cd`s to that path before spawning the agent. The agent's working directory is the project, not the workspace.

```
delegate({
  title: "Add date filter",
  task: "...",
  assignee: "coder",
  project: "/home/ubuntu/repos/openclaw-dashboard"
})
```

If `project` is omitted, the executor uses a default (the workspace, for backward compat). Over time, all tasks should specify a project.

Task metadata gains:
```json
{
  "project": "/home/ubuntu/repos/openclaw-dashboard",
  "baseBranch": "main",
  "baseHash": "abc123",
  "taskBranch": "task/T502",
  "resultHash": "def456"
}
```

**Merge policy:** Agent's branch is not auto-merged. The reviewer (or executor) decides whether to merge, cherry-pick, or discard.

### Phase 3: Structured Validation

**Problem:** "Review" means a reviewer agent gets spawned with the task description and the assignee's evidence text. There are no explicit criteria to check, no scoring, no structured output.

**Solution:** Every task carries `validation_criteria` — a list of checkable statements. The reviewer agent is given:
1. The criteria
2. The branch diff (`git diff base..task/T502`)
3. Tools to inspect the repo (read files, run tests)

The reviewer outputs structured JSON:
```json
{
  "criteria_results": [
    { "criterion": "Date filter component renders", "passed": true, "evidence": "..." },
    { "criterion": "Existing search filter untouched", "passed": false, "evidence": "..." }
  ],
  "score": 0.5,
  "verdict": "reject",
  "rejection_reason": "Search filter was modified despite scope boundary"
}
```

Score thresholds:
- `>= 0.8` → auto-accept (merge branch, mark done)
- `0.5–0.8` → needs human review
- `< 0.5` → auto-reject (mark pending with rejection notes, retry)

The `delegate` tool gains a `validation_criteria` field:
```
delegate({
  title: "Add date filter to dashboard",
  task: "...",
  assignee: "coder",
  reviewer: "orchestrator",
  validation_criteria: [
    "Date range filter component renders above results list",
    "Memory list filters by selected date range",
    "Existing search filter is not modified",
    "No new Python dependencies added"
  ]
})
```

### Phase 4: Goal Decomposition (Two Parts)

**Problem:** Today, the human or Hermes writes every task prompt by hand. Complex work requires manually breaking things down, creating multiple `delegate` calls, and sequencing them.

**4a. Kanban Hierarchy.**

Tasks can have parent/child relationships on the board:
```json
{
  "id": 510,
  "parentTask": 502,
  "depth": 1,
  "subtasks": [511, 512, 513],
  "dependencies": [511]
}
```

This is pure coordination — visible to everyone. The orchestrator creates child tasks via `delegate` with a `parentTask` field. The executor understands dependencies: don't dispatch T512 until T511 is done. A parent task completes only when all children complete.

**4b. The Brain Loop (Tier 3 agents only).**

The orchestrator gets an internal goal decomposition engine — the midpoint-agi core loop:

```
while goal not achieved:
  observe: read current state (repo, branch, task results)
  orient: compare state against goal criteria
  decide: decompose further, retry, delegate externally, or conclude
  act: execute decision
```

This lives inside the agent process, not in the executor. The executor sees the orchestrator as a single in_progress task. Internally, the orchestrator may be running subgoals, creating kanban tasks (boundary crossing), waiting on external results, and retrying failed approaches.

Brain state is persisted to the agent's memory so that if the orchestrator process dies, it can resume from the last checkpoint.

Decomposition rules:
- Depth limit (configurable, default 3) prevents infinite decomposition.
- When an internal subgoal needs external help → boundary crossing → new kanban task with `parentTask` link.
- Child kanban tasks inherit the parent's branch as their base.

### Phase 5: Failure Analysis and Learning

**Problem:** When a task fails 3 times, we mark it "blocked" with "max retries exceeded." No diagnosis, no learning. The next attempt starts from scratch.

**Solution:** After each failed attempt, run a failure analysis step before retrying:

1. **Diagnose:** An analyst agent reviews the execution trace, the diff, and the error output. Produces a structured diagnosis:
   ```json
   {
     "root_cause": "Agent tried to import a module that doesn't exist in the workspace",
     "category": "missing_dependency",
     "suggestion": "Add explicit file paths to the task prompt",
     "confidence": 0.9
   }
   ```

2. **Amend the prompt:** The failure diagnosis is appended to the next attempt's prompt under a "Previous Attempts" section. The agent knows what went wrong and what to try differently.

3. **Escalate intelligently:** If the same root cause repeats, escalate to a different agent type or to the human — don't just retry the same thing.

Task metadata gains:
```json
{
  "attempts": [
    {
      "startedAt": "...",
      "exitCode": 1,
      "diagnosis": { "root_cause": "...", "suggestion": "..." },
      "branchHash": "..."
    }
  ]
}
```

### Phase 6: Agent and Team State Under Version Control

**Problem:** The agent's own state (scripts, config, memory) is unversioned. The team's configuration (roster, dispatch rules, prompt templates) is unversioned. If an update breaks something, there's no rollback. You can't answer "what version of itself was this agent running when it did T500?"

**Solution:** Two levels of version control:

**6a. Agent repo.** Initialize the workspace as a git repo (or split into agent repo + memory repo). Key properties:

- Every change to agent internals (new script, updated config, new AGENTS.md) is a commit.
- The agent's origin is tracked: `origin: openclaw@2026.2.19-2 (commit 45d9b20)`.
- Self-update is explicit: a task can produce changes to the openclaw repo (project), and a separate "adopt" step updates the agent's origin pointer and installs the new version. Both steps are commits in the agent repo.
- Memory writes (MEMORY.md updates, daily notes) are also commits — you can see the agent's memory evolve over time.

**6b. Team repo.** The team configuration is its own versioned entity:

- Agent roster (who exists, what tier, what capabilities)
- Dispatch rules (concurrency, priority logic)
- Prompt templates
- Review policies and score thresholds
- Performance baselines

Changes to the team go through the same task/review cycle. "Change coder's max retries from 3 to 5" is a team-config change that gets committed, measured, and can be reverted.

First step: `git init` the workspace directory, commit the current state, and start tracking changes. Separation into agent repo + team repo can come later.

### Phase 7: Execution Memory Across Tasks

**Problem:** Each agent starts fresh. If task T502 failed because the API schema was wrong, task T503 (which depends on T502) has no way to know this.

**Solution:** A workspace-level execution memory:
- Key decisions and findings are stored per-task in a structured log.
- When dispatching a task that depends on a completed task, the executor includes the parent's summary in the prompt.
- The failure history for a task is always included in retry prompts.

Not a full vector-search memory system (that's overkill for now). Just structured JSON entries keyed by task ID, read at dispatch time by the prompt builder.

```json
{
  "T502": {
    "summary": "Added date filter component. Used DatePicker from existing UI library.",
    "files_modified": ["dashboard.jsx", "api/filters.py"],
    "decisions": ["Used existing DatePicker rather than building custom"],
    "warnings": ["The memory search endpoint returns max 100 results by default"]
  }
}
```

### Phase 8: Budget and Cost Awareness

**Problem:** Agents can run unbounded — burning tokens on dead ends, making hundreds of tool calls for a task that should take ten.

**Solution:** Soft budget limits per task:
- `maxDurationSeconds` — executor kills the agent process if it exceeds this.
- `maxToolCalls` — the prompt includes "you have N tool calls remaining" (advisory, not enforced by executor).
- Cost tracking per task — logged from the `openclaw agent --json` output.

Priority affects budget: `critical` tasks get longer timeouts than `low` tasks.

---

## Design Principles

1. **Agent, project, team, and origin are separate things.** The agent is who does the work. The project is what gets worked on. The team is how agents coordinate. The origin is where the agent came from. Mixing any of them causes problems.

2. **Focus on the next step.** Don't plan everything upfront. Decompose one level, execute, validate, then reassess. The world changes between steps.

3. **Git is truth.** If it's not committed, it didn't happen. Branches are cheap. Diffs are verifiable. Hashes are checksums. This applies to project repos, agent repos, and the team repo.

4. **Agents are ephemeral.** They wake up, do one thing, report, and die. No persistent state in the agent process — all state lives in repos and tasks.json. (Exception: Tier 3 agents maintain brain state within a goal's lifetime, but that state is checkpointed to disk.)

5. **The executor doesn't think.** It's a state machine: read tasks.json, apply dispatch rules, monitor children, react to exits. All intelligence lives in the agents and the prompt builder.

6. **Evidence over trust.** Don't trust the agent's self-report. Diff the branch. Run the tests. Score against criteria. The validator has tools — it can check.

7. **Fail fast, learn, retry.** Three blind retries is worse than one diagnosed retry. Spend the tokens on understanding the failure before spending them on another attempt.

8. **Self-improvement is gated.** An agent can work on its own codebase. The team can work on its own configuration. But adopting changes is always a separate, tracked, revertible step. Version N decides whether to become version N+1.

9. **Complexity is earned.** Not every agent needs a brain. Not every task needs decomposition. Start simple (Tier 1), add complexity only when the role demands it. Three similar lines of code is better than a premature abstraction.

10. **The team improves, not just agents.** Individual agent improvement is necessary but not sufficient. The team's coordination rules, dispatch policies, prompt templates, and roster are all things that can be measured and improved. The team is a versioned, improvable entity.

---

## Implementation Priority

Roughly ordered by value and feasibility:

| Phase | Effort | Impact | Dependency |
|-------|--------|--------|------------|
| 2: Branch isolation + project separation | Medium | High | None |
| 3: Structured validation | Medium | High | Phase 2 |
| 5: Failure analysis | Medium | High | Phase 3 |
| 6: Agent + team state under version control | Medium | High | None |
| 7: Execution memory | Medium | Medium | Phase 5 |
| 4a: Kanban hierarchy | Medium | High | Phase 3 |
| 4b: Brain loop (Tier 3) | Large | High | Phase 4a |
| 8: Budget/cost | Small | Medium | None |

Phase 2 solves the immediate commit contamination problem and introduces the agent/project separation. Phase 3 solves "did the agent actually do what was asked." Phase 6 makes the agent and team first-class versioned entities. Phase 4 splits into two: kanban hierarchy is simpler and comes first; the brain loop is the big one and comes last among the high-impact phases.

---

## Non-Goals (For Now)

- **Parallel strategy exploration.** Midpoint envisioned trying multiple approaches in parallel and picking the best. Too complex for now — sequential with good failure analysis is sufficient.
- **Full vector-search memory.** Midpoint had a persistent memory repo with embeddings. We'll start with structured per-task summaries and add embeddings later if needed.
- **Human-in-the-loop approval gates.** The `human` reviewer type exists but isn't wired to anything. We'll add Telegram notifications for human review later.
- **Dynamic tool selection.** Agents get a fixed tool set. Midpoint envisioned choosing tools based on the task. Not worth the complexity yet.
- **Infrastructure as state.** Terraform/Nix/Docker state as part of the agent's versioned state is the right long-term idea, but too much scope for now. Start with git repos for code and config, add infra state later.

---

## Open Questions

1. **Repo topology.** One repo for everything? Agent repo + team repo + memory repo? How many repos is the right number before it becomes overhead?

2. **Brain state format.** What does the orchestrator's internal goal tree look like on disk? JSON? A directory of files? How does it checkpoint and resume?

3. **Performance baselines.** What's the minimum viable performance tracking? Per-task duration and retry count? Or do we need validation scores from day one?

4. **Boundary crossing notifications.** When a kanban task created by boundary crossing completes, how does the brain get notified? Polling tasks.json? A callback mechanism? The brain agent is dead by then — it needs to be re-dispatched.

5. **Team repo vs agent repo.** Are they the same repo with different directories? Or truly separate? The team repo feels like it should be shared across all agents, while agent repos are per-agent.
