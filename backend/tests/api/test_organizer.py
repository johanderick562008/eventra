from collections.abc import Callable

from fastapi.testclient import TestClient

from tests.conftest import UserCtx


def book(client: TestClient, user: UserCtx, event_id: str, quantity: int = 1) -> dict:
    response = client.post(
        f"/api/v1/events/{event_id}/bookings", json={"quantity": quantity}, headers=user.headers
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def test_organizer_sees_only_own_events(
    client: TestClient,
    organizer: UserCtx,
    attendee: UserCtx,
    make_user: Callable[..., UserCtx],
    make_event: Callable[..., dict],
) -> None:
    mine = make_event(capacity=20)
    make_event(owner=make_user("ORGANIZER"))
    book(client, attendee, mine["id"], 3)

    body = client.get("/api/v1/organizer/events", headers=organizer.headers).json()
    assert body["meta"]["total"] == 1
    item = body["data"][0]
    assert item["id"] == mine["id"]
    assert item["tickets_sold"] == 3 and item["remaining_capacity"] == 17
    assert item["confirmed_bookings"] == 1 and item["total_bookings"] == 1

    assert client.get("/api/v1/organizer/events", headers=attendee.headers).status_code == 403


def test_organizer_lists_bookings_for_own_event_only(
    client: TestClient,
    organizer: UserCtx,
    attendee: UserCtx,
    make_user: Callable[..., UserCtx],
    make_event: Callable[..., dict],
) -> None:
    event = make_event()
    book(client, attendee, event["id"], 2)

    response = client.get(f"/api/v1/events/{event['id']}/bookings", headers=organizer.headers)
    assert response.status_code == 200
    rows = response.json()["data"]
    assert len(rows) == 1
    assert rows[0]["attendee"] == {"id": attendee.id, "name": "Test User", "email": attendee.email}
    assert "password" not in response.text

    rival = make_user("ORGANIZER")
    denied = client.get(f"/api/v1/events/{event['id']}/bookings", headers=rival.headers)
    assert denied.status_code == 403
    assert denied.json()["code"] == "NOT_EVENT_OWNER"
    assert (
        client.get(f"/api/v1/events/{event['id']}/bookings", headers=attendee.headers).status_code
        == 403
    )
    assert (
        client.get(f"/api/v1/events/{event['id']}/summary", headers=rival.headers).status_code
        == 403
    )


def test_sales_summary_is_accurate(
    client: TestClient,
    organizer: UserCtx,
    make_user: Callable[..., UserCtx],
    make_event: Callable[..., dict],
) -> None:
    event = make_event(capacity=50, ticket_price="100.00")
    a, b, c = make_user(), make_user(), make_user()
    book(client, a, event["id"], 2)  # 200 confirmed
    book(client, b, event["id"], 3)  # 300 confirmed
    cancelled = book(client, c, event["id"], 4)  # 400, then cancelled by attendee
    client.patch(f"/api/v1/bookings/{cancelled['id']}/cancel", headers=c.headers)

    summary = client.get(f"/api/v1/events/{event['id']}/summary", headers=organizer.headers).json()[
        "data"
    ]
    assert summary["tickets_sold"] == 5 and summary["remaining_capacity"] == 45
    assert summary["confirmed_bookings"] == 2 and summary["confirmed_tickets"] == 5
    assert summary["cancelled_bookings"] == 1 and summary["cancelled_tickets"] == 4
    assert summary["gross_booking_value"] == "900.00"
    assert summary["cancelled_booking_value"] == "400.00"
    assert summary["net_booking_value"] == "500.00"
    assert summary["refund_pending_value"] == "0.00"

    client.post(f"/api/v1/events/{event['id']}/cancel", headers=organizer.headers)
    after = client.get(f"/api/v1/events/{event['id']}/summary", headers=organizer.headers).json()[
        "data"
    ]
    assert after["status"] == "CANCELLED"
    assert after["confirmed_bookings"] == 0 and after["net_booking_value"] == "0.00"
    assert after["refund_pending_bookings"] == 2 and after["refund_pending_tickets"] == 5
    assert after["refund_pending_value"] == "500.00"
    assert after["gross_booking_value"] == "900.00"


def test_organizer_bookings_status_filter(
    client: TestClient,
    organizer: UserCtx,
    make_user: Callable[..., UserCtx],
    make_event: Callable[..., dict],
) -> None:
    event = make_event()
    a, b = make_user(), make_user()
    book(client, a, event["id"])
    cancelled = book(client, b, event["id"])
    client.patch(f"/api/v1/bookings/{cancelled['id']}/cancel", headers=b.headers)
    rows = client.get(
        f"/api/v1/events/{event['id']}/bookings",
        params={"status": "CANCELLED"},
        headers=organizer.headers,
    ).json()["data"]
    assert [r["id"] for r in rows] == [cancelled["id"]]
