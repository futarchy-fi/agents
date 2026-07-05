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

def _raw_payouts(b: float, p: float, q: float) -> tuple[float, float]:
    """Raw (unrounded) settlement amounts for won=True and won=False.

    Both stake_for_edit and payout_for_edit must derive from these exact same
    float quantities: computing "the same" log ratio via different but
    mathematically-equivalent expressions (e.g. log(p/q) vs -log(q/p)) can
    differ by an ULP, and after scaling by a large b and rounding to 1e-6 that
    ULP can cross a rounding tick, making stake < -payout. Sharing one raw
    computation eliminates that drift.
    """
    raw_won = b * math.log(q / p)
    raw_lost = b * math.log((1 - q) / (1 - p))
    return raw_won, raw_lost

def stake_for_edit(b: Decimal, p: float, q: float) -> Decimal:
    _validate(p, q)
    raw_won, raw_lost = _raw_payouts(float(b), p, q)
    worst = max(-raw_won, -raw_lost, 0.0)
    return _round_up(worst)

def payout_for_edit(b: Decimal, p: float, q: float, won: bool) -> Decimal:
    _validate(p, q)
    raw_won, raw_lost = _raw_payouts(float(b), p, q)
    raw = raw_won if won else raw_lost
    return _round_down(raw) if raw >= 0 else -_round_up(-raw)
