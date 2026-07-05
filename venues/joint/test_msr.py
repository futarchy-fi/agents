from decimal import Decimal
import math
from venues.joint.msr import stake_for_edit, payout_for_edit

B = Decimal("50")

def test_stake_is_worst_case_loss():
    # moving 0.5 -> 0.9: worst case is outcome NO: 50*ln(0.5/0.1)
    s = stake_for_edit(B, 0.5, 0.9)
    assert abs(float(s) - 50 * math.log(0.5 / 0.1)) < 1e-4
    # stake covers the worst payout exactly (within rounding, stake >= |loss|)
    lose = payout_for_edit(B, 0.5, 0.9, won=False)
    assert lose < 0 and s >= -lose

def test_no_edit_no_stake():
    assert stake_for_edit(B, 0.42, 0.42) == Decimal("0")

def test_downward_edit_worst_case_is_yes():
    s = stake_for_edit(B, 0.9, 0.4)
    assert abs(float(s) - 50 * math.log(0.9 / 0.4)) < 1e-4

def test_payout_signs():
    assert payout_for_edit(B, 0.3, 0.7, won=True) > 0
    assert payout_for_edit(B, 0.3, 0.7, won=False) < 0

def test_telescoping_two_traders():
    # A: 0.5->0.7, B: 0.7->0.9; outcome YES.
    # Total payout = 50*ln(0.9/0.5) within rounding.
    total = payout_for_edit(B, 0.5, 0.7, True) + payout_for_edit(B, 0.7, 0.9, True)
    assert abs(float(total) - 50 * math.log(0.9 / 0.5)) < 1e-3
