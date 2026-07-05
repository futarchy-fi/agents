"""Inference-specific exceptions kept behind the server's HTTP contract layer."""

from __future__ import annotations

from typing import Any, Mapping


class InferenceError(Exception):
    """Base exception for inference-layer failures hidden behind the API."""

    default_code = "inference_error"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        """Initialize an inference error with a machine-readable code."""
        super().__init__(message)
        self.code = code or self.default_code
        self.message = message
        self.details = dict(details or {})


class InferenceCompileError(InferenceError):
    """Error raised when a market snapshot cannot be compiled."""

    default_code = "inference_compile_error"


class InferenceQueryError(InferenceError):
    """Error raised when querying a compiled artifact fails."""

    default_code = "inference_query_error"


class InferenceUnsupportedQueryError(InferenceQueryError):
    """Error raised when a query shape is unsupported by the backend."""

    default_code = "inference_unsupported_query"


__all__ = [
    "InferenceCompileError",
    "InferenceError",
    "InferenceQueryError",
    "InferenceUnsupportedQueryError",
]
