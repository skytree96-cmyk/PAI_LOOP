"""Shared client wait, attachment-start reservation and safe transport labels.

The reservation is admission accounting, not a wall-clock cancellation timer.
An HTTPX timeout describes a client I/O phase; it cannot establish whether the
gateway/provider completed or charged for the request after the client stopped.
"""
from typing import Literal, get_args

import httpx


DEFAULT_EXTRACTION_CLIENT_TIMEOUT_SECONDS = 200

ClientTransportErrorCode = Literal[
    "CLIENT_CONNECT_TIMEOUT", "CLIENT_READ_TIMEOUT", "CLIENT_WRITE_TIMEOUT",
    "CLIENT_POOL_TIMEOUT", "CLIENT_TIMEOUT", "CLIENT_REQUEST_ERROR",
]
_CLIENT_TRANSPORT_ERROR_CODES = frozenset(get_args(ClientTransportErrorCode))


def safe_transport_error_code(value: object) -> ClientTransportErrorCode | None:
    """Accept a fixed diagnostic code, never exception text or request data."""
    return value if type(value) is str and value in _CLIENT_TRANSPORT_ERROR_CODES else None


def classify_client_transport_error(error: httpx.RequestError) -> ClientTransportErrorCode:
    # Inspect types only: str(error), request URLs, headers and bodies may all
    # contain private material and must never enter persisted diagnostics.
    if isinstance(error, httpx.ConnectTimeout):
        return "CLIENT_CONNECT_TIMEOUT"
    if isinstance(error, httpx.ReadTimeout):
        return "CLIENT_READ_TIMEOUT"
    if isinstance(error, httpx.WriteTimeout):
        return "CLIENT_WRITE_TIMEOUT"
    if isinstance(error, httpx.PoolTimeout):
        return "CLIENT_POOL_TIMEOUT"
    if isinstance(error, httpx.TimeoutException):
        return "CLIENT_TIMEOUT"
    return "CLIENT_REQUEST_ERROR"


def attachment_start_reservation_seconds(
    *, download_timeout_seconds: float, model_timeout_seconds: float,
    max_model_calls: int, guard_seconds: float,
) -> float:
    """Reserve three download attempts plus the allowed model calls and guard.

    Two normal model calls mean an initial response and one possible correction,
    not permission to resend an ambiguous timeout. LONG_OUTPUT_ONCE passes one.
    """
    return download_timeout_seconds * 3 + model_timeout_seconds * max_model_calls + guard_seconds
