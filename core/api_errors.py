"""
API error handling. Structured JSON errors with codes.

Every error response: {"error": {"code": "...", "message": "...", "details": {...}}}
"""

from fastapi import Request
from fastapi.responses import JSONResponse

from core.risk_engine import InsufficientBalance


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
