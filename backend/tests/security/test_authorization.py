"""Token forgery and role/ownership boundaries."""

import base64
import json
from collections.abc import Callable
from datetime import timedelta

import jwt
import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.utils.time import utcnow
from tests.conftest import UserCtx, event_payload


def _b64(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def test_alg_none_token_rejected(client: TestClient, attendee: UserCtx) -> None:
    now = int(utcnow().timestamp())
    claims = {
        "sub": attendee.id,
        "role": "ORGANIZER",
        "type": "access",
        "iss": get_settings().jwt_issuer,
        "iat": now,
        "exp": now + 600,
    }
    token = f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64(claims)}."
    response = client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


def test_token_signed_with_wrong_key_rejected(client: TestClient, attendee: UserCtx) -> None:
    claims = {
        "sub": attendee.id,
        "role": "ORGANIZER",
        "type": "access",
        "iss": get_settings().jwt_issuer,
        "iat": utcnow(),
        "exp": utcnow() + timedelta(minutes=5),
    }
    token = jwt.encode(claims, "attacker-secret-attacker-secret-1234", algorithm="HS256")
    response = client.post(
        "/api/v1/events", json=event_payload(), headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 401


def test_tampered_role_claim_rejected(client: TestClient, attendee: UserCtx) -> None:
    header, payload, signature = attendee.token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    claims["role"] = "ORGANIZER"
    forged = f"{header}.{_b64(claims)}.{signature}"
    response = client.post(
        "/api/v1/events", json=event_payload(), headers={"Authorization": f"Bearer {forged}"}
    )
    assert response.status_code == 401


def test_role_claim_is_not_trusted_over_database(client: TestClient, attendee: UserCtx) -> None:
    """Even a validly signed token claiming ORGANIZER cannot act as one: the DB role wins."""
    settings = get_settings()
    claims = {
        "sub": attendee.id,
        "role": "ORGANIZER",
        "type": "access",
        "iss": settings.jwt_issuer,
        "iat": utcnow(),
        "exp": utcnow() + timedelta(minutes=5),
    }
    token = jwt.encode(claims, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    response = client.post(
        "/api/v1/events", json=event_payload(), headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 403


def test_token_for_deleted_or_unknown_user_rejected(client: TestClient) -> None:
    settings = get_settings()
    claims = {
        "sub": "00000000-0000-0000-0000-000000000001",
        "role": "ATTENDEE",
        "type": "access",
        "iss": settings.jwt_issuer,
        "iat": utcnow(),
        "exp": utcnow() + timedelta(minutes=5),
    }
    token = jwt.encode(claims, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    assert (
        client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {token}"}).status_code
        == 401
    )


def test_refresh_token_cannot_be_used_as_access_token(
    client: TestClient, attendee: UserCtx
) -> None:
    response = client.get(
        "/api/v1/users/me", headers={"Authorization": f"Bearer {attendee.refresh_token}"}
    )
    assert response.status_code == 401


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "/api/v1/users/me", None),
        ("get", "/api/v1/bookings/me", None),
        ("get", "/api/v1/organizer/events", None),
        ("post", "/api/v1/events", {}),
        ("post", "/api/v1/events/00000000-0000-0000-0000-000000000000/bookings", {"quantity": 1}),
        ("patch", "/api/v1/bookings/00000000-0000-0000-0000-000000000000/cancel", None),
    ],
)
def test_protected_endpoints_require_authentication(
    client: TestClient, method: str, path: str, body: dict | None
) -> None:
    response = client.request(method, path, json=body)
    assert response.status_code == 401
    assert response.json()["code"] == "NOT_AUTHENTICATED"


def test_role_matrix(
    client: TestClient, organizer: UserCtx, attendee: UserCtx, make_event: Callable[..., dict]
) -> None:
    event = make_event()
    assert client.get("/api/v1/bookings/me", headers=organizer.headers).status_code == 403
    assert client.get("/api/v1/organizer/events", headers=attendee.headers).status_code == 403
    assert (
        client.patch(
            f"/api/v1/events/{event['id']}", json={"title": "x" * 5}, headers=attendee.headers
        ).status_code
        == 403
    )
    assert (
        client.get(f"/api/v1/events/{event['id']}/summary", headers=attendee.headers).status_code
        == 403
    )


def test_no_sensitive_fields_anywhere(
    client: TestClient, organizer: UserCtx, attendee: UserCtx, make_event: Callable[..., dict]
) -> None:
    event = make_event()
    booking = client.post(
        f"/api/v1/events/{event['id']}/bookings", json={"quantity": 1}, headers=attendee.headers
    )
    responses = [
        booking,
        client.get("/api/v1/users/me", headers=attendee.headers),
        client.get(f"/api/v1/events/{event['id']}"),
        client.get(f"/api/v1/events/{event['id']}/bookings", headers=organizer.headers),
        client.get("/api/v1/users", headers=attendee.headers),
    ]
    for response in responses:
        assert "password" not in response.text
        assert "argon2" not in response.text
        assert get_settings().jwt_secret_key not in response.text
    # Public event endpoints never expose the organizer's email.
    assert organizer.email not in responses[2].text
