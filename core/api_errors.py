"""
API error handling. Structured JSON errors with codes.

Every error response: {"error": {"code": "...", "message": "...", "details": {...}}}
"""

from fastapi import Request
from fastapi.responses import JSONResponse

from core.risk_engine import InsufficientBalance
from venues.joint.venue import (
    ContextContradicted,
    InsufficientCredits,
    InsufficientTreasury,
    InvalidOutcome,
    InvalidTarget,
    MarketClosed,
    UnknownMarket,
    UnknownVariable,
    VenueError,
    WidthBudgetExceeded,
)


class APIError(Exception):
    """Structured API error with HTTP status and machine-readable code."""

    def __init__(self, status: int, code: str, message: str,
                 details: dict | None = None):
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}

    def response(self) -> JSONResponse:
        return JSONResponse(
            status_code=self.status,
            content={"error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            }},
        )


async def api_error_handler(request: Request, exc: APIError) -> JSONResponse:
    return exc.response()


def translate_engine_error(exc: Exception) -> APIError:
    """Translate engine exceptions to structured API errors."""
    msg = str(exc)

    if isinstance(exc, InsufficientBalance):
        return APIError(400, "insufficient_balance", msg)

    if "not found" in msg:
        if "market" in msg:
            return APIError(404, "market_not_found", msg)
        if "account" in msg:
            return APIError(404, "account_not_found", msg)

    if "is resolved" in msg or "is void" in msg:
        return APIError(400, "market_closed", msg)

    if "unknown outcome" in msg:
        return APIError(400, "invalid_outcome", msg)

    if "budget too small" in msg:
        return APIError(400, "budget_too_small", msg)

    if "can't sell" in msg or "sell amount" in msg:
        return APIError(400, "invalid_amount", msg)

    if "exceeds precision" in msg:
        return APIError(400, "invalid_amount", msg)

    return APIError(400, "bad_request", msg)


def translate_venue_error(exc: VenueError) -> APIError:
    """Translate net-venue (Plan B / JointVenue) errors to structured API errors.

    Exact mapping per planB-constraints.md:
    UnknownVariable/UnknownMarket -> 404 unknown_market; InvalidTarget ->
    400 invalid_target; InvalidOutcome -> 400 invalid_outcome;
    InsufficientCredits -> 400 insufficient_credits; MarketClosed -> 409
    market_closed; ContextContradicted -> 409 context_contradicted;
    WidthBudgetExceeded -> 422 width_budget; InsufficientTreasury -> 409
    insufficient_treasury.

    ``TradeRejected`` (and any other, currently unforeseen, ``VenueError``
    subtype) isn't in that list — it's the catch-all for a rejected
    trade_to_probability call that's neither a width-budget nor a
    degenerate-price failure, so it falls through to a generic 400
    trade_rejected rather than silently matching one of the specific
    branches above.
    """
    msg = str(exc)

    if isinstance(exc, (UnknownVariable, UnknownMarket)):
        return APIError(404, "unknown_market", msg)
    if isinstance(exc, InvalidTarget):
        return APIError(400, "invalid_target", msg)
    if isinstance(exc, InvalidOutcome):
        return APIError(400, "invalid_outcome", msg)
    if isinstance(exc, InsufficientCredits):
        return APIError(400, "insufficient_credits", msg)
    if isinstance(exc, MarketClosed):
        return APIError(409, "market_closed", msg)
    if isinstance(exc, ContextContradicted):
        return APIError(409, "context_contradicted", msg)
    if isinstance(exc, WidthBudgetExceeded):
        return APIError(422, "width_budget", msg)
    if isinstance(exc, InsufficientTreasury):
        # Server-side solvency guard, not a client error: the resolve was
        # refused to protect state integrity. 409 signals a conflict with
        # current server state that an operator must investigate.
        return APIError(409, "insufficient_treasury", msg)

    return APIError(400, "trade_rejected", msg)
