"""Application exceptions and the centralised handlers that turn them into JSON errors.

Every error response has the shape ``{"detail": str, "code": str}``; validation errors add an
``errors`` list. Internal details (stack traces, SQL, submitted values) are never returned.
"""

import logging
from typing import Any, cast

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class AppError(Exception):
    status_code: int = status.HTTP_400_BAD_REQUEST
    code: str = "BAD_REQUEST"
    detail: str = "Bad request"

    def __init__(
        self,
        detail: str | None = None,
        *,
        code: str | None = None,
        status_code: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.detail = detail or self.detail
        self.code = code or self.code
        self.status_code = status_code or self.status_code
        self.headers = headers
        super().__init__(self.detail)


class UnauthorizedError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "NOT_AUTHENTICATED"
    detail = "Not authenticated"

    def __init__(self, detail: str | None = None, *, code: str | None = None) -> None:
        super().__init__(detail, code=code, headers={"WWW-Authenticate": "Bearer"})


class ForbiddenError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "FORBIDDEN"
    detail = "You do not have permission to perform this action"


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "NOT_FOUND"
    detail = "Resource not found"


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "CONFLICT"
    detail = "The request conflicts with the current state of the resource"


class BusinessValidationError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "VALIDATION_ERROR"
    detail = "Request validation failed"


class RateLimitedError(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "RATE_LIMITED"
    detail = "Too many requests, please retry later"


_HTTP_STATUS_CODES = {
    400: "BAD_REQUEST",
    401: "NOT_AUTHENTICATED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    413: "PAYLOAD_TOO_LARGE",
    415: "UNSUPPORTED_MEDIA_TYPE",
    429: "RATE_LIMITED",
}

# SQLSTATEs for which a transaction can be retried from scratch.
RETRYABLE_SQLSTATES = frozenset({"40001", "40P01"})  # serialization_failure, deadlock_detected


def _error_body(detail: str, code: str, **extra: Any) -> dict[str, Any]:
    return {"detail": detail, "code": code, **extra}


async def _app_error_handler(_: Request, exc: Exception) -> JSONResponse:
    exc = cast(AppError, exc)
    return JSONResponse(
        _error_body(exc.detail, exc.code), status_code=exc.status_code, headers=exc.headers
    )


async def _validation_error_handler(_: Request, exc: Exception) -> JSONResponse:
    exc = cast(RequestValidationError, exc)
    # Deliberately drop `input` and `ctx`: they can echo passwords or other submitted values.
    errors = [
        {"loc": list(err.get("loc", ())), "msg": err.get("msg", ""), "type": err.get("type", "")}
        for err in exc.errors()
    ]
    return JSONResponse(
        _error_body("Request validation failed", "VALIDATION_ERROR", errors=errors),
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
    )


async def _http_exception_handler(_: Request, exc: Exception) -> JSONResponse:
    exc = cast(StarletteHTTPException, exc)
    code = _HTTP_STATUS_CODES.get(exc.status_code, "HTTP_ERROR")
    detail = exc.detail if isinstance(exc.detail, str) else "Request failed"
    return JSONResponse(_error_body(detail, code), status_code=exc.status_code, headers=exc.headers)


async def _integrity_error_handler(_: Request, exc: Exception) -> JSONResponse:
    constraint = getattr(getattr(getattr(exc, "orig", None), "diag", None), "constraint_name", None)
    logger.warning("Integrity error (constraint=%s)", constraint)
    return JSONResponse(
        _error_body("The request conflicts with existing data", "INTEGRITY_ERROR"),
        status_code=status.HTTP_409_CONFLICT,
    )


async def _database_error_handler(_: Request, exc: Exception) -> JSONResponse:
    sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
    logger.error("Database unavailable or timed out (sqlstate=%s)", sqlstate)
    return JSONResponse(
        _error_body("The service is temporarily unavailable, please retry", "SERVICE_UNAVAILABLE"),
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        headers={"Retry-After": "1"},
    )


async def _unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
    return JSONResponse(
        _error_body("An unexpected error occurred", "INTERNAL_ERROR"),
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(IntegrityError, _integrity_error_handler)
    app.add_exception_handler(OperationalError, _database_error_handler)
    app.add_exception_handler(DBAPIError, _database_error_handler)
    app.add_exception_handler(Exception, _unhandled_error_handler)
