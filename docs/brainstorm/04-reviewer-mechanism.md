# Reviewer Mechanism Design

The hardest role to design. Who reviews the reviewer?

## The Product Angle

**Review-as-a-service is a business on its own.** Intelligence is cheap, models are cheap, review is hard. People desperately need a way of filtering PRs/contributions. A currency-based review system solves this.

### Permissionless PR Prediction Markets

- Anybody can create a "project"
- PRs are judged by the owner of the project
- Prediction markets (internal currency) open on each PR: "Will this get approved?"
- Can integrate directly into GitHub
- Prediction markets on PRs in OTHER repos too (external projects we don't control)

This works for external users immediately:
- **Project owners:** Signal on which PRs are worth reviewing first
- **Contributors:** Signal on whether your PR will land before investing more effort
- **Forecasters:** Earn currency by being right about code quality

### Automatic Market Makers

AMMs on the most important PRs on the most important repos. If we merely want to estimate P(PR approved | authority evaluates), it's straightforward binary markets.

Constructive criticism is a totally different problem — probably not needed for the market mechanism.

## The Escalation Game (Reality.eth Style)

For our own system where we need to minimize trusted authority's workload:

1. PR is submitted. Market opens.
2. Reviewer posts verdict + stakes bond: "Approve, 50 credits"
3. No challenge within window → verdict stands. Reviewer gets bond back + reward.
4. Someone disagrees → counter-bond (larger): "Reject, 100 credits"
5. Escalation continues — each challenge requires larger bond
6. At threshold → trusted authority evaluates. Winner gets all bonds. Loser loses everything.

**For external repos:** No escalation needed. The repo owner IS the oracle. Conditional markets: "IF maintainer reviews, will they approve?" Resolution is automatic on merge/close.

**For our own system:** Escalation game reduces authority's workload to only contested cases.

### Properties
- Most PRs never escalate (cheap)
- Authority only reviews contested cases (efficient allocation of scarce attention)
- Bond escalation makes frivolous challenges expensive
- Higher stakes → higher quality review

### Open Design Questions
- Right bond curve? (Reality.eth doubles each level — is that right here?)
- What triggers authority evaluation? Max bond? Number of rounds?
- What's the right initial bond size?

## Autonomy Levels

| Level | Description | Risk | Use Case |
|-------|-------------|------|----------|
| 1: Signal only | Market shows probability. Human merges. | Zero | External projects |
| 2: Auto-approve (high confidence) | 95%+ market, no challenges, min bond met → auto-merge. Human can override. | Low | Trusted external projects |
| 3: Full autonomy | Market decides. No human in loop. | High | **Our own repo** |

**Key decision:** We run Level 3 on our own codebase. Eat our own dogfood at the hardest setting.

"We run Level 3 on our own repo. Here's the track record. You can start at Level 1."

## Near-Term: Trusted Authority Model

- Human or trusted system is ultimate reviewer ("supreme court")
- Agent reviewers distill the trusted authority
- Traders bet on review outcomes
- Random fraction audited by authority
- Reviewer accuracy measured against authority verdicts
- Financial engineering makes honest reviewing the dominant strategy

## Long-Term: Decentralized Review

Much harder. Possibilities:
- Multiple independent reviewers, agreement/disagreement as signal
- Downstream production performance as review (long feedback loop)
- On-chain resolution via token-weighted governance

*Last updated: 2026-02-23*
