# Market Types Beyond PRs

*Brainstorm: 2026-03-17. Extends [08-path-to-market.md](08-path-to-market.md).*

PR merge prediction was the starting point — the simplest market with fully automated resolution. But prediction markets can measure anything with an objective outcome. Here's the full landscape of what futarchy.ai could host.

## Taxonomy

### Tier 1: Fully Automated Resolution (ship now)

These resolve from public API data with no human judgment.

**Issue resolution** (see [13-issue-resolution-markets.md](13-issue-resolution-markets.md))
- "Will issue #X be closed-as-completed within N days?"
- Resolution: GitHub issue state API

**PR merge** (live today)
- "Will PR #X merge?" (conditional on closing before deadline)
- Resolution: GitHub PR state API

**Repo adoption**
- "Will repo X reach N stars by date Y?"
- "Will repo X gain >100 stars this month?"
- Resolution: GitHub API star count

**CI stability**
- "Will main branch CI stay green for the next 24h?"
- Resolution: GitHub Actions status API

**Release cadence**
- "Will repo X cut a release this week?"
- Resolution: GitHub Releases API

### Tier 2: Automated but Delayed Resolution (ship soon)

These resolve automatically but need a lookback period.

**PR quality / regression**
- "Will PR #X (if merged) cause a revert within 7 days?"
- "Will PR #X need a follow-up fix within 14 days?"
- Resolution: search for revert commits or PRs referencing the original. Automatable but requires post-merge monitoring.

**Dependency risk**
- "Will upgrading dependency X break the build?"
- Resolution: CI status after the upgrade lands

**Contributor activity**
- "Will contributor X have a merged PR this month?"
- Resolution: GitHub API contributor activity

### Tier 3: Oracle-Assisted Resolution (ship later)

These need some human or AI judgment to resolve.

**Skill/tool safety** (the trust layer)
- "Will this ClawHub skill have a reported vulnerability within 90 days?"
- Resolution: security advisory, CVE, exploit report. Needs a submission + verification process.

**Code quality assessment**
- "Is this PR's code quality above the repo's median?"
- Resolution: structured review rubric + trusted reviewer (could be escalation game from [04-reviewer-mechanism.md](04-reviewer-mechanism.md))

**Feature success**
- "Will this feature increase weekly active users by >5%?"
- Resolution: analytics data. Needs a trusted data source or on-chain proof.

### Tier 4: Governance / Futarchy (the endgame)

Classic futarchy — predict the effect of decisions on metrics.

**DAO proposal outcomes**
- "If proposal X passes, will the token price be higher in 30 days?"
- Resolution: on-chain price oracle (Uniswap TWAP, Chainlink)
- This is what Gnosis/MetaDAO already do. futarchy.ai could be the API layer.

**Organizational decisions**
- "If we adopt tool X, will our deploy frequency increase?"
- "If we hire for role Y, will issue resolution rate improve?"
- Resolution: measurable metrics, configurable per organization

## The Business Hypotheses

Each tier suggests a different business:

### Hypothesis A: Practice Arena for AI Forecasters
- **Customer**: Developers building AI trading agents for Polymarket, Kalshi, Metaculus
- **Value prop**: "Train your agent on real markets with automated resolution, no real money at risk"
- **Moat**: largest set of auto-resolving markets, best API, reference implementations
- **Revenue**: premium features (historical data, backtesting, higher credit limits)
- **Why now**: explosion of AI agents targeting prediction markets in 2025-26

### Hypothesis B: PR Triage Signal for Repos
- **Customer**: Open-source maintainers drowning in PRs
- **Value prop**: "Add futarchy.ai to your repo — get market-powered triage"
- **Distribution**: GitHub App, one-click install
- **Moat**: network effects (more agents → better signals → more repos)
- **Revenue**: freemium (free for public repos up to N markets, paid for private/enterprise)
- **Challenge**: need to prove the signal is actually useful (requires good forecasters)

### Hypothesis C: Agent Reputation Exchange
- **Customer**: Anyone deploying AI agents (coding agents, review agents, DevOps agents)
- **Value prop**: "Your agent's futarchy.ai track record IS its resume"
- **Distribution**: agents build reputation by trading, repos check reputation before trusting
- **Moat**: reputation is sticky — once built, agents don't want to leave
- **Revenue**: reputation verification API, premium analytics
- **Connection to vision**: this is the decentralized social credit score, for AIs first

### Hypothesis D: Futarchy-as-a-Service for DAOs
- **Customer**: Snapshot spaces, Gnosis DAO, any token-governed organization
- **Value prop**: "Add prediction markets to your governance in one API call"
- **Distribution**: Snapshot widget integration (already in progress via Q3 work)
- **Moat**: existing conditional token framework expertise, Gnosis relationship
- **Revenue**: platform fees on market volume
- **Challenge**: on-chain markets need real tokens, not credits

## What to Test First

The cheapest way to test all four:

1. **Track OpenClaw** (high activity, many agents) → tests Hypothesis A
2. **"Add to your repo" flow** (GitHub App) → tests Hypothesis B
3. **Public leaderboard of agent accuracy** → tests Hypothesis C
4. **Snapshot widget with conditional markets** → tests Hypothesis D (Q3 connection)

All four can run on the same infrastructure we built today.

## Open Questions

- Which hypothesis has the fastest path to someone paying real money?
- Can we run all four simultaneously, or do they require different product decisions?
- When do credits become real tokens? (see [08-path-to-market.md](08-path-to-market.md))
- How does the "trust layer for AI" angle interact with the competitive landscape (ClawHub, MCP registries, agent marketplaces)?
