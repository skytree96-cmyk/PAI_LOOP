"""Fixed, non-prose error metadata for PPS diagnostics."""

from typing import Any, Literal, get_args

PpsErrorType = Literal[
    "UNKNOWN", "NETWORK_ERROR", "HTTP_ERROR", "INVALID_JSON", "INVALID_PAYLOAD",
    "SERVICE_ERROR", "MISSING_RESPONSE", "MISSING_HEADER", "MISSING_BODY",
    "PROVIDER_RESULT_ERROR", "MISSING_TOTAL_COUNT", "INVALID_TOTAL_COUNT",
    "INVALID_ITEMS", "AWARD_PAGE_INVALID",
]
_ERROR_TYPES = frozenset(get_args(PpsErrorType))
# Numeric protocol codes are diagnostic labels only; no authorization/quota
# meaning is inferred. Arbitrary provider strings/messages never cross here.
_PROVIDER_CODES = frozenset(["0", *(f"{value:02d}" for value in range(100))])


def safe_pps_error_metadata(
    error_type: object = "UNKNOWN", http_status: object = None, provider_code: object = None,
) -> dict[str, Any]:
    kind = error_type if isinstance(error_type, str) and error_type in _ERROR_TYPES else "UNKNOWN"
    return {
        "error_type": kind,
        "http_status": http_status if kind == "HTTP_ERROR" and type(http_status) is int and 100 <= http_status <= 599 else None,
        "provider_code": provider_code if kind in {"SERVICE_ERROR", "PROVIDER_RESULT_ERROR"}
        and isinstance(provider_code, str) and provider_code in _PROVIDER_CODES else None,
    }
