from __future__ import annotations

from enum import Enum
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError

# Literal codes: Starlette deprecates several HTTP_..._UNPROCESSABLE_ENTITY aliases.
HTTP_403_FORBIDDEN = 403
HTTP_404_NOT_FOUND = 404
HTTP_409_CONFLICT = 409
HTTP_413_TOO_LARGE = 413
HTTP_415_UNSUPPORTED = 415
HTTP_422_UNPROCESSABLE = 422
HTTP_500_INTERNAL = 500
HTTP_503_UNAVAILABLE = 503
from fastapi.responses import JSONResponse


class ErrorCode(str, Enum):
    VALIDATION = "validation_error"
    NOT_FOUND = "not_found"
    OUT_OF_COVERAGE = "out_of_coverage"
    INVALID_COORDINATES = "invalid_coordinates"
    UNSUPPORTED_UPLOAD = "unsupported_upload"
    UPLOAD_TOO_LARGE = "upload_too_large"
    ARCHIVE_UNSAFE = "archive_unsafe"
    CRS_UNKNOWN = "crs_unknown"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_AUTH = "provider_auth_required"
    PROVIDER_RATE_LIMIT = "provider_rate_limited"
    MODEL_UNAVAILABLE = "model_unavailable"
    APPROVAL_REQUIRED = "approval_required"
    PERMISSION_DENIED = "permission_denied"
    INTERNAL = "internal_error"


class AppError(Exception):
    code = ErrorCode.INTERNAL
    http_status = HTTP_500_INTERNAL

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}
        self.retryable = retryable


class ValidationError(AppError):
    code = ErrorCode.VALIDATION
    http_status = HTTP_422_UNPROCESSABLE


class InvalidCoordinates(ValidationError):
    code = ErrorCode.INVALID_COORDINATES


class NotFoundError(AppError):
    code = ErrorCode.NOT_FOUND
    http_status = HTTP_404_NOT_FOUND


class OutOfCoverageError(NotFoundError):
    code = ErrorCode.OUT_OF_COVERAGE
    http_status = HTTP_422_UNPROCESSABLE


class UnsupportedUploadError(AppError):
    code = ErrorCode.UNSUPPORTED_UPLOAD
    http_status = HTTP_415_UNSUPPORTED


class UploadTooLargeError(AppError):
    code = ErrorCode.UPLOAD_TOO_LARGE
    http_status = HTTP_413_TOO_LARGE


class ArchiveUnsafeError(AppError):
    code = ErrorCode.ARCHIVE_UNSAFE
    http_status = HTTP_422_UNPROCESSABLE


class CRSUnknownError(ValidationError):
    code = ErrorCode.CRS_UNKNOWN


class ProviderUnavailableError(AppError):
    code = ErrorCode.PROVIDER_UNAVAILABLE
    http_status = HTTP_503_UNAVAILABLE
    retryable = True

    def __init__(self, provider: str, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message, details={"provider": provider, "provider_status": status_code}, retryable=True)


class ProviderAuthError(ProviderUnavailableError):
    code = ErrorCode.PROVIDER_AUTH

    def __init__(self, provider: str, message: str, *, missing_env: str | None = None) -> None:
        super().__init__(provider, message)
        self.retryable = False
        if missing_env:
            self.details["missing_env"] = missing_env
            self.details["configuration_hint"] = f"Set {missing_env} in the backend environment."


class ProviderRateLimitError(ProviderUnavailableError):
    code = ErrorCode.PROVIDER_RATE_LIMIT


class ModelUnavailableError(AppError):
    code = ErrorCode.MODEL_UNAVAILABLE
    http_status = HTTP_503_UNAVAILABLE


class ApprovalRequiredError(AppError):
    code = ErrorCode.APPROVAL_REQUIRED
    http_status = HTTP_409_CONFLICT


class PermissionDeniedError(AppError):
    code = ErrorCode.PERMISSION_DENIED
    http_status = HTTP_403_FORBIDDEN


def error_payload(error: AppError) -> dict[str, Any]:
    return {
        "error": {
            "code": error.code.value,
            "message": error.message,
            "retryable": error.retryable,
            "details": error.details,
        }
    }


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content=error_payload(exc))

    @app.exception_handler(RequestValidationError)
    async def _handle_request_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        """Keep body/query validation in the same envelope as every other error.

        Only location, type and message are echoed: the offending input may contain a
        drawn polygon, and copying it into an error response would be needlessly large.
        """
        errors = [
            {
                "loc": [str(part) for part in error.get("loc", ())],
                "type": error.get("type"),
                "message": error.get("msg"),
            }
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=HTTP_422_UNPROCESSABLE,
            content=error_payload(
                ValidationError("Request validation failed.", details={"errors": errors})
            ),
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        from .logging import get_logger

        get_logger("app.errors").exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=HTTP_500_INTERNAL,
            content=error_payload(AppError("Internal server error.", details={"type": type(exc).__name__})),
        )
