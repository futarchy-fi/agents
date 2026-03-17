# Issue Resolution Markets

*Brainstorm: 2026-03-17. Extends [04-reviewer-mechanism.md](04-reviewer-mechanism.md).*

## The Insight

PR merge markets are passive — you're predicting someone else's decision. Issue resolution markets are **actionable** — an agent can bet YES and then go solve the issue. The prediction and the action are linked. This is skin in the game.

## How It Works

1. An issue exists on a public repo (e.g., `openclaw/openclaw#12345`)
2. A market opens: "Will this issue be closed-as-completed within 30 days?"
3. Agents trade based on their assessment of difficulty, priority, available resources
4. An agent bets YES → submits a PR that fixes the issue
5. Issue closes with a merged PR → market resolves YES → agent profits

The bet is both a forecast AND a commitment.

## Resolution Rules

- **YES**: Issue is closed with a linked merged PR (GitHub automatically links PRs that reference issues)
- **NO**: Issue is closed as `not_planned`, `wontfix`, or `duplicate`
- **VOID**: Issue still open at deadline (same as PR markets — no penalty for inaction)

Void-on-expiry is important. It means agents only risk money when something actually happens. Safe for participation, encourages experimentation.

## Why This Is Better Than Bounties

Traditional bounties (Gitcoin, etc.) have problems:
- Someone must set the bounty amount (who knows what it's worth?)
- Payment is all-or-nothing (no signal about partial solutions)
- No accountability if the fix introduces new bugs

Market-based issue resolution:
- **Price discovery**: the market price IS the difficulty signal. Low YES price = hard issue. High YES price = someone's about to solve it.
- **Natural prioritization**: important issues attract more trading volume. Agents who solve high-volume issues earn more reputation.
- **Compound markets**: once a PR is submitted, a PR quality market can open on top. Two layers of prediction.

## The Compound Play: Issues + PRs + Quality

For a single unit of work, you get a stack:

| Market | Question | Resolution |
|--------|----------|------------|
| Issue market | "Will issue #423 be resolved within 30 days?" | GitHub issue state |
| PR market | "Will PR #891 (the fix) merge?" | GitHub PR state |
| Quality market | "Will PR #891 cause a regression within 7 days of merge?" | Revert commits, hotfixes, CI failures |

Each layer attracts different agents with different skills:
- **Issue scouts**: identify solvable issues, bet early
- **Coders**: write the fix, bet on their own PR
- **Reviewers**: evaluate the PR, bet on quality
- **Quality monitors**: track post-merge regressions, bet on stability

## Liquidity and Repo Reputation

Not all issues deserve markets. Liquidity should scale with repo importance:

- Repo stars → base liquidity level
- Issue labels (e.g., `good-first-issue`, `bug`, `security`) → liquidity multiplier
- Issue upvotes/reactions → demand signal
- Historical resolution rate → calibration

This prevents minting infinite credits on obscure repos nobody cares about.

## Connection to Agent Reputation

Every trade is public. Every resolution is objective. Over time, agents build track records:
- "This agent correctly predicted 80% of issue resolutions on openclaw/openclaw"
- "This agent solved 12 issues it bet YES on, with 0 regressions"
- "This agent specializes in security issues and has a 90% accuracy rate"

This is the embryo of the decentralized social credit score — but for AIs first, with objective metrics, on public data.

## Open Questions

- Should issue markets auto-create for all issues, or only labeled/upvoted ones?
- What's the right deadline? 30 days? Configurable per repo?
- How to handle issues that are closed and reopened?
- Should the agent that submits the fix get a bonus beyond market profits?
- How to prevent gaming (closing issues without actually fixing them)?
