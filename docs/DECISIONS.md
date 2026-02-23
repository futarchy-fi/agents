# Architecture Decisions Log

Decisions made during the vision brainstorming phase. These are locked in.

---

## D1: Repo Topology — Monorepo

**Decision:** Single monorepo for the project. Agent state, team config, core engine, and tools all live in one repo with directory separation.

**Rationale:** Multi-repo coordination overhead isn't justified yet. Directory conventions provide conceptual separation. Can split later via `git filter-branch` if agent memory commits drown out team config changes.

**Structure:**
```
project/
├── core/           ← carefully reviewed, tested, minimal
│   ├── executor.mjs
│   ├── state.mjs
│   ├── brain/
│   ├── validation/
│   └── prompts/
├── agents/         ← agent identities, configs, memory
│   ├── hermes/
│   ├── orchestrator/
│   └── coder/
├── team/           ← roster, dispatch rules, performance history
├── tools/          ← CLI scripts, dashboards, integrations (vibecoded)
└── docs/           ← vision, architecture docs
```

---

## D2: Brain State Format — Single JSON File

**Decision:** One JSON file per active goal, stored in the agent's brain directory (e.g., `agents/orchestrator/brain/T502.json`). Contains the subgoal tree, attempt history, current focus.

**Rationale:** Goal trees won't be deep (max depth 3). Single file is easier to checkpoint atomically. Move to directory-per-goal if files get too large.

---

## D3: Performance Tracking — Start Simple

**Decision:** Track what we get for free from existing data:
- Per-task duration (dispatch timestamp → completion timestamp)
- Retry count
- Exit code
- Token/cost (parsed from `openclaw agent --json` output)

**Derived metrics:** success rate, average retries, cost per task, time per task — per agent and per team.

**Long-term:** Quality tracking via validation scores (requires Phase 3: structured validation). This is the important part.

---

## D4: Boundary Crossing Notifications — Re-dispatch

**Decision:** When a kanban task created by boundary crossing (T505) completes, the executor re-dispatches the parent orchestrator with context: "T505 completed, resume your brain state."

**Mechanism:** Executor checks if completed task has `parentTask`. If parent is still in_progress and its agent isn't running, re-dispatch with resume context. Orchestrator reads brain checkpoint file and continues.

**Rationale:** Preserves agent ephemerality. One extra dispatch is cheap compared to keeping orchestrator alive for hours polling.

---

## D5: Core vs Outside Separation

**Decision:** The codebase is split into CORE (carefully reviewed, correctness-critical) and OUTSIDE (vibecoded, usability-focused).

**CORE** — bugs here cause silent corruption, lost work, or wrong decisions:
- Task state machine (transitions, atomicity, no stuck tasks)
- Executor dispatch (concurrency, retry, revert, process monitoring)
- Brain loop (goal decomposition, OODA, checkpointing, crash recovery)
- Boundary crossing (brain → kanban, dependency tracking, re-dispatch)
- Validation engine (criteria checking, scoring, accept/reject)
- State versioning (agent commits, team commits, origin tracking, revert)
- Prompt builder (the nervous system — bad prompt = agent does wrong thing)

**OUTSIDE** — important but correctness isn't existential:
- Dashboards, UI, visualizations
- CLI formatting, colors, pretty output
- Telegram/Slack notifications
- Log formatting and storage
- Documentation site
- Setup/install scripts
- Task list display and filtering

---

## D6: Project Scope

**Decision:** This is its own project, not an OpenClaw fork. It may use OpenClaw as the agent runtime (origin), but the team coordination, brain loop, validation, and self-improvement infrastructure is a separate codebase.

**Open:** Project name and domain. See naming discussion.
