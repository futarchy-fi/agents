# Futarchy Nonprofits (FNPs)

*Brainstorm: 2026-06-08. A futarchic mechanism for credibly-neutral, impact-directed charitable funding. Related: [docs/FUTARCHIC_ORGANIZATIONS.md](../FUTARCHIC_ORGANIZATIONS.md), [07-counterfactual-eval.md](07-counterfactual-eval.md).*

## Nonprofits Today

Nonprofits as they exist today are a complete mess:

- They often have very unclear goals.
- When they do have objective metrics they are seeking to improve, it is often the case that these metrics are not evaluated after the fact.
- Even if metrics are evaluated, it is often not possible to do proper credit-assignment to tell whether any improvement is actually due to the nonprofit's work.
- Even when you can tell a nonprofit is responsible for an improvement, you often can't tell how much difference each marginal dollar made.
- Even if you could tell how effective a charity was after the fact, we don't have a credibly neutral mechanism to estimate this *before* the fact.

There are some proposals to fix some of these problems:

- **Retroactive Public Goods Funding** — aims to evaluate projects after the fact, improving credit-assignment.
- **Impact Certificates** — allow funding charities before the fact, by transferring future expected rewards to speculators.
- **Quadratic Funding** — a proposal to fund or distribute resources in a more credibly-neutral manner.

None of these, however, allow credibly-neutral funding to be directed towards an objective metric, and none of them (by themselves) solve the problem of evaluating the impact of a *marginal dollar* on some objective. We therefore introduce a new framework for market estimates of impact.

## Futarchy Nonprofits

The first three steps towards establishing a Futarchy Nonprofit are:

1. **Define an objective measure** that we are trying to maximize. You need to define an organization or contract responsible for measuring and evaluating the outcome at the designated time or condition (called the **oracle**).

2. **Choose the settlement asset**, and create/designate a derivative market over the objective measure that uses this asset for collateral and settlement. It should ideally be yield-bearing (the kind you'd want to hold in a long-term portfolio). This also requires initial funds to a liquidity pool for this derivative market (over the range of the objective measure).

3. **Choose a funding asset**, where donations will be made. It can be the same as the settlement asset.

Funding for the nonprofit can be given in different ways:

- **Unconditional funding.** This funding can be used by any proposal that increases the objective measure.
- **Marginal funding.** This is revocable funding that can only be used in "efficient" proposals. Contributing to marginal funding is like placing a limit order: you define how much of the funding asset to give, but also what is analogous to the limit price — the minimal ratio of "impact" to "funding".

Furthermore, the nonprofit can be endowed with control over other assets or decision-power. This power can be wielded by the winning proposal, through the mechanism described below.

## The Cycle

The futarchy runs based on cycles of funding, proposal submissions, and proposal evaluations.

1. **Funding.** During this stage donors submit "orders" for unconditional funding, or marginal funding, as described above. These funds will be potentially available for the proposals.

2. **Proposal submission.** Third parties are now allowed to submit proposals of any kind, designed to improve the objective measure. Proposals can include making decisions under futarchy control, as well as "using" funds from the funding orders available. In this design, a maximum of one proposal is accepted per cycle.

3. **Proposal evaluation.** This is the crucial step, and is expanded upon below.

## Proposal Evaluation

### Conditional market setup

In the proposal evaluation step, we want to obtain a market estimate of what the expected value will be for the objective measure, conditional on which proposal is accepted, as well as conditional on no proposal being accepted for this cycle (called the **null-proposal**).

To do so, we need to have conditional markets for each of the proposals (including the null-proposal).

We already have some funds (denominated in the settlement asset) in a liquidity pool, providing liquidity to the derivative market. What we have to do is take the funds in this liquidity pool and split them into conditional tokens.

For example, let us imagine that USDC is the settlement asset (in practice we probably want a yield-bearing asset). If we have N different proposals, plus the 0-th null proposal, we can split this into a list of conditional tokens: USDC-0, USDC-1, …, USDC-N.

Each of these conditional tokens represents ownership over a single unit of the settlement asset (USDC) if the proposal is selected. So the spot market for each of these tokens is a prediction market for the probability of each of the proposals being selected (or no proposal, for the USDC-0 token).

Now, interestingly — just as you can use USDC as the settlement asset in a derivative to estimate the expected value of the objective measure — if you create a derivatives market using USDC-i as the settlement asset, this will estimate the expected value of the objective measure *conditional on proposal "i" being accepted*.

Therefore, by taking the funds in the liquidity pool and splitting them into conditional tokens, N+1 liquidity pools can be created, allowing the establishment of N+1 derivatives markets, each estimating the potential impact of a proposal on the objective metric.

### Trading period

As soon as the markets are established, the trading period begins. Initially the conditional metric (the "score") for each of the proposals will be equal to the expected (unconditional) value of the objective metric, but this will soon fluctuate as traders start to place conditional bets on each of the conditional derivatives markets.

For this implementation, we suggest 2 different two-day periods.

For each period, we'll be calculating the **estimated impact** of each proposal "i" as the expected value of the objective metric conditional on proposal "i" being accepted, minus the expected value of the objective metric conditional on no proposal being accepted (the null proposal).

The **conservative estimate** for each proposal will be the minimum impact estimated across the trading periods. Therefore, proposals can already be eliminated in the first period if they are already worse than the null proposal, not making it to the "second round".

### Funding evaluation

Get all the "limit orders" for the donations and turn this into a **demand-for-impact curve**.

Now take all the proposals, alongside their estimated impact and cost, get all that are on the Pareto-efficiency frontier, and convert this into a **supply curve**. Find the intersection of the curves.

Now that we have the conservative impact estimate (the score) for each of the proposals, we can do the funding evaluation — a proposal will be approved on this step if the funding assets required by the proposal are less than the funding available from donors (given the estimated impact).

The **impact efficiency** of the proposal is the estimated impact divided by the funding required.

**Funding available** is defined as: all unconditional funding + all funding orders with a "limit price" higher than or equal to the impact efficiency of the proposal.

If the funding available is higher than or equal to the funding required, the proposal moves forward to final evaluation.

### Final evaluation

In the final evaluation, proposals are ranked by total estimated impact, using the conservative estimate. The proposal with the most impact on the objective metric is selected and implemented, if it is above a minimum threshold impact.

To ensure accurate market estimates, in some percentage of cycles (say ~5%) the null proposal is selected regardless of impact.

### Distribution of marginal impact certificates

Impact certificates can be distributed to the donors, the total being proportional to the estimated impact of the proposal.

## Open Questions

- **Auctioning off the N slots** (if there is an N-maximum-proposals constraint) — how does the auction interact with the funding evaluation?
- **Paying the winning proposal** — what exactly does the winner receive, and how is the payment structured so that it remains incentive-compatible with the conservative impact estimate? *(Think about it.)*
- Choice of two-day-period structure vs. other schedules; manipulation resistance of the min-across-periods conservative estimate.
- Sybil / collusion resistance on the marginal-funding limit orders.
