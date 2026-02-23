"""
LMSR (Logarithmic Market Scoring Rule) — pure math, no state.

All functions take Decimal inputs and return Decimal outputs.
The caller (market engine) handles state, rounding, and persistence.

Notation:
    q: dict mapping outcome -> quantity sold (e.g. {"yes": Decimal, "no": Decimal})
    b: Decimal, liquidity parameter (higher = deeper book, max loss = b * ln(n))
"""

from decimal import Decimal
import math


ZERO = Decimal("0")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _normalize(q: dict[str, Decimal]) -> dict[str, Decimal]:
    """Subtract min(q) from all values. Preserves prices, avoids overflow."""
    m = min(q.values())
    if m == ZERO:
        return q
    return {k: v - m for k, v in q.items()}


def _exp_sum(q: dict[str, Decimal], b: Decimal) -> Decimal:
    """Σ e^(q_i / b) with normalization for stability."""
    qn = _normalize(q)
    return sum(Decimal(str(math.exp(float(v / b)))) for v in qn.values())


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------

def cost(q: dict[str, Decimal], b: Decimal) -> Decimal:
    """
    Cost function: C(q) = b * ln(Σ e^(q_i / b))

    This is the total amount collected by the market maker so far
    (relative to the initial state). Not useful on its own — trading
    costs are always C(after) - C(before).
    """
    return b * Decimal(str(math.log(float(_exp_sum(q, b)))))


def prices(q: dict[str, Decimal], b: Decimal) -> dict[str, Decimal]:
    """
    Current prices (probabilities) for each outcome.

    p_i = e^(q_i / b) / Σ e^(q_j / b)

    Always sum to 1. This is softmax over q/b.
    """
    qn = _normalize(q)
    exp_vals = {k: Decimal(str(math.exp(float(v / b)))) for k, v in qn.items()}
    total = sum(exp_vals.values())
    return {k: v / total for k, v in exp_vals.items()}


def cost_to_buy(q: dict[str, Decimal], b: Decimal,
                outcome: str, amount: Decimal) -> Decimal:
    """
    Credits required to buy `amount` tokens of `outcome`.

    cost = C(q_after) - C(q_before)

    Returns positive Decimal (cost to the buyer).
    For selling, pass negative amount — returns negative (credit back).
    """
    q_after = dict(q)
    q_after[outcome] = q_after[outcome] + amount
    return cost(q_after, b) - cost(q, b)


def amount_for_cost(q: dict[str, Decimal], b: Decimal,
                    outcome: str, budget: Decimal) -> Decimal:
    """
    Inverse of cost_to_buy. Given a credit budget, how many tokens
    can you buy?

    amount = b * ln(S * (e^(budget/b) - 1) / e_o + 1)

    where S = Σ e^(q_i/b) and e_o = e^(q_outcome/b).

    Positive budget → tokens you can buy.
    Negative budget → tokens you must sell to receive that many credits.
    """
    qn = _normalize(q)
    S = sum(Decimal(str(math.exp(float(v / b)))) for v in qn.values())
    e_o = Decimal(str(math.exp(float(qn[outcome] / b))))
    inner = S * (Decimal(str(math.exp(float(budget / b)))) - 1) / e_o + 1
    return b * Decimal(str(math.log(float(inner))))


def cost_to_move_price(q: dict[str, Decimal], b: Decimal,
                       outcome: str, target_price: Decimal) -> tuple[Decimal, Decimal]:
    """
    How many tokens to buy/sell to move outcome's price to target_price.

    Returns (amount, cost) where:
        amount: tokens to buy (positive) or sell (negative)
        cost: credits to pay (positive) or receive (negative)

    Derivation: target = e^(q_new/b) / Σ e^(q_j/b) with only q[outcome] changing.
    Solve for q_new, then amount = q_new - q_old.
    """
    qn = _normalize(q)
    others_sum = sum(
        Decimal(str(math.exp(float(v / b))))
        for k, v in qn.items() if k != outcome
    )
    # target = e^(q_new/b) / (e^(q_new/b) + others_sum)
    # => e^(q_new/b) = target * others_sum / (1 - target)
    # => q_new = b * ln(target * others_sum / (1 - target))
    ratio = target_price * others_sum / (1 - target_price)
    q_new_normalized = b * Decimal(str(math.log(float(ratio))))

    # Convert back from normalized space
    m = min(q.values())
    q_new = q_new_normalized + m
    amount = q_new - q[outcome]
    trade_cost = cost_to_buy(q, b, outcome, amount)
    return amount, trade_cost


# ---------------------------------------------------------------------------
# Liquidity
# ---------------------------------------------------------------------------

def liquidity_cost(q: dict[str, Decimal], b: Decimal,
                   new_b: Decimal) -> tuple[dict[str, Decimal], Decimal]:
    """
    Cost to change liquidity parameter from b to new_b.

    Rescales q to preserve current prices:
        q'_i = q_i * (new_b / b)

    Returns (new_q, funding) where:
        funding > 0 means AMM needs more credits (adding liquidity)
        funding < 0 means AMM gets credits back (removing liquidity)

    funding = (new_b - b) * ln(Σ e^(q_i / b))
    """
    ratio = new_b / b
    new_q = {k: v * ratio for k, v in q.items()}
    funding = (new_b - b) * Decimal(str(math.log(float(_exp_sum(q, b)))))
    return new_q, funding


def b_for_funding(q: dict[str, Decimal], b: Decimal,
                  funding: Decimal) -> tuple[Decimal, dict[str, Decimal]]:
    """
    Inverse of liquidity_cost. Given additional funding, what's the new b?

    new_b = b + funding / ln(Σ e^(q_i / b))

    Returns (new_b, new_q) with q rescaled to preserve prices.
    Positive funding → increase liquidity. Negative → decrease.
    """
    log_S = Decimal(str(math.log(float(_exp_sum(q, b)))))
    new_b = b + funding / log_S
    ratio = new_b / b
    new_q = {k: v * ratio for k, v in q.items()}
    return new_b, new_q


def max_loss(b: Decimal, n: int) -> Decimal:
    """Maximum market maker loss: b * ln(n). The required initial funding."""
    return b * Decimal(str(math.log(n)))
