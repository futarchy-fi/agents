"""Log-MSR staking math for probability-edit bets.

An edit moves P(outcome) from p to q with liquidity b. Settlement on
resolution: b*ln(q/p) if the edited outcome occurred, b*ln((1-q)/(1-p))
otherwise. The stake frozen at order time is the worst case of the two.
"""
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import math

PLACES = Decimal("0.000001")

def _round_up(x: float) -> Decimal:
    return Decimal(str(x)).quantize(PLACES, rounding=ROUND_CEILING)

def _round_down(x: float) -> Decimal:
    return Decimal(str(x)).quantize(PLACES, rounding=ROUND_FLOOR)

def _validate(p: float, q: float) -> None:
    if not (0.0 < p < 1.0 and 0.0 < q < 1.0):
        raise ValueError("probabilities must be strictly inside (0, 1)")

def stake_for_edit(b: Decimal, p: float, q: float) -> Decimal:
    _validate(p, q)
    worst = max(math.log(p / q), math.log((1 - p) / (1 - q)), 0.0)
    return _round_up(float(b) * worst)

def payout_for_edit(b: Decimal, p: float, q: float, won: bool) -> Decimal:
    _validate(p, q)
    raw = float(b) * (math.log(q / p) if won else math.log((1 - q) / (1 - p)))
    return _round_down(raw) if raw >= 0 else -_round_up(-raw)
