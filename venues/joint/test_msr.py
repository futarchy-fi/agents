from decimal import Decimal
import math
import random
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


def test_stake_covers_worst_payout_regression():
    # Counterexample found by property testing on this platform: at b ~ 1e5-1e7,
    # stake_for_edit and payout_for_edit compute the same mathematical quantity
    # via independent math.log expressions (log(p/q) vs log(q/p), etc). Their
    # float results can differ by one ULP, which after scaling by b and rounding
    # crosses a 1e-6 tick, giving stake < -payout for the won=True branch.
    b = Decimal("8655174.00088")
    p = 0.6228557863186213
    q = 0.10524297800372509
    s = stake_for_edit(b, p, q)
    for won in (False, True):
        payout = payout_for_edit(b, p, q, won)
        assert s >= -payout, f"won={won}: stake={s} payout={payout}"


def test_stake_covers_worst_payout_property():
    rng = random.Random(20260705)
    for _ in range(20_000):
        p = rng.uniform(0.001, 0.999)
        q = rng.uniform(0.001, 0.999)
        b = Decimal(str(round(10 ** rng.uniform(0, 7), 6)))
        s = stake_for_edit(b, p, q)
        for won in (False, True):
            payout = payout_for_edit(b, p, q, won)
            assert s >= -payout, f"p={p} q={q} b={b} won={won}: stake={s} payout={payout}"


def test_validate_rejects_boundary_probabilities():
    import pytest
    with pytest.raises(ValueError):
        stake_for_edit(B, 0.0, 0.5)
    with pytest.raises(ValueError):
        stake_for_edit(B, 0.5, 1.0)
    with pytest.raises(ValueError):
        stake_for_edit(B, 1.0, 0.5)


def test_payout_zero_when_no_edit():
    assert payout_for_edit(B, 0.42, 0.42, won=True) == Decimal("0")
    assert payout_for_edit(B, 0.42, 0.42, won=False) == Decimal("0")
