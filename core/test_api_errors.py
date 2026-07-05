"""Direct unit tests for venue-error -> APIError translation.

The individual mappings are also exercised via the integration tests in
test_api_net.py, but InsufficientTreasury can't be triggered through the
public API (there's no treasury-drain endpoint), so its 409 mapping is
locked here at the boundary function instead.
"""
from core.api_errors import translate_venue_error
from venues.joint.venue import (
    InsufficientCredits,
    InsufficientTreasury,
    TradeRejected,
    WidthBudgetExceeded,
)


def test_insufficient_treasury_maps_to_409():
    err = translate_venue_error(InsufficientTreasury("treasury dry"))
    assert err.status == 409
    assert err.code == "insufficient_treasury"
    assert "treasury dry" in err.message


def test_known_mappings_are_not_swallowed_by_treasury_branch():
    # Guard against ordering bugs in the isinstance chain.
    assert translate_venue_error(InsufficientCredits("x")).code == "insufficient_credits"
    assert translate_venue_error(WidthBudgetExceeded("x")).code == "width_budget"


def test_unmapped_venue_error_falls_through_to_trade_rejected():
    err = translate_venue_error(TradeRejected("nope"))
    assert err.status == 400
    assert err.code == "trade_rejected"
