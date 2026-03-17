# Trust Layer for AI Agents

*Brainstorm: 2026-03-17.*

## The Problem

The AI agent ecosystem is exploding — ClawHub skills, MCP servers, autonomous coding agents, review bots. The #1 problem: **how do you trust a third-party agent or skill?**

Today's answers are weak:
- Verified publisher badges (centralized, doesn't catch post-publish changes)
- Download counts (popularity ≠ safety)
- Manual audits (don't scale, quickly outdated)
- User reviews (subjective, gameable)

## The Prediction Market Solution

Instead of trusting a badge or a review, check what the market thinks.

**"Will skill X have a reported vulnerability within 90 days?"**

If the market trades at 5% → high confidence it's safe.
If the market trades at 40% → something is suspicious, stay away.

The market aggregates information from:
- Security researchers who audited the code
- Agents that tested the skill in sandboxes
- Developers who used it and noticed issues
- Automated static analysis tools

No single source has the full picture. The market does.

## Why This Creates a Bug Bounty

An agent that finds a vulnerability can:
1. Buy YES on "will this skill have a vulnerability?"
2. Report the vulnerability
3. Market resolves YES → agent profits

The market IS the bug bounty. No bounty program needed. The payout is the market spread. Higher-profile skills attract more liquidity → bigger bounties for finding bugs.

## Market Types for Trust

**Skill safety**
- "Will skill X have a reported vulnerability within 90 days?"
- Resolution: security advisory submission + verification process

**Skill reliability**
- "Will skill X have >99% uptime this month?"
- Resolution: monitoring data (automated)

**Skill adoption**
- "Will skill X reach >1000 installs within 60 days?"
- Resolution: registry stats (automated)

**Agent trustworthiness**
- "Will agent X complete its next 10 tasks without a failure?"
- Resolution: task outcome data (automated via taskcore or similar)

**Model quality**
- "Will model X score above Y on benchmark Z?"
- Resolution: benchmark results (automated, public)

## The Trust Score

An agent or skill's trust score is derived from its market portfolio:
- Safety market trading at 95% safe
- Reliability market at 99%+ uptime
- 50 resolved markets with no incidents

This composite score is the **prediction-market-derived trust rating**. It's:
- Dynamic (updates in real time as new info emerges)
- Incentive-aligned (people who know things are rewarded for sharing)
- Resistant to gaming (manipulating the market costs real money)
- Decentralized (no central authority decides who's trusted)

## Connection to the Vision

This is the "decentralized social credit score" from the political vision document — but for AIs first.

Movements have status scores. AIs have trust scores. Both are:
- Multi-dimensional (safety, reliability, effectiveness, reputation)
- Maintained by prediction markets
- Used to decide who gets access, opportunities, resources

Starting with AI trust is strategic because:
- Resolution is objective (code either has a vulnerability or it doesn't)
- Participants are agents (faster iteration than humans)
- The ecosystem needs it NOW (trust is the bottleneck for AI adoption)
- It proves the mechanism before applying it to human coordination

## Competitive Landscape

- **Socket.dev** — dependency security scanning (static analysis, not market-based)
- **Snyk** — vulnerability database (centralized, reactive)
- **ClawHub verification** — publisher badges (centralized trust)
- **MCP registries** — listing services (no trust signal beyond "it's listed")

None of these use market mechanisms. The market approach is complementary — it aggregates the signals that static tools miss (social engineering, supply chain attacks, subtle backdoors that pass automated checks).

## Open Questions

- Who submits vulnerability reports? Open to anyone, or credentialed researchers?
- How to prevent false reports (griefing)? Require a bond? Escalation game?
- How to bootstrap liquidity on skill markets? (Most skills are obscure)
- Can the trust score be composable across registries?
- Should trust markets be free (public good) or paid (business model)?
