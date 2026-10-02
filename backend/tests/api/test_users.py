from collections.abc import Callable

from fastapi.testclient import TestClient

from tests.conftest import PASSWORD, UserCtx


def test_get_me(client: TestClient, attendee: UserCtx) -> None:
    response = client.get("/api/v1/users/me", headers=attendee.headers)
    assert response.status_code == 200
    assert response.json()["data"]["email"] == attendee.email
    assert "password_hash" not in response.text


def test_update_name(client: TestClient, attendee: UserCtx) -> None:
    response = client.patch("/api/v1/users/me", json={"name": "New Name"}, headers=attendee.headers)
    assert response.status_code == 200
    assert response.json()["data"]["name"] == "New Name"


def test_cannot_escalate_role_or_mass_assign(client: TestClient, attendee: UserCtx) -> None:
    for body in (
        {"role": "ORGANIZER"},
        {"is_active": False},
        {"email": "x@example.com", "name": "x"},
    ):
        response = client.patch("/api/v1/users/me", json=body, headers=attendee.headers)
        assert response.status_code == 422, body
    me = client.get("/api/v1/users/me", headers=attendee.headers).json()["data"]
    assert me["role"] == "ATTENDEE"


def test_password_change_requires_current_password(client: TestClient, attendee: UserCtx) -> None:
    missing = client.patch(
        "/api/v1/users/me", json={"new_password": "Another123!x"}, headers=attendee.headers
    )
    assert missing.status_code == 422

    wrong = client.patch(
        "/api/v1/users/me",
        json={"current_password": "Wrong123!x", "new_password": "Another123!x"},
        headers=attendee.headers,
    )
    assert wrong.status_code == 403

    ok = client.patch(
        "/api/v1/users/me",
        json={"current_password": PASSWORD, "new_password": "Another123!x"},
        headers=attendee.headers,
    )
    assert ok.status_code == 200
    login = client.post(
        "/api/v1/auth/login", json={"email": attendee.email, "password": "Another123!x"}
    )
    assert login.status_code == 200
    # Password change revokes existing refresh tokens.
    refresh = client.post("/api/v1/auth/refresh", json={"refresh_token": attendee.refresh_token})
    assert refresh.status_code == 401


def test_cannot_modify_or_delete_other_users(
    client: TestClient, make_user: Callable[..., UserCtx]
) -> None:
    alice, bob = make_user(), make_user()
    url = f"/api/v1/users/{bob.id}"
    assert client.patch(url, json={"name": "Hacked"}, headers=alice.headers).status_code == 403
    assert client.delete(url, headers=alice.headers).status_code == 403
    assert (
        client.patch(
            f"/api/v1/users/{alice.id}", json={"name": "Alice"}, headers=alice.headers
        ).status_code
        == 200
    )


def test_attendee_profiles_are_private_but_organizers_are_public(
    client: TestClient, make_user: Callable[..., UserCtx]
) -> None:
    alice, bob, org = make_user(), make_user(), make_user("ORGANIZER")
    assert client.get(f"/api/v1/users/{bob.id}", headers=alice.headers).status_code == 404
    public = client.get(f"/api/v1/users/{org.id}", headers=alice.headers)
    assert public.status_code == 200
    assert set(public.json()["data"]) == {"id", "name", "role"}

    directory = client.get("/api/v1/users", headers=alice.headers).json()
    assert [u["id"] for u in directory["data"]] == [org.id]
    assert directory["meta"]["total"] == 1


def test_organizer_with_upcoming_events_cannot_deactivate(
    client: TestClient, organizer: UserCtx, make_event: Callable[..., dict]
) -> None:
    make_event()
    response = client.delete("/api/v1/users/me", headers=organizer.headers)
    assert response.status_code == 409
    assert response.json()["code"] == "ACCOUNT_HAS_ACTIVE_EVENTS"
