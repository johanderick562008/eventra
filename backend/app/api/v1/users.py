import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from app.api.dependencies import CurrentUser, DbSession
from app.schemas.common import (
    DataResponse,
    PaginatedResponse,
    Pagination,
    error_responses,
    pagination_params,
)
from app.schemas.user import UserPublic, UserRead, UserUpdate
from app.services import user_service

router = APIRouter(prefix="/users", tags=["Users"])

_PROFILE_ERRORS = error_responses(401, 403, 409, 422)


@router.get("/me", response_model=DataResponse[UserRead], responses=error_responses(401))
def get_me(user: CurrentUser) -> DataResponse[UserRead]:
    """The caller's own profile."""
    return DataResponse(data=UserRead.model_validate(user))


@router.patch("/me", response_model=DataResponse[UserRead], responses=_PROFILE_ERRORS)
def update_me(payload: UserUpdate, user: CurrentUser, db: DbSession) -> DataResponse[UserRead]:
    """Update `name` and/or change password (`current_password` + `new_password`).
    `role`, `email` and `is_active` cannot be changed; unknown fields return 422.
    Changing the password revokes all refresh tokens."""
    return DataResponse(
        data=UserRead.model_validate(user_service.update_profile(db, user, payload))
    )


@router.delete(
    "/me",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses=_PROFILE_ERRORS,
)
def deactivate_me(user: CurrentUser, db: DbSession) -> None:
    """Deactivates (soft-deletes) the account. Booking and event history are preserved.
    Refused while the account still has upcoming events (organizer) or bookings (attendee)."""
    user_service.deactivate_account(db, user)


@router.get("", response_model=PaginatedResponse[UserPublic], responses=error_responses(401))
def list_organizers(
    _: CurrentUser,
    db: DbSession,
    pagination: Annotated[Pagination, Depends(pagination_params)],
) -> PaginatedResponse[UserPublic]:
    """Directory of active organizers (public fields only). There is no administrator role, so
    attendee accounts are never listed to anyone."""
    users, total = user_service.list_organizers(db, pagination)
    return PaginatedResponse(
        data=[UserPublic.model_validate(u) for u in users], meta=pagination.meta(total)
    )


@router.get(
    "/{user_id}",
    response_model=DataResponse[UserPublic],
    responses=error_responses(401, 404),
)
def get_user(user_id: uuid.UUID, viewer: CurrentUser, db: DbSession) -> DataResponse[UserPublic]:
    """Public profile of an organizer, or of the caller. Other attendees return 404."""
    return DataResponse(
        data=UserPublic.model_validate(user_service.get_visible_user(db, user_id, viewer))
    )


@router.patch("/{user_id}", response_model=DataResponse[UserRead], responses=_PROFILE_ERRORS)
def update_user(
    user_id: uuid.UUID, payload: UserUpdate, user: CurrentUser, db: DbSession
) -> DataResponse[UserRead]:
    """Same as `PATCH /users/me`; any other `user_id` returns 403 (no administrator role)."""
    user_service.ensure_self(user_id, user)
    return DataResponse(
        data=UserRead.model_validate(user_service.update_profile(db, user, payload))
    )


@router.delete(
    "/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses=_PROFILE_ERRORS,
)
def deactivate_user(user_id: uuid.UUID, user: CurrentUser, db: DbSession) -> None:
    """Same as `DELETE /users/me`; any other `user_id` returns 403."""
    user_service.ensure_self(user_id, user)
    user_service.deactivate_account(db, user)
