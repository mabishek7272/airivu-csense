"""Stable problem-response error model (TRD §10.1).

Every error response has the shape: `{code, message, details, correlation_id, retryable}`.
No exception handler here ever includes a stack trace, secret, or raw DB error in the
response body — only in server-side structured logs.
"""
from __future__ import annotations

from typing import Any

from fastapi import Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from csense_shared.logging import get_logger

logger = get_logger(__name__)


class ProblemResponse(BaseModel):
    code: str
    message: str
    details: dict[str, Any] | None = None
    correlation_id: str | None = None
    retryable: bool = False


class ApiError(Exception):
    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
        retryable: bool = False,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        self.retryable = retryable
        super().__init__(message)


class AuthenticationError(ApiError):
    def __init__(self, message: str = "Authentication required or token invalid."):
        super().__init__(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="authentication_required",
            message=message,
        )


class AuthorizationError(ApiError):
    def __init__(self, message: str = "Not authorized for this action."):
        super().__init__(
            status_code=status.HTTP_403_FORBIDDEN, code="not_authorized", message=message
        )


class NotFoundError(ApiError):
    def __init__(self, message: str = "Resource not found."):
        super().__init__(status_code=status.HTTP_404_NOT_FOUND, code="not_found", message=message)


class ConflictError(ApiError):
    def __init__(self, message: str = "Conflicting state."):
        super().__init__(status_code=status.HTTP_409_CONFLICT, code="conflict", message=message)


async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    correlation_id = getattr(request.state, "correlation_id", None)
    logger.warning(
        "api_error",
        extra={
            "code": exc.code,
            "status_code": exc.status_code,
            "correlation_id": correlation_id,
            "path": request.url.path,
        },
    )
    body = ProblemResponse(
        code=exc.code,
        message=exc.message,
        details=exc.details,
        correlation_id=correlation_id,
        retryable=exc.retryable,
    )
    return JSONResponse(status_code=exc.status_code, content=body.model_dump())


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    correlation_id = getattr(request.state, "correlation_id", None)
    logger.error(
        "unhandled_exception",
        extra={"correlation_id": correlation_id, "path": request.url.path, "error_type": type(exc).__name__},
        exc_info=True,
    )
    body = ProblemResponse(
        code="internal_error",
        message="An unexpected error occurred.",
        correlation_id=correlation_id,
        retryable=False,
    )
    return JSONResponse(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, content=body.model_dump())
