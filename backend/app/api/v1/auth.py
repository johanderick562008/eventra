from fastapi import APIRouter, Response, status

from app.api.dependencies import DbSession
from app.schemas.auth import LoginRequest, RefreshRequest, RegisterRequest, TokenResponse
from app.schemas.common import DataResponse, error_responses
from app.schemas.user import UserRead
from app.services import auth_service

router = APIRouter(prefix="/auth", tags=["Auth"])


@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    response_model=DataResponse[UserRead],
    responses=error_responses(409, 422),
    summary="Register an attendee or organizer",
)
def register(payload: RegisterRequest, db: DbSession) -> DataResponse[UserRead]:
    """Creates an account. The email is lower-cased; the role is fixed for the account's lifetime.

    Password rules: 8-128 characters with lowercase, uppercase, digit and symbol.
    """
    user = auth_service.register_user(db, payload)
    return DataResponse(data=UserRead.model_validate(user))


@router.post(
    "/login",
    response_model=DataResponse[TokenResponse],
    responses=error_responses(401, 403, 422),
    summary="Log in and receive an access token + refresh token",
)
def login(payload: LoginRequest, db: DbSession) -> DataResponse[TokenResponse]:
    """Returns a short-lived JWT (`Authorization: Bearer <access_token>`) and a rotating refresh
    token. Wrong email and wrong password return the same `INVALID_CREDENTIALS` error."""
    user = auth_service.authenticate(db, payload.email, payload.password)
    return DataResponse(data=auth_service.issue_tokens(db, user))


@router.post(
    "/refresh",
    response_model=DataResponse[TokenResponse],
    responses=error_responses(401, 422),
    summary="Rotate a refresh token",
)
def refresh(payload: RefreshRequest, db: DbSession) -> DataResponse[TokenResponse]:
    """Each refresh token works once. Re-using a rotated token revokes every token from the same
    login (`REFRESH_TOKEN_REVOKED`), which limits the damage of a stolen token."""
    return DataResponse(data=auth_service.rotate_refresh_token(db, payload.refresh_token))


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Revoke a refresh token",
)
def logout(payload: RefreshRequest, db: DbSession) -> None:
    """Revokes the refresh token and its rotation family. Idempotent. Access tokens remain valid
    until they expire (at most `ACCESS_TOKEN_EXPIRE_MINUTES`)."""
    auth_service.logout(db, payload.refresh_token)
