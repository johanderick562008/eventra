from datetime import timedelta

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import User
from app.utils.time import utcnow
from tests.conftest import PASSWORD, UserCtx

REGISTER = "/api/v1/auth/register"
LOGIN = "/api/v1/auth/login"


def _register(client: TestClient, **overrides: str):
    body = {"name": "Johan", "email": "johan@example.com", "password": PASSWORD, "role": "ATTENDEE"}
    body.update(overrides)
    return client.post(REGISTER, json=body)


def test_register_success_returns_safe_user(client: TestClient, db: Session) -> None:
    response = _register(client, email="  Johan@Example.COM ")
    assert response.status_code == 201
    data = response.json()["data"]
    assert data["email"] == "johan@example.com"
    assert data["role"] == "ATTENDEE"
    assert "password" not in response.text and "password_hash" not in response.text

    stored = db.scalar(select(User).where(User.email == "johan@example.com"))
    assert stored is not None
    assert stored.password_hash.startswith("$argon2id$")
    assert PASSWORD not in stored.password_hash


def test_register_duplicate_email_is_case_insensitive(client: TestClient) -> None:
    assert _register(client).status_code == 201
    response = _register(client, email="JOHAN@example.com")
    assert response.status_code == 409
    assert response.json()["code"] == "EMAIL_ALREADY_REGISTERED"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("email", "not-an-email"),
        ("password", "short1!"),
        ("password", "alllowercase123!"),
        ("password", "NoDigitsHere!"),
        ("password", "NoSymbols123"),
        ("role", "ADMIN"),
        ("name", "   "),
    ],
)
def test_register_rejects_invalid_input(client: TestClient, field: str, value: str) -> None:
    response = _register(client, **{field: value})
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert any(field in err["loc"] for err in body["errors"])
    if field == "password":
        assert value not in response.text  # submitted passwords are never echoed back


def test_register_rejects_unknown_fields(client: TestClient) -> None:
    response = _register(client, is_active="false")
    assert response.status_code == 422


def test_login_success(client: TestClient) -> None:
    _register(client)
    response = client.post(LOGIN, json={"email": "JOHAN@example.com", "password": PASSWORD})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["token_type"] == "bearer"
    assert data["expires_in"] == get_settings().access_token_expire_minutes * 60
    claims = jwt.decode(data["access_token"], options={"verify_signature": False})
    assert set(claims) == {"sub", "role", "type", "iss", "iat", "exp", "jti"}
    assert claims["sub"] == data["user"]["id"]


def test_login_wrong_password_and_unknown_email_look_identical(client: TestClient) -> None:
    _register(client)
    wrong = client.post(LOGIN, json={"email": "johan@example.com", "password": "Wrong123!x"})
    unknown = client.post(LOGIN, json={"email": "nobody@example.com", "password": PASSWORD})
    assert wrong.status_code == unknown.status_code == 401
    assert (
        wrong.json()
        == unknown.json()
        == {
            "detail": "Invalid email or password",
            "code": "INVALID_CREDENTIALS",
        }
    )


def test_missing_token(client: TestClient) -> None:
    response = client.get("/api/v1/users/me")
    assert response.status_code == 401
    assert response.json()["code"] == "NOT_AUTHENTICATED"
    assert response.headers["www-authenticate"] == "Bearer"


def test_invalid_token(client: TestClient) -> None:
    response = client.get("/api/v1/users/me", headers={"Authorization": "Bearer not.a.jwt"})
    assert response.status_code == 401
    assert response.json()["code"] == "INVALID_TOKEN"


def test_expired_token(client: TestClient, attendee: UserCtx) -> None:
    settings = get_settings()
    past = utcnow() - timedelta(hours=2)
    token = jwt.encode(
        {
            "sub": attendee.id,
            "role": "ATTENDEE",
            "type": "access",
            "iss": settings.jwt_issuer,
            "iat": past,
            "exp": past + timedelta(minutes=5),
        },
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )
    response = client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert response.json()["code"] == "TOKEN_EXPIRED"


def test_inactive_account_cannot_log_in_or_use_tokens(
    client: TestClient, attendee: UserCtx
) -> None:
    assert client.delete("/api/v1/users/me", headers=attendee.headers).status_code == 204

    login = client.post(LOGIN, json={"email": attendee.email, "password": PASSWORD})
    assert login.status_code == 403
    assert login.json()["code"] == "ACCOUNT_INACTIVE"

    me = client.get("/api/v1/users/me", headers=attendee.headers)
    assert me.status_code == 401

    refresh = client.post("/api/v1/auth/refresh", json={"refresh_token": attendee.refresh_token})
    assert refresh.status_code == 401


def test_refresh_token_rotation(client: TestClient, attendee: UserCtx) -> None:
    first = client.post("/api/v1/auth/refresh", json={"refresh_token": attendee.refresh_token})
    assert first.status_code == 200
    rotated = first.json()["data"]
    assert rotated["refresh_token"] != attendee.refresh_token
    assert (
        client.get(
            "/api/v1/users/me", headers={"Authorization": f"Bearer {rotated['access_token']}"}
        ).status_code
        == 200
    )


def test_refresh_token_reuse_revokes_whole_family(client: TestClient, attendee: UserCtx) -> None:
    rotated = client.post(
        "/api/v1/auth/refresh", json={"refresh_token": attendee.refresh_token}
    ).json()["data"]["refresh_token"]

    # An attacker replays the original (already rotated) token...
    replay = client.post("/api/v1/auth/refresh", json={"refresh_token": attendee.refresh_token})
    assert replay.status_code == 401
    assert replay.json()["code"] == "REFRESH_TOKEN_REVOKED"

    # ...which also kills the legitimate successor.
    successor = client.post("/api/v1/auth/refresh", json={"refresh_token": rotated})
    assert successor.status_code == 401


def test_logout_revokes_refresh_token(client: TestClient, attendee: UserCtx) -> None:
    body = {"refresh_token": attendee.refresh_token}
    assert client.post("/api/v1/auth/logout", json=body).status_code == 204
    assert client.post("/api/v1/auth/logout", json=body).status_code == 204  # idempotent
    assert client.post("/api/v1/auth/refresh", json=body).status_code == 401


def test_unknown_refresh_token(client: TestClient) -> None:
    response = client.post("/api/v1/auth/refresh", json={"refresh_token": "x" * 64})
    assert response.status_code == 401
    assert response.json()["code"] == "INVALID_REFRESH_TOKEN"
