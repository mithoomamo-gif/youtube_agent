"""Unified API error envelope + exception handlers.

Contract (MASTER_ROADMAP Part 4 / research 31_backend_protocol.md):
every error response is JSON of the shape::

    {"error": {"code": ..., "message": ..., "details": {...},
               "request_id": ..., "retryable": false}}

The HTTP status carries the class, the body carries the detail.
``request_id`` is taken from ``request.state.request_id`` when the
middleware set one, otherwise a fresh uuid4 hex is generated; it is
also echoed in the ``X-Request-Id`` response header.
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class ApiError(Exception):
    """Application error that maps directly onto the error envelope."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: Optional[dict] = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        self.retryable = retryable


def _new_request_id() -> str:
    return uuid.uuid4().hex


def get_request_id(request: Optional[Request]) -> str:
    """Request id from request.state if the middleware set one, else fresh."""
    if request is not None:
        rid = getattr(request.state, "request_id", None)
        if rid:
            return rid
    return _new_request_id()


def build_error_envelope(
    code: str,
    message: str,
    details: Optional[dict] = None,
    request_id: Optional[str] = None,
    retryable: bool = False,
) -> dict:
    """Build the single canonical error body: ``{"error": {...}}``."""
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details if details is not None else {},
            "request_id": request_id or _new_request_id(),
            "retryable": bool(retryable),
        }
    }


def error_response(
    status_code: int,
    code: str,
    message: str,
    details: Optional[dict] = None,
    request_id: Optional[str] = None,
    retryable: bool = False,
) -> JSONResponse:
    """JSONResponse carrying the envelope + X-Request-Id header."""
    rid = request_id or _new_request_id()
    body = build_error_envelope(code, message, details, rid, retryable)
    return JSONResponse(
        status_code=status_code,
        content=body,
        headers={"X-Request-Id": rid},
    )


# Sensible machine codes for bare Starlette HTTPExceptions.
_STATUS_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    422: "validation_error",
    429: "rate_limited",
    503: "not_ready",
}


def register_exception_handlers(app: FastAPI) -> None:
    """Install handlers so EVERY error response uses the envelope."""

    @app.exception_handler(ApiError)
    async def _api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
        return error_response(
            status_code=exc.status_code,
            code=exc.code,
            message=exc.message,
            details=exc.details,
            request_id=get_request_id(request),
            retryable=exc.retryable,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return error_response(
            status_code=422,
            code="validation_error",
            message="Request validation failed",
            details={"errors": jsonable_encoder(exc.errors())},
            request_id=get_request_id(request),
            retryable=False,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, dict) and "code" in detail:
            # Already envelope-shaped detail: honour it.
            return error_response(
                status_code=exc.status_code,
                code=str(detail.get("code")),
                message=str(detail.get("message", "Request failed")),
                details=detail.get("details"),
                request_id=get_request_id(request),
                retryable=bool(detail.get("retryable", False)),
            )
        message = detail if isinstance(detail, str) else "Request failed"
        return error_response(
            status_code=exc.status_code,
            code=_STATUS_CODES.get(exc.status_code, "http_error"),
            message=message,
            details=None,
            request_id=get_request_id(request),
            retryable=exc.status_code == 429,
        )

    @app.exception_handler(Exception)
    async def _generic_handler(request: Request, exc: Exception) -> JSONResponse:
        # Never leak internals (exception text, tracebacks) to the client.
        return error_response(
            status_code=500,
            code="internal_error",
            message="Internal server error",
            details=None,
            request_id=get_request_id(request),
            retryable=False,
        )
