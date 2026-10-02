import math
from typing import Annotated, Generic, TypeVar

from fastapi import Query
from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ErrorItem(BaseModel):
    loc: list[str | int]
    msg: str
    type: str


class ErrorResponse(BaseModel):
    detail: str
    code: str
    errors: list[ErrorItem] | None = None

    model_config = ConfigDict(
        json_schema_extra={
            "example": {"detail": "Only 1 ticket(s) remaining", "code": "INSUFFICIENT_CAPACITY"}
        }
    )


class DataResponse(BaseModel, Generic[T]):
    """Envelope for every successful single-resource response."""

    data: T


class PaginationMeta(BaseModel):
    page: int
    limit: int
    total: int
    pages: int


class PaginatedResponse(BaseModel, Generic[T]):
    """Envelope for every list response."""

    data: list[T]
    meta: PaginationMeta


class Pagination(BaseModel):
    page: int = Field(default=1, ge=1, le=10_000, description="1-based page number")
    limit: int = Field(default=20, ge=1, le=100, description="Items per page (max 100)")

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.limit

    def meta(self, total: int) -> PaginationMeta:
        return PaginationMeta(
            page=self.page, limit=self.limit, total=total, pages=math.ceil(total / self.limit)
        )


def pagination_params(
    page: Annotated[int, Query(ge=1, le=10_000, description="1-based page number")] = 1,
    limit: Annotated[int, Query(ge=1, le=100, description="Items per page (max 100)")] = 20,
) -> Pagination:
    return Pagination(page=page, limit=limit)


def error_responses(*status_codes: int) -> dict[int | str, dict[str, object]]:
    """OpenAPI `responses=` helper documenting the shared error schema."""
    descriptions = {
        401: "Missing, invalid or expired access token",
        403: "Authenticated but not allowed (wrong role or not the owner)",
        404: "Resource not found",
        409: "Conflicts with the current state (capacity, lifecycle, duplicates)",
        422: "Request validation failed",
        429: "Rate limit exceeded",
    }
    return {
        code: {"model": ErrorResponse, "description": descriptions.get(code, "Error")}
        for code in status_codes
    }
