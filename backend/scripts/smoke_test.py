"""End-to-end smoke test against a running deployment.

Usage:  python scripts/smoke_test.py http://localhost:8000
Creates throwaway users/events (random emails) and walks the main flows. Exits non-zero on failure.
"""

import sys
import uuid
from datetime import UTC, datetime, timedelta

import httpx

PASSWORD = "SmokeTest123!"


def check(response: httpx.Response, expected: int, label: str) -> dict:
    ok = response.status_code == expected
    print(f"[{'PASS' if ok else 'FAIL'}] {label}: {response.status_code}")
    if not ok:
        print(response.text)
        sys.exit(1)
    is_json = response.headers.get("content-type", "").startswith("application/json")
    return response.json() if is_json else {}


def main(base_url: str) -> None:
    tag = uuid.uuid4().hex[:8]
    with httpx.Client(base_url=base_url, timeout=30) as http:
        check(http.get("/health"), 200, "GET /health")
        check(http.get("/ready"), 200, "GET /ready")
        check(http.get("/docs"), 200, "GET /docs")
        check(http.get("/openapi.json"), 200, "GET /openapi.json")

        def account(role: str) -> dict[str, str]:
            email = f"smoke-{role.lower()}-{tag}@example.com"
            body = {
                "name": f"Smoke {role.title()}",
                "email": email,
                "password": PASSWORD,
                "role": role,
            }
            check(http.post("/api/v1/auth/register", json=body), 201, f"register {role}")
            login = check(
                http.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD}),
                200,
                f"login {role}",
            )
            return {"Authorization": f"Bearer {login['data']['access_token']}"}

        org, att = account("ORGANIZER"), account("ATTENDEE")
        start = datetime.now(UTC) + timedelta(days=10)
        event = check(
            http.post(
                "/api/v1/events",
                headers=org,
                json={
                    "title": f"Smoke Test Event {tag}",
                    "description": "Created by scripts/smoke_test.py",
                    "category": "MEETUP",
                    "city": "Chennai",
                    "venue": "Smoke Hall",
                    "start_at": start.isoformat(),
                    "end_at": (start + timedelta(hours=2)).isoformat(),
                    "ticket_price": "150.00",
                    "capacity": 2,
                },
            ),
            201,
            "create event",
        )["data"]
        eid = event["id"]
        check(
            http.post("/api/v1/events", headers=att, json={}), 403, "attendee cannot create event"
        )
        check(
            http.get("/api/v1/events", params={"city": "chennai", "q": "smoke"}),
            200,
            "search events",
        )
        booking = check(
            http.post(f"/api/v1/events/{eid}/bookings", headers=att, json={"quantity": 2}),
            201,
            "book 2",
        )["data"]
        check(
            http.post(f"/api/v1/events/{eid}/bookings", headers=att, json={"quantity": 1}),
            409,
            "sold out",
        )
        check(http.get("/api/v1/bookings/me", headers=att), 200, "booking history")
        check(
            http.patch(f"/api/v1/bookings/{booking['id']}/cancel", headers=att),
            200,
            "cancel booking",
        )
        check(
            http.patch(f"/api/v1/bookings/{booking['id']}/cancel", headers=att),
            409,
            "double cancel",
        )
        summary = check(
            http.get(f"/api/v1/events/{eid}/summary", headers=org), 200, "sales summary"
        )
        assert summary["data"]["tickets_sold"] == 0, summary
        check(http.post(f"/api/v1/events/{eid}/cancel", headers=org), 200, "cancel event")
        check(http.delete(f"/api/v1/events/{eid}", headers=org), 204, "delete cancelled event")
    print("Smoke test passed")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000")
